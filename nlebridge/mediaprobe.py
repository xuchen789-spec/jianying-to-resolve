# -*- coding: utf-8 -*-
"""素材容器嗅探 + 扩展名容错 + 静帧改名

两个坑, 都是"文件名/容器骗了目标端":

1) 后缀说谎
   剪映曲库缓存会把 AAC(M4A) 存成 .mp3 后缀, 达芬奇/NLE 按后缀选解码器,
   于是报 "File does not exist, or file format is not recognizable"。
   按文件头识别真实容器, 生成后缀正确的副本(硬链接优先)。

2) 静帧名里带数字 → 被当成图片序列
   剪映的贴图/PNG 素材名多为 `<哈希>.png`, 带数字。达芬奇导入 FCPXML 时会把
   `....dfc16d.png` 解析成序列 `....dfc[16-259215]d.png`, 然后去找不存在的帧,
   时间线上就成了离线/丢图。实测 3 张 PNG 有 2 张中招。
   处理办法: 静帧一律复制成 **纯字母** 文件名 (still_a.png / still_b.png ...),
   文件里一个数字都不出现。

两次都只读原文件, 副本写在输出目录的 _media_relink/ 下。
"""
import os
import shutil
import string
import struct
import subprocess

# 容器 -> 正确后缀 (仅在"后缀明显说谎"时纠正)
_FTYP = b"ftyp"


def has_audio_stream(path: str):
    """探测文件里有没有音频流。返回 True / False / None(没有 ffprobe, 无法判断)。

    剪映草稿的 videos 素材里**没有** has_audio 字段(实测 90 个 mp4 全都没有),
    所以"这段视频带不带声音"只能自己探。不探的后果是 FCPXML 里 asset 写成
    hasAudio="0", 原声一律丢掉。
    """
    exe = shutil.which("ffprobe")
    if not exe or not os.path.exists(path):
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20)
        return bool(out.stdout.strip())
    except Exception:
        return None


def sniff(path: str):
    """返回 (容器描述, 建议后缀); 无法识别返回 (None, None)"""
    try:
        with open(path, "rb") as f:
            h = f.read(64)
    except OSError:
        return None, None
    if len(h) < 12:
        return None, None

    if h[4:8] == _FTYP:
        brand = h[8:12]
        if brand in (b"M4A ", b"M4B ", b"mp42", b"mp41", b"iso2", b"isom", b"dash"):
            # ftyp 容器至少是 MP4 家族; 音频类靠后缀纠正为 m4a
            return "ftyp:" + brand.decode("ascii", "replace").strip(), ".m4a"
        return "ftyp:" + brand.decode("ascii", "replace").strip(), ".mp4"
    if h[:3] == b"ID3" or (h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):
        return "mpeg-audio", ".mp3"
    if h[:4] == b"fLaC":
        return "flac", ".flac"
    if h[:4] == b"RIFF":
        return "riff", ".wav"
    if h[:4] == b"\x1aE\xdf\xa3":
        return "matroska", ".mkv"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", ".png"
    if h[:3] == b"\xff\xd8\xff":
        return "jpeg", ".jpg"
    return None, None


# 只在"后缀不可能是这种容器"时才改, 避免误伤正常的 mp4/mov
def _wrong_ext(ext: str, container: str, sug: str):
    if not sug or not ext:
        return None
    ext = ext.lower()
    if ext == sug:
        return None
    if container.startswith("ftyp"):
        if ext in (".mp4", ".mov", ".m4v", ".m4a", ".qt"):
            return None                     # MP4 家族后缀互换无所谓
        return sug                           # .mp3/.wav 里装的其实是 MP4 容器
    if container == "mpeg-audio":
        return None if ext in (".mp3", ".mp2", ".mpa") else sug
    if container in ("png", "jpeg", "flac", "riff", "matroska"):
        return None if ext == sug else sug
    return None


def apply(tl, work_dir: str, verbose: bool = True):
    """就地修正 tl.media 里路径的扩展名。返回 [(原路径, 新路径)]"""
    fixed, done = [], {}
    cache = os.path.join(work_dir, "_media_relink")

    for m in list(tl.media.values()):
        p = m.path
        if not p or m.missing or not os.path.isfile(p):
            continue
        if p in done:
            m.path = done[p]
            continue

        ext = os.path.splitext(p)[1]
        container, sug = sniff(p)
        want = _wrong_ext(ext, container or "", sug)
        if not want:
            done[p] = p
            continue

        os.makedirs(cache, exist_ok=True)
        stem = os.path.splitext(os.path.basename(p))[0]
        dst = os.path.join(cache, stem + want)
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(p):
            try:
                os.link(p, dst)             # 同盘: 硬链接, 零拷贝
            except OSError:
                shutil.copyfile(p, dst)
        done[p] = dst
        m.path = dst
        fixed.append((p, dst))
        if verbose:
            print(f"  ! 容器与后缀不符({container} 存成 {ext}), 已生成副本: "
                  f"{os.path.basename(dst)}")

    # 同一路径的其他素材条目一起指向副本
    for m in tl.media.values():
        if m.path in done:
            m.path = done[m.path]
    if fixed:
        names = ", ".join(sorted({os.path.basename(b) for _a, b in fixed}))
        tl.warnings.append(
            f"{len(fixed)} 个素材的扩展名与实际容器不符(剪映曲库缓存多为 M4A 存成 .mp3), "
            f"已在 _media_relink/ 生成后缀正确的副本: {names}")
    return fixed


def image_size(path: str):
    """读图片真实像素尺寸(不依赖 PIL): PNG 的 IHDR / JPEG 的 SOFn / GIF。
    读不出来返回 (0, 0)。"""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:2] == b"\xff\xd8":            # JPEG: 找 SOFn
                f.seek(2)
                while True:
                    b = f.read(1)
                    if not b:
                        break
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        break
                    if 0xC0 <= marker[0] <= 0xCF and marker[0] not in (0xC4, 0xC8, 0xCC):
                        f.read(3)                   # 长度2 + 精度1
                        h, w = struct.unpack(">HH", f.read(4))
                        return int(w), int(h)
                    ln = struct.unpack(">H", f.read(2))[0]
                    f.seek(ln - 2, 1)
    except OSError:
        pass
    return 0, 0


def _letters(i: int) -> str:
    """0 -> a, 25 -> z, 26 -> aa ... 只用字母, 保证文件名里没有数字"""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = string.ascii_lowercase[r] + out
    return out


def apply_stills(tl, work_dir: str, verbose: bool = True):
    """把静态图片素材改指向"纯字母文件名"的副本, 避免达芬奇当图片序列读。

    返回 [(原路径, 新路径)]
    """
    cache = os.path.join(work_dir, "_media_relink")
    imgs = [m for m in tl.media.values()
            if m.kind == "image" and m.path and not m.missing and os.path.isfile(m.path)]
    imgs.sort(key=lambda m: m.path.lower())
    done, fixed, n = {}, [], 0
    for m in imgs:
        src = m.path
        if src in done:
            m.path = done[src]
            continue
        _c, sug = sniff(src)
        ext = sug or os.path.splitext(src)[1].lower() or ".png"
        if ext == ".jpeg":
            ext = ".jpg"
        dst = os.path.join(cache, f"still_{_letters(n)}{ext}")
        while os.path.exists(dst) and not _same_file(dst, src):
            n += 1
            dst = os.path.join(cache, f"still_{_letters(n)}{ext}")
        n += 1
        if not os.path.exists(dst):
            os.makedirs(cache, exist_ok=True)
            try:
                os.link(src, dst)          # 同盘: 硬链接, 零拷贝
            except OSError:
                shutil.copyfile(src, dst)
        done[src] = dst
        m.path = dst
        fixed.append((src, dst))
        if verbose:
            print(f"  ! 静帧改名避开序列解析: {os.path.basename(src)} -> {os.path.basename(dst)}")

    for m in tl.media.values():
        if m.path in done:
            m.path = done[m.path]
    if fixed:
        pairs = "; ".join(f"{os.path.basename(a)} -> {os.path.basename(b)}" for a, b in fixed)
        tl.warnings.append(
            f"{len(fixed)} 张静帧已复制成纯字母文件名(达芬奇会把带数字的 PNG 当成图片序列读, "
            f"导致丢图): {pairs}")
    return fixed


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False
