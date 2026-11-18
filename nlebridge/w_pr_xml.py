# -*- coding: utf-8 -*-
"""IR → Premiere Pro XML (Final Cut Pro 7 / xmeml)

结构照抄 Premiere 自己 File→Export→Final Cut Pro XML 的产物, 这是唯一被 PR
完整支持的写法:
  - bin 里放 masterclip, 里面嵌 <file id="fileN">
  - 序列 clipitem 用 <file ref> 引用, 并带 <sourcetrack>
  - 变换写成 <filter><effect> Basic Motion(effectid=basic), center 用
    <horiz>/<vert> 且是**归一化**值(PR 导出的就是 0.xxxx 这种), 不是像素
  - 变速用 <filter> Time Remap(effectid=timeremap)
  - 图片要标 stillframe, 否则 PR 当单帧视频处理
"""
import json
import math
import os
from xml.sax.saxutils import escape
from urllib.parse import quote

from .ir import Timeline, us_to_frames

TICKS_PER_SECOND = 254016000000


def _url(p: str) -> str:
    return "file://localhost/" + quote(os.path.abspath(p).replace("\\", "/"))


def _c(text: str) -> str:
    """XML 注释内容: 不能含 "--", 且要转义 & < >"""
    s = escape(str(text))
    return s.replace("--", "- -")


def _path_key(tl, mid):
    m = tl.media.get(mid)
    if not m or not m.path or m.missing:
        return None
    return os.path.normcase(os.path.abspath(m.path))


def _is_image(m) -> bool:
    return m.kind == "image" or m.path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"))


def write(tl: Timeline, out_path: str, include_grade_note: bool = True) -> str:
    fps = tl.fps
    rate = f"<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>"
    tick = TICKS_PER_SECOND / fps

    def ticks(frames: int) -> int:
        return int(round(frames * tick))

    # ---- 素材索引: 按路径去重 ----
    keys, media_by_key, need = [], {}, {}
    for mid, m in tl.media.items():
        k = _path_key(tl, mid)
        if not k:
            continue
        need[k] = max(need.get(k, 0), 0)
    for t in tl.tracks:
        for c in t.clips:
            k = _path_key(tl, c.media_id)
            if k:
                need[k] = max(need.get(k, 0), c.src_in + max(c.src_dur, c.dur))
    for mid, m in tl.media.items():
        k = _path_key(tl, mid)
        if k and k not in media_by_key:
            media_by_key[k] = m
            keys.append(k)

    ref = {}                       # path_key -> (masterclip id, file id)
    for i, k in enumerate(keys, start=1):
        ref[k] = (f"masterclip-{i}", f"file-{i}")

    # 同一文件可能既作视频又作音频: 合并能力, 否则音轨会导入成无声/无流
    cap = {}
    for mid, m in tl.media.items():
        k = _path_key(tl, mid)
        if not k:
            continue
        cur = cap.setdefault(k, {"video": False, "audio": False, "image": False})
        if m.kind == "audio":
            cur["audio"] = True
        elif m.kind == "image":
            cur["image"] = True
        else:
            cur["video"] = True
        if m.has_audio:
            cur["audio"] = True

    X = ['<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE xmeml>",
         '<xmeml version="4">', "  <project>",
         f"    <name>{escape(tl.name)}</name>", "    <children>",
         '      <bin><name>Media</name><children>']

    for k in keys:
        m = media_by_key[k]
        mc, fid = ref[k]
        c0 = cap.get(k) or {}
        img = bool(c0.get("image")) and not c0.get("video")
        dur_us = max(int(m.duration_us or 0), int(need.get(k, 0)) + 1_000_000)
        if img and dur_us < 3_600_000_000:
            dur_us = 3_600_000_000
        dur_f = max(us_to_frames(dur_us, fps), 1)
        has_audio = bool(c0.get("audio"))
        is_audio_only = has_audio and not c0.get("video") and not img
        w = m.width or tl.width
        h = m.height or tl.height

        X += [f'        <clip id="{mc}" explodedTracks="true">',
              f"          <masterclipid>{mc}</masterclipid>",
              "          <ismasterclip>TRUE</ismasterclip>",
              f"          <duration>{dur_f}</duration>",
              f"          {rate}",
              f"          <name>{escape(os.path.basename(m.path))}</name>",
              "          <media>"]
        # masterclip 内部: 视频 clipitem (带 file), 纯音频素材放 audio 段
        if not is_audio_only:
            X += ["            <video><track>",
                  f'              <clipitem id="mc-{fid}">',
                  f"                <name>{escape(os.path.basename(m.path))}</name>",
                  f"                {rate}",
                  f'                <file id="{fid}">',
                  f"                  <name>{escape(os.path.basename(m.path))}</name>",
                  f"                  <pathurl>{_url(m.path)}</pathurl>",
                  f"                  {rate}",
                  f"                  <duration>{dur_f}</duration>",
                  # PR 自己导出的每个 file 都带 timecode, 补上更保险(缺了也能导, 但别赌)
                  "                  <timecode>",
                  f"                    {rate}",
                  "                    <string>00:00:00:00</string>",
                  "                    <frame>0</frame>",
                  "                    <displayformat>NDF</displayformat>",
                  "                  </timecode>"]
            X += ["                  <media>",
                  "                    <video><samplecharacteristics>",
                  f"                      {rate}",
                  f"                      <width>{w}</width><height>{h}</height>",
                  "                      <anamorphic>FALSE</anamorphic>",
                  "                      <pixelaspectratio>square</pixelaspectratio>",
                  "                      <fielddominance>none</fielddominance>",
                  "                    </samplecharacteristics></video>"]
            if has_audio:
                # 同一文件既要能给视频轨用, 也要能给音频轨用 → file 里要声明音频流
                X += ["                    <audio><samplecharacteristics>"
                      "<depth>16</depth><samplerate>48000</samplerate>"
                      "</samplecharacteristics>",
                      "                    <channelcount>2</channelcount></audio>"]
            X += ["                  </media>",
                  "                </file>"]
            if img:
                # 注意: <stillframe> 的父元素是 clip/clipitem, **不是 file**。
                # 放进 file 里 PR 会直接报"文件导入失败"(DTD 不认这个子元素)。
                X.append("                <stillframe>TRUE</stillframe>")
            X += ["                <sourcetrack><mediatype>video</mediatype>"
                  "<trackindex>1</trackindex></sourcetrack>",
                  "              </clipitem>",
                  "            </track></video>"]
        if is_audio_only:
            X += ["            <audio><track>",
                  f'              <clipitem id="mc-{fid}">',
                  f"                <name>{escape(os.path.basename(m.path))}</name>",
                  f"                {rate}",
                  f'                <file id="{fid}">',
                  f"                  <name>{escape(os.path.basename(m.path))}</name>",
                  f"                  <pathurl>{_url(m.path)}</pathurl>",
                  f"                  {rate}",
                  f"                  <duration>{dur_f}</duration>",
                  "                  <timecode>",
                  f"                    {rate}",
                  "                    <string>00:00:00:00</string>",
                  "                    <frame>0</frame>",
                  "                    <displayformat>NDF</displayformat>",
                  "                  </timecode>",
                  "                  <media><audio>",
                  "                    <samplecharacteristics>"
                  "<depth>16</depth><samplerate>48000</samplerate>"
                  "</samplecharacteristics>",
                  "                    <channelcount>2</channelcount>",
                  "                  </audio></media>",
                  "                </file>",
                  "                <sourcetrack><mediatype>audio</mediatype>"
                  "<trackindex>1</trackindex></sourcetrack>",
                  "              </clipitem>",
                  "            </track></audio>"]
        X += ["          </media>", "        </clip>"]

    X.append("      </children></bin>")

    # ---- 序列 ----
    # 子元素顺序按 PR 自己的导出排: duration, rate, name, media, timecode
    X += [f'      <sequence id="seq-1">',
          f"        <duration>{max(us_to_frames(tl.duration, fps), 1)}</duration>",
          f"        {rate}",
          f"        <name>{escape(tl.name)}</name>",
          "        <media>",
          "          <video>",
          "            <format><samplecharacteristics>",
          f"              {rate}",
          f"              <width>{tl.width}</width><height>{tl.height}</height>",
          "              <anamorphic>FALSE</anamorphic>",
          "              <pixelaspectratio>square</pixelaspectratio>",
          "              <fielddominance>none</fielddominance>",
          "            </samplecharacteristics></format>"]

    def motion_filter(c, idx):
        tf = c.transform
        sx, sy = tf.scale_x, tf.scale_y
        if tf.flip_h:
            sx = -sx
        if tf.flip_v:
            sy = -sy
        kfs = c.kfs or {}

        def frames_of(pts, key):
            out = []
            for p in pts:
                v = key(p)
                out.append((us_to_frames(p[0], tl.fps), v))
            return out

        def kf_block(pts, key, fmt="{:.4f}"):
            if not pts or len(pts) < 2:
                return []
            r = []
            for fr, v in frames_of(pts, key):
                r.append(f"                      <keyframe>"
                         f"<when>{fr}</when><value>{fmt.format(v)}</value>"
                         f"</keyframe>")
            return r

        # PR 导出的 center 是**相对画面中心的偏移**, 0,0 就是居中(实测真实的 PR 导出
        # 里居中片段全是 horiz=0 vert=0, 取值范围 ±0.44), 按画面宽/高归一化。
        # 写 0.5+x/w 会把整条时间线的素材甩到右下角。
        cx = tf.x / tl.width
        cy = -tf.y / tl.height               # PR 的 Y 向上为正
        p0 = kfs.get("position") or []
        if len(p0) >= 2:
            cx, cy = p0[0][1] / tl.width, -p0[0][2] / tl.height
        s0 = kfs.get("scale") or []
        if len(s0) >= 2:
            sx = s0[0][1] * (-1 if tf.flip_h else 1)
            sy = s0[0][2] * (-1 if tf.flip_v else 1)
        r0 = kfs.get("rotation") or []
        rot = r0[0][1] if len(r0) >= 2 else tf.rotation

        p = [f'              <filter>', "                <effect>",
             "                  <name>Basic Motion</name>",
             "                  <effectid>basic</effectid>",
             "                  <effectcategory>motion</effectcategory>",
             "                  <effecttype>motion</effecttype>",
             "                  <mediatype>video</mediatype>",
             "                  <pproBypass>false</pproBypass>",
             '                  <parameter authoringApp="PremierePro">',
             "                    <parameterid>scale</parameterid><name>Scale</name>",
             "                    <valuemin>0</valuemin><valuemax>1000</valuemax>",
             f"                    <value>{sx * 100:.4f}</value>"]
        p += kf_block(s0, lambda q: q[1] * 100)
        p += ["                  </parameter>",
              '                  <parameter authoringApp="PremierePro">',
              "                    <parameterid>rotation</parameterid><name>Rotation</name>",
              "                    <valuemin>-8640</valuemin><valuemax>8640</valuemax>",
              f"                    <value>{rot:.3f}</value>"]
        p += kf_block(r0, lambda q: q[1], "{:.3f}")
        p += ["                  </parameter>",
              '                  <parameter authoringApp="PremierePro">',
              "                    <parameterid>center</parameterid><name>Center</name>",
              "                    <value>",
              f"                      <horiz>{cx:.6f}</horiz>",
              f"                      <vert>{cy:.6f}</vert>",
              "                    </value>"]
        # PR 的 center 关键帧是整组 horiz/vert, 每个关键帧写一个 <value>
        if len(p0) >= 2:
            for t, vx, vy in p0:
                fr = us_to_frames(t, tl.fps)
                p.append(f"                      <keyframe><when>{fr}</when><value>"
                         f"<horiz>{vx / tl.width:.6f}</horiz>"
                         f"<vert>{-vy / tl.height:.6f}</vert></value>"
                         f"</keyframe>")
        p += ["                  </parameter>",
              "                </effect>",
              "              </filter>"]
        return p

    def speed_filter(c, idx):
        return [f'              <filter>', "                <effect>",
                "                  <name>Time Remap</name>",
                "                  <effectid>timeremap</effectid>",
                "                  <effectcategory>motion</effectcategory>",
                "                  <effecttype>motion</effecttype>",
                "                  <mediatype>video</mediatype>",
                '                  <parameter authoringApp="PremierePro">',
                "                    <parameterid>variablespeed</parameterid>"
                "<name>variablespeed</name><value>0</value>",
                "                  </parameter>",
                '                  <parameter authoringApp="PremierePro">',
                "                    <parameterid>speed</parameterid><name>speed</name>",
                f"                    <value>{c.speed:.6f}</value>",
                "                  </parameter>",
                '                  <parameter authoringApp="PremierePro">',
                "                    <parameterid>reverse</parameterid><name>reverse</name>",
                "                    <value>FALSE</value>",
                "                  </parameter>",
                "                </effect>",
                "              </filter>"]

    idx = 0
    for tr in tl.video_tracks():
        if not tr.clips:
            continue                    # 空轨不写出, 免得目标端凭空多一条 V 轨
        X.append("            <track>")
        for c in sorted(tr.clips, key=lambda c: c.start):
            k = _path_key(tl, c.media_id)
            if not k or k not in ref:
                continue
            idx += 1
            mc, fid = ref[k]
            m = media_by_key[k]
            ts, te = us_to_frames(c.start, fps), us_to_frames(c.start + c.dur, fps)
            sin = us_to_frames(max(c.src_in, 0), fps)
            sout = us_to_frames(max(c.src_in, 0) + max(c.src_dur, c.dur), fps)
            dur_f = max(us_to_frames(max(int(m.duration_us or 0),
                                          c.src_in + max(c.src_dur, c.dur)), fps), 1)
            X += [f'              <clipitem id="v{idx}">',
                  f"                <masterclipid>{mc}</masterclipid>",
                  f"                <name>{escape(os.path.basename(m.path))}</name>",
                  "                <enabled>TRUE</enabled>",
                  f"                <duration>{dur_f}</duration>",
                  f"                {rate}",
                  f"                <start>{ts}</start><end>{te}</end>",
                  f"                <in>{sin}</in><out>{sout}</out>",
                  f"                <pproTicksIn>{ticks(sin)}</pproTicksIn>",
                  f"                <pproTicksOut>{ticks(sout)}</pproTicksOut>",
                  "                <alphatype>none</alphatype>",
                  "                <pixelaspectratio>square</pixelaspectratio>",
                  "                <anamorphic>FALSE</anamorphic>",
                  f'                <file id="{fid}"/>']
            if _is_image(m):
                X.append("                <stillframe>TRUE</stillframe>")
            X += ["                <sourcetrack><mediatype>video</mediatype>"
                  "<trackindex>1</trackindex></sourcetrack>"]
            if not c.transform.is_identity() or c.kfs:
                X += motion_filter(c, idx)
            if abs(c.speed - 1.0) > 1e-4:
                X += speed_filter(c, idx)
            if include_grade_note and c.grade:
                # <note> 不是 clipitem 的合法子元素(PR 的 xmeml 解析器会整份拒绝),
                # 调色参数只留成注释, 供人看, 不参与解析。
                X.append(f'                <!-- grade: {_c(json.dumps(c.grade, ensure_ascii=False))} -->')
            X.append("              </clipitem>")
        X.append("            </track>")
    X += ["          </video>"]

    # ---- 音频: 音频轨 + 视频片段音量备份轨(禁用态) ----
    audio_blocks = []

    def audio_clip(c, prefix, enabled="TRUE", note=None, idx_seed=0):
        k = _path_key(tl, c.media_id)
        if not k or k not in ref:
            return []
        mc, fid = ref[k]
        m = media_by_key[k]
        ts, te = us_to_frames(c.start, fps), us_to_frames(c.start + c.dur, fps)
        sin = us_to_frames(max(c.src_in, 0), fps)
        sout = us_to_frames(max(c.src_in, 0) + max(c.src_dur, c.dur), fps)
        dur_f = max(us_to_frames(max(int(m.duration_us or 0),
                                      c.src_in + max(c.src_dur, c.dur)), fps), 1)
        out = [f'              <clipitem id="{prefix}{idx_seed}">',
               f"                <masterclipid>{mc}</masterclipid>",
               f"                <name>{escape(os.path.basename(m.path))}</name>",
               f"                <enabled>{enabled}</enabled>",
               f"                <duration>{dur_f}</duration>",
               f"                {rate}",
               f"                <start>{ts}</start><end>{te}</end>",
               f"                <in>{sin}</in><out>{sout}</out>",
               f"                <pproTicksIn>{ticks(sin)}</pproTicksIn>",
               f"                <pproTicksOut>{ticks(sout)}</pproTicksOut>",
               f'                <file id="{fid}"/>',
               "                <sourcetrack><mediatype>audio</mediatype>"
               "<trackindex>1</trackindex></sourcetrack>"]
        if note:
            out.append(f"                <!-- {_c(note)} -->")
        if abs(c.speed - 1.0) > 1e-4:
            out += speed_filter(c, f"{prefix}{idx_seed}")
        if abs(c.volume - 1) > 1e-4:
            db = 20 * math.log10(c.volume) if c.volume > 0 else -96.0
            out += [f'                <filter>',
                    "                  <effect>",
                    "                    <name>Audio Levels</name>",
                    "                    <effectid>audiolevels</effectid>",
                    "                    <effectcategory>audiolevels</effectcategory>",
                    "                    <effecttype>audiolevels</effecttype>",
                    "                    <mediatype>audio</mediatype>",
                    '                    <parameter authoringApp="PremierePro">',
                    "                      <parameterid>level</parameterid><name>Level</name>",
                    f"                      <value>{db:.2f}</value>",
                    "                    </parameter>",
                    "                  </effect>",
                    "                </filter>"]
        out.append("              </clipitem>")
        return out

    ai = 0
    for tr in tl.audio_tracks():
        block = ["            <track>"]
        for c in sorted(tr.clips, key=lambda c: c.start):
            ai += 1
            block += audio_clip(c, "a", idx_seed=ai)
        block.append("            </track>")
        if len(block) > 2:
            audio_blocks += block

    va = [c for tr in tl.video_tracks() for c in tr.clips
          if abs(c.volume - 1) > 1e-4 or c.fade_in or c.fade_out]
    if va:
        block = ["            <track>",
                 "              <!-- 视频片段自带音频的音量/淡变备份(disabled): 数值供核对, 需要时手动启用 -->"]
        for i, c in enumerate(va, start=1):
            block += audio_clip(
                c, "va", enabled="FALSE",
                note=f"剪映视频片段音量 {c.volume:.3f} 淡入{c.fade_in / 1e6:.2f}s 淡出{c.fade_out / 1e6:.2f}s",
                idx_seed=i)
        block.append("            </track>")
        if len(block) > 3:
            audio_blocks += block

    if audio_blocks:
        X.append("          <audio>")
        # PR 自己在导出的每条序列 audio 段开头都会写这三块，缺了会让某些
        # 版本的导入器判定序列不完整（表现为整份 XML 拒绝、错误消息为空）
        X += ["            <numOutputChannels>2</numOutputChannels>",
              "            <format><samplecharacteristics><depth>16</depth>"
              "<samplerate>48000</samplerate></samplecharacteristics></format>",
              "            <outputs>",
              "              <group><index>1</index><numchannels>1</numchannels>"
              "<downmix>0</downmix><channel><index>1</index></channel></group>",
              "              <group><index>2</index><numchannels>1</numchannels>"
              "<downmix>0</downmix><channel><index>2</index></channel></group>",
              "            </outputs>"]
        X += audio_blocks
        X.append("          </audio>")

    # timecode 放在 media 之后（PR 自己导出的顺序）
    X += ["        </media>",
          "        <timecode>",
          f"          {rate}",
          "          <string>00:00:00:00</string>",
          "          <frame>0</frame>",
          "          <displayformat>NDF</displayformat>",
          "        </timecode>",
          "      </sequence>", "    </children>",
          "  </project>", "</xmeml>"]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(X))
    return out_path
