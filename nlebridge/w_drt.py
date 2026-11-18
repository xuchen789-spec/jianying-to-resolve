# -*- coding: utf-8 -*-
"""达芬奇 .drp 工程写出器 (直接生成原生工程包, 绕过 FCPXML 导入)

格式逆向自 Resolve 21.0.0.47 真实 .drt/.drp 样本 (ref/fade_sample.drt):
- .drp = ZIP { project.xml, MediaPool/Master/MpFolder.xml, SeqContainer/<guid>.xml }
- 二进制 blob 结构见 drt_blobs.py (字典/protobuf/转场/timemap 均已对齐样本字节)

已知限制 (v1):
- 变速片段: MediaTimemapBA 仅破解出恒等映射形态 (素材帧数-1)/fps,
  变速片段同样写恒等 timemap, 导入后速度需人工核对 (待变速样本校准)
- 片段变换/调色等属性不在 v1 范围
- mp3 纯音频素材的 Sm2MpAudioClip 为同构合成, 未经真实样本验证
"""
import os
import re
import json
import struct
import uuid
import zipfile
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime

from . import drt_blobs as db

_TPL_DIR = os.path.join(os.path.dirname(__file__), "templates_drt")

VIDEO_TRACK_BLOB = ("000000010000000100000012004e0075006d004c0061007900650072007300"
                    "0000020000000000")                       # NumLayers=0
AUDIO_TRACK_BLOB = ("000000010000000200000012004e0075006d004c0061007900650072007300"
                    "00000200000000000000003e004500780063006c00750064006500540072006100"
                    "63006b00460072006f006d00530065007100750065006e00630065004300610063"
                    "00680069006e0067000000010001")           # NumLayers=0 + ExcludeTrack=1
VIDEO_EFFECT_FILTERS = ("000000020000003a800a37081238024a004a004a004a004a29088b015224"
                        "ffffffe00000000000000c0000000000000000001800000000000c000000000000"
                        "00f03f")
_TRANS_TEXT = {"video": ("Cross Dissolve", "交叉叠化"),
               "audio": ("Cross Fade -3DB", "交叉淡变 - 3dB")}

_GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# fade_sample 提取的模板中, 样本工程专属 GUID(需全部映射, 含 blob 内 utf16be-hex 形态)
_OLD_MASTER_FOLDER = "1a6f03da-78c1-4990-b404-d9d7428064b9"   # Master 根文件夹
_OLD_SEQ_CONTAINER = "21474be0-0f0b-414e-a9ed-963e9c6ef4ff"   # SeqContainer 文件
# 无害残留白名单: Sm2Sequence blob 缩略图字典的 UniqueId(纯内部标识, 无外部依赖)
_HIDDEN_OK = {"2c2a83d6-8e55-4992-9174-eb530efcee5f"}


def _tpl(name):
    with open(os.path.join(_TPL_DIR, name), encoding="utf-8") as f:
        return f.read()


def _guid():
    return str(uuid.uuid4())


def _x(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _replace_guids(text, mapping):
    for old, new in mapping.items():
        text = text.replace(old, new)
        oh = old.encode("utf-16-be").hex()
        nh = new.encode("utf-16-be").hex()
        if oh in text:
            text = text.replace(oh, nh)
    return text


# ---------------- ffprobe ----------------

def probe(path):
    out = {"duration_s": 0.0, "fps": 0.0, "vcodec": "", "acodec": "",
           "sample_rate": 0, "channels": 0, "has_video": False, "has_audio": False,
           "width": 0, "height": 0}
    try:
        j = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, timeout=30).stdout
        d = json.loads(j.decode("utf-8", "replace"))
    except Exception:
        return out
    out["duration_s"] = float(d.get("format", {}).get("duration") or 0)
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and not out["has_video"]:
            out["has_video"] = True
            out["vcodec"] = s.get("codec_name", "")
            out["width"] = s.get("width", 0)
            out["height"] = s.get("height", 0)
            fr = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
            try:
                num, den = fr.split("/")
                out["fps"] = float(num) / float(den) if float(den) else 0
            except Exception:
                pass
            if s.get("duration"):
                out["duration_s"] = float(s["duration"])
        elif s.get("codec_type") == "audio" and not out["has_audio"]:
            out["has_audio"] = True
            out["acodec"] = s.get("codec_name", "")
            out["sample_rate"] = int(s.get("sample_rate") or 0)
            out["channels"] = int(s.get("channels") or 0)
            if s.get("duration"):
                out["duration_s"] = float(s["duration"])
    return out


# ---------------- 素材池 ----------------

def _mtime_fields(path):
    try:
        mt = os.stat(path).st_mtime
    except OSError:
        mt = 0
    return datetime.fromtimestamp(mt).strftime("%a %b %d %H:%M:%S %Y"), int(mt * 1_000_000)


def _codec_name(info, audio_info):
    c = info["acodec" if audio_info else "vcodec"].lower()
    if audio_info:
        return {"aac": "AAC", "mp3": "mp3"}.get(c, (info["acodec"] or "AAC").upper())
    return {"h264": "avc1", "hevc": "hvc1", "png": "png", "mjpeg": "mjpeg"}.get(
        c, info["vcodec"] or "avc1")


def _tracks_ba(info, guid):
    """视频池条目的 TracksBA: 外层字典 {'0': 音轨字典} (样本实证),
    音轨字段类型码与样本一致 (StartTime=0x06, SampleRate/BitDepth=0x03)"""
    return db.dict_encode([("0", ("block", _track_dict(info, guid)))]).hex()


def _track_dict(info, guid):
    """音轨字典 (TracksBA 内层 / FieldsBlob f5 共用)"""
    sr = info["sample_rate"] or 48000
    ch = info["channels"] or 2
    samples = int(round(info["duration_s"] * sr))
    return db.dict_encode([
        ("UniqueId", ("str", guid)),
        ("StartTime", ("int64x", 0)),
        ("SampleRate", ("int3", sr)),
        ("NumChannels", ("int", ch)),
        ("IdxTrack", ("int", 0)),
        ("Duration", ("int64", samples)),
        ("DbType", ("str", "BtAudioTrack")),
        ("CodecName", ("str", _codec_name(info, True))),
        ("ChannelLayout", ("int", 2 if ch >= 2 else 1)),
        ("BitDepth", ("int3", 3)),
    ])


def _video_fields_blob(info, guid):
    """Sm2MpVideoClip 的 FieldsBlob (样本实证嵌套结构):
       pb{ f1: pb{ f1: b'\\x30\\x04', f5: {'0': 音轨字典} },
           f2: 000000000000000600000000ffffffff }
       媒体路径信息在 <Clip> 元素 blob 里, 不在这里"""
    wrapped = db.dict_encode([("0", ("block", _track_dict(info, guid)))])
    inner = (db.pb_field_bytes(1, b"\x30\x04")
             + db.pb_field_bytes(5, wrapped))
    mid = (db.pb_field_bytes(1, inner)
           + db.pb_field_bytes(2, bytes.fromhex("000000000000000600000000ffffffff")))
    return db.blob_wrap(db.pb_field_bytes(1, mid)).hex()


def build_video_pool_item(tpl, media, info, fps_fallback, folder_guid):
    """样本 Sm2MpVideoClip 模板 -> 新条目 XML; 返回 (xml, pool_dbid)"""
    # 样本实证: blob 内路径为 Windows 反斜杠
    path = media.path.replace("/", "\\")
    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    mt_s, mt_us = _mtime_fields(path)
    fps = info["fps"] or fps_fallback or 24.0
    nframes = max(1, int(round(info["duration_s"] * fps)))

    local_map = {old: _guid() for old in set(_GUID_RE.findall(tpl))}
    xml = _replace_guids(tpl, local_map)
    media_guid = re.search(r'<Sm2MpVideoClip DbId="([^"]*)"', xml).group(1)

    vblob = db.blob_wrap(db.media_clip_blob(
        directory, filename, mt_s, mt_us, _codec_name(info, False), media_guid)).hex()
    ablob = db.blob_wrap(db.media_clip_blob(
        directory, filename, mt_s, mt_us, _codec_name(info, True), media_guid,
        is_audio=True)).hex()
    time_blob = db.dict_encode([
        ("UniqueId", ("str", _guid())),
        ("StartFrame", ("int", 0)),
        ("NumFrames", ("int", nframes)),
        ("FrameRate", ("block", struct.pack(">Q", int(round(fps * 600)) << 8) + b"\x00" * 8)),
        ("DbType", ("str", "BtVideoTime")),
    ]).hex()
    geometry = db.dict_encode([
        ("UniqueId", ("str", _guid())),
        ("Resolution", ("block", struct.pack(">Q", info["width"] or 1920)
                        + struct.pack(">Q", info["height"] or 1080))),
        ("FrameSize", ("int", 0x2F96)),
        ("DbType", ("str", "BtGeometry")),
    ]).hex()

    xml = re.sub(r"<Name>[^<]*</Name>", "<Name>%s</Name>" % _x(filename), xml, count=1)
    # 模板里 MpFolder 引用的是样本的 Master 文件夹, 必须指向本次生成的根文件夹
    xml = re.sub(r"(<MpFolder>)[^<]*(</MpFolder>)",
                 lambda m: m.group(1) + folder_guid + m.group(2), xml, count=1)
    xml = re.sub(r"<CurPlayheadPosition>\d+</CurPlayheadPosition>",
                 "<CurPlayheadPosition>0</CurPlayheadPosition>", xml)
    xml = re.sub(r'(<Sm2MpVideoClip DbId="%s">\s*<FieldsBlob>)[0-9a-f]*(</FieldsBlob>)'
                 % media_guid, lambda m: m.group(1) + _video_fields_blob(info, _guid())
                 + m.group(2), xml, count=1, flags=re.S)
    xml = re.sub(r"(<Clip>)[0-9a-f]*(</Clip>)", lambda m: m.group(1) + vblob + m.group(2),
                 xml, count=1)
    xml = re.sub(r"(<Video>.*?<Time>)[0-9a-f]*(</Time>)",
                 lambda m: m.group(1) + time_blob + m.group(2), xml, count=1, flags=re.S)
    xml = re.sub(r"(<Geometry>)[0-9a-f]*(</Geometry>)",
                 lambda m: m.group(1) + geometry + m.group(2), xml, count=1)
    xml = re.sub(r"(<TracksBA>)[0-9a-f]*(</TracksBA>)",
                 lambda m: m.group(1) + _tracks_ba(info, _guid()) + m.group(2), xml, count=1)
    xml = re.sub(r"(<BtAudioInfo [^>]*>\s*<FieldsBlob/>\s*<Clip>)[0-9a-f]*(</Clip>)",
                 lambda m: m.group(1) + ablob + m.group(2), xml, count=1, flags=re.S)
    return xml, media_guid


def build_audio_pool_item(media, info, folder_guid):
    """mp3 等纯音频素材: Sm2MpAudioClip (同构合成, 待真实样本校验)"""
    # 样本实证: blob 内路径为 Windows 反斜杠
    path = media.path.replace("/", "\\")
    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    mt_s, mt_us = _mtime_fields(path)
    media_guid, item_guid, info_guid = _guid(), _guid(), _guid()
    acodec = _codec_name(info, True)
    blob_hex = db.blob_wrap(db.media_clip_blob(
        directory, filename, mt_s, mt_us, acodec, media_guid, is_audio=True)).hex()
    sr = info["sample_rate"] or 44100
    ch = info["channels"] or 2
    samples = int(round(info["duration_s"] * sr))
    tracks_ba = db.dict_encode([
        ("UniqueId", ("str", _guid())),
        ("StartTime", ("int64", 0)),
        ("SampleRate", ("int", sr)),
        ("NumChannels", ("int", ch)),
        ("IdxTrack", ("int", 0)),
        ("Duration", ("int64", samples)),
        ("DbType", ("str", "BtAudioTrack")),
        ("CodecName", ("str", acodec)),
        ("ChannelLayout", ("int", 2 if ch >= 2 else 1)),
        ("BitDepth", ("int", 3)),
    ]).hex()
    return f"""<Element>
   <Sm2MpAudioClip DbId="{media_guid}">
    <FieldsBlob>{blob_hex}</FieldsBlob>
    <Name>{_x(filename)}</Name>
    <MpFolder>{folder_guid}</MpFolder>
    <UniqueMediaPoolItemId>{item_guid}</UniqueMediaPoolItemId>
    <MarkIn/>
    <MarkInVideo/>
    <MarkInAudio/>
    <MarkOut/>
    <MarkOutVideo/>
    <MarkOutAudio/>
    <CurPlayheadPosition>0</CurPlayheadPosition>
    <PinsBA/>
    <VirtualAudioTracksBA/>
    <MatteVec/>
    <AudioSource>AUDIO_SOURCE_INLINE</AudioSource>
    <PTZRPreset>
     <SmPTZRPreset DbId="{_guid()}">
      <FieldsBlob/>
      <Name/>
      <ID>100</ID>
      <IsCustom>true</IsCustom>
      <Producer>Producer@Resolve</Producer>
      <Presets>7b7d</Presets>
     </SmPTZRPreset>
    </PTZRPreset>
    <PTZRPresetType>0</PTZRPresetType>
    <SlateTC>-68719476737</SlateTC>
    <Audio>
     <BtAudioInfo DbId="{info_guid}">
      <FieldsBlob/>
      <Clip>{blob_hex}</Clip>
      <TracksBA>{tracks_ba}</TracksBA>
      <MediaMetadata/>
     </BtAudioInfo>
    </Audio>
   </Sm2MpAudioClip>
  </Element>"""


# ---------------- 时间线片段 / 转场 ----------------

def build_clip_xml(tpl, *, clip_dbid, fields_blob, name, start, dur, src_in,
                   media_ref, media_path, fps, timemap):
    xml = re.sub(r'(DbId=")[0-9a-f-]{36}(")',
                 lambda m: m.group(1) + clip_dbid + m.group(2), tpl, count=1)
    xml = re.sub(r"(<FieldsBlob>)[0-9a-f]*(</FieldsBlob>)",
                 lambda m: m.group(1) + fields_blob + m.group(2), xml, count=1)
    xml = re.sub(r"<Name>[^<]*</Name>", lambda m: "<Name>%s</Name>" % _x(name), xml, count=1)
    xml = re.sub(r"<Start>\d+</Start>", lambda m: "<Start>%d</Start>" % start, xml)
    xml = re.sub(r"<Duration>\d+</Duration>", lambda m: "<Duration>%d</Duration>" % dur, xml)
    xml = re.sub(r"<In>\d*</In>", lambda m: "<In>%d</In>" % src_in, xml)
    xml = re.sub(r"<MediaRef>[^<]*</MediaRef>",
                 lambda m: "<MediaRef>%s</MediaRef>" % media_ref, xml)
    xml = re.sub(r"<MediaStartTime>[^<]*</MediaStartTime>",
                 lambda m: "<MediaStartTime>0</MediaStartTime>", xml)
    xml = re.sub(r"<MediaFilePath>[^<]*</MediaFilePath>",
                 lambda m: "<MediaFilePath>%s</MediaFilePath>" % _x(media_path), xml)
    xml = re.sub(r"<MediaFrameRate>[^<]*</MediaFrameRate>",
                 lambda m: "<MediaFrameRate>%s</MediaFrameRate>" % db.media_frame_rate_hex(fps),
                 xml)
    xml = re.sub(r"<MediaTimemapBA>[^<]*</MediaTimemapBA>",
                 lambda m: "<MediaTimemapBA>%s</MediaTimemapBA>" % timemap, xml)
    # 片段根 DbId 已替换, 其余对象(缩略图等)的 DbId 随机化, 避免所有片段共用同一 ID
    xml = re.sub(r'(DbId=")[0-9a-f-]{36}(")',
                 lambda m: m.group(1) + _guid() + m.group(2), xml)
    return xml


def build_transition_xml(kind, *, dbid, start, dur, tail):
    pretty, name = _TRANS_TEXT[kind]
    eff = VIDEO_EFFECT_FILTERS if kind == "video" else ""
    align = 3 if tail else 1
    return f"""<Element>
       <Sm2TiTransition DbId="{dbid}">
        <FieldsBlob>{db.transition_blob(start).hex()}</FieldsBlob>
        <PrettyType>{pretty}</PrettyType>
        <Name>{name}</Name>
        <Start>{start}</Start>
        <Duration>{dur}</Duration>
        <LinkedItemSync/>
        <WasDisbanded>false</WasDisbanded>
        <MarkersBA/>
        <UiMemento>0</UiMemento>
        <Flags>0</Flags>
        <PriorityIndex>0</PriorityIndex>
        <EffectFiltersBA>{eff}</EffectFiltersBA>
        <ImportExportMetadataBA/>
        <RenderTextEnabled>true</RenderTextEnabled>
        <RenderTextGanged>true</RenderTextGanged>
        <RenderTextPrefixed>true</RenderTextPrefixed>
        <AlignmentType>{align}</AlignmentType>
        <Position>{align}</Position>
       </Sm2TiTransition>
      </Element>"""


# ---------------- 主写出 ----------------

def write(tl, out_path):
    fps = float(tl.fps) or 24.0
    seq_guid = _guid()        # SeqContainer 文件名 / 根 DbId
    seq_obj_guid = _guid()    # MpFolder 内 Sm2Sequence 对象 (样本中两者是不同 ID)
    folder_guid = _guid()

    # ---- 探测素材 ----
    media_by_path = {}
    for m in tl.media.values():
        media_by_path.setdefault(m.path, m)
    probes = {p: probe(p) for p in media_by_path}
    missing = [p for p, i in probes.items() if not i["has_video"] and not i["has_audio"]
               and i["duration_s"] == 0]

    # ---- 素材池 ----
    pool_items = []
    pool_guid_of = {}          # path -> 素材池 DbId
    tpl_mp_video = _tpl("mp_video_clip.xml")
    src_frames = {}            # path -> 源总帧数 (恒等 timemap 用)
    for path, m in media_by_path.items():
        info = probes[path]
        if path in missing:
            continue
        if path.lower().endswith((".mp3", ".aac", ".wav", ".flac")) and not info["has_video"]:
            xml = build_audio_pool_item(m, info, folder_guid)
            pool_guid_of[path] = re.search(r'<Sm2MpAudioClip DbId="([^"]*)"', xml).group(1)
            src_frames[path] = max(1, int(round(info["duration_s"] * fps)))
        else:
            xml, g = build_video_pool_item(tpl_mp_video, m, info, fps, folder_guid)
            pool_guid_of[path] = g
            sfps = info["fps"] or fps
            src_frames[path] = max(1, int(round(info["duration_s"] * sfps)))
        pool_items.append(xml)

    def frames(us):
        return int(round(us * fps / 1_000_000))

    # 达芬奇时间线以 01:00:00:00 为原点 (样本实证: 片段 Start 全部 >= 86400@24fps)
    tl_off = round(fps) * 3600

    def identity_timemap(path):
        return db.timemap_hex(max(0, src_frames.get(path, 1) - 1) * (1.0 / fps))

    # ---- 视频轨 ----
    video_xml = []
    speed_clips = 0
    for t in tl.video_tracks():
        items = []
        for c in sorted(t.clips, key=lambda c: c.start):
            m = tl.media.get(c.media_id)
            pref = pool_guid_of.get(m.path)
            if m is None or not pref or m.path in missing:
                continue
            if abs(c.speed - 1) > 1e-6:
                speed_clips += 1
            start, dur, src_in = frames(c.start) + tl_off, frames(c.dur), frames(c.src_in)
            clip_id = _guid()
            xml = build_clip_xml(
                _tpl("tl_video_clip.xml"),
                clip_dbid=clip_id,
                fields_blob=db.clip_fields_blob_hex(clip_id),
                name=os.path.basename(m.path), start=start, dur=dur,
                src_in=src_in, media_ref=pref,
                media_path=m.path.replace("/", "\\"),
                fps=fps, timemap=identity_timemap(m.path))
            items.append(xml)
            fi, fo = frames(c.fade_in), frames(c.fade_out)
            if fi > 0:
                items.append(build_transition_xml("video", dbid=_guid(),
                                                  start=start, dur=fi, tail=False))
            if fo > 0:
                items.append(build_transition_xml("video", dbid=_guid(),
                                                  start=start + dur - fo, dur=fo, tail=True))
        video_xml.append(_track_xml(VIDEO_TRACK_BLOB, "0", "73", seq_obj_guid, items))

    # ---- 音频轨 ----
    audio_xml = []
    for t in tl.audio_tracks():
        items = []
        for c in sorted(t.clips, key=lambda c: c.start):
            m = tl.media.get(c.media_id)
            pref = pool_guid_of.get(m.path)
            if m is None or not pref or m.path in missing:
                continue
            if abs(c.speed - 1) > 1e-6:
                speed_clips += 1
            start, dur, src_in = frames(c.start) + tl_off, frames(c.dur), frames(c.src_in)
            clip_id = _guid()
            xml = build_clip_xml(
                _tpl("tl_audio_clip.xml"),
                clip_dbid=clip_id,
                fields_blob=db.audio_clip_fields_blob_hex(clip_id),
                name=os.path.basename(m.path), start=start, dur=dur,
                src_in=src_in, media_ref=pref,
                media_path=m.path.replace("/", "\\"),
                fps=fps, timemap=identity_timemap(m.path))
            items.append(xml)
            fi, fo = frames(c.fade_in), frames(c.fade_out)
            if fi > 0:
                items.append(build_transition_xml("audio", dbid=_guid(),
                                                  start=start, dur=fi, tail=False))
            if fo > 0:
                items.append(build_transition_xml("audio", dbid=_guid(),
                                                  start=start + dur - fo, dur=fo, tail=True))
        audio_xml.append(_track_xml(AUDIO_TRACK_BLOB, "1", "0", seq_obj_guid, items))

    # 用户实测(2026-11-12): AudioTrackVec 首位 = A1, 剪映音轨顺序直接写即可
    # (此前依据 fade_sample 反转的结论是错的)

    seq_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!--DbAppVer="21.0.0.0047" DbPrjVer="17"-->
<Sm2SequenceContainer DbId="{seq_guid}">
 <FieldsBlob/>
  <VideoTrackVec>
{chr(10).join(video_xml)}
  </VideoTrackVec>
  <AudioTrackVec>
{chr(10).join(audio_xml)}
  </AudioTrackVec>
  <SubtitleTrackVec/>
  <GeometryTrackVec/>
  <DbSavedTime>0</DbSavedTime>
</Sm2SequenceContainer>
"""

    # ---- MpFolder ----
    tl_tpl = _tpl("mp_timeline_clip.xml")
    tl_map = {old: _guid() for old in set(_GUID_RE.findall(tl_tpl))}
    # 模板内隐藏引用(fade_sample 提取):
    #   - 根文件夹 1a6f03da..(明文 MpFolder + project.xml blob 内 utf16be-hex RootFolderRef)
    #   - SeqContainer 容器 21474be0..(仅存在于 Sm2Sequence blob 的 SeqRef, utf16be-hex 形态)
    # 这两个是时间线/素材池能否加载的关键, 必须映射到本次生成的真实 GUID
    tl_map[_OLD_MASTER_FOLDER] = folder_guid
    tl_map[_OLD_SEQ_CONTAINER] = seq_guid
    tl_xml = _replace_guids(tl_tpl, tl_map)
    tl_xml = re.sub(r'(<Sm2Sequence DbId=")[^"]*(")',
                    lambda m: m.group(1) + seq_obj_guid + m.group(2), tl_xml, count=1)
    tl_xml = re.sub(r"<Name>[^<]*</Name>", "<Name>%s</Name>" % _x(tl.name), tl_xml, count=1)
    # Sm2Timeline 对象的 Name 决定时间线在 UI 里显示的名字
    tl_xml = re.sub(r"(<Sm2Timeline[^>]*>.*?<Name>)[^<]*(</Name>)",
                    lambda m: m.group(1) + _x(tl.name) + m.group(2), tl_xml,
                    count=1, flags=re.S)
    tl_xml = re.sub(r"<CurPlayheadPosition>\d+</CurPlayheadPosition>",
                    "<CurPlayheadPosition>0</CurPlayheadPosition>", tl_xml)

    mp_folder = f"""<?xml version="1.0" encoding="UTF-8"?>
<!--DbAppVer="21.0.0.0047" DbPrjVer="17"-->
<Sm2MpFolder DbId="{folder_guid}">
 <FieldsBlob/>
 <Name>Master</Name>
 <UniqueMediaPoolItemId>{_guid()}</UniqueMediaPoolItemId>
 <MediaVec>
{chr(10).join(pool_items)}
{tl_xml}
 </MediaVec>
 <MediaPool/>
 <Folded>false</Folded>
 <ColorTag>FOLDER_COLOR_NONE</ColorTag>
 <LockSysId/>
 <DbSavedTime>5988165</DbSavedTime>
</Sm2MpFolder>
"""

    # ---- project.xml ----
    project_tpl = _tpl("project.xml")
    proj_map = {old: _guid() for old in set(_GUID_RE.findall(project_tpl))}
    # blob 内隐藏的 RootFolderRef(H1a6f03da.. utf16be-hex) 必须指向本次的 Master 文件夹
    proj_map[_OLD_MASTER_FOLDER] = folder_guid
    tl_handle = re.search(r'<Sm2Timeline DbId="([^"]*)"', tl_xml).group(1)
    old_handle = re.search(r"<TimelineHandleVec>\s*<Element>([^<]*)</Element>",
                           project_tpl).group(1)
    proj_map[old_handle] = tl_handle
    project = _replace_guids(project_tpl, proj_map)
    project = re.sub(r"<ProjectName>[^<]*</ProjectName>",
                     "<ProjectName>%s</ProjectName>" % _x(tl.name), project)

    # ---- 残留引用自检: 模板里的样本 GUID 不得以任何形态残留在产物中 ----
    residual = []
    for tpl_name in ("mp_timeline_clip.xml", "mp_video_clip.xml", "project.xml",
                     "tl_video_clip.xml", "tl_audio_clip.xml"):
        for g in set(_GUID_RE.findall(_tpl(tpl_name))) - _HIDDEN_OK:
            for label, text in (("mp", mp_folder), ("proj", project), ("seq", seq_xml)):
                if g in text or g.encode("utf-16-be").hex() in text.lower():
                    residual.append(f"{label}:{g[:8]}")
    if residual:
        print("  ! 警告: 产物中残留样本 GUID(可能致命):",
              sorted(set(residual)))

    # ---- 引用链自检 ----
    chain_problems = _verify_chain(project, mp_folder, seq_xml, seq_guid)
    for q in chain_problems:
        print("  ! 引用链问题:", q)

    # ---- XML 良构校验 (ListMgt:: 双冒号是达芬奇自有格式, 掩码后校验) ----
    for label, text in (("project.xml", project), ("MpFolder.xml", mp_folder),
                        ("SeqContainer.xml", seq_xml)):
        masked = re.sub(r'<(/?)ListMgt::', r"<\1ListMgt__", text)
        try:
            ET.fromstring(masked)
        except Exception as e:
            print("  ! XML 良构错误 [%s]: %s" % (label, e))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.xml", project)
        zf.writestr("MediaPool/Master/MpFolder.xml", mp_folder)
        zf.writestr("SeqContainer/%s.xml" % seq_guid, seq_xml)

    return {"path": out_path, "pool": len(pool_items), "missing": missing,
            "speed_clips": speed_clips}


def _verify_chain(project, mp_folder, seq_xml, seq_guid):
    """达芬奇加载 .drp 的对象引用链全检, 返回问题列表(空=通过)"""
    probs = []

    def need(cond, msg):
        if not cond:
            probs.append(msg)

    h = re.search(r"<TimelineHandleVec>\s*<Element>([0-9a-f-]{36})", project)
    st = re.search(r'<Sm2Timeline DbId="([0-9a-f-]{36})"', mp_folder)
    need(h and st and h.group(1) == st.group(1),
         "TimelineHandleVec 与 Sm2Timeline DbId 不一致")
    mtc = re.search(r'<Sm2MpTimelineClip DbId="([0-9a-f-]{36})"', mp_folder)
    mtl = re.search(r"<MpTimelineClip>([0-9a-f-]{36})</MpTimelineClip>", mp_folder)
    need(mtc and mtl and mtl.group(1) == mtc.group(1),
         "Sm2Timeline.MpTimelineClip 反向引用不一致")
    ss = re.search(r'<Sm2Sequence DbId="([0-9a-f-]{36})"', mp_folder)
    trk = set(re.findall(r"<Sequence>([0-9a-f-]{36})</Sequence>", seq_xml))
    need(ss and trk == {ss.group(1)}, "轨内 <Sequence> 与 Sm2Sequence DbId 不一致")
    root = re.search(r'<Sm2SequenceContainer DbId="([0-9a-f-]{36})"', seq_xml)
    need(root and root.group(1) == seq_guid, "SeqContainer 根 DbId 与文件名 guid 不一致")
    need(seq_guid.encode("utf-16-be").hex() in mp_folder.lower(),
         "Sm2Sequence blob 的 SeqRef 未指向 SeqContainer 文件 guid")
    fg = re.search(r'<Sm2MpFolder DbId="([0-9a-f-]{36})"', mp_folder)
    need(fg and fg.group(1).encode("utf-16-be").hex() in project.lower(),
         "project.xml RootFolderRef 未指向 Master 文件夹 guid")
    folds = set(re.findall(r"<MpFolder>([0-9a-f-]{36})</MpFolder>", mp_folder))
    need(folds == {fg.group(1)} if fg else False,
         "存在 MpFolder 不指向 Master 的池条目: %s" % (folds - {fg.group(1) if fg else ""}))
    prefs = set(re.findall(r'<Sm2Mp(?:Video|Audio)Clip DbId="([0-9a-f-]{36})"', mp_folder))
    refs = set(re.findall(r"<MediaRef>([0-9a-f-]{36})</MediaRef>", seq_xml))
    dangling = refs - prefs
    need(not dangling, "MediaRef 悬空: %s" % sorted(g[:8] for g in dangling))
    return probs


def _track_xml(blob, ttype, subtype, seq_guid, items):
    return f"""<Element>
   <Sm2TiTrack DbId="{_guid()}">
    <FieldsBlob>{blob}</FieldsBlob>
    <Type>{ttype}</Type>
    <SubType>{subtype}</SubType>
    <Flags>0</Flags>
    <Sequence>{seq_guid}</Sequence>
    <Items>
{chr(10).join(items)}
    </Items>
    <FusionCompHolderItems/>
    <UserDefinedName/>
    <LayersVec/>
   </Sm2TiTrack>
  </Element>"""
