# -*- coding: utf-8 -*-
"""Premiere Pro XML (Final Cut Pro 7 / xmeml) 读取器

支持:
  - bin 内 <clip> 定义与 clipitem 内 <file> 引用
  - 序列 <rate><timebase> 决定帧率
  - 视频 Motion 效果: center / scale / rotation
  - 音量 audiolevels (dB) → 线性
  - <fi> 变速标记 (恒定变速)
不支持(记入 warning): Time Remap 曲线、Lumetri、转场细节、嵌套序列
"""
import os
import math
import xml.etree.ElementTree as ET
from typing import Dict, Optional
from urllib.parse import unquote, urlparse

from .ir import Timeline, Track, Clip, Media, Transform


def _text(node, path, default=None):
    if node is None:
        return default
    n = node.find(path)
    return n.text if n is not None and n.text is not None else default


def _num(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _int(s, default=0):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _path_from_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("file:"):
        p = urlparse(u)
        raw = unquote(p.path)
        if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]                 # /D:/... -> D:/...
        return raw.replace("/", os.sep) if os.sep == "\\" else raw
    return unquote(u)


def _rate_of(node, default: int) -> int:
    r = node.find("rate") if node is not None else None
    tb = _int(_text(r, "timebase"), 0)
    return tb or default


class _FileIndex:
    def __init__(self):
        self.by_id: Dict[str, dict] = {}

    def register(self, file_el):
        fid = file_el.get("id") or ""
        path = _path_from_url(_text(file_el, "pathurl", "") or "")
        dur = _int(_text(file_el, "duration"), 0)
        tb = _int(_text(file_el, "rate/timebase"), 0)
        v = file_el.find("media/video")
        a = file_el.find("media/audio")
        info = {
            "path": path,
            "duration": dur,
            "fps": tb,
            "width": _int(_text(v, "samplecharacteristics/width"), 0) if v is not None else 0,
            "height": _int(_text(v, "samplecharacteristics/height"), 0) if v is not None else 0,
            "has_audio": a is not None,
            "has_video": v is not None,
        }
        if fid:
            self.by_id[fid] = info
        return info


def read(path: str, name: Optional[str] = None) -> Timeline:
    root = ET.parse(path).getroot()
    if root.tag != "xmeml":
        raise ValueError("不是 Premiere/FCP7 XML (根节点应为 xmeml)")

    fps_default = 24
    seq = root.find(".//sequence")
    if seq is None:
        raise ValueError("XML 中未找到 sequence")
    fps = _rate_of(seq, fps_default)

    files = _FileIndex()
    # FCP7 XML 里素材定义有两种: bin 内的 <clip id=..> 和 clipitem 内的 <file id=..>
    for el in root.iter():
        if el.tag in ("clip", "file") and el.get("id") and el.find("pathurl") is not None:
            files.register(el)

    # 序列格式
    fmt = seq.find("media/video/format/samplecharacteristics")
    w = _int(_text(fmt, "width"), 1920) if fmt is not None else 1920
    h = _int(_text(fmt, "height"), 1080) if fmt is not None else 1080

    tl = Timeline(name=name or _text(seq, "name", "PR Sequence") or "PR Sequence",
                  width=w, height=h, fps=fps, source_app="premiere")

    def ensure_media(fpath: str, info: Optional[dict], kind: str) -> str:
        mid = os.path.normcase(fpath) if fpath else f"missing-{len(tl.media)}"
        if mid in tl.media:
            return mid
        tl.add_media(Media(
            id=mid, path=fpath, kind=kind,
            duration_us=_frames_us((info or {}).get("duration", 0), fps),
            width=(info or {}).get("width", 0), height=(info or {}).get("height", 0),
            fps=(info or {}).get("fps", 0) or fps,
            has_audio=bool((info or {}).get("has_audio")),
            name=os.path.basename(fpath) if fpath else "unknown",
            missing=not (fpath and os.path.exists(fpath)),
        ))
        return mid

    # ---- 视频轨 ----
    video = seq.find("media/video")
    if video is not None:
        for track_el in video.findall("track"):
            track = Track(kind="video")
            for ci in track_el.findall("clipitem"):
                clip = _read_clipitem(ci, fps, tl, files, ensure_media)
                if clip:
                    track.clips.append(clip)
            if track.clips:
                tl.tracks.append(track)

    # ---- 音频轨 ----
    audio = seq.find("media/audio")
    if audio is not None:
        for track_el in audio.findall("track"):
            track = Track(kind="audio")
            for ci in track_el.findall("clipitem"):
                clip = _read_clipitem(ci, fps, tl, files, ensure_media)
                if clip:
                    track.clips.append(clip)
            if track.clips:
                tl.tracks.append(track)

    if not tl.video_tracks():
        tl.warnings.append("序列中没有视频片段")

    # 转场与高级效果只记录不转换
    n_trans = len(list(seq.iter("transitionitem")))
    if n_trans:
        tl.warnings.append(f"{n_trans} 个转场未转换（PR 转场语义与目标端不同）")
    if seq.find(".//effectid[.='TimeRemap']") is not None:
        tl.warnings.append("检测到 Time Remap 变速，已按 clipitem 时长/源范围估算速度")

    n_missing = sum(1 for m in tl.media.values() if m.missing)
    if n_missing:
        tl.warnings.append(f"{n_missing} 个素材文件缺失，导入后需重链")
    tl.stats = {"clips": tl.clip_count(), "transitions": n_trans}
    return tl


def _frames_us(frames: int, fps: int) -> int:
    return int(round(frames * 1_000_000 / fps)) if fps else 0


def _motion_params(ci, tl=None) -> Transform:
    """读取 Motion 变换.

    PR 自己导出的 XML 里: effectid=basic, center 用 <horiz>/<vert>,
    且是**归一化**值(1.0 = 一个画面宽/高), 不是像素.
    兼容早期本工具写过的 <h>/<v> 像素写法.
    """
    tf = Transform()
    w = getattr(tl, "width", 1920) or 1920
    h = getattr(tl, "height", 1080) or 1080
    effects = ci.findall("filter/effect") + ci.findall("effect")
    for eff in effects:
        eid = _text(eff, "effectid", "") or ""
        if eid not in ("VideoMotion", "basic", "motion"):
            continue
        for p in eff.findall("parameter"):
            pid = _text(p, "parameterid", "")
            val = p.find("value")
            if val is None:
                continue
            if pid == "center":
                hz = _text(val, "horiz")
                vt = _text(val, "vert")
                if hz is not None or vt is not None:
                    # 0.5,0.5 = 画面中心; 直接乘会得到离中心两倍的偏移
                    tf.x = (_num(hz, 0.5) - 0.5) * w
                    tf.y = (0.5 - _num(vt, 0.5)) * h   # PR 的 Y 向上为正
                else:                                  # 旧写法: 像素
                    tf.x = _num(_text(val, "h"), 0.0)
                    tf.y = _num(_text(val, "v"), 0.0)
            elif pid == "scale":
                s = _num(val.text, 100.0) / 100.0
                tf.scale_x = tf.scale_y = s
            elif pid in ("scaleX", "scale_width"):
                tf.scale_x = _num(val.text, 100.0) / 100.0
            elif pid in ("scaleY", "scale_height"):
                tf.scale_y = _num(val.text, 100.0) / 100.0
            elif pid == "rotation":
                tf.rotation = _num(val.text, 0.0)
    if tf.scale_x < 0:
        tf.flip_h, tf.scale_x = True, abs(tf.scale_x)
    if tf.scale_y < 0:
        tf.flip_v, tf.scale_y = True, abs(tf.scale_y)
    return tf


def _motion_kfs(ci, tl=None) -> dict:
    """读 PR 的 Motion 关键帧 (<parameter><keyframe><when frame><value>)"""
    w = getattr(tl, "width", 1920) or 1920
    h = getattr(tl, "height", 1080) or 1080
    fps = getattr(tl, "fps", 24) or 24
    out = {}
    for eff in ci.findall("filter/effect") + ci.findall("effect"):
        if (_text(eff, "effectid", "") or "") not in ("VideoMotion", "basic", "motion"):
            continue
        for p in eff.findall("parameter"):
            pid = _text(p, "parameterid", "")
            kfs = p.findall("keyframe")
            if len(kfs) < 2:
                continue
            pts = []
            for kf in kfs:
                t = int(round(_num(_text(kf, "when"), 0) * 1_000_000 / fps))
                v = kf.find("value")
                if pid == "center" and v is not None:
                    pts.append((t, (_num(_text(v, "horiz"), 0.5) - 0.5) * w,
                                (0.5 - _num(_text(v, "vert"), 0.5)) * h))
                elif pid == "scale":
                    s = _num(_text(kf, "value"), 100.0) / 100.0
                    pts.append((t, s, s))
                elif pid in ("scaleX",):
                    pts.append((t, _num(_text(kf, "value"), 100.0) / 100.0,))
                elif pid == "rotation":
                    pts.append((t, _num(_text(kf, "value"), 0.0)))
            if len(pts) >= 2:
                out[{"center": "position", "scale": "scale",
                     "rotation": "rotation"}.get(pid, pid)] = pts
    return out


def _volume_of(ci) -> float:
    effects = ci.findall("filter/effect") + ci.findall("effect")
    for eff in effects:
        eid = _text(eff, "effectid", "")
        if eid not in ("audiolevels", "audioVolume"):
            continue
        for p in eff.findall("parameter"):
            if _text(p, "parameterid", "") in ("level", "volume"):
                db = _num(_text(p, "value"), 0.0)
                return round(10 ** (db / 20.0), 4) if db > -96 else 0.0
    return 1.0


def _speed_of(ci) -> float:
    # Time Remap (timeremap) 的恒定变速
    effects = ci.findall("filter/effect") + ci.findall("effect")
    for eff in effects:
        if (_text(eff, "effectid", "") or "") != "timeremap":
            continue
        for p in eff.findall("parameter"):
            if _text(p, "parameterid", "") == "speed":
                v = _num(_text(p, "value"), 1.0)
                return v if v > 0 else 1.0
    fi = ci.find("fi")
    if fi is None:
        return 1.0
    kf = fi.find("keyframe")
    v = _num(_text(kf, "value"), 1.0) if kf is not None else 1.0
    return v if v > 0 else 1.0


def _read_clipitem(ci, fps, tl, files, ensure_media):
    if (_text(ci, "enabled", "TRUE") or "TRUE").upper() == "FALSE":
        return None                        # 禁用片段(如音量备份轨)不参与时间线
    fl = ci.find("file")
    info = None
    fpath = ""
    if fl is not None:
        # PR 用 <file id="fileN"/> 作引用 (同一 id 在 bin 的 masterclip 里定义)
        fid = fl.get("ref") or fl.get("id")
        if fid and fid in files.by_id:
            info = files.by_id[fid]
            fpath = info["path"]
        else:
            info = files.register(fl)
            fpath = info["path"]
    if not fpath:
        return None                                    # 嵌套序列/生成器：跳过

    start = _int(_text(ci, "start"), -1)
    end = _int(_text(ci, "end"), -1)
    if start < 0 or end <= start:
        return None
    sin = _int(_text(ci, "in"), 0)
    sout = _int(_text(ci, "out"), sin + (end - start))
    kind = "audio" if (_text(ci, "sourcetrack/mediatype", "") == "audio"
                       or ci.get("mediaType") == "audio"
                       or _text(ci, "file/media/audio") is not None) else "video"
    if kind == "video" and info and not info.get("has_video") and not info.get("width"):
        kind = "audio"

    mid = ensure_media(fpath, info, kind)
    src_dur_f = max(sout - sin, 0)
    tl_dur_f = end - start
    speed = _speed_of(ci)
    if abs(speed - 1.0) < 1e-4 and src_dur_f and tl_dur_f:
        speed = round(src_dur_f / tl_dur_f, 4)         # 由时长差推断
    return Clip(
        media_id=mid,
        start=_frames_us(start, fps),
        dur=_frames_us(tl_dur_f, fps),
        src_in=_frames_us(max(sin, 0), fps),
        src_dur=_frames_us(src_dur_f, fps),
        speed=speed,
        transform=_motion_params(ci, tl),
        volume=_volume_of(ci),
        label=_text(ci, "name", "") or "",
        source_app="premiere",
        kfs=_motion_kfs(ci, tl),
    )
