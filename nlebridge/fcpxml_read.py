# -*- coding: utf-8 -*-
"""FCPXML 读取器 (达芬奇 / Final Cut 导出, 也读本工具生成的 Resolve 中间文件)

用于反向路线: 达芬奇时间线 → 剪映 / PR
支持: asset-clip / clip, lane 分层, position/scale/rotation, speed, audioVolume
不支持(记 warning): 复合片段、转场、Fusion 效果、调色节点
"""
import os
import re
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import unquote, urlparse

from .ir import Timeline, Track, Clip, Media, Transform
from .mediaprobe import image_size

_TIME_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*s\s*$")


def parse_time(t: Optional[str]) -> int:
    """FCPXML 时间 → 微秒"""
    if not t:
        return 0
    t = t.strip()
    m = _TIME_RE.match(t)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        return int(round(num / den * 1_000_000)) if den else 0
    m2 = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*s\s*$", t)
    if m2:
        return int(round(float(m2.group(1)) * 1_000_000))
    m3 = re.match(r"^\s*(-?\d+)\s*$", t)
    if m3:
        return int(m3.group(1))
    return 0


def _num(s, d=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return d


def _path_from_src(u: str) -> str:
    if not u:
        return ""
    if u.startswith("file:"):
        p = urlparse(u)
        raw = unquote(p.path)
        if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]
        return raw
    return unquote(u)


def read(path: str, name: Optional[str] = None) -> Timeline:
    root = ET.parse(path).getroot()
    if root.tag != "fcpxml":
        raise ValueError("不是 FCPXML 文件 (根节点应为 fcpxml)")

    # 格式 / 帧率
    fmt = root.find(".//resources/format")
    width = int(_num(fmt.get("width"), 1920)) if fmt is not None else 1920
    height = int(_num(fmt.get("height"), 1080)) if fmt is not None else 1080
    fps = 24
    if fmt is not None and fmt.get("frameDuration"):
        us = parse_time(fmt.get("frameDuration"))
        if us:
            fps = int(round(1_000_000 / us))

    # 素材表
    assets = {}
    for a in root.iter("asset"):
        src = _path_from_src(a.get("src", ""))
        assets[a.get("id")] = {
            "path": src,
            "name": a.get("name") or os.path.basename(src),
            "duration": parse_time(a.get("duration")),
            "has_video": a.get("hasVideo") == "1",
            "has_audio": a.get("hasAudio") == "1",
        }

    proj = root.find(".//project")
    seq = root.find(".//sequence")
    tl = Timeline(name=name or (proj.get("name") if proj is not None else "Resolve Timeline"),
                  width=width, height=height, fps=fps, source_app="resolve")

    def ensure_media(ref: str) -> str:
        info = assets.get(ref, {})
        p = info.get("path", "")
        mid = os.path.normcase(p) if p else f"missing-{ref}"
        is_img = os.path.splitext(p)[1].lower() in (
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff")
        iw, ih = image_size(p) if is_img else (0, 0)
        if mid not in tl.media:
            tl.add_media(Media(
                id=mid, path=p,
                kind="image" if is_img else ("video" if info.get("has_video", True) else "audio"),
                width=iw, height=ih,
                duration_us=info.get("duration", 0),
                name=info.get("name", ""),
                has_audio=info.get("has_audio", False),
                missing=not (p and os.path.exists(p)),
            ))
        return mid

    def make_clip(el) -> Optional[Clip]:
        ref = el.get("ref")
        if not ref or ref not in assets:
            return None
        offset = parse_time(el.get("offset"))
        dur = parse_time(el.get("duration"))
        start = parse_time(el.get("start"))
        if dur <= 0:
            return None
        speed = _num((el.get("speed") or "1x").rstrip("x"), 1.0)
        # 变速: timeMap(time = 新时间线时间, value = 源时间)
        tm = el.find("timeMap")
        if tm is not None:
            pts = [(parse_time(p.get("time")), parse_time(p.get("value")))
                   for p in tm.findall("timept")]
            if len(pts) >= 2 and pts[-1][0] > 0:
                speed = pts[-1][1] / pts[-1][0]
                start = pts[0][1]
        tf = Transform()
        at = el.find("adjust-transform")
        kfs = {}
        if at is not None:
            pos = (at.get("position") or "").split()
            sc = (at.get("scale") or "").split()
            # FCPXML 的 position 单位是"画面高度的百分比", x 也用高度作基准
            # (Apple: "5 percent ... of the project's height")
            k = height / 100.0
            tf.x = _num(pos[0], 0) * k if pos else 0.0
            tf.y = -_num(pos[1], 0) * k if len(pos) > 1 else 0.0   # FCPXML y 向上
            tf.scale_x = _num(sc[0], 1) if sc else 1.0
            tf.scale_y = _num(sc[1], 1) if len(sc) > 1 else tf.scale_x
            tf.rotation = _num(at.get("rotation"), 0.0)
            for pr in at.findall("param"):
                name = pr.get("name") or ""
                pts = []
                for kf in pr.findall("keyframeAnimation/keyframe"):
                    t = parse_time(kf.get("time"))
                    raw = (kf.get("value") or "").split()
                    if not raw:
                        continue
                    if name in ("position", "scale"):
                        vx = _num(raw[0], 0)
                        vy = _num(raw[1], 0) if len(raw) > 1 else vx
                        if name == "position":
                            pts.append((t, vx * k, -vy * k))
                        else:
                            pts.append((t, vx, vy))
                    else:
                        pts.append((t, _num(raw[0], 0)))
                if len(pts) >= 2:
                    kfs[name] = pts
        # 负缩放 = 镜像
        if tf.scale_x < 0:
            tf.flip_h, tf.scale_x = True, abs(tf.scale_x)
        if tf.scale_y < 0:
            tf.flip_v, tf.scale_y = True, abs(tf.scale_y)
        # 写端给静帧做了"达芬奇底边对齐"补偿(position 加 渲染高/2), 这里减回去,
        # 否则往返一圈贴图会越搬越低。
        pth = (assets.get(ref) or {}).get("path") or ""
        if os.path.splitext(pth)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"):
            iw, ih = image_size(pth)
            if ih:
                fit = min(width / iw, height / ih) if iw and ih else 1.0
                dy = ih * abs(tf.scale_y) * fit / 2.0
                tf.y -= dy
                if kfs.get("position"):
                    kfs["position"] = [(t, x, y - dy) for t, x, y in kfs["position"]]
        vol = _num(el.get("audioVolume"), 1.0)
        return Clip(
            media_id=ensure_media(ref), start=offset, dur=dur,
            src_in=start, src_dur=int(round(dur * speed)),
            speed=speed, transform=tf, volume=vol,
            fade_in=parse_time(el.get("audioFadeIn")),
            fade_out=parse_time(el.get("audioFadeOut")),
            label=(assets.get(ref, {}).get("name") or ""),
            source_app="resolve",
            kfs=kfs if at is not None else {},
        )

    spine = seq.find("spine") if seq is not None else None
    if spine is None:
        raise ValueError("FCPXML 中未找到 spine（序列内容）")

    # 主叙事线 (lane 无或 lane=0) 与上层 lane 分层; 音频是负 lane。
    # 挂靠片段(connected clip)是主故事线元素的**子元素**, offset 相对父元素起点,
    # 所以这里递归解析(以前只读 spine 的直接子元素, 嵌套结构会整块漏掉)。
    layers = {}      # lane -> Track
    audio_layers = {}

    def walk(parent, base, lane_ovr=None):
        for el in parent:
            if el.tag not in ("asset-clip", "clip", "gap", "title", "spine"):
                continue
            off = base + parse_time(el.get("offset"))
            lane = int(_num(el.get("lane"), 0))
            if lane_ovr is not None and lane == 0:
                lane = lane_ovr      # storyline 内部的片段继承 storyline 的轨
            if el.tag in ("asset-clip", "clip"):
                clip = make_clip(el)
                if clip is not None:
                    clip.start = off          # 挂靠片段的 offset 是相对父元素的
                    is_audio = (el.get("srcEnable") == "audio") or lane < 0
                    if is_audio:
                        audio_layers.setdefault(lane, Track(kind="audio")).clips.append(clip)
                    else:
                        layers.setdefault(lane, Track(kind="video")).clips.append(clip)
            walk(el, off, lane)               # gap / clip / spine 都可能带子元素

    walk(spine, 0)

    # lane 0 = 底层
    for lane in sorted(layers):
        tr = layers[lane]
        tr.clips.sort(key=lambda c: c.start)
        tl.tracks.append(tr)

    # 音频(负 lane)。写端把"音频1"(剪映界面最上面那条)写成 lane -1, 达芬奇也把
    # -1 当 A1, 两端一致。所以读回时按 lane 从大到小(-1..-N)排, 才是 音频1..N。
    for lane in sorted(audio_layers, reverse=True):
        tr = audio_layers[lane]
        tr.clips.sort(key=lambda c: c.start)
        tl.tracks.append(tr)

    # 音频
    audio = seq.find("audio") if seq is not None else None
    if audio is not None:
        algroups = {}
        for el in audio:
            if el.tag not in ("asset-clip", "clip"):
                continue
            lane = int(_num(el.get("lane"), 1))
            clip = make_clip(el)
            if not clip:
                continue
            algroups.setdefault(lane, Track(kind="audio")).clips.append(clip)
        for lane in sorted(algroups):
            tr = algroups[lane]
            tr.clips.sort(key=lambda c: c.start)
            tl.tracks.append(tr)

    if not tl.video_tracks():
        tl.warnings.append("FCPXML 中没有视频片段（可能为复合或纯音频序列）")
    n_missing = sum(1 for m in tl.media.values() if m.missing)
    if n_missing:
        tl.warnings.append(f"{n_missing} 个素材路径缺失")
    tl.stats = {"clips": tl.clip_count()}
    return tl
