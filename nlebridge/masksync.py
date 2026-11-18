"""剪映蒙版 → 达芬奇 Fusion comp（文件导入方案）。

原理: 导出片段现有 comp 为 .comp 文本 → Python 做文本手术(注入蒙版工具) →
ImportFusionComp 导回。完全不依赖 comp.Execute(Lua), 避开沙箱不稳定。

comp 文本要点(以 21.1 实际导出为准):
  - 头部必须是 "Composition { ... }"
  - MediaOut 在 .comp 里是 Saver 工具
  - PolylineMask 的点存在子 BezierSpline 的 KeyFrames[0].Value.Polyline.Points
  - MediaIn 的蒙版接 EffectMask = Input { SourceOp = "NBPolyMask", Source = "Mask" }
"""

from __future__ import annotations

import json
import os
import re


# ---------------------------------------------------------------- 坐标

def jy_to_fusion(x: float, y: float) -> tuple[float, float]:
    """剪映蒙版坐标(中心原点,±1,y向下) → Fusion(左下原点,0..1,y向上)。"""
    return (x + 1.0) / 2.0, (1.0 - y) / 2.0


def lua_num(v) -> str:
    s = "%.6f" % float(v)
    return s.rstrip("0").rstrip(".") if "." in s else s


# ---------------------------------------------------------------- 计划收集

def _is_bottom_only(spans, v_no: int, start_us: int, dur_us: int) -> bool:
    """该段下方是否没有任何重叠的视频层。

    蒙版让画面透明, 透明区露出下层; 最底层露出的只能是黑色。
    实测 2026-11-18: 642/1419/6888 三段 AI 抠像落在最底层, 成片整段黑底。
    """
    a, b = start_us, start_us + max(0, dur_us)
    for o, s0, s1 in spans:
        if o >= v_no:
            continue
        if s1 > a and s0 < b:
            return False
    return True


def collect_mask_plans(draft_dir: str, fps: int = 24, allow_bottom: bool = False) -> list[dict]:
    """扫描草稿, 返回带蒙版的视频片段计划。

    返回条目: {track_no, start_frame, clip_hint, kind, masks:[...], feather, points}
    kind: 'poly' | 'rect' | 'ai'
    """
    from nlebridge import alphamask
    d = json.loads(alphamask.load_draft(draft_dir)) if False else None
    # 直接解密
    from nlebridge import jy_read
    raw = jy_read.decrypt_draft_text(os.path.join(draft_dir, "draft_content.json"))
    d = json.loads(raw)
    masks = {m["id"]: m for m in d["materials"].get("common_mask", [])}
    videos = {m["id"]: m for m in d["materials"].get("videos", [])}

    plans = []
    # 预扫各视频轨段区间, 供"最底层不套蒙版"安全网使用
    spans = []
    _vn = 0
    for tr in d["tracks"]:
        if tr.get("type") != "video":
            continue
        _vn += 1
        for seg in tr.get("segments", []):
            tt = seg.get("target_timerange") or {}
            # 坑 12: 从第 0 帧开始的段不写 start 键, 缺键按 0 处理
            if "duration" in tt:
                _st = tt.get("start", 0)
                spans.append((_vn, _st, _st + tt["duration"]))

    v_no = 0
    for tr in d["tracks"]:
        if tr.get("type") != "video":
            continue
        segs = []
        for seg in tr.get("segments", []):
            v = videos.get(seg.get("material_id"), {})
            nm = v.get("material_name") or ""
            if nm.startswith("水印") or "materials/video/水印" in (v.get("path") or ""):
                continue
            # 剪映只在"关闭"时写 False; 缺省(None)按开启处理
            if seg.get("enable_video_mask") is False:
                continue
            # 调节蒙版独立开关, False = 剪映里没生效
            if seg.get("enable_adjust_mask") is False:
                continue
            segs.append((seg, v, nm))
        if not segs:
            continue
        v_no += 1
        for seg, v, nm in segs:
            t = seg.get("target_timerange", {})
            s = seg.get("source_timerange", {})
            # 最底层套蒙版 → 透明区露黑, 成片必错 (--allow-bottom 可强制照草稿套)
            if (not allow_bottom) and _is_bottom_only(spans, v_no, t.get("start", 0),
                                                      t.get("duration", 0)):
                continue
            ai_masks, shape_masks = [], []
            for h in seg.get("extra_material_refs", []):
                mk = masks.get(h)
                if not mk:
                    continue
                rt = mk.get("resource_type")
                if "adjust_mask" in (mk.get("loader_work_space") or ""):
                    ai_masks.append(mk)
                elif rt == "contour":
                    shape_masks.append(("poly", mk))
                elif rt == "rectangle":
                    shape_masks.append(("rect", mk))
            if not ai_masks and not shape_masks:
                continue
            plans.append({
                "track_no": v_no,
                "start_frame": int(round(t.get("start", 0) / 1e6 * fps)),
                "start_us": t.get("start", 0),
                "dur_us": t.get("duration", 0),
                "src_start_us": s.get("start", 0),
                "src_dur_us": s.get("duration", 0),
                "speed": seg.get("speed", 1.0) or 1.0,
                "material": nm,
                "ai_masks": ai_masks,
                "shape_masks": shape_masks,
            })
    return plans


# ---------------------------------------------------------------- comp 文本生成

def _polyline_points_text(points: list[dict]) -> str:
    rows = []
    for p in points:
        fx, fy = jy_to_fusion(p["anchor"]["x"], p["anchor"]["y"])
        rows.append("\t\t\t\t\t\t\t{ Linear = true, LockY = true, X = %s, Y = %s },"
                    % (lua_num(fx), lua_num(fy)))
    return "\n".join(rows).rstrip(",")


POLYLINE_MASK_T = """\t\tNBPolyMask = PolylineMask {{
\t\t\tDrawMode = "ClickAppend",
\t\t\tDrawMode2 = "InsertAndModify",
\t\t\tInputs = {{
\t\t\t\tFilter = Input {{ Value = FuID {{ "Fast Gaussian" }}, }},
\t\t\t\tSoftEdge = Input {{ Value = {feather}, }},
\t\t\t\tSolid = Input {{ Value = 1, }},
\t\t\t\tLevel = Input {{ Value = 1, }},
\t\t\t\tMaskWidth = Input {{ Value = {w}, }},
\t\t\t\tMaskHeight = Input {{ Value = {h}, }},
\t\t\t\tUseFrameFormatSettings = Input {{ Value = 1, }},
\t\t\t\tClippingMode = Input {{ Value = FuID {{ "None" }}, }},
\t\t\t\tPolyline = Input {{
\t\t\t\t\tSourceOp = "NBPolyMaskSpline",
\t\t\t\t\tSource = "Value",
\t\t\t\t}},
\t\t\t}},
\t\t}},
\t\tNBPolyMaskSpline = BezierSpline {{
\t\t\tSplineColor = {{ Red = 160, Green = 252, Blue = 4 }},
\t\t\tNameSet = true,
\t\t\tKeyFrames = {{
\t\t\t\t[0] = {{ 0, Flags = {{ Linear = true, LockedY = true }}, Value = Polyline {{
\t\t\t\t\tClosed = true,
\t\t\t\t\tPoints = {{
{points}
\t\t\t\t\t}}
\t\t\t\t}} }}
\t\t\t}}
\t\t}},"""

RECT_MASK_T = """\t\tNBRectMask = RectangleMask {{
\t\t\tInputs = {{
\t\t\t\tFilter = Input {{ Value = FuID {{ "Fast Gaussian" }}, }},
\t\t\t\tSoftEdge = Input {{ Value = {feather}, }},
\t\t\t\tSolid = Input {{ Value = 1, }},
\t\t\t\tLevel = Input {{ Value = 1, }},
\t\t\t\tMaskWidth = Input {{ Value = {w}, }},
\t\t\t\tMaskHeight = Input {{ Value = {h}, }},
\t\t\t\tWidth = Input {{ Value = {rw}, }},
\t\t\t\tHeight = Input {{ Value = {rh}, }},
\t\t\t\tUseFrameFormatSettings = Input {{ Value = 1, }},
\t\t\t}},
\t\t}},"""

AI_Matte_T = """\t\tNBAlphaLoader = Loader {{
\t\t\tInputs = {{
\t\t\t\tClip = Input {{
\t\t\t\t\tValue = {{
\t\t\t\t\t\tFile = "{path}",
\t\t\t\t\t\tFormat = "PNGFormat",
\t\t\t\t\t\tStartFrame = 0,
\t\t\t\t\t\tLength = 0,
\t\t\t\t\t\tEndFrame = {end_frame},
\t\t\t\t\t\tTrimIn = 0,
\t\t\t\t\t\tTrimOut = {end_frame},
\t\t\t\t\t\tExtendFirst = 0,
\t\t\t\t\t\tExtendLast = {end_frame},
\t\t\t\t\t\tLoop = 0,
\t\t\t\t\t\tAspectMode = 0,
\t\t\t\t\t\tDepth = 0,
\t\t\t\t\t\tTimeCode = 0,
\t\t\t\t\t}},
\t\t\t\t}},
\t\t\t}},
\t\t}},
\t\tNBAlphaScale = Transform {{
\t\t\tInputs = {{
\t\t\t\tSize = Input {{ Value = {scale}, }},
\t\t\t\tInput = Input {{
\t\t\t\t\tSourceOp = "NBAlphaLoader",
\t\t\t\t\tSource = "Output",
\t\t\t\t}},
\t\t\t}},
\t\t}},
\t\tNBAlphaMask = BitmapMask {{
\t\t\tInputs = {{
\t\t\t\tImage = Input {{
\t\t\t\t\tSourceOp = "NBAlphaScale",
\t\t\t\t\tSource = "Output",
\t\t\t\t}},
\t\t\t\tChannel = Input {{ Value = 4, }},
\t\t\t\tLevel = Input {{ Value = 1, }},
\t\t\t\tSoftEdge = Input {{ Value = 0, }},
\t\t\t\tUseFrameFormatSettings = Input {{ Value = 1, }},
\t\t\t}},
\t\t}},"""

EFFECT_MASK_T = """\t\t\t\tEffectMask = Input {{
\t\t\t\t\tSourceOp = "{op}",
\t\t\t\t\tSource = "Mask",
\t\t\t\t}},"""


def build_comp_text(template: str, plan: dict, out_dir: str,
                    width: int = 1920, height: int = 1080) -> str:
    """在导出的 comp 模板基础上注入蒙版工具, 返回新 comp 文本。"""
    txt = template
    tools = []
    mask_op = None

    if plan["shape_masks"]:
        kind, mk = plan["shape_masks"][0]
        if kind == "poly":
            pts = mk["contour_path"]["coutour_path"]
            feather = (mk.get("config") or {}).get("feather") or 0.0
            tools.append(POLYLINE_MASK_T.format(
                feather=lua_num(feather), w=width, h=height,
                points=_polyline_points_text(pts)))
            mask_op = "NBPolyMask"
        else:
            cfg = mk.get("config") or {}
            tools.append(RECT_MASK_T.format(
                w=width, h=height,
                rw=lua_num(cfg.get("width", 0.5)), rh=lua_num(cfg.get("height", 0.5)),
                feather=lua_num(cfg.get("feather") or 0.0)))
            mask_op = "NBRectMask"
    elif plan["ai_masks"]:
        from nlebridge import alphamask
        frame_sets = []
        for mk in plan["ai_masks"]:
            mdir = alphamask.resolve_mask_dir(plan["_draft_dir"], mk)
            if mdir:
                frame_sets.append(alphamask.load_frames(mdir))
        if frame_sets:
            frames = alphamask.union_frames(frame_sets)
            step = 1e6 / (24.0 * plan["speed"])
            n = max(1, int(round(plan["src_dur_us"] / step)))
            key = "V%d@%.2fs_%s" % (plan["track_no"], plan["start_us"] / 1e6,
                                    plan["material"].split(".")[0][:12].replace(" ", "_"))
            seq_dir = os.path.join(out_dir, key, "alpha")
            alphamask.export_matte_sequence(frames, seq_dir, plan["src_start_us"],
                                            plan["src_dur_us"], 24.0 * plan["speed"])
            first = os.path.join(seq_dir, "frame_0001.png").replace("\\", "/")
            tools.append(AI_Matte_T.format(
                path=first, end_frame=n - 1, scale=lua_num(width / 512.0)))
            mask_op = "NBAlphaMask"

    if not tools:
        return txt

    # 注入工具到 Tools 表尾部(在收尾 "}," 与 "Frames" 之间)
    inject = "\n".join(tools)
    txt = re.sub(r"(\n\t\},\n\tFrames = \{)", "\n" + inject + r"\1", txt, count=1)

    # MediaIn1 的 Inputs 注入 EffectMask
    if mask_op:
        em = EFFECT_MASK_T.format(op=mask_op)
        m = re.search(r"(MediaIn1 = Loader \{\s*.*?Inputs = \{)", txt, re.S)
        if m:
            txt = txt[:m.end()] + "\n" + em + txt[m.end():]
    return txt
