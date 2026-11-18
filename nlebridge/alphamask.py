"""剪映 AI 智能抠像(抠像/custom mask) alpha 序列解码与导出。

剪映把 AI 抠像结果存在草稿的 adjust_mask/<uuid>.zip（解开后是目录同名):
  - 每帧 147456 字节 = 512x288 8bit 原始灰度 alpha（无头、无压缩、无加密）
  - 文件名 = 源片段时间戳(微秒), 步进 41667us(24fps)
  - data.json: 每帧主体包围盒
  - desc.json: {"startTime": 起始us, "endTime": 结束us, ...}
  - interactiveFrame / config_*.ini: 运行时缓存, 忽略

导出策略:
  - 保持原生 512x288(缩放在 Fusion 里做, 质量可控且可改)
  - 按时间线片段的 source_timerange 对齐, 缺帧输出全透明
  - 输出到 out/masks/<片段键>/alpha/frame_0001.png ...
"""

from __future__ import annotations

import json
import os
import struct
import zlib

FRAME_W = 512
FRAME_H = 288
FRAME_BYTES = FRAME_W * FRAME_H


# ---------------------------------------------------------------- PNG (灰度)

def write_gray_png(path: str, gray: bytes, w: int = FRAME_W, h: int = FRAME_H) -> None:
    """写 8bit 灰度 PNG（纯标准库, 无依赖）。"""
    raw = b"".join(b"\x00" + gray[y * w:(y + 1) * w] for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_rgba_png(path: str, gray: bytes, w: int = FRAME_W, h: int = FRAME_H) -> None:
    """写 8bit RGBA PNG: RGB=灰度(便于肉眼检查), A=灰度(供 Fusion Loader 直接作蒙版)。"""
    def row(y):
        line = bytearray(b"\x00")
        base = y * w
        for x in range(w):
            g = gray[base + x]
            line += bytes((g, g, g, g))
        return bytes(line)

    raw = b"".join(row(y) for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # color type 6 = RGBA
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- 读取 alpha

def resolve_mask_dir(draft_dir: str, mask: dict) -> str | None:
    """从 common_mask 条目解析出 adjust_mask 下的目录名, 返回绝对路径或 None。"""
    ws = mask.get("loader_work_space") or ""
    if "adjust_mask/" not in ws:
        return None
    tail = ws.split("adjust_mask/")[-1]
    uid = tail.replace("\\", "/").split("/")[0]
    if uid.lower().endswith(".zip"):
        uid = uid[:-4]
    p = os.path.join(draft_dir, "adjust_mask", uid)
    return p if os.path.isdir(p) else None


def load_frames(mask_dir: str) -> dict[int, bytes]:
    """返回 {源时间戳us: 灰度bytes}。"""
    out: dict[int, bytes] = {}
    for name in os.listdir(mask_dir):
        if not name.isdigit():
            continue
        p = os.path.join(mask_dir, name)
        if os.path.getsize(p) != FRAME_BYTES:
            continue
        with open(p, "rb") as f:
            out[int(name)] = f.read()
    return out


def load_desc(mask_dir: str) -> dict:
    p = os.path.join(mask_dir, "desc.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def union_frames(frame_sets: list[dict[int, bytes]]) -> dict[int, bytes]:
    """多个 mask 合并（逐像素取最大 → 并集), 用于同一片段多次抠像叠加。"""
    if len(frame_sets) == 1:
        return frame_sets[0]
    keys = sorted(set().union(*[set(s) for s in frame_sets]))
    merged: dict[int, bytes] = {}
    for k in keys:
        layers = [s[k] for s in frame_sets if k in s]
        if len(layers) == 1:
            merged[k] = layers[0]
            continue
        a = bytearray(layers[0])
        for other in layers[1:]:
            for i, v in enumerate(other):
                if v > a[i]:
                    a[i] = v
        merged[k] = bytes(a)
    return merged


# ---------------------------------------------------------------- 导出序列

BLANK_FRAME = bytes(FRAME_BYTES)


def export_matte_sequence(frames: dict[int, bytes], out_dir: str,
                          src_start_us: float, dur_us: float, fps: float,
                          max_frames: int = 0, rgba: bool = True) -> dict:
    """按片段源区间导出 PNG 序列, 返回导出信息。

    帧对齐: 片段第 i 帧(相对片段起点) 对应源时间 src_start_us + i*1e6/fps,
    取最近的 alpha 帧; 超范围 / 缺帧 → 全透明。
    rgba=True 时输出 RGBA(RGB=luma, A=luma), 供 Fusion Loader 直接作蒙版。
    """
    alpha_dir = os.path.join(out_dir, "alpha")
    os.makedirs(alpha_dir, exist_ok=True)
    # 清理旧帧, 保证帧数与本次一致
    for name in os.listdir(alpha_dir):
        if name.startswith("frame_") and name.endswith(".png"):
            os.remove(os.path.join(alpha_dir, name))

    step = 1e6 / fps
    n = int(round(dur_us / step))
    if max_frames:
        n = min(n, max_frames)
    keys = sorted(frames)
    blank = 0
    writer = write_rgba_png if rgba else write_gray_png
    for i in range(n):
        t = src_start_us + i * step
        # 最近帧(容差 1 帧)
        if not keys:
            gray = BLANK_FRAME
        else:
            near = min(keys, key=lambda k: abs(k - t))
            gray = frames[near] if abs(near - t) <= step * 0.75 else BLANK_FRAME
        if gray is BLANK_FRAME:
            blank += 1
        writer(os.path.join(alpha_dir, "frame_%04d.png" % (i + 1)), gray)

    return {
        "dir": alpha_dir,
        "count": n,
        "blank": blank,
        "width": FRAME_W,
        "height": FRAME_H,
        "fps": fps,
        "first_frame_path": os.path.join(alpha_dir, "frame_0001.png"),
    }


def coverage(gray: bytes) -> float:
    """alpha 覆盖率（调试用）。"""
    return sum(1 for b in gray if b > 10) / len(gray) * 100.0
