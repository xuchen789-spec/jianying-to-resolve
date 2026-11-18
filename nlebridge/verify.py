# -*- coding: utf-8 -*-
"""写出后的自检: 捕捉"全片单帧""音频无流"这类致命但静默的错误"""
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote


def _t(x):
    if not x:
        return 0.0
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*s\s*$", x)
    if m:
        d = float(m.group(2))
        return float(m.group(1)) / d if d else 0.0
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*s\s*$", x)
    return float(m.group(1)) if m else 0.0


def check_fcpxml(path: str):
    """返回 (问题列表, 统计)"""
    root = ET.parse(path).getroot()
    assets = {}
    for a in root.iter("asset"):
        assets[a.get("id")] = {
            "dur": _t(a.get("duration")),
            "video": a.get("hasVideo") == "1",
            "audio": a.get("hasAudio") == "1",
        }
    problems, n_audio, n_video = [], 0, 0
    for c in root.iter("asset-clip"):
        ref = c.get("ref")
        info = assets.get(ref)
        if info is None:
            problems.append(f"片段引用了未定义素材 {ref}")
            continue
        dur = _t(c.get("duration"))
        if dur <= 1.0 / 60:
            problems.append(f"片段时长异常({dur:.3f}s) ref={ref}")
        if info["dur"] and _t(c.get("start")) + dur > info["dur"] + 1e-6:
            # start 是"补偿后"的值(= 源入点 ÷ 倍速), duration 是时间线时长。
            # 换算回源时间轴必须整体乘倍速, 否则 0.8x 段会被冤枉成"素材不够长"。
            eff = _t(c.get("start")) + dur
            spd = 1.0
            tm = c.find(".//timeMap")
            if tm is not None:
                pts = [(_t(p.get("time")), _t(p.get("value")))
                       for p in tm.iter("timept")]
                if len(pts) >= 2:
                    v_end = pts[-1][1]
                    if dur > 0 and v_end > 0:
                        spd = v_end / dur
            if eff * spd > info["dur"] + 1.0 / 24:
                problems.append(f"素材时长不足: ref={ref} 需要 {eff * spd:.2f}s "
                                f"实际 {info['dur']:.2f}s")
        if c.get("srcEnable") == "audio":
            n_audio += 1
            if not info["audio"]:
                problems.append(f"音频片段所在素材没有音频流: ref={ref}")
            if info["video"]:
                # 含视频流的素材被音频片段引用, 达芬奇会无视 srcEnable 而当视频片段处理,
                # 时间线上凭空多出视频轨。必须为音频单独声明 hasVideo="0" 的素材。
                problems.append(f"音频片段引用了带视频流的素材(会被当成视频片段另开轨): ref={ref}")
        else:
            n_video += 1
            if not info["video"]:
                problems.append(f"视频片段所在素材没有视频流: ref={ref}")
    stats = {"assets": len(assets), "video_clips": n_video, "audio_clips": n_audio}

    # ---- 静帧文件名不能含数字: 达芬奇会当成图片序列去找不存在的帧 ----
    for a in root.iter("asset"):
        name = a.get("name") or ""
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".psd")) \
                and re.search(r"\d", os.path.splitext(name)[0]):
            problems.append(f"静帧文件名含数字, 可能被当成图片序列: {name}"
                            "（用 mediaprobe.apply_stills 改名）")

    # ---- 结构检查: spine 必须首尾相接, 否则达芬奇会在断口处停止解析 ----
    spine = root.find(".//spine")
    if spine is not None:
        cursor, holes = 0.0, 0
        for el in spine:
            lane = el.get("lane")
            if lane is not None:
                problems.append(f"spine 里出现了带 lane 的平级片段({el.get('lane')}) —— "
                                "挂靠片段必须作为主故事线元素的子元素, 平级写 lane 会被忽略")
                continue
            off = _t(el.get("offset"))
            if abs(off - cursor) > 1e-4:
                holes += 1
                if holes <= 3:
                    problems.append(f"主故事线断口: 期望 {cursor:.3f}s 实际 {off:.3f}s")
            cursor = off + _t(el.get("duration"))
        if holes:
            problems.append(f"主故事线共 {holes} 处断口 → 达芬奇会截断时间线(片段大量丢失)")

        # 逐 lane 检查重叠: 挂靠片段的 offset 相对父元素, 要先还原成绝对时间
        lanes, nesting = {}, 0
        def walk(parent, base, depth=0):
            nonlocal nesting
            for el in parent:
                if el.tag not in ("asset-clip", "clip", "gap"):
                    continue
                off = base + _t(el.get("offset"))
                ln = el.get("lane")
                if ln is not None and el.tag == "asset-clip":
                    nesting += 1 if depth else 0
                    lanes.setdefault(ln, []).append((off, _t(el.get("duration"))))
                walk(el, off, depth + 1)
        walk(spine, 0)
        if nesting == 0 and lanes:
            problems.append("所有片段都平铺在 spine 里, 没有任何挂靠片段 —— "
                            "达芬奇会忽略 lane 并按空轨重排(音轨顺序会乱)")
        for ln, items in lanes.items():
            items.sort()
            for (a, da), (b, _db) in zip(items, items[1:]):
                if b < a + da - 1e-4:
                    problems.append(f"lane {ln} 内片段重叠: {a:.2f}s 与 {b:.2f}s")
                    break
        stats["connected_clips"] = nesting

    # ---- 变换单位: FCPXML 的 position 是"画面高度百分比", 写像素会大 10.8 倍 ----
    for tr in root.iter("adjust-transform"):
        pos = tr.get("position") or ""
        try:
            vx, vy = [float(v) for v in pos.split()]
        except ValueError:
            problems.append(f"adjust-transform position 不是两个数: {pos!r}")
            continue
        if max(abs(vx), abs(vy)) > 200:
            problems.append(f"adjust-transform position={pos} 数值过大 —— "
                            "FCPXML 的位置单位是画面高度百分比, 不是像素")
            break

    # ---- 素材路径: 逐条确认文件真实存在, 避免导入时满屏"未找到" ----
    missing = 0
    for a in root.iter("asset"):
        src = a.get("src") or ""
        if not src:
            continue
        p = unquote(src.replace("file:///", "").replace("file://localhost/", ""))
        p = p.replace("/", os.sep)
        if not os.path.exists(p):
            missing += 1
            if missing <= 3:
                problems.append(f"素材文件不存在: {a.get('name')} → {p}")
    if missing:
        problems.append(f"共 {missing} 个素材文件在磁盘上找不到")
    stats["missing_media"] = missing
    return problems, stats


def _pr_tree_check(root):
    """PR 的 xmeml 解析器对"多出来的元素"是整份拒绝的(报"文件导入失败"且不给原因)。
    这里按 Apple xmeml 文档 + PR 自己导出的合法子元素做白名单校验。"""
    allowed = {
        "xmeml": {"project", "bin", "clip", "sequence", "importoptions"},
        "project": {"name", "children"},
        "children": {"clip", "sequence", "bin"},
        "bin": {"name", "children", "labels", "comments", "uuid", "updatebehavior"},
        "clip": {"uuid", "masterclipid", "ismasterclip", "duration", "rate", "name",
                 "media", "logginginfo", "colorinfo", "labels", "start", "end", "in", "out",
                 "alphatype", "pixelaspectratio", "anamorphic", "file", "filter",
                 "stillframe", "sourcetrack", "comments"},
        "sequence": {"uuid", "name", "duration", "rate", "in", "out", "timecode", "media",
                     "marker", "labels", "comment", "comments", "masterclipid", "ismasterclip",
                     "description", "logginginfo", "filmdata", "file", "pixelaspectratio",
                     "updatebehavior", "sequence"},
        "media": {"video", "audio"},
        "video": {"format", "track", "samplecharacteristics"},
        "audio": {"format", "track", "samplecharacteristics", "channelcount",
                  "numOutputChannels", "outputs"},
        "track": {"clipitem", "generatoritem", "transitionitem", "enabled", "locked",
                  "outputchannelindex"},
        "clipitem": {"masterclipid", "name", "enabled", "duration", "rate", "start", "end",
                     "in", "out", "alphatype", "pixelaspectratio", "anamorphic", "fielddominance",
                     "file", "sourcetrack", "stillframe", "stillframeoffset", "filter", "link",
                     "logginginfo", "colorinfo", "labels", "comments", "timecode", "marker",
                     "sequence", "multiclip", "itemhistory", "subclipinfo",
                     # PR 专有但 PR 自己会导出
                     "pproTicksIn", "pproTicksOut", "primarytimecode"},
        "file": {"name", "pathurl", "rate", "duration", "timecode", "media", "width",
                 "height", "mediaSource"},
        "filter": {"effect"},
        "effect": {"name", "effectid", "effectcategory", "effecttype", "mediatype",
                   "pproBypass", "parameter", "wipecode", "wipeaccuracy", "startratio",
                   "endratio", "reverse"},
        "parameter": {"parameterid", "name", "valuemin", "valuemax", "value", "keyframe",
                      "interpolation"},
        "keyframe": {"when", "value", "interp", "speedvirtualkf", "speedkfin",
                     "speedkfout", "speedkfend"},
        "rate": {"timebase", "ntsc"},
        "timecode": {"rate", "string", "frame", "displayformat"},
        "samplecharacteristics": {"rate", "width", "height", "anamorphic",
                                  "pixelaspectratio", "fielddominance", "depth",
                                  "samplerate", "codec", "colordepth"},
    }
    out, seen = [], set()
    for parent in root.iter():
        ok = allowed.get(parent.tag)
        if ok is None:
            continue
        for child in parent:
            if child.tag not in ok:
                key = (parent.tag, child.tag)
                if key in seen:
                    continue
                seen.add(key)
                out.append(f"xmeml 结构非法: <{parent.tag}> 里出现 <{child.tag}> "
                           f"（PR 会整份拒绝导入, 报\"文件导入失败\"）")
    return out


def check_pr_xml(path: str):
    root = ET.parse(path).getroot()
    problems = []
    problems += _pr_tree_check(root)
    # file 定义
    files = {}
    for f in root.iter("file"):
        if f.find("pathurl") is not None:
            files[f.get("id")] = {
                "dur": int((f.findtext("duration") or "0").strip() or 0),
                "video": f.find("media/video") is not None,
                "audio": f.find("media/audio") is not None,
                "still": f.find("stillframe") is not None,
            }
    n_v = n_a = 0
    for tr in root.findall(".//sequence/media/video/track"):
        n_v += len(tr.findall("clipitem"))
    for tr in root.findall(".//sequence/media/audio/track"):
        n_a += len(tr.findall("clipitem"))
    for ci in root.findall(".//sequence//clipitem"):
        if (ci.findtext("enabled") or "TRUE").strip().upper() == "FALSE":
            continue
        f = ci.find("file")
        if f is None:
            continue
        fid = f.get("ref") or f.get("id")
        info = files.get(fid)
        if info is None:
            problems.append(f"clipitem {ci.get('id')} 的 file 未定义: {fid}")
            continue
        try:
            s, e = int(ci.findtext("start")), int(ci.findtext("end"))
        except (TypeError, ValueError):
            problems.append(f"clipitem {ci.get('id')} 缺少 start/end")
            continue
        if e - s <= 0:
            problems.append(f"clipitem {ci.get('id')} 时长<=0")
        if info["dur"] and e - s > info["dur"] + 2:
            problems.append(f"clipitem {ci.get('id')} 超过素材长度 {e - s}>{info['dur']}")
        st = ci.find("sourcetrack")
        mt = st.findtext("mediatype") if st is not None else None
        if mt == "audio" and not info["audio"]:
            problems.append(f"clipitem {ci.get('id')} 标为音频但 file 没有音频流")
        if mt == "video" and info["video"] is False:
            problems.append(f"clipitem {ci.get('id')} 标为视频但 file 没有视频流")
    stats = {"files": len(files), "video_clips": n_v, "audio_clips": n_a}
    return problems, stats
