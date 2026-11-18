"""清掉时间线上「草稿里根本不需要」的 Fusion comp（空壳），按草稿参数重建片段。

为什么必须重建而不能直接删 comp（达芬奇 21.1 实测）:
  - `TimelineItem.DeleteFusionCompByName(name)` 在 dir() 里存在且 callable,
    但对任何存在的 comp 都返回 False —— 试过 6 种组合(原名/无空格名/索引 1/
    字符串 "1"/附加 True/False 参数/先 LoadFusionCompByName/切到 Fusion 页),
    count 始终不变。
  - Fusion 侧的 `comp:Delete()` 不存在(getmetatable 无该方法, 调用报 nil)。
  - UI 右键对无 comp 片段执行"重置"反而会凭空造一个 comp。
  结论: comp 无法单独删除, 只能"删片段 → 重新放置 → 回填属性"。

判定依据（草稿是唯一事实源）:
  need = masklive.build_plan() 给出的蒙版计划（已含最底层/开关关闭的否决）
  时间线段有 comp 但 (轨, 帧) 不在 need 里 → 该 comp 是空壳, 候选清除。
  预演会把 comp 里的工具清单打出来, 供人工确认（非空壳会有 >2 个工具）。

用法(DaVinciScripts 控制台或命令行):
    python cli.py fix-shell -i "<草稿目录>"              # 预演, 只读
    python cli.py fix-shell -i "<草稿目录>" --go         # 执行
    python cli.py fix-shell -i "<草稿目录>" --go --only 835
    python cli.py fix-shell -i "<草稿目录>" --go --enable  # 顺带修被禁用的段

保留/回填: 时间线起止、源入点(草稿)、变速、变换(Zoom/Pan/Tilt/Rotation/...)、
          淡变、片段名、标记色、启用状态。
拒绝重建: 该段有调色节点(GetNumNodes() > 1) —— 重建会丢调色, 需 --force。
"""

from __future__ import annotations

import os
import time

from nlebridge import jy_read, masklive

# 重建后需要回填的片段属性白名单（其余键多为只读或宿主内部字段）
PROP_KEYS = (
    "ZoomX", "ZoomY", "Pan", "Tilt", "Rotation",
    "AnchorPointX", "AnchorPointY", "Pitch", "Yaw", "FlipX", "FlipY",
    "CropLeft", "CropRight", "CropTop", "CropBottom",
    "CropSoftness", "CropRetain", "Distortion", "Opacity",
)

TOOLS_LUA = r"""
local t = comp:GetToolList()
local s = ""
for _, x in pairs(t) do
  local a = x:GetAttrs()
  s = s .. tostring(a.TOOLS_RegID or "?") .. ":" .. tostring(a.TOOLS_Name or "") .. " "
end
RES = tostring(#t) .. "|" .. s
"""


# ---------------------------------------------------------------- 草稿索引

def draft_index(draft_dir: str, fps: float):
    """(视频轨序号, 起始帧, 时长帧) → 草稿里的段信息。"""
    d, _base = jy_read.load_draft_json(draft_dir)
    videos = {m["id"]: m for m in d.get("materials", {}).get("videos", [])}
    idx = {}
    vno = 0
    for tr in d.get("tracks", []):
        if tr.get("type") != "video":
            continue
        vno += 1
        for seg in tr.get("segments", []):
            tt = seg.get("target_timerange") or {}
            if "duration" not in tt:
                continue          # 坑 12: 从 0 帧起的段缺 start, 但 duration 必有
            f = int(round(tt.get("start", 0) / 1e6 * fps))
            dur = int(round(tt["duration"] / 1e6 * fps))
            st = seg.get("source_timerange") or {}
            v = videos.get(seg.get("material_id"), {})
            idx[(vno, f, dur)] = {
                "name": v.get("material_name") or "",
                "path": v.get("path") or "",
                "src_in": int(round(st.get("start", 0) / 1e6 * fps)),
                "src_len": int(round(st.get("duration", 0) / 1e6 * fps)),
                "speed": float(seg.get("speed") or 1.0),
                "frame": f,
                "dur": dur,
            }
    return idx


def _lookup(idx, track, start, dur):
    """严格三元组优先, 退化为同轨同起点唯一。"""
    if (track, start, dur) in idx:
        return idx[(track, start, dur)]
    cands = [(k, v) for k, v in idx.items() if k[0] == track and k[1] == start]
    if len(cands) == 1:
        return cands[0][1]
    return None


# ---------------------------------------------------------------- 达芬奇侧

def _pool_item_by_path(pool, path):
    """媒体池按绝对路径找素材（GetMediaPoolItem 失败时兜底）。"""
    if not path:
        return None
    stack = [pool.GetRootFolder()]
    while stack:
        f = stack.pop()
        for it in (f.GetClipList() or []):
            if (it.GetClipProperty("File Path") or "") == path:
                return it
        stack.extend(f.GetSubFolderList() or [])
    return None


def _comp_tools(comp, outdir, tag):
    r = masklive._ex(comp, TOOLS_LUA, tag, outdir)
    if "|" not in r:
        return None, r
    n, lst = r.split("|", 1)
    try:
        return int(n), lst.strip()
    except ValueError:
        return None, r


def scan(tl, draft_dir, fps, log=print):
    """列出所有 comp>0 的视频段, 按草稿判定该不该有 comp。"""
    outdir = os.path.join(os.path.dirname(os.path.abspath(draft_dir.rstrip("/\\"))), "_maskcheck")
    os.makedirs(outdir, exist_ok=True)

    skips = []
    plan_masks, _ = masklive.build_plan(draft_dir, fps, allow_bottom=False, skips=skips)
    need = {(p["jy_track"], p["frame"]) for p in plan_masks}
    idx = draft_index(draft_dir, fps)

    rows = []
    for track in range(1, tl.GetTrackCount("video") + 1):
        for it in (tl.GetItemListInTrack("video", track) or []):
            nm = it.GetName() or ""
            if "交叉叠化" in nm:
                continue                      # 转场, 不是素材段
            ncomp = it.GetFusionCompCount()
            if ncomp <= 0:
                continue
            start, end = it.GetStart(), it.GetEnd()
            dur = end - start
            info = _lookup(idx, track, start, dur)
            tools = []
            for ci in range(1, ncomp + 1):
                c = it.GetFusionCompByIndex(ci)
                n, lst = _comp_tools(c, outdir, "scan_%d_%d_%d" % (track, start, ci)) if c else (None, "")
                tools.append(lst or "?")
            row = {
                "track": track, "start": start, "dur": dur, "name": nm,
                "ncomp": ncomp, "tools": tools,
                "info": info, "need": (track, start) in need,
                "nodes": it.GetNumNodes(), "enabled": it.GetClipEnabled(),
                "ftype": it.GetType(),
            }
            # 只有「草稿能对上普通素材段 + 媒体池里真有文件」才可能被判空壳。
            # Fusion 复合片段(文字层/生成的合成)GetMediaPoolItem 有值但 File Path 为空,
            # 删掉就没了, 一律保留。
            mi = it.GetMediaPoolItem()
            path = (mi.GetClipProperty("File Path") or "") if mi else ""
            row["file"] = bool(path)
            if row["need"]:
                row["verdict"] = "保留"
                row["why"] = "草稿要求这段有 comp"
            elif info is None:
                row["verdict"] = "保留"
                row["why"] = "草稿里没有对应素材段(Fusion/复合片段或补生成), 不动"
            elif not path:
                row["verdict"] = "保留"
                row["why"] = "媒体池里没有文件路径(复合片段)"
            else:
                row["verdict"] = "空壳"
                row["why"] = "草稿不需要 comp"
            rows.append(row)

    rows.sort(key=lambda r: (r["track"], r["start"]))
    return rows, need, idx, outdir


# ---------------------------------------------------------------- 重建

def rebuild(tl, pool, row, idx, tl_fps=24.0, force=False, log=print):
    """删片段 + 按草稿重新放置 + 回填属性。返回 (ok, note)。"""
    track, frame, dur = row["track"], row["start"], row["dur"]
    info = row["info"]
    if not info:
        return False, "草稿里找不到对应段(轨/帧/时长都不匹配), 不猜, 跳过"
    if row["nodes"] > 1 and not force:
        return False, "该段有 %d 个调色节点, 重建会丢调色; 确认可丢再加 --force" % row["nodes"]

    same = [it for it in (tl.GetItemListInTrack("video", track) or [])
            if it.GetStart() == frame and "交叉叠化" not in (it.GetName() or "")]
    old = next((it for it in same if abs((it.GetEnd() - it.GetStart()) - dur) <= 1), None)
    if old is None and len(same) == 1:
        old = same[0]                       # 时长已经跑偏的段, 也要能修回来
    if old is None:
        return False, "定位失败(同起点有 %d 个片段)" % len(same)

    snap = {
        "props": old.GetProperty() or {},
        "fades": old.GetFades() or {},
        "color": old.GetClipColor(),
        "name": old.GetName(),
        "enabled": old.GetClipEnabled(),
        "markers": old.GetMarkers() or {},
        "flags": old.GetFlagList() or [],
    }
    mi = old.GetMediaPoolItem()
    if mi is None:
        mi = _pool_item_by_path(pool, info.get("path") or "")
    if mi is None:
        return False, "媒体池里找不到素材(File Path 也对不上)"

    # 变速段: 先按 1x 源帧数放, 再 SetSpeed —— 顺序反了速度不生效(坑 25)
    #
    # 帧号必须折算素材帧率(坑 29): 草稿的 target/source_timerange 都按"时间线 fps"
    # 记帧号, 而 AppendToTimeline 的 startFrame/endFrame 按"素材 fps"记。
    # 实测 30fps 素材 + 24fps 时间线, 直接拿草稿帧号会 79 帧缩成 63 帧(79*24/30)。
    speed = info["speed"]
    try:
        src_fps = float(mi.GetClipProperty("FPS") or 0) or tl_fps
    except (TypeError, ValueError):
        src_fps = tl_fps
    ratio = src_fps / tl_fps if tl_fps else 1.0
    src_in = int(info["src_in"] * ratio)
    src_len = int(round(dur * speed * ratio))
    if src_len <= 0:
        src_len = dur

    n0 = len(tl.GetItemListInTrack("video", track) or [])
    tl.DeleteClips([old], False)
    time.sleep(0.7)
    n1 = len(tl.GetItemListInTrack("video", track) or [])
    if n1 != n0 - 1:
        return False, "删除未生效或连带删了别的(V%d 段数 %d->%d)" % (track, n0, n1)

    obj = {"mediaPoolItem": mi, "startFrame": src_in,
           "endFrame": src_in + src_len,          # 坑 24: endFrame 是排他值
           "recordFrame": frame, "trackIndex": track, "mediaType": 1}
    pool.AppendToTimeline([obj])
    time.sleep(0.9)

    new = None
    for it in (tl.GetItemListInTrack("video", track) or []):
        if it.GetStart() == frame and "交叉叠化" not in (it.GetName() or ""):
            new = it
            break
    if new is None:
        return False, "放置后该位置没有片段"

    if abs(speed - 1.0) > 1e-3:
        new.SetSpeed({"Percentage": round(speed * 100)})
        time.sleep(1.0)
        for it in (tl.GetItemListInTrack("video", track) or []):
            if it.GetStart() == frame and "交叉叠化" not in (it.GetName() or ""):
                new = it
                break

    # 属性回填: 以"刚放置的片段"为基线, 只回填差异键
    base = new.GetProperty() or {}
    delta = {k: v for k, v in (snap["props"] or {}).items()
             if k in PROP_KEYS and base.get(k) != v}
    if delta:
        new.SetProperties(delta)
        time.sleep(0.5)

    if snap["fades"] and (snap["fades"].get("FadeIn") or snap["fades"].get("FadeOut")):
        try:
            new.SetFades(snap["fades"])
        except Exception:
            pass
    if snap["name"] and snap["name"] != new.GetName():
        try:
            new.SetName(snap["name"])
        except Exception:
            pass
    if snap["color"]:
        try:
            new.SetClipColor(snap["color"])
        except Exception:
            pass
    if snap["enabled"] is False:
        pass                              # 原状态就是禁用, 不擅自打开

    got_len = new.GetEnd() - new.GetStart()
    problems = []
    if new.GetStart() != frame:
        problems.append("start=%d(期望%d)" % (new.GetStart(), frame))
    if abs(got_len - dur) > 1:
        problems.append("时长=%d(期望%d)" % (got_len, dur))
    if new.GetFusionCompCount() != 0:
        problems.append("comp=%d" % new.GetFusionCompCount())
    if new.GetClipEnabled() != snap["enabled"]:
        problems.append("enabled=%s(期望%s)" % (new.GetClipEnabled(), snap["enabled"]))
    p2 = new.GetProperty() or {}
    for k in delta:
        if p2.get(k) is not None and abs(float(p2.get(k)) - float(delta[k])) > 1e-3:
            problems.append("%s=%s(期望%s)" % (k, p2.get(k), delta[k]))
    note = "; ".join(problems) if problems else "comp=0 len=%d 属性已回填" % got_len
    if snap["markers"]:
        note += " | ! 原片段有 %d 个标记未回填" % len(snap["markers"])
    if snap["flags"]:
        note += " | ! 原片段有旗标 %s 未回填" % ",".join(snap["flags"])
    return (not problems), note


# ---------------------------------------------------------------- 主流程

def fix(draft_dir: str, go: bool = False, only: int | None = None,
        force: bool = False, enable: bool = False, log=print):
    resolve = masklive._connect()
    pm = resolve.GetProjectManager()
    proj = pm.GetCurrentProject()
    tl = proj.GetCurrentTimeline()
    pool = proj.GetMediaPool()
    # 坑: 21.1 里 GetFrameRate 是 None 属性(不可调用), 与 SaveProject 同类
    try:
        _fr = tl.GetFrameRate
        fps = float(_fr() if callable(_fr) else _fr) or 24.0
    except Exception:
        fps = 24.0
    log("timeline=%s  fps=%g  总长=%d" % (tl.GetName(), fps, tl.GetEndFrame()))
    log("草稿=%s" % os.path.basename(os.path.normpath(draft_dir)))

    rows, need, idx, outdir = scan(tl, draft_dir, fps, log)

    targets = [r for r in rows if r["verdict"] == "空壳"]
    if only is not None:
        targets = [r for r in targets if r["start"] == only]
        rows = [r for r in rows if r["start"] == only]

    log("\n带 comp 的视频段: %d 段, 草稿要求有 comp 的: %d 段" % (len(rows), len(need)))
    if not rows:
        log("时间线上没有带 comp 的段。")
        return 0, 0, []

    log("\n%-6s %-3s %-5s %-6s %-4s %-5s %-6s %s"
        % ("帧", "轨", "时长", "comp", "节点", "判定", "启用", "素材 / comp 内工具"))
    log("-" * 108)
    for r in rows:
        tool_txt = " | ".join(t[:70] for t in r["tools"]) or "-"
        log("%-6d V%-2d %-5d %-6d %-4d %-5s %-6s %s"
            % (r["start"], r["track"], r["dur"], r["ncomp"], r["nodes"],
               r["verdict"], str(r["enabled"]), (r["name"] or "")[:30]))
        log("%-62s└ %s" % ("", r["why"]))
        if tool_txt != "-":
            log("%-62s  %s" % ("", tool_txt))

    if enable:
        offs = [r for r in rows if r["enabled"] is False]
        if offs:
            log("\n禁用段: %d 段" % len(offs))
            for r in offs:
                if not go:
                    log("  V%d@%d %s" % (r["track"], r["start"], r["name"][:30]))
                    continue
                for it in (tl.GetItemListInTrack("video", r["track"]) or []):
                    if it.GetStart() == r["start"]:
                        it.SetClipEnabled(True)
                        log("  V%d@%d 已启用" % (r["track"], r["start"]))
                        break
                time.sleep(0.3)

    if not targets:
        log("\n没有需要清除的空壳 comp。")
        return 0, 0, []

    log("\n待清除空壳: %d 段" % len(targets))
    for r in targets:
        log("  V%d@%d  %s" % (r["track"], r["start"], (r["name"] or "")[:40]))

    if not go:
        log("\n预演: 上面 %d 段将被「删片段 + 重新放置」。加 --go 执行。" % len(targets))
        return 0, len(targets), targets

    ok = 0
    results = []
    for r in targets:
        log("\n--- V%d @%d (%s) ---" % (r["track"], r["start"], (r["name"] or "")[:30]))
        good, note = rebuild(tl, pool, r, idx, tl_fps=fps, force=force, log=log)
        log("  %s %s" % ("OK  " if good else "FAIL", note))
        results.append({"track": r["track"], "start": r["start"], "ok": good, "note": note})
        ok += 1 if good else 0
        time.sleep(0.3)

    log("\n完成 %d/%d 段" % (ok, len(targets)))
    if ok:
        log("保存: %s" % pm.SaveProject())
    return ok, len(targets), results
