"""剪映蒙版 + 文字层 → 达芬奇当前时间线（Lua 实写方案，正式管线版）。

用法:
    python cli.py masks -i "剪映草稿目录"           # 写入
    python cli.py masks -i "剪映草稿目录" --audit   # 只读对账

覆盖内容:
  - 形状蒙版 (common_mask: contour 钢笔 / rectangle 矩形), 仅 enable_video_mask=True 的段
  - 文字层 (text 轨): 独立片段(顶部专用轨"JY文字", Text+ -> Glow 复刻剪映发光),
    不嵌入下层素材 comp
  - 链路: 蒙版= MediaIn(EffectMask) -> MediaOut
          文字 = TextPlus -> Glow -> MediaOut (纯生成器, alpha 叠加下层)

已知要点(21.1 实测):
  - 点位必须走子样条 BezierSpline:SetKeyFrames, bmd.readstring 构造 Polyline
  - StyledText 必须 SetInput (tx.Text= 的 userdata 赋值静默失败)
  - comp 导出头部 RenderRange 是界面状态残留, 不影响播放
"""

from __future__ import annotations

import json
import os
import re
import time

from nlebridge import jy_read
from nlebridge import maskwrite

# 剪映字号(0-10 滑杆, 5=默认) → Fusion Text+ Size。5 -> 0.06
JY_SIZE_FACTOR = 0.012
# 剪映文字字体(庞门正道标题体, 用户已安装; TTF 内部家族名)
# 注意: 必须用英文名 PangMenZhengDao — Text+ 解析不了中文别名"庞门正道标题体",
# 会静默回退到微软雅黑(实测 21.1); 该字体无 Bold 字重, Style 需固定 Regular
JY_FONT = "PangMenZhengDao"
# 剪映发光(发光: 颜色/强度50/加距40) → Fusion Glow 参数
# TODO: 强度/加距的映射比例是首版目测值, 待视觉核对微调
# 发光常量 = 用户手改定稿(2026-11-18 从时间线 comp 导出核实), 勿改回草稿映射值
GLOW_COLOR = (0.0, 1.0, 1.0)     # Red=0, Green/Blue=默认1
GLOW_XSIZE = 5                   # XGlowSize
GLOW_YSIZE = 0.04                # YGlowSize(保持草稿值)
GLOW_GAIN = 1.0                  # Glow 强度(默认)
GLOW_BLEND = 0.244               # Glow Blend
GLOW_COLOR_SCALE = -1            # ColorScale
GLOW_BLUE_SCALE = 1.339          # BlueScale
GLOW_FILTER = "Fast Gaussian"    # Filter FuID
TEXT_CLIP_COLOR = "Orange"       # 独立文字片段标记色
TEXT_TRACK_NAME = "JY文字"

CLEAN_LUA = r"""
local del = {}
for _, tool in pairs(comp:GetToolList()) do
  local a = tool:GetAttrs()
  local nm = tostring(a.TOOLS_Name or "")
  if nm:sub(1, 2) == "NB" or a.TOOLS_RegID == "BezierSpline" then
    del[#del + 1] = tool
  end
end
local n = 0
for _, tool in ipairs(del) do
  local ok = pcall(function() tool:Delete() end)
  if ok then n = n + 1 end
end
RES = "cleaned " .. n
"""

TEXT_LUA = r"""
-- 独立文字片段 comp: 清模板 → TextPlus(白字) -> Glow(剪映发光) -> MediaOut
-- 标题片段是纯生成器(无 MediaIn), alpha 直接输出到时间线, 自动叠加下层
for _, tool in pairs(comp:GetToolList()) do
  if tostring(tool:GetAttrs().TOOLS_RegID) ~= "MediaOut" then
    pcall(function() tool:Delete() end)
  end
end
local tx = comp:AddTool("TextPlus")
tx:SetAttrs({TOOLS_Name = "NBText"})
local okT = pcall(function() tx:SetInput("StyledText", [[%(text)s]]) end)
pcall(function() tx:SetInput("Center", { %(cx)s, %(cy)s }) end)
tx:SetInput("Size", %(size)s)
tx:SetInput("Font", "%(font)s")
tx:SetInput("Style", "Regular")
tx:SetInput("Red1", %(r1)s)
tx:SetInput("Green1", %(g1)s)
tx:SetInput("Blue1", %(b1)s)
tx:SetInput("Alpha1", 1.0)
tx:SetInput("Opacity1", 1.0)
local gl = comp:AddTool("Glow")
gl:SetAttrs({TOOLS_Name = "NBGlow"})
gl:SetInput("Red", %(gr)s)
gl:SetInput("Green", %(gg)s)
gl:SetInput("Blue", %(gb)s)
gl:SetInput("Alpha", 1.0)
gl:SetInput("XGlowSize", %(gx)s)
gl:SetInput("YGlowSize", %(gy)s)
gl:SetInput("Glow", %(ga)s)
gl:SetInput("Blend", %(gb2)s)
gl:SetInput("ColorScale", %(gcs)s)
gl:SetInput("BlueScale", %(gbs)s)
pcall(function() gl:SetInput("Filter", "%(gf)s") end)
local mo = comp:FindTool("MediaOut1")
if mo then
  gl.Input = tx
  mo.Input = gl
end
RES = "text ok=" .. tostring(okT)
"""


# ---------------------------------------------------------------- 计划

def _is_bottom_only(spans, v_no: int, start_us: int, dur_us: int) -> bool:
    """该段下方(更低的视频轨)是否存在同时长重叠的段。

    蒙版把画面变透明, 透明区露出的是下层。若该段就是最底层, 露出的只能是
    黑色 —— 除非剪辑师本意就是"角色浮在纯黑上", 否则必是误套。
    实测 2026-11-18: 642/1419/6888 三段 AI 抠像全落在最底层, 成片整段黑底。
    """
    a, b = start_us, start_us + max(0, dur_us)
    for o, s0, s1 in spans:
        if o >= v_no:
            continue          # 同轨或更高轨, 不构成"下层"
        if s1 > a and s0 < b:
            return False
    return True


def build_plan(draft_dir: str, fps: float, allow_bottom: bool = False, skips: list | None = None):
    """扫描剪映草稿 → (蒙版计划, 文字计划)。

    草稿是唯一事实源: 计划只由草稿内容决定, 不做"补齐"。
    `skips` 非 None 时, 被跳过的段连原因一并回填, 便于如实汇报 →
    这些段不进计划, 也就不会被创建 Fusion comp(不留空壳)。
    `allow_bottom=True` 为忠实模式: 即使该段在最底层也照草稿套(默认跳过)。
    """
    src = os.path.join(draft_dir, "draft_content.json")
    d = json.loads(jy_read.decrypt_draft_text(src))
    masks = {m["id"]: m for m in d["materials"].get("common_mask", [])}
    videos = {m["id"]: m for m in d["materials"].get("videos", [])}
    texts = {m["id"]: m for m in d["materials"].get("texts", [])}
    anims = {m["id"]: m for m in d["materials"].get("material_animations", [])}

    # 预扫各视频轨的段区间, 供"最底层不套蒙版"安全网使用(见 _is_bottom_only)
    spans = []
    _vn = 0
    for tr in d["tracks"]:
        if tr.get("type") != "video":
            continue
        _vn += 1
        for seg in tr.get("segments", []):
            t = seg.get("target_timerange") or {}
            # 坑 12: 从第 0 帧开始的段不写 start 键, 缺键按 0 处理
            if "duration" in t:
                _st = t.get("start", 0)
                spans.append((_vn, _st, _st + t["duration"]))

    plan_masks, plan_text = [], []
    vti = 0
    for tr in d["tracks"]:
        if tr.get("type") == "video":
            vti += 1
            for seg in tr.get("segments", []):
                v = videos.get(seg.get("material_id"), {})
                nm = v.get("material_name") or ""
                if nm.startswith("水印") or "materials/video/水印" in (v.get("path") or ""):
                    continue
                # 剪映只在"关闭"时写 False; 缺省(None)按开启处理 —— 见下方 reason 判定
                t = seg.get("target_timerange", {})
                _f = int(round(t.get("start", 0) / 1e6 * fps))
                _dur = int(round(t.get("duration", 0) / 1e6 * fps))
                # 三个跳过条件: 前两条直接来自草稿开关, 第三条是结果约束(套了必露黑)。
                # 命中即不进计划 —— 不进计划就不会 AddFusionComp, 不会留下空壳。
                reason = None
                if seg.get("enable_video_mask") is False:
                    reason = "草稿里蒙版已关闭 enable_video_mask=False"
                elif seg.get("enable_adjust_mask") is False:
                    reason = "调节蒙版未启用 enable_adjust_mask=False (只限定调色区, 不裁画面)"
                elif (not allow_bottom) and _is_bottom_only(spans, vti, t.get("start", 0),
                                                            t.get("duration", 0)):
                    reason = "该段是最底层, 套蒙版必露黑"
                if reason:
                    if skips is not None:
                        skips.append({"jy_track": vti, "frame": _f, "dur_frames": _dur,
                                      "clip": nm[:30], "reason": reason})
                    continue
                for h in seg.get("extra_material_refs", []):
                    m = masks.get(h)
                    if not m:
                        continue
                    rt = m.get("resource_type")
                    if rt not in ("contour", "rectangle"):
                        continue
                    cfg = m.get("config", {}) or {}
                    rec = {
                        "jy_track": vti,
                        "start_us": t.get("start", 0),
                        "frame": int(round(t.get("start", 0) / 1e6 * fps)),
                        "dur_frames": int(round(t.get("duration", 0) / 1e6 * fps)),
                        "clip": nm[:30],
                        "rt": rt,
                        "invert": cfg.get("invert", 0),
                        "feather": cfg.get("feather") or 0,
                        "w": cfg.get("width"),
                        "h": cfg.get("height"),
                    }
                    if rt == "contour":
                        rec["pts"] = (m.get("contour_path") or {}).get("coutour_path") or []
                        rec["n"] = len(rec["pts"])
                    plan_masks.append(rec)
        elif tr.get("type") == "text":
            for seg in tr.get("segments", []):
                tx = texts.get(seg.get("material_id"), {})
                if not tx:
                    continue
                try:
                    content = json.loads(tx.get("content") or "{}")
                except Exception:
                    content = {}
                t = seg.get("target_timerange", {})
                trf = (seg.get("clip", {}) or {}).get("transform", {}) or {}
                # 入场动画"渐显" -> 时间线片段 FadeIn(与头部交叉叠化等效)
                fade_in_frames = 0
                for h in seg.get("extra_material_refs", []):
                    am = anims.get(h)
                    if not am:
                        continue
                    for a in am.get("animations", []) or []:
                        if a.get("type") == "in" and a.get("name") == "渐显":
                            fade_in_frames = int(round(a.get("duration", 0) / 1e6 * fps))
                plan_text.append({
                    "start_us": t.get("start", 0),
                    "dur_us": t.get("duration", 0),
                    "frame": int(round(t.get("start", 0) / 1e6 * fps)),
                    "dur_frames": int(round(t.get("duration", 0) / 1e6 * fps)),
                    "text": content.get("text") or "",
                    "font_size": content.get("font_size") or tx.get("font_size") or 5,
                    "x": trf.get("x", 0),
                    "y": trf.get("y", 0),
                    "fade_in_frames": fade_in_frames,
                })
    return plan_masks, plan_text


# ---------------------------------------------------------------- 桥接

def _connect():
    from nlebridge.fadesync import _connect
    return _connect()


def _getclip(tl, track, frame):
    items = tl.GetItemListInTrack("video", track) or []
    hits = [it for it in items
            if "交叉叠化" not in it.GetName() and it.GetStart() == frame]
    return hits[0] if len(hits) == 1 else None


def _getcomp(clip, create=True):
    """取片段第一个 comp; 新导入的时间线没有 comp, 需要时创建。"""
    if clip.GetFusionCompCount() == 0:
        if not create:
            return None
        c = clip.AddFusionComp()
        if c is not None:
            return c
    return clip.GetFusionCompByIndex(1)


def _ex(comp, lua, tag, outdir):
    fp = os.path.join(outdir, "ms_%s.txt" % tag)
    if os.path.exists(fp):
        os.remove(fp)
    comp.Execute("\nRES='nil'\n" + lua +
                 '\nlocal f=io.open([[' + fp + ']],"w") if f then f:write(tostring(RES)) f:close() end', {})
    time.sleep(0.35)
    if os.path.exists(fp):
        return open(fp, encoding="utf-8", errors="replace").read()
    return "<NOFILE>"


def _export_comp(comp, path):
    if os.path.exists(path):
        os.remove(path)
    comp.Export(path, 0)
    time.sleep(0.2)
    if os.path.exists(path):
        return open(path, encoding="utf-8", errors="replace").read()
    return ""


def _find_text_target(tl, frame):
    """文字出现时刻, 从最高视频轨往下找带 comp 的承载片段。"""
    for ti in range(tl.GetTrackCount("video"), 0, -1):
        for it in (tl.GetItemListInTrack("video", ti) or []):
            if "交叉叠化" in it.GetName():
                continue
            if it.GetStart() <= frame < it.GetEnd():
                return it, ti
    return None, 0


def _frame_to_tc(f, fps):
    fps = int(fps)
    return "%02d:%02d:%02d:%02d" % (f // (3600 * fps), f // (60 * fps) % 60,
                                    f // fps % 60, f % fps)


def _find_text_clip(tl):
    """按标记色找独立文字片段。"""
    for ti in range(1, tl.GetTrackCount("video") + 1):
        for it in (tl.GetItemListInTrack("video", ti) or []):
            try:
                if it.GetClipColor() == TEXT_CLIP_COLOR:
                    return it
            except Exception:
                pass
    return None


def _has_dissolve_at(tl, frame):
    """任意视频轨同起点是否存在交叉叠化片段(用户手动方案: 用叠化替代渐显)。"""
    for ti in range(1, tl.GetTrackCount("video") + 1):
        for it in (tl.GetItemListInTrack("video", ti) or []):
            if "交叉叠化" in it.GetName() and it.GetStart() == frame:
                return True
    return False


def _ensure_text_clip(tl, frame, dur_frames, fps):
    """确保存在独立文字片段(指定帧/长度)。返回 (item, created)。

    机理(实测 21.1):
      - 锁住既有视频轨后, InsertFusionTitleIntoTimeline 只落顶部空轨且不推其他轨
      - 二次插入(在 frame+dur)分割首段 → 两次波纹删除把长度修到精确帧数
    """
    existing = _find_text_clip(tl)
    if existing is not None:
        return existing, False
    n = tl.GetTrackCount("video")
    top = n
    if tl.GetTrackName("video", top) != TEXT_TRACK_NAME:
        tl.AddTrack("video")
        top = tl.GetTrackCount("video")
        tl.SetTrackName("video", top, TEXT_TRACK_NAME)
    for ti in range(1, top):
        tl.SetTrackLock("video", ti, True)
    try:
        tl.SetCurrentTimecode(_frame_to_tc(frame, fps))
        time.sleep(0.4)
        t1 = tl.InsertFusionTitleIntoTimeline("Text+")
        if t1 is None:
            return None, False
        time.sleep(0.4)
        tl.SetCurrentTimecode(_frame_to_tc(frame + dur_frames, fps))
        time.sleep(0.4)
        tl.InsertFusionTitleIntoTimeline("Text+")
        time.sleep(0.4)
        for _ in range(2):
            rest = [it for it in (tl.GetItemListInTrack("video", top) or [])
                    if it.GetStart() == frame + dur_frames]
            if not rest:
                break
            tl.DeleteClips(rest, True)
            time.sleep(0.4)
        item = [it for it in (tl.GetItemListInTrack("video", top) or [])
                if it.GetStart() == frame]
        if not item:
            return None, False
        item[0].SetClipColor(TEXT_CLIP_COLOR)
        return item[0], True
    finally:
        for ti in range(1, top):
            tl.SetTrackLock("video", ti, False)


# ---------------------------------------------------------------- 主流程

def run(draft_dir: str, audit_only: bool = False, log=print, allow_bottom: bool = False):
    resolve = _connect()
    proj = resolve.GetProjectManager().GetCurrentProject()
    tl = proj.GetCurrentTimeline()
    fps = 0.0
    try:
        fps = float(tl.GetFrameRate() or 0)
    except Exception:
        fps = 0.0
    if not fps:
        # 部分版本 Timeline 没有 GetFrameRate, 从工程设置取; 再不行才退回 24
        try:
            fps = float(proj.GetSetting("timelineFrameRate") or 0)
        except Exception:
            fps = 0.0
    if not fps:
        fps = 24.0
    log("timeline=%s fps=%g" % (tl.GetName(), fps))

    skips = []
    plan_masks, plan_text = build_plan(draft_dir, fps, allow_bottom=allow_bottom, skips=skips)
    log("plan: %d shape masks, %d text segments" % (len(plan_masks), len(plan_text)))
    if skips:
        from collections import Counter
        cnt = Counter(r["reason"] for r in skips)
        log("skip: %d 段带蒙版但不处理(不建 comp, 不留空壳)" % len(skips))
        for why, n in cnt.most_common():
            log("  %-46s %3d 段" % (why, n))
        if not allow_bottom:
            log("  提示: 若草稿确实要角色浮在纯黑上, 加 --allow-bottom 强制照草稿套")
    for r in plan_masks:
        log("  mask JY-V%d f=%d %-26s %s n=%s feather=%s" %
            (r["jy_track"], r["frame"], r["clip"], r["rt"], r.get("n"), r["feather"]))
    for r in plan_text:
        log("  text f=%d dur=%d '%s'" % (r["frame"], r["dur_frames"], r["text"]))

    outdir = os.path.dirname(os.path.abspath(draft_dir.rstrip("/\\")))
    outdir = os.path.join(outdir, "_maskcheck")
    os.makedirs(outdir, exist_ok=True)

    results = []

    # ---- 文字(独立片段, 类剪映文字轨) ----
    for ti_, t in enumerate(plan_text, 1):
        item, created = _ensure_text_clip(tl, t["frame"], t["dur_frames"], fps)
        if item is None:
            results.append({"kind": "text", "idx": ti_, "text": t["text"], "ok": False,
                            "note": "ensure text clip fail at frame %d" % t["frame"]})
            continue
        comp = _getcomp(item, create=not audit_only)
        if comp is None:
            results.append({"kind": "text", "idx": ti_, "ok": False, "note": "comp None"})
            continue
        cx = (t["x"] + 1) / 2
        cy = (t["y"] + 1) / 2  # 剪映文字 transform y 向上(与蒙版同一勘误)
        size = max(0.01, float(t["font_size"]) * JY_SIZE_FACTOR)
        if audit_only:
            txt = _export_comp(comp, os.path.join(outdir, "at_t%d.comp" % ti_))
            st = re.findall(r'StyledText = Input \{ Value = "([^"]*)"', txt)
            wired = re.search(r'NBGlow = Glow \{.*?Input = Input \{\s*SourceOp = "NBText"', txt, re.S)
            mo_in = re.search(r'MediaOut1 = Saver \{.*?Input = Input \{\s*SourceOp = "NBGlow"', txt, re.S)
            font_ok = JY_FONT in txt
            ok = (bool(st) and st[0] == t["text"] and bool(wired) and bool(mo_in)
                  and font_ok
                  and item.GetStart() == t["frame"]
                  and item.GetEnd() == t["frame"] + t["dur_frames"])
            # 渐显对账(头部有交叉叠化片段时期望 FadeIn=0, 叠化即渐显)
            exp_fi = t.get("fade_in_frames") or 0
            if _has_dissolve_at(tl, item.GetStart()):
                exp_fi = 0
            try:
                got_fi = int(item.GetFades().get("FadeIn") or 0)
            except Exception:
                got_fi = 0
            ok = ok and got_fi == exp_fi
            results.append({"kind": "text", "idx": ti_, "ok": ok,
                            "found": st[0] if st else None,
                            "target": "V%d@%d-%d" % (tl.GetTrackCount("video"),
                                                     item.GetStart(), item.GetEnd())})
        else:
            lua = TEXT_LUA % {"text": t["text"],
                              "cx": maskwrite._n(cx), "cy": maskwrite._n(cy),
                              "size": maskwrite._n(size), "font": JY_FONT,
                              "r1": "1", "g1": "1", "b1": "1",  # 剪映填充: 白色
                              "gr": maskwrite._n(GLOW_COLOR[0]),
                              "gg": maskwrite._n(GLOW_COLOR[1]),
                              "gb": maskwrite._n(GLOW_COLOR[2]),
                              "gx": maskwrite._n(GLOW_XSIZE),
                              "gy": maskwrite._n(GLOW_YSIZE),
                              "ga": maskwrite._n(GLOW_GAIN),
                              "gb2": maskwrite._n(GLOW_BLEND),
                              "gcs": maskwrite._n(GLOW_COLOR_SCALE),
                              "gbs": maskwrite._n(GLOW_BLUE_SCALE),
                              "gf": GLOW_FILTER}
            r = _ex(comp, lua, "t%d" % ti_, outdir)
            ok = "text ok=true" in r.lower()
            try:
                item.SetName("JY-" + (t["text"] or "text"))
            except Exception:
                pass
            # 剪映文字入场"渐显" -> 片段 FadeIn(与头部交叉叠化等效)
            # 若头部已有交叉叠化片段(用户手动方案), 不写 FadeIn, 避免双重渐显
            fi = t.get("fade_in_frames") or 0
            if fi and _has_dissolve_at(tl, item.GetStart()):
                fi = 0
            if fi:
                try:
                    item.SetFades({"FadeIn": fi, "FadeOut": 0})
                except Exception:
                    fi = 0
            results.append({"kind": "text", "idx": ti_, "ok": ok,
                            "ret": r,
                            "target": "V%d@%d-%d%s" % (tl.GetTrackCount("video"),
                                                       item.GetStart(), item.GetEnd(),
                                                       " (new)" if created else "")})

    # ---- 蒙版 ----
    for mi_, p in enumerate(plan_masks, 1):
        clip = _getclip(tl, p["jy_track"], p["frame"])
        if not clip:
            results.append({"kind": "mask", "idx": mi_, "ok": False,
                            "note": "locate fail JY-V%d f=%d" % (p["jy_track"], p["frame"])})
            continue
        comp = _getcomp(clip, create=not audit_only)
        if comp is None:
            results.append({"kind": "mask", "idx": mi_, "ok": False, "note": "comp None"})
            continue
        if audit_only:
            txt = _export_comp(comp, os.path.join(outdir, "am_m%d.comp" % mi_))
            has_mask = "NBPolyMask = PolylineMask" in txt or "NBRectMask = RectangleMask" in txt
            wired = re.search(r'EffectMask = Input \{\s*SourceOp = "(NB\w+)"', txt, re.S)
            mo_in = re.search(r'MediaOut1 = Saver \{.*?Inputs = \{.*?Index.*?Input = Input \{\s*SourceOp = "(\w+)"', txt, re.S)
            npts = len(re.findall(r'X = [0-9.eE+-]+, Y = ', txt))
            exp_n = p.get("n") if p["rt"] == "contour" else None
            closed = bool(re.search(r'Closed\s*=\s*true', txt))
            se = re.findall(r'SoftEdge = Input \{ Value = ([0-9.]+)', txt)
            exp_se = maskwrite.apply_feather(p["feather"])
            se_ok = (not exp_se and not se) or (se and abs(float(se[0]) - exp_se) < 1e-4)
            ok = has_mask and bool(wired) and bool(mo_in) and mo_in.group(1).startswith("MediaIn")
            if exp_n is not None:
                ok = ok and closed and npts >= exp_n
            ok = ok and se_ok
            results.append({"kind": "mask", "idx": mi_, "clip": p["clip"], "ok": ok,
                            "has_mask": has_mask, "wired": bool(wired),
                            "mo_in": mo_in.group(1) if mo_in else None, "npts": npts,
                            "closed": closed})
        else:
            r1 = _ex(comp, CLEAN_LUA, "m%dc" % mi_, outdir)
            if p["rt"] == "contour":
                r2 = _ex(comp, maskwrite.poly_lua(p["pts"], float(p["feather"]), int(p.get("invert", 0))),
                         "m%dw" % mi_, outdir)
            else:
                r2 = _ex(comp, maskwrite.rect_lua(float(p["w"]), float(p["h"]),
                                                  float(p["feather"]), int(p.get("invert", 0))),
                         "m%dw" % mi_, outdir)
            r3 = _ex(comp, maskwrite.wire_lua(), "m%dwire" % mi_, outdir)
            wrote = "poly=true" in r2 if p["rt"] == "contour" else "rect ok=true" in r2
            ok = wrote and r3.startswith("wired")
            results.append({"kind": "mask", "idx": mi_, "clip": p["clip"], "ok": ok,
                            "clean": r1, "write": r2, "wire": r3})

    ok_n = sum(1 for r in results if r["ok"])
    fail_n = len(results) - ok_n
    log("\n===== 对账结果: OK %d / problem %d =====" % (ok_n, fail_n))
    for r in results:
        tag = "OK  " if r["ok"] else "FAIL"
        detail = r.get("note") or r.get("ret") or ""
        log("  %s %s%d %s %s %s" % (tag, r["kind"], r["idx"],
                                    r.get("clip") or r.get("text", ""), detail,
                                    r.get("target") or ""))
    return ok_n, fail_n, results
