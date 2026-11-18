# -*- coding: utf-8 -*-
"""达芬奇 .drp/.drt 二进制 blob 编解码 (2026-11 对 Resolve 21.0 样本逆向)

三类 blob:
1. FieldsBlob 容器: [BE u32 ver=2][BE u32 raw_len][0x81][zstd 帧]; ver=1 时为明文。
2. protobuf 载荷 (媒体池 Clip/时间线片段 FieldsBlob)。
3. 旧式字典 (Time/Geometry/TracksBA/轨 FieldsBlob):
   [BE u32 ver=1][BE u32 count] + N 个条目
   条目 = [BE u32 key_utf16字节数][utf16be key][value]
   value = [BE u32 type][0x00][payload]
     type 0x0a 字符串: payload = [BE u32 字节数][utf16be 文本]
     type 0x02/0x03 整数: payload = [BE u32 值]              (总 9 字节)
     type 0x04/0x06 长整数: payload = [BE u64 值]            (总 13 字节)
     type 0x0c 数值块: payload = [BE u32 数据字节数][数据]    (FrameRate/Resolution)

其他实测格式:
- MediaFrameRate 元素: pack('>Q', fps*600<<8) + 8 字节 0
- MediaTimemapBA: b'\\x02' + pack('>d', 源消耗秒数)   (恒速/恒等映射)
- Sm2TiTransition FieldsBlob:
  b'\\x80' + pb(1: [varint 168, varint start帧]) + pb(2: [BE u32 44] + zlib块)
"""
import struct
import io
import zlib

try:
    import zstandard as _zstd
    _ZSTD_OK = True
except ImportError:
    _ZSTD_OK = False

# ---------------- protobuf ----------------

def pb_varint(v: int) -> bytes:
    out = b""
    while True:
        b7 = v & 0x7F
        v >>= 7
        if v:
            out += bytes([b7 | 0x80])
        else:
            return out + bytes([b7])


def pb_field_varint(fid: int, v: int) -> bytes:
    return pb_varint(fid << 3) + pb_varint(v)


def pb_field_bytes(fid: int, data: bytes) -> bytes:
    return pb_varint(fid << 3 | 2) + pb_varint(len(data)) + data


def pb_read_varint(b: bytes, i: int):
    v = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << s
        if not (x & 0x80):
            return v, i
        s += 7


def pb_parse(data: bytes):
    """protobuf -> [(fid, wiretype, value)]; value: int 或 bytes"""
    out = []
    i = 0
    n = len(data)
    while i < n:
        key, i = pb_read_varint(data, i)
        fid, wt = key >> 3, key & 7
        if wt == 0:
            v, i = pb_read_varint(data, i)
            out.append((fid, 0, v))
        elif wt == 2:
            ln, i = pb_read_varint(data, i)
            out.append((fid, 2, data[i:i + ln]))
            i += ln
        elif wt == 5:
            out.append((fid, 5, data[i:i + 4]))
            i += 4
        elif wt == 1:
            out.append((fid, 1, data[i:i + 8]))
            i += 8
        else:
            raise ValueError("wiretype %d" % wt)
    return out


# ---------------- 通用容器 ----------------

def blob_wrap(payload: bytes) -> bytes:
    """[BE u32 2][BE u32 raw_len][0x81][zstd]"""
    if not _ZSTD_OK:
        raise RuntimeError("需要 zstandard 库")
    comp = _zstd.ZstdCompressor(level=3).compress(payload)
    return struct.pack(">II", 2, len(payload)) + b"\x81" + comp


def blob_unwrap(raw: bytes):
    ver, rawlen = struct.unpack(">II", raw[:8])
    body = raw[8:]
    if body[:1] == b"\x81":
        return ver, _zstd.ZstdDecompressor().stream_reader(io.BytesIO(body[1:])).read()
    return ver, body


def blob_hex(raw: bytes) -> str:
    return raw.hex()


# ---------------- 旧式字典 ----------------

def _be_u32(v):
    return struct.pack(">I", v)


def _be_u64(v):
    return struct.pack(">Q", v & 0xFFFFFFFFFFFFFFFF)


def dict_encode(pairs) -> bytes:
    """[(key, value)] -> dict 字节; value:
       ('str', 文本) | ('int', v) | ('int64', v) | ('block', bytes)
       变体: ('int3', v)=0x03 | ('int64x', v)=0x06 (样本视频 TracksBA 实证)"""
    out = _be_u32(1) + _be_u32(len(pairs))
    for key, (kind, v) in pairs:
        kb = key.encode("utf-16-be")
        out += _be_u32(len(kb)) + kb
        if kind == "str":
            sb = v.encode("utf-16-be")
            out += _be_u32(0x0A) + b"\x00" + _be_u32(len(sb)) + sb
        elif kind == "int":
            out += _be_u32(0x02) + b"\x00" + _be_u32(v)
        elif kind == "int3":
            out += _be_u32(0x03) + b"\x00" + _be_u32(v)
        elif kind == "int64":
            out += _be_u32(0x04) + b"\x00" + _be_u64(v)
        elif kind == "int64x":
            out += _be_u32(0x06) + b"\x00" + _be_u64(v)
        elif kind == "block":
            out += _be_u32(0x0C) + b"\x00" + _be_u32(len(v)) + v
        else:
            raise ValueError(kind)
    return out


def dict_parse(raw: bytes):
    """dict 字节 -> [(key, (kind, value))]"""
    i = 0
    ver, count = struct.unpack(">II", raw[0:8])
    i = 8
    out = []
    for _ in range(count):
        klen = struct.unpack(">I", raw[i:i + 4])[0]
        i += 4
        key = raw[i:i + klen].decode("utf-16-be")
        i += klen
        vtype = struct.unpack(">I", raw[i:i + 4])[0]
        i += 5  # type + 0x00
        if vtype == 0x0A:
            ln = struct.unpack(">I", raw[i:i + 4])[0]
            i += 4
            out.append((key, ("str", raw[i:i + ln].decode("utf-16-be"))))
            i += ln
        elif vtype in (0x02, 0x03):
            out.append((key, ("int", struct.unpack(">I", raw[i:i + 4])[0])))
            i += 4
        elif vtype in (0x04, 0x06):
            out.append((key, ("int64", struct.unpack(">Q", raw[i:i + 8])[0])))
            i += 8
        elif vtype == 0x0C:
            ln = struct.unpack(">I", raw[i:i + 4])[0]
            i += 4
            out.append((key, ("block", raw[i:i + ln])))
            i += ln
        else:
            raise ValueError("dict type 0x%x key %s" % (vtype, key))
    return out


# ---------------- 媒体池 <Clip> protobuf ----------------

def media_clip_blob(directory: str, filename: str, mtime_s: str, mtime_us: int,
                    codec: str, guid: str, is_audio: bool = False) -> bytes:
    """媒体池 Clip blob (BtVideoInfo.Clip / BtAudioInfo.Clip 及 Sm2MpClip.FieldsBlob)"""
    name = filename
    fields = b""
    fields += pb_field_bytes(1, directory.encode("utf-8"))
    fields += pb_field_bytes(2, name.encode("utf-8"))
    fields += pb_field_bytes(3, mtime_s.encode("utf-8"))
    fields += pb_field_bytes(5, codec.encode("utf-8"))
    fields += pb_field_bytes(6, name.encode("utf-8"))
    fields += pb_field_bytes(7, guid.encode("utf-8"))
    fields += pb_field_varint(13, mtime_us)
    if not is_audio:
        fields += pb_field_varint(14, 1028)
    fields += pb_field_varint(15, 2)
    fields += pb_field_varint(16, 100)
    fields += pb_field_varint(18, 16384)
    return fields


# ---------------- 转场 ----------------

# 样本中 4 个转场的 zlib 块完全一致 (44 字节), 直接固化
_TRANS_ZLIB = bytes.fromhex("789c636660642016000000da0005")


def transition_blob(start_frame: int) -> bytes:
    """Sm2TiTransition.FieldsBlob: 头部/尾部转场通用, start 为时间线帧号
    (含 [BE u32 2][BE u32 28] 头, 明文不压缩)"""
    inner = pb_varint(168) + pb_varint(start_frame)   # 裸 varint, 非嵌套字段
    f2 = _be_u32(44) + _TRANS_ZLIB
    payload = pb_field_bytes(1, inner) + pb_field_bytes(2, f2)
    return struct.pack(">II", 2, len(payload)) + payload


# ---------------- 其他 ----------------

def media_frame_rate_hex(fps: float) -> str:
    """<MediaFrameRate> 元素内容: BE u64 (fps*600) + 8 字节 0"""
    return (struct.pack(">Q", int(round(fps * 600))) + b"\x00" * 8).hex()


def timemap_hex(consumed_seconds: float) -> str:
    """<MediaTimemapBA>: b'\\x02' + BE double
    注意: 用 frames * (1.0/fps) 计算才能与达芬奇写入的位模式一致"""
    return (b"\x02" + struct.pack(">d", consumed_seconds)).hex()


def _clip_x_bytes(dbid: str) -> bytes:
    """片段 FieldsBlob 的 f1 内容 (106B 结构):
       X = { f1: Y, f4: 1 };  Y = { f1: dict{'0': dbid} } + 裸 varint 168 + 86528"""
    z = dict_encode([("0", ("str", dbid))])
    y = pb_field_bytes(1, z) + bytes([0xA8, 0x01, 0x80, 0xA3, 0x05])
    x = pb_field_bytes(1, y) + pb_field_varint(4, 1)
    return x


def clip_fields_blob_hex(clip_dbid: str) -> str:
    """视频片段 FieldsBlob: f1=X, f2 固定 16B, f8=1, f19=1"""
    x = _clip_x_bytes(clip_dbid)
    f2 = bytes.fromhex("000000000000000600000000ffffffff")
    payload = (pb_field_bytes(1, x) + pb_field_bytes(2, f2)
               + pb_field_varint(8, 1) + pb_field_varint(19, 1))
    return blob_wrap(payload).hex()


def audio_clip_fields_blob_hex(clip_dbid: str) -> str:
    """音频片段 FieldsBlob: f1=X, f15=4"""
    x = _clip_x_bytes(clip_dbid)
    payload = pb_field_bytes(1, x) + pb_field_varint(15, 4)
    return blob_wrap(payload).hex()
