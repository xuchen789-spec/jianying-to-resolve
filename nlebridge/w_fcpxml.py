# -*- coding: utf-8 -*-
"""IR → FCPXML 1.9 (达芬奇 / Final Cut 导入)

关键格式要点(踩过的坑, 每条都在达芬奇里复现过):
  - <asset> **必须** 带 duration, 否则达芬奇把素材当 0 长, 全部片段变单帧
  - **spine 必须首尾相接**: 主故事线上的元素要连续排列, 断口处必须写 <gap>。
    把一堆绝对定位的片段直接平铺进 spine, 达芬奇会在第一个断口处停止解析,
    时间线上只剩十几个片段(实测 177 个只进来 18 个)。
  - **其余轨道必须是"挂靠片段"(connected clip), 即作为主故事线元素的子元素**。
    和 spine 平级地写 lane 是无效的: 达芬奇会把 lane 整个忽略, 按"能塞进哪条空轨
    就塞哪条"重排。实测证据: 音频 6 条轨被重排成 1/1/9/11/22/42 段, 而 A6 里混着
    我们写的 lane -1..-5 的片段 —— 所以"音轨顺序怎么改都是反的/乱的"根本不是
    lane 编号问题, 而是结构问题。改成子元素后 lane 才会被真正采纳。
  - **挂靠片段的 offset, 达芬奇按"时间线绝对时间"解释, 不是 FCPX 的相对父元素**。
    规范写法(offset 相对父元素)导进达芬奇会整体错位: 挂在第 2 个主元素下的片段
    被当成 0s 起, 结果是片段左移/重叠/被吞(用户实测"剪辑错误")。
    做法: 所有挂靠片段一律挂到 **主故事线第一个元素**(它起点必为 0)下, offset 写
    绝对时间 —— 此时相对==绝对, 两种解释都对。实测这份是达芬奇里唯一完全正确的。
  - **音轨顺序**: 挂靠片上 lane **-1 就是 A1**(最靠近视频的那条), -2 → A2 …
    剪映 tracks 数组的第 i 条音轨(数组顺序 = 界面从上到下)写 lane=-i。
  - ⚠️ **音频连接片段的 lane 达芬奇根本不读**: 视频连接片段的正 lane 会被采纳
    (57/25/6/1 完美保留), 但音频连接片段的负 lane 会被无视, 达芬奇按"时间重叠
    贪心装箱"自行重排轨(2026-11-11 实测: 剪映 34/19/20/7/5/1 → 达芬奇 2/2/25/8/21/28,
    时间全对、轨全乱)。这是达芬奇的已知缺陷(Syncaila 官方文档记载 "FCPXML: clips
    may be randomly moved to other tracks")。
    **解法 = 每条音轨打包成一个连接 storyline**: <clip lane="-N" offset="0s"
    duration=…><spine>片段+补 gap</spine></clip>。storyline 的轨映射是达芬奇可靠
    支持的(Syncaila 同款 workaround)。导入后音频轨上是复合片段, 全选 → 右键
    "分解到原位(Decompose in Place)"还原成单片段。
    注意: storyline 的 offset 写 0, 片段在内部 spine 里写**绝对时间**(相对==绝对,
    规避"达芬奇把挂靠 offset 当绝对时间读"的坑)。
  - **静帧的 position.y**: 达芬奇对 FCPXML 里的图片素材按"**底边**"对齐 —— 实测
    position y 写 -31.235(即 337px), 画面里图片中心却在 254px 处, 差额正好是
    图片渲染高度的一半。所以写静帧位置时要 **加上 渲染高度/2** 补偿,
    否则贴图会整体偏高半张图(本工程的"窃蛋龙"就是这样, 视觉上差 83px)。
    视频素材没有这个现象, 只补图片。
  - **音频片段的素材声明**: 若音频片段引用的文件本身含视频流(剪映"提取音频"常见),
    必须在 resources 里单独给它一个 hasVideo="0" 的 asset。否则达芬奇无视
    srcEnable="audio", 把它当视频片段甩到新的视频轨上 —— 表现为"视频轨道莫名其妙变多"。
  - 音量用子元素 <adjust-volume amount="xdB">, audioVolume 不是 asset-clip 的合法属性
  - 变换不是 asset-clip 的属性, 必须写成子元素 <adjust-transform position scale rotation>
    (属性 position/scale 是非法的, 会被静默忽略 → 缩放位移全丢)
  - 淡变按 FCPXML DTD 标准写成 adjust-volume>param>fadeIn/fadeOut(片段级参数, 可编辑)。
    ⚠️ 禁止写 audioFadeIn/audioFadeOut 属性 —— DTD 里根本没有, 非标准标记有让达芬奇
    导入时整段丢弃片段的风险(2026-11-11 用户实测报告"音频片段丢失")。达芬奇若不导入
    该淡变, 用 cli 生成的 <名>_postimport.lua 兜底(打印手动清单)
  - 变速用 <timeMap>: time = 新时间线时间, value = 源时间(相对源入点, 终点=src_dur)
  - 音频片段用负 lane + srcEnable="audio" 限定只取音频流
  - **视频片段自带的声音**要单独写成音频片段(jy_read 已生成 "Vn 原声" 轨),
    视频片段本身只写 srcEnable="video"。否则原声在 FCPXML 里无声消失。
    同一文件因此会有两个 asset: hasVideo=1 的(视频用)与 hasVideo=0 的(音频用),
    后者不可省略 —— 含视频流的文件被当音频引用时, 达芬奇会无视 srcEnable="audio"。
  - 素材 src 用 file:///D:/... , 盘符冒号**不能**转义成 %3A, 否则解析不出路径
  - 图片素材: 文件名里不能有数字, 否则达芬奇按图片序列解析(见 mediaprobe.apply_stills),
    时长按"实际用到的最长源范围 + 5s"给, 不要给几小时
"""
import json
import math
import os
from fractions import Fraction
from urllib.parse import quote

from .ir import Timeline

STILL_PAD_US = 5_000_000        # 静帧素材时长余量

# 剪映转场名 → FCPXML/达芬奇能认的转场名。
# 只有"纯叠化"和"闪黑"能靠 <transition> 表达:
#   推镜变焦/放射/炫光 带运镜与光效, 达芬奇没有同名转场; 闪白要白色 dip, 颜色参数
#   解析不稳(默认黑), 一律留在人工/BCC 清单里。
TRANSITION_MAP = {
    "叠化": "Cross Dissolve",
    "溶解": "Cross Dissolve",
    "交叉叠化": "Cross Dissolve",
    "闪黑": "Dip to Color Dissolve",
}


def _frac(us: int, fps: int) -> str:
    """微秒 → FCPXML 有理数时间 (分母跟随帧率)"""
    f = int(round(us * fps / 1_000_000))
    g = math.gcd(abs(f), fps) or 1
    return f"{f // g}/{fps // g}s"


def _path_key(tl, mid):
    """素材唯一键 = 规范化绝对路径 (视频/音频素材常指向同一文件)"""
    m = tl.media.get(mid)
    if not m or not m.path or m.missing:
        return None
    return os.path.normcase(os.path.abspath(m.path))


def _url(p: str) -> str:
    # 注意: Windows 盘符的冒号不能转义成 %3A, 否则达芬奇/PR 解不出路径
    return "file:///" + quote(os.path.abspath(p).replace("\\", "/"), safe="/:")


def _frames(us: int, fps: int) -> int:
    """微秒 → 帧号(四舍五入), 保证时间线绝对对齐帧栅格"""
    return int(round(us * fps / 1_000_000))


def _xml_attr(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def write(tl: Timeline, out_path: str, y_up: bool = True,
          rename_stills: bool = True, scale_mode: str = "fit",
          still_anchor: str = "bottom", connect: str = "absolute",
          video_audio: str = "", with_transitions: bool = False) -> str:
    """y_up: FCPXML 的 position.y 向上为正(FCPX 约定). 若目标端表现上下镜像,
    用 y_up=False 按 y 向下写.
    rename_stills: 把静帧硬链接成纯字母文件名。达芬奇会把带数字的 PNG 当图片序列读,
    这一步不是可选项, 除非你确定素材名里没有数字。
    scale_mode: 'fit' = 写"相对适应画布"的缩放(达芬奇/FCP 默认 image scaling 会先做
    contain 适配, 1.0 就是铺满画布), 'native' = 写相对素材原始像素的缩放
    (项目里关掉了 Image Scaling 时用)。
    still_anchor: 'bottom' = 达芬奇的实测行为(图片按底边对齐 position), 写的时候
    把 y 加半个渲染高度补回来; 'center' = 目标端把 position 当图片中心(不补)。
    connect: 'absolute' = 达芬奇实测行为: 挂靠片段的 offset 按**时间线绝对时间**读,
    所以全部挂到主故事线第一个元素下(起点 0, 相对==绝对), 不会错位(推荐给达芬奇)。
    'parent' = 标准 FCPX 写法, 挂在"包含它起点的主故事线元素"下、offset 相对父元素;
    严格按规范读的工具(FCPX 本身/部分第三方)用这个, 但**达芬奇会读错位置**。"""
    if rename_stills:
        # 放在写出器里而不是 CLI 里: 任何调用方都不该有机会漏掉
        from .mediaprobe import apply_stills
        apply_stills(tl, os.path.dirname(os.path.abspath(out_path)) or ".")

    fps = tl.fps
    video_audio = video_audio or getattr(tl, "video_audio_mode", "track")
    X = ['<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE fcpxml>",
         '<fcpxml version="1.9">', "  <resources>",
         f'    <format id="r1" name="FFVideoFormat{tl.height}p{fps}" '
         f'frameDuration="1/{fps}s" width="{tl.width}" height="{tl.height}" '
         f'colorSpace="1-1-1 (Rec. 709)"/>']

    # 只保留有片段的轨道, 并让 lane 编号连续(空轨不占号)
    vts = [t for t in tl.video_tracks() if t.clips]
    ats = [t for t in tl.audio_tracks() if t.clips]

    # ---- 素材键: (路径, 用途) ----
    # 用途 'av' = 视频片段用(可带视频流); 'a' = 音频片段用(声明成纯音频)。
    # 同一文件既被视频轨用又被音轨用时, 会得到两个 asset, 这是刻意的。
    def akey(mid, is_audio):
        k = _path_key(tl, mid)
        return None if k is None else (k, "a" if is_audio else "av")

    need = {}                       # akey -> 需要的最长源范围
    for t, is_audio in [(t, False) for t in vts] + [(t, True) for t in ats]:
        for c in t.clips:
            k = akey(c.media_id, is_audio)
            if k:
                need[k] = max(need.get(k, 0), c.src_in + max(c.src_dur, c.dur))

    ref, media_by_key = {}, {}
    for t, is_audio in [(t, False) for t in vts] + [(t, True) for t in ats]:
        for c in t.clips:
            k = akey(c.media_id, is_audio)
            if k and k not in media_by_key:
                media_by_key[k] = tl.media[c.media_id]

    for k, m in media_by_key.items():
        aid = f"a{len(ref) + 1}"
        ref[k] = aid
        as_audio = k[1] == "a"

        dur = max(int(m.duration_us or 0), int(need.get(k, 0)) + 1_000_000)
        if m.kind == "image":
            # 静帧: 够用就好。给几小时会被目标端当超长素材/序列处理
            dur = max(int(need.get(k, 0)) + STILL_PAD_US, 2_000_000)

        attrs = [f'id="{aid}"', f'name="{_xml_attr(os.path.basename(m.path))}"',
                 f'src="{_xml_attr(_url(m.path))}"', 'start="0s"',
                 f'duration="{_frac(dur, fps)}"']
        if as_audio:
            # 纯音频用途: 明确 hasVideo="0", 否则含视频流的 mp4 会被当成视频片段
            attrs += ['hasVideo="0"', 'hasAudio="1"', 'audioSources="1"',
                      'audioChannels="2"', 'audioRate="48000"']
        else:
            attrs += ['hasVideo="1"', 'videoSources="1"']
            # 音频走独立片段时, 视频素材要声明 hasAudio="0": 否则达芬奇会额外生成一份
            # "链接音频", 与我们的原声音轨叠加 → 同一段声音出现两遍(实测多出 6 条音轨)
            if m.has_audio and video_audio == "video":
                attrs += ['hasAudio="1"', 'audioSources="1"',
                          'audioChannels="2"', 'audioRate="48000"']
            else:
                attrs += ['hasAudio="0"']
        X.append("    <asset " + " ".join(attrs) + "/>")
    X.append("  </resources>")
    X.append(f'  <library><event name="{_xml_attr(tl.name)}">'
             f'<project name="{_xml_attr(tl.name)}">')
    X.append(f'    <sequence format="r1" duration="{_frac(tl.duration, fps)}" '
             f'tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">')
    X.append("      <spine>")

    def _fr(n: int) -> str:
        n = int(round(n))
        g = math.gcd(abs(n), fps) or 1
        return f"{n // g}/{fps // g}s"

    def _start_attr(c) -> str:
        """asset-clip 的 start(= 源入点)。

        ⚠️ 达芬奇导入变速片段时会把 start **再乘一次倍速**(实测 in_eff = in_export × speed),
        不补偿的话每段入点偏移 = 源起点 ×(speed−1)。1.1x 的蛋段偏 9 帧、0.9x 的科普段
        偏 90 帧(3.75s), 画面整体超前/滞后, 但速度本身是对的 —— 极难肉眼定位。
        这里预先除以"实际倍速"(用 src_dur/dur, 与 timeMap 斜率严格一致)。
        """
        n = max(_frames(c.src_in, fps), 0)
        dur = getattr(c, 'dur', 0) or 0
        sp = (c.src_dur / dur) if dur > 0 else getattr(c, 'speed', 1.0)
        if abs(sp - 1.0) <= 1e-4:
            return _fr(n)
        f = Fraction(n) / (Fraction(int(round(fps))) * Fraction(sp).limit_denominator(10000))
        return f"{f.numerator}/{f.denominator}s"

    def clip_xml(c, lane=None, is_audio=False, off_f=None, dur_f=None,
                 ind="        ", kids=None):
        k = akey(c.media_id, is_audio)
        if not k or k not in ref:
            return None
        off_f = _frames(c.start, fps) if off_f is None else off_f
        dur_f = max(_frames(c.dur, fps), 1) if dur_f is None else dur_f
        attrs = [f'ref="{ref[k]}"',
                 f'offset="{_fr(off_f)}"',
                 f'start="{_start_attr(c)}"',
                 f'duration="{_fr(dur_f)}"',
                 'tcFormat="NDF"',
                 'srcEnable="audio"' if is_audio
                 else ('srcEnable="all"' if (getattr(c, "own_audio", False)
                                             and video_audio == "video")
                       else 'srcEnable="video"')]
        if lane is not None:
            attrs.append(f'lane="{lane}"')
        if is_audio:
            attrs.append('audioRole="dialogue"')

        i2 = ind + "  "
        body = []
        # 变速判定用"实际源消耗/时间线时长", 不看 c.speed 字段 —— 剪映有些片段
        # speed 写着 1.0, 但源区间比时间线区间短(0.987x 这种), 只信字段会漏写
        # timeMap, 导入后按 1x 播、越到后面偏得越多(2026-11-18 实测 A2 两段差 3~4 帧)。
        spd_eff = (c.src_dur / c.dur) if getattr(c, 'dur', 0) else c.speed
        if abs(spd_eff - 1.0) > 1e-3:
            # timeMap: time = 新时间线时间, value = 源素材时间(相对片段源入点)。
            # ⚠️ 终点 value 必须是 c.src_dur(片段实际消耗的源时长), 不是 max(src,dur):
            # 慢速(0.8x/0.9x)时 src_dur < dur, 取 max 会写成恒等映射(斜率1) —— 达芬奇里
            # 变速直接丢失、按原速播放(2026-11-11 实测: 0.9x 片段全部变成 1x)。
            # ⚠️ value 用**精确秒**(不是整帧): 斜率必须严格等于 src_dur/dur, 才能和
            # _start_attr 里"除以倍速"的补偿相互抵消。按整帧取整会让 0.9x 变 0.906,
            # 补偿与斜率不一致 → 残留几帧入点误差(2026-11-18 实测 A2@2335 差 7 帧)。
            src_q = Fraction(int(round(c.src_dur)), 1_000_000)   # 微秒 → 精确秒
            if src_q <= 0:
                src_q = Fraction(max(_frames(c.src_dur, fps), 1), int(round(fps)))
            body.append(f"{i2}<timeMap>")
            body.append(f'{i2}  <timept time="0s" value="0s" interp="linear"/>')
            body.append(f'{i2}  <timept time="{_fr(dur_f)}" '
                        f'value="{src_q.numerator}/{src_q.denominator}s" interp="linear"/>')
            body.append(f"{i2}</timeMap>")

        tf = c.transform
        # FCPXML 的 position 单位是"画面高度的百分比"(Apple: "5 percent ... of the
        # project's height"), 不是像素 —— 按像素写会被放大 height/100 倍(1080p 就是
        # 10.8 倍), 表现为画面飞出画外。x 同样以高度为基准。
        h = float(tl.height)

        def _pos(vx, vy):
            return f"{vx / h * 100:.4f} {(-vy if y_up else vy) / h * 100:.4f}"

        def _scl(sx, sy):
            if scale_mode == "fit":
                fx = tf.fit_x or 1.0
                fy = tf.fit_y or 1.0
                sx, sy = sx / fx, sy / fy
            return f"{sx:.5f} {sy:.5f}"

        sx, sy = tf.scale_x, tf.scale_y
        if tf.flip_h:
            sx = -sx
        if tf.flip_v:
            sy = -sy
        px, py = tf.x, tf.y
        rot = tf.rotation

        # 静帧补偿: 达芬奇把图片的**底边**放在 position 上(实测), 所以中心会偏高
        # 半个渲染高度。这里把 y 往下补 h/2, 落到达芬奇里图片中心正好在目标点。
        dy = 0.0
        if still_anchor == "bottom" and not is_audio:
            m = tl.media.get(c.media_id)
            if m is not None and m.kind == "image" and m.height:
                dy = m.height * abs(tf.scale_y) / 2.0

        kfs = c.kfs or {}
        has_kf = any(kfs.get(k) for k in ("position", "scale", "rotation", "opacity"))
        if has_kf:
            # 有动画时, 静态属性取第一帧的值(万一目标端丢掉 keyframe, 至少状态是对的)
            p0 = (kfs.get("position") or [])
            if len(p0) >= 2:
                px, py = p0[0][1], p0[0][2]
            s0 = (kfs.get("scale") or [])
            if len(s0) >= 2:
                sx = s0[0][1] * (-1 if tf.flip_h else 1)
                sy = s0[0][2] * (-1 if tf.flip_v else 1)
            r0 = (kfs.get("rotation") or [])
            if len(r0) >= 2:
                rot = r0[0][1]
        if not tf.is_identity() or has_kf:
            body.append(f'{i2}<adjust-transform position="{_pos(px, py + dy)}" '
                        f'scale="{_scl(sx, sy)}" rotation="{rot:.3f}">')
            i3 = i2 + "  "
            for name in ("position", "scale", "rotation", "opacity"):
                pts = kfs.get(name) or []
                if len(pts) < 2:
                    continue
                if name == "position":
                    vals = [_pos(p[1], p[2] + dy) for p in pts]
                    base = vals[0]
                elif name == "scale":
                    vals = [_scl(p[1] * (-1 if tf.flip_h else 1),
                                 p[2] * (-1 if tf.flip_v else 1)) for p in pts]
                    base = vals[0]
                elif name == "rotation":
                    vals = [f"{p[1]:.3f}" for p in pts]
                    base = vals[0]
                else:
                    vals = [f"{p[1]:.4f}" for p in pts]
                    base = vals[0]
                body.append(f'{i3}<param name="{name}" value="{base}">')
                body.append(f"{i3}  <keyframeAnimation>")
                for p, v in zip(pts, vals):
                    body.append(f'{i3}    <keyframe time="{_fr(_frames(p[0], fps))}" '
                                f'value="{v}"/>')
                body.append(f"{i3}  </keyframeAnimation>")
                body.append(f"{i3}</param>")
            body.append(f"{i2}</adjust-transform>")

        # ---- 音量与淡变: FCPXML DTD 标准写法 ----
        # DTD: adjust-volume (param*), param (fadeIn?, fadeOut?, keyframeAnimation?, param*)
        # 淡变是"音量参数上的 fadeIn/fadeOut 子元素" —— 片段级参数, 目标端支持时
        # 导入即是可编辑的原生淡变, 不是烧进媒体。
        # ⚠️ 禁止写 audioFadeIn/audioFadeOut 之类的属性: DTD 里没有这种属性,
        # 非标准标记有让达芬奇整段丢弃片段的风险(2026-11-11 用户报告音频片段丢失)。
        # ⚠️ 位置必须在 timeMap/adjust-transform 之后: DTD 顺序是
        #    timing-params(conform-rate?, timeMap?) -> intrinsic(video..., audio...)
        has_vol = abs(c.volume - 1) > 1e-4
        has_fade = bool(c.fade_in or c.fade_out)
        # 视频片段只有在"声音跟着片段走"的模式下才写音量/淡变;
        # track 模式下这段声音由独立音轨承载, 视频片段本身没有音频可调。
        own_v = (not is_audio) and video_audio == "video" and getattr(c, "own_audio", False)
        if (is_audio and (has_vol or has_fade)) or (own_v and (has_vol or has_fade)):
            # 音频片段: 音量+淡变; 视频片段: 只在剪映给"自带声音"挂了淡变时写
            # (asset-clip 的 intrinsic-params 包含 adjust-volume, 视频片段同样合法)
            if has_vol:
                db = 20 * math.log10(c.volume) if c.volume > 0 else -96.0
            else:
                db = 0.0
            amount = f"{db:.2f}dB"
            if has_fade:
                body.append(f'{i2}<adjust-volume amount="{amount}">')
                body.append(f'{i2}  <param name="volume" value="{amount}">')
                if c.fade_in:
                    body.append(f'{i2}    <fadeIn type="linear" '
                                f'duration="{_fr(_frames(c.fade_in, fps))}"/>')
                if c.fade_out:
                    body.append(f'{i2}    <fadeOut type="linear" '
                                f'duration="{_fr(_frames(c.fade_out, fps))}"/>')
                body.append(f'{i2}  </param>')
                body.append(f'{i2}</adjust-volume>')
            else:
                body.append(f'{i2}<adjust-volume amount="{amount}"/>')

        if c.grade:
            body.append(f'{i2}<!-- grade: {json.dumps(c.grade, ensure_ascii=False)} -->')

        if kids:
            body.extend(kids)

        head = f'{ind}<asset-clip {" ".join(attrs)}'
        if not body:
            return head + "/>"
        return head + ">\n" + "\n".join(body) + f"\n{ind}</asset-clip>"

    # ---- 主故事线(spine) ----
    # FCPXML 硬性要求: spine 里的元素必须首尾相接, 断口处必须补 <gap>。
    # 直接把绝对定位的片段平铺进 spine 会让达芬奇在第一个断口处停止解析
    # (之前 177 个片段只进来 18 个就是这个原因)。
    primary = None
    for cand in vts:
        sp = sorted(((_frames(c.start, fps), _frames(c.dur, fps)) for c in cand.clips))
        if sp and all(b[0] >= a[0] + a[1] for a, b in zip(sp, sp[1:])):
            primary = cand
            break

    cursor = 0
    items = []                     # [(start_f, dur_f, clip|None)]  None = gap
    if primary is not None:
        for c in sorted(primary.clips, key=lambda c: c.start):
            s_f = _frames(c.start, fps)
            d_f = max(_frames(c.dur, fps), 1)
            if s_f > cursor:
                items.append((cursor, s_f - cursor, None))
                cursor = s_f
            items.append((s_f, d_f, c))
            cursor = max(cursor, s_f + d_f)
    total_f = _frames(tl.duration, fps)
    if cursor < total_f:
        items.append((cursor, total_f - cursor, None))
    if not items:
        items.append((0, max(total_f, 1), None))

    # ---- 转场: 只写主故事线上的纯叠化/闪黑 ----
    # 剪映把转场挂在**宿主片段尾部**, 切点 = host_start + host_dur(见 jy_read)。
    # FCPXML 的 <transition> 骑在切点上: offset = 切点 − 时长/2, 两侧片段的位置和长度
    # 都不动(转场吃素材余量), 时间线总长不变。不在主故事线上的(叠层轨 V2/V4/V8)
    # 一律不写 —— 靠 (host_start, host_dur) 精确匹配, 避免落到同帧的 V1 切点上。
    tr_by_item = {}
    tr_manual = []
    if with_transitions and getattr(tl, "transitions", None):
        key_index = {}
        for i, (st, du, cl) in enumerate(items):
            if cl is not None:
                key_index.setdefault((st, du), []).append(i)
        for t in sorted(tl.transitions, key=lambda x: x["at"]):
            fcp_name = TRANSITION_MAP.get(t.get("name") or "")
            dd = _frames(t.get("dur") or 0, fps)
            if not fcp_name or dd < 4:
                tr_manual.append(t)
                continue
            hs = _frames(t["host_start"], fps)
            hd = max(_frames(t["host_dur"], fps), 1)
            hit = None
            for i in key_index.get((hs, hd), []):
                nxt = items[i + 1] if i + 1 < len(items) else None
                if nxt and nxt[2] is not None and nxt[0] == hs + hd:
                    hit = i
                    break
            if hit is None:
                tr_manual.append(t)
                continue
            tr_by_item[hit] = (fcp_name, dd, dd // 2)
    else:
        tr_manual = list(getattr(tl, "transitions", []) or [])
    tl.fcpxml_transitions = len(tr_by_item)
    tl.fcpxml_transitions_manual = tr_manual

    # ---- 其余轨道的片段: 必须"挂在"某个主故事线元素下, 不能和 spine 平级 ----
    # lane 只对 connected clip(子元素) 有意义。平级写在 spine 里时达芬奇会把 lane
    # 整个忽略, 按"能塞进哪条空轨就塞哪条"重排 —— 实测 A6 里混着 lane -1..-5 的片段,
    # 这就是'音轨顺序怎么改都是乱的/反的'的真正原因。改成子元素后 lane 才会被采纳。
    # 下面先按"挂在包含它起点的主元素下"收集(相对偏移), 最终是否摊平到第一个元素
    # 由 connect 参数决定(达芬奇必须摊平, 见上方注释)。
    conn = [[] for _ in items]      # [(relative_start_f, lane, clip, is_audio)]

    def place(c, lane, is_audio):
        s_f = _frames(c.start, fps)
        idx = None
        for i, (st, du, _cl) in enumerate(items):
            if st <= s_f < st + du:
                idx = i
                break
        if idx is None:             # 落在时间线之外(不该发生): 挂到最后一项
            idx = len(items) - 1 if s_f >= items[-1][0] else 0
        conn[idx].append((s_f - items[idx][0], lane, c, is_audio))

    # 其余视频轨: lane 1..N (lane 越大越靠上), 空轨不占号
    lane_i = 0
    for tr in vts:
        if tr is primary:
            continue
        lane_i += 1
        for c in sorted(tr.clips, key=lambda c: c.start):
            place(c, lane_i, False)

    # 音频轨: 负 lane。⚠️ 达芬奇 21.0 实测把 storyline 的 lane -k 放到
    # A(N-k+1)(**反向映射**, N=音频storyline数): lane -1→A6, -5→A2。
    # 要让剪映第 i 条音轨(0 起)落到 A(i+1), 必须写 lane = -(N-i)。
    # 视频轨"原声"轨由 jy_read 追加在音轨列表末尾, 因此落在最上面的 A 轨,
    # 剪映自己的音轨编号(A1..An)不受影响。
    n_at = len(ats)
    lane_name = {}
    for li, tr in enumerate(ats):
        lane = -(n_at - li)
        lane_name[lane] = tr.name or f"音轨{li + 1}"
        for c in sorted(tr.clips, key=lambda c: c.start):
            place(c, lane, True)

    if connect != "parent":
        # 达芬奇把 connected clip 的 offset 当**时间线绝对时间**读(实测: 规范写法
        # "相对父元素"导进去会整体左移/重叠, 用户看到的就是"剪辑错误")。
        # 全部挂到第一个主故事线元素下 —— 它起点必为 0, 相对偏移正好等于绝对偏移,
        # 于是"相对"和"绝对"两种解释都对。实测这份在达芬奇里完全正确。
        for i in range(1, len(items)):
            conn[0].extend((rel + items[i][0], lane, c, ia)
                           for rel, lane, c, ia in conn[i])
            conn[i] = []

    def audio_storyline_xml(lane_no, entries, name):
        """一条音轨 → 连接 storyline(达芬奇唯一保留音轨结构的写法)。
        entries: [(abs_start_f, dur_f, clip)]。storyline offset=0,
        内部 spine 里的片段偏移写绝对时间(== 相对, 规避歧义), 空隙补 gap。"""
        segs = []
        cursor = 0
        end = 0
        for s_f, d_f, c in sorted(entries, key=lambda t: t[0]):
            if s_f > cursor:
                segs.append((cursor, s_f - cursor, None))
            segs.append((s_f, d_f, c))
            cursor = s_f + d_f
            end = cursor
        i2 = ind_story + "  "
        lines = [f'{ind_story}<clip name="{_xml_attr(name)}" lane="{lane_no}" '
                 f'offset="0s" start="0s" duration="{_fr(max(end, 1))}" '
                 f'tcFormat="NDF">',
                 f"{i2}<spine>"]
        for s_f, d_f, c in segs:
            if c is None:
                lines.append(f'{i2}  <gap name="Gap" offset="{_fr(s_f)}" '
                             f'start="0s" duration="{_fr(d_f)}"/>')
            else:
                x = clip_xml(c, lane=None, is_audio=True, off_f=s_f,
                             ind=i2 + "  ")
                if x:
                    lines.append(x)
        lines += [f"{i2}</spine>", f"{ind_story}</clip>"]
        return lines

    ind_story = "          "      # 挂靠子元素缩进(10 空格)
    for i, (st, du, cl) in enumerate(items):
        kids = sorted(conn[i], key=lambda t: (t[0], t[1]))
        kx = []
        aud_by_lane = {}          # lane -> [(abs_start_f, dur_f, clip)]
        for rel, lane, c, ia in kids:
            if not ia:
                x = clip_xml(c, lane=lane, is_audio=False, off_f=rel,
                             ind="          ")
                if x:
                    kx.append(x)
                continue
            aud_by_lane.setdefault(lane, []).append(
                (rel, max(_frames(c.dur, fps), 1), c))
        for lane in sorted(aud_by_lane):
            kx.extend(audio_storyline_xml(lane, aud_by_lane[lane],
                                          lane_name.get(lane, f"音轨{-lane}")))
        kx = [x for x in kx if x]
        if cl is None:
            if not kx:                       # 普通空档
                X.append(f'        <gap name="Gap" offset="{_fr(st)}" '
                         f'start="0s" duration="{_fr(du)}"/>')
            else:                            # 空档里也有挂靠片段: gap 可以带子元素
                X.append(f'        <gap name="Gap" offset="{_fr(st)}" '
                         f'start="0s" duration="{_fr(du)}">')
                X.extend(kx)
                X.append("        </gap>")
            continue
        line = clip_xml(cl, off_f=st, dur_f=du, kids=kx)
        if line:
            X.append(line)
            tr = tr_by_item.get(i)
            if tr:
                nm, dd, half = tr
                X.append(f'        <transition name="{nm}" '
                         f'offset="{_fr(st + du - half)}" duration="{_fr(dd)}"/>')

    X += ["      </spine>", "    </sequence></project></event></library></fcpxml>"]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(X))
    return out_path
