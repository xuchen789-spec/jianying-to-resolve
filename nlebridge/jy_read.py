# -*- coding: utf-8 -*-
"""剪映 (JianYing / CapCut) 草稿读取器

支持:
  - 明文草稿 (5.9 及以下 / 代码生成)
  - 新版加密草稿 (AES-256-GCM) —— 需要可选的 jy_crypto.py, 删掉即降级为只读明文
  - ##_draftpath_placeholder_<GUID>_## 相对路径占位符
  - source_timerange.start/duration 缺省 (=0 / 取 target 等长)
格式事实(2026-11 实测, 剪映 10.x):
  - 基础调色 = materials.effects 中 effect_id=7416258989467374091 的条目, type 即滑块名, value 为强度
  - 变速 = source.duration / target.duration
  - 变换 = segment.clip.{scale{x,y}, transform{x,y}, flip{horizontal,vertical}, rotation}
"""
import os
import re
import json
from typing import Optional

from .ir import Timeline, Track, Clip, Media, Transform
from .mediaprobe import has_audio_stream

COLOR_EFFECT_ID = "7416258989467374091"
PLACEHOLDER_RE = re.compile(r"##_draftpath_placeholder_[0-9A-Fa-f-]+_##")

def _fit_factor(canvas_w: int, canvas_h: int, mat_w: int, mat_h: int) -> float:
    """剪映的 clip.scale 是相对"已适应画布"的尺寸, 不是素材原始像素.

    剪映把任意分辨率的素材放进画布时先做一次 contain 适配(4K 与 720p 素材
    在 1080p 工程里 scale 都是 1.0 可证), 在此基础上再乘 clip.scale.
    转成 IR 的原生像素缩放需要乘回这个适配系数.
    """
    if not mat_w or not mat_h or not canvas_w or not canvas_h:
        return 1.0
    return min(canvas_w / mat_w, canvas_h / mat_h)


def is_encrypted(path: str) -> bool:
    with open(path, encoding="utf-8", errors="ignore") as f:
        return not f.read(64).lstrip().startswith("{")


# 解密是可选能力：jy_crypto.py 可以整文件删除，删掉后只支持未加密草稿，
# 其余功能（转换/审计/蒙版）完全不受影响。见 jy_crypto.py 顶部的用途边界。
try:
    from .jy_crypto import decrypt_draft_text          # noqa: F401
    CRYPTO_AVAILABLE = True
except ImportError:                                    # pragma: no cover
    CRYPTO_AVAILABLE = False

    def decrypt_draft_text(path: str) -> str:
        raise RuntimeError(
            "这个草稿是加密格式，但 nlebridge/jy_crypto.py 不存在。\n"
            "两条路任选：\n"
            "  1) 在剪映里「另存为 / 导出草稿」，拿到未加密版本\n"
            "  2) 恢复 jy_crypto.py 并 pip install pycryptodome")



def load_draft_json(src: str):
    """返回 (草稿 dict, 草稿目录)"""
    if os.path.isdir(src):
        base, path = src, os.path.join(src, "draft_content.json")
    else:
        base, path = os.path.dirname(os.path.abspath(src)), src
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到草稿文件: {path}")
    if is_encrypted(path):
        text = decrypt_draft_text(path)
    else:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    return json.loads(text), base


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


class _Ctx:
    def __init__(self, d, base):
        self.d, self.base = d, base
        m = d.get("materials", {})
        self.videos = {x["id"]: x for x in m.get("videos", [])}
        self.audios = {x["id"]: x for x in m.get("audios", [])}
        self.speeds = {x["id"]: x for x in m.get("speeds", [])}
        self.texts = {x["id"]: x for x in m.get("texts", [])}
        self.effects = {x["id"]: x for x in m.get("effects", [])}
        self.fades = {x["id"]: x for x in m.get("audio_fades", [])}
        self.masks = {x["id"]: x for x in m.get("common_mask", [])}
        self.anims = {x["id"]: x for x in m.get("material_animations", [])}

    def path_of(self, mat) -> str:
        p = mat.get("path", "")
        if not p:
            return ""
        p = PLACEHOLDER_RE.sub(self.base.replace("\\", "/"), p)
        return p if os.path.isabs(p) else os.path.join(self.base, p)

    def color_params(self, seg) -> dict:
        out = {}
        for rid in seg.get("extra_material_refs", []):
            e = self.effects.get(rid)
            if e and e.get("effect_id") == COLOR_EFFECT_ID and e.get("type") != "color_match":
                out[e["type"]] = round(e.get("value", 0), 4)
        return out

    def effects_of(self, seg) -> list:
        out = []
        for rid in seg.get("extra_material_refs", []):
            e = self.effects.get(rid)
            if not e:
                continue
            eid = e.get("effect_id", "")
            if eid and eid != COLOR_EFFECT_ID:
                out.append(e.get("name") or ("轮廓光" if e.get("type") == "bloom" else e["type"]))
            elif e.get("type") == "color_match":
                out.append(f"AI色彩匹配({e.get('value',0)*100:.0f}%)")
            elif e.get("type") in ("figure", "makeup_root"):
                out.append(e.get("name") or e["type"])
        return out

    def masks_of(self, seg) -> list:
        out = []
        for rid in seg.get("extra_material_refs", []):
            mk = self.masks.get(rid)
            if mk and mk.get("type") == "mask":
                cfg = mk.get("config", {}) or {}
                out.append({
                    "name": mk.get("name", ""),
                    "width": cfg.get("width"),
                    "height": cfg.get("height"),
                    "feather": cfg.get("feather"),
                    "bezier": bool(mk.get("contour_path")),
                })
        return out

    def speed_of(self, seg) -> float:
        for rid in seg.get("extra_material_refs", []):
            sp = self.speeds.get(rid)
            if sp:
                return float(sp.get("speed", 1.0))
        return 1.0

    def fade_of(self, seg):
        for rid in seg.get("extra_material_refs", []):
            f = self.fades.get(rid)
            if f and (f.get("fade_in_duration") or f.get("fade_out_duration")):
                return int(f.get("fade_in_duration") or 0), int(f.get("fade_out_duration") or 0)
        return 0, 0

    def vanim_of(self, seg):
        """视频画面的叠化: 入场「渐显」/出场「渐隐」动画 → (淡入µs, 淡出µs)。
        剪映把这类效果放在 material_animations 里, 不是 audio_fades;
        其它动画(推近/滑动/翻书…)是位移/缩放运动, 不在此处理。"""
        vin = vout = 0
        for rid in seg.get("extra_material_refs", []):
            a = self.anims.get(rid)
            if not a:
                continue
            for an in a.get("animations", []):
                name = an.get("name") or ""
                dur = int(an.get("duration") or 0)
                t = an.get("type")
                if dur <= 0:
                    continue
                if t == "in" and ("FadeIn" in name or "渐显" in name):
                    vin = dur
                elif t == "out" and ("FadeOut" in name or "渐隐" in name):
                    vout = dur
        return vin, vout


def _seg_time(seg):
    """返回 (src_in, src_dur, target_start, target_dur) —— 兼容缺省字段

    关键: target_timerange 只写 duration 不写 start 是剪映的正常写法,
    表示"从 0 开始"(片头第一个片段常见)。以前把缺 start 当占位段跳过,
    导致时间线整体后移 3 秒、片头出现空洞。
    """
    tr = seg.get("target_timerange") or {}
    sr = seg.get("source_timerange") or {}
    if not tr.get("duration"):
        return None
    if tr.get("start") is None and len(tr) > 1:
        return None                     # 有其他字段却没 start: 真占位段
    src_in = int(sr.get("start", 0) or 0)
    src_dur = int(sr.get("duration") or tr["duration"])
    return src_in, src_dur, int(tr.get("start") or 0), int(tr["duration"])


_KF_MAP = {
    "KFTypePositionX": "position_x",
    "KFTypePositionY": "position_y",
    "KFTypeScaleX": "scale_x",
    "KFTypeScaleY": "scale_y",
    "KFTypeRotation": "rotation",
    "KFTypeAlpha": "opacity",
    "KFTypeOpacity": "opacity",
}


def _at(pts, t):
    """在 [(t, v)] 上取 t 时刻的值(线性插值, 越界取端点)"""
    if not pts:
        return None
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return pts[-1][1]


def _keyframes_of(seg, w, h, fit):
    """把剪映 common_keyframes 转成 IR 关键帧(像素 / 原生缩放 / 度)

    剪映属性名: KFTypeScaleX/Y, KFTypePositionX/Y, KFTypeRotation, KFTypeAlpha,
    与 clip.scale / clip.transform 同单位 —— 位置同样是"半个画布", y 轴向上。
    """
    raw = {}
    for kf in seg.get("common_keyframes") or []:
        prop = _KF_MAP.get(kf.get("property_type") or "")
        if not prop:
            continue
        pts = []
        for k in kf.get("keyframe_list") or []:
            vals = k.get("values") or []
            if not vals:
                continue
            pts.append((int(k.get("time_offset") or 0), float(vals[0])))
        if pts:
            pts.sort()
            raw[prop] = pts
    if not raw:
        return {}

    out = {}
    if "position_x" in raw or "position_y" in raw:
        ts = sorted({t for p in ("position_x", "position_y") for t, _ in raw.get(p, [])}
                    or {0})
        xs, ys = raw.get("position_x"), raw.get("position_y")
        out["position"] = [
            (t, (_at(xs, t) or 0.0) * w / 2, -(_at(ys, t) or 0.0) * h / 2) for t in ts]
    if "scale_x" in raw or "scale_y" in raw:
        ts = sorted({t for p in ("scale_x", "scale_y") for t, _ in raw.get(p, [])} or {0})
        sx, sy = raw.get("scale_x"), raw.get("scale_y")
        out["scale"] = [
            (t, (_at(sx or sy, t) or 1.0) * fit, (_at(sy or sx, t) or 1.0) * fit)
            for t in ts]
    if "rotation" in raw:
        out["rotation"] = [(t, v) for t, v in raw["rotation"]]
    if "opacity" in raw:
        out["opacity"] = [(t, min(max(v, 0.0), 1.0)) for t, v in raw["opacity"]]
    return out


def _is_jy_watermark(path: str) -> bool:
    """剪映推广水印: <草稿>/materials/video/水印.png (铺满整条时间线)"""
    p = (path or "").replace("\\", "/")
    return p.endswith("/materials/video/水印.png")


# 剪映的"静音"不是 volume=0, 而是把音量压到 0.001(-60dB), 用 1e-4 当阈值会漏掉
SILENT_VOL = 0.01          # ≈ -40dB, 低于此值当静音, 不生成原声
DEDUP_COVER = 0.60         # 同源音频覆盖该片段 60% 以上 → 声音已在音轨上
DEDUP_SRC_TOL = 500_000    # 源入点相差 ≤0.5s 才算同一段内容(微秒)
DEDUP_SELF = 0.90          # 原声轨之间同源重叠 90% 以上 → 只留一条


_FPS_CANDIDATES = (24, 25, 30, 50, 60, 120)


def infer_fps(d) -> tuple:
    """草稿帧率 -> (fps, 对齐率, 边界数)

    新版剪映(11.x)的 draft_content.json **不再写 fps 字段**(draft_meta_info 也没有),
    旧逻辑 `d.get("fps") or 24` 会把 30fps 工程当 24fps 转 —— 所有剪切点落在
    半帧上, 写出端四舍五入后同轨片段 1 帧重叠, 时间线整体帧率也错。
    这里用全部片段边界反推时间网格: 30fps 工程 1304 个边界 100% 落在整数帧,
    按 24fps 只有 19% —— 区分度足够。同分取最粗的网格(60/120 上 30fps 也全对齐)。
    """
    v = d.get("fps")
    if v:
        try:
            return int(round(float(v))), 1.0, -1   # -1 = 草稿自带, 无需推断
        except (TypeError, ValueError):
            pass
    bounds = []
    for t in d.get("tracks") or []:
        for s in t.get("segments") or []:
            tr = s.get("target_timerange") or {}
            du = tr.get("duration")
            if not du:
                continue
            st = tr.get("start") or 0
            bounds.append(int(st))
            bounds.append(int(st) + int(du))
    if not bounds:
        return 30, 0.0, 0
    best = None                      # (fps, score), 严格大于才替换 -> 同分留小的
    for f in _FPS_CANDIDATES:
        ok = sum(1 for b in bounds
                 if abs(b * f / 1e6 - round(b * f / 1e6)) < 0.02)
        score = ok / len(bounds)
        if best is None or score > best[1] + 1e-9:
            best = (f, score)
    return best[0], best[1], len(bounds)


def read(src: str, name: Optional[str] = None, keep_watermark: bool = False,
         orig_audio: str = "track", dedup_jy: bool = True,
         fps: Optional[int] = None, size: Optional[tuple] = None) -> Timeline:
    """orig_audio: 视频轨自带声音怎么落到 IR
      'track' (默认) = 单独生成 "Vn 原声" 音频轨, 视频片段只带视频
      'video'        = 声音挂在视频片段上(own_audio), 不生成额外音轨
      'off'          = 丢弃原声
    dedup_jy: 剪映音轨里"同一素材、同一段"被放在两条轨上(一条完全被另一条包住、
      速度和淡变都一样、音量都没加强)时, 删掉多余那条 —— 那是重复, 不是创作。
      刻意加强(音量>1)或有不同处理的叠放一律保留。
    size: 目标工程尺寸 (w, h)。默认取草稿 canvas_config。给不同的值时按新画布
      重新算 fit 系数与像素位移（用户确认"工程是 4K 但草稿按 1080 做的"就用这个）。
    """
    d, base = load_draft_json(src)
    ctx = _Ctx(d, base)

    fps_auto = None
    if fps:
        fps = int(round(fps))
    else:
        fps, fps_score, n_bounds = infer_fps(d)
        fps_auto = (fps, fps_score, n_bounds)
    cc = d.get("canvas_config") or {}
    cw = int(cc.get("width", 1920))
    ch = int(cc.get("height", 1080))
    if size:
        w, h = int(size[0]), int(size[1])
    else:
        w, h = cw, ch
    tl = Timeline(
        name=name or os.path.basename(base.rstrip("/\\")),
        width=w,
        height=h,
        fps=fps,
        source_app="jianying",
        video_audio_mode=orig_audio,
    )
    tl.canvas_from_draft = (cw, ch)      # 草稿原本写的画布, 给确认单显示用
    tl.fps_inferred = fps_auto           # (fps, 对齐率, 参与推断的边界数) 或 None
    if size and (w, h) != (cw, ch):
        tl.warnings.append(
            "工程尺寸按 %dx%d 输出 (草稿画布是 %dx%d), 位移与缩放已按新画布重算, "
            "导入后请先看一个带位移的镜头是否还在原位" % (w, h, cw, ch))
    if fps_auto:
        a_fps, a_score, a_n = fps_auto
        if a_n == 0:
            tl.warnings.append(f"草稿里没有帧率字段, 也没有可推断的片段, 按 {a_fps}fps 处理")
        elif a_score < 0.9:
            tl.warnings.append(f"草稿未写帧率, 按片段边界推断为 {a_fps}fps "
                               f"(对齐率 {a_score * 100:.0f}%), 请核对; 不对就用 --fps 指定")

    tracked = 0
    skipped = 0
    no_src_start = 0
    transformed = 0
    jy_watermark = 0
    vol_kf = 0
    orig_audio_n = 0          # 视频轨"自带原声"片段数
    vanim_n = 0               # 视频画面渐显/渐隐(叠化)片段数

    # 剪映的视频片段是**带音频**的: 素材本身有音轨, 音量/淡变挂在 video segment 上。
    # 之前只写视频轨(srcEnable="video"), 原声就整条消失了。这里按下面的规则还原:
    #   mat.has_sound_separated = True  → 音轨已被"分离音频"搬到独立音频段, 视频轨不再带声音
    #   segment.volume ≈ 0              → 剪映里静音, 不要写
    #   素材无音频流                     → 不写
    # 其余情况生成一条"Vn 原声"音频轨, 片段与视频片段严格同位同长。
    audio_cache = {}
    rep_audio = orig_audio != "off"       # 是否还原视频片段自带的声音

    def _has_audio(path):
        if path not in audio_cache:
            audio_cache[path] = has_audio_stream(path)
        return audio_cache[path]

    # 视频轨: 轨序 0 = 底层 (render_index 优先)
    vts = [t for t in d.get("tracks", []) if t.get("type") == "video"]
    # render_index 只有"每条轨一段连续、轨与轨不重叠"时才代表图层序。
    # 新版剪映(11.x)把它当全局自增号用: 同一段 1..20 散落在各条轨上,
    # 按 min() 排序会把轨序搅乱(e.g. 36 段轨与 18 段轨互换)。
    # 区间有交叉就退回草稿数组顺序(数组序=界面从上到下, 与既有实测一致)。
    def _ri_range(t):
        v = [s.get("render_index") for s in t.get("segments") or []
             if s.get("render_index") is not None]
        return (min(v), max(v)) if v else None
    rng = [_ri_range(t) for t in vts]
    if len(vts) > 1 and all(rng):
        order = sorted(range(len(vts)), key=lambda i: rng[i][0])
        if all(rng[order[k]][1] < rng[order[k + 1]][0]
               for k in range(len(order) - 1)):
            vts = [vts[i] for i in order]

    orig_tracks = []          # 视频轨原声, 排在剪映音轨之后追加
    for vi, vt in enumerate(vts):
        track = Track(kind="video", name=vt.get("name", ""))
        orig = Track(kind="audio", name=f"V{vi + 1} 原声")
        for seg in sorted(vt.get("segments", []),
                          key=lambda s: (s.get("target_timerange") or {}).get("start", 0)):
            tm = _seg_time(seg)
            if tm is None:
                skipped += 1
                continue
            src_in, src_dur, tstart, tdur = tm
            if not (seg.get("source_timerange") or {}).get("start"):
                no_src_start += 1
            mat = ctx.videos.get(seg.get("material_id"))
            if not mat:
                skipped += 1
                continue
            path = ctx.path_of(mat)
            # 剪映自带的"我在剪辑, 低于 x 元/天"推广水印: 位于 <草稿>/materials/video/水印.png,
            # 铺满整条时间线。它不是用户内容, 带进成片就是事故。
            if _is_jy_watermark(path) and not keep_watermark:
                jy_watermark += 1
                continue
            mid = mat["id"]
            is_photo = mat.get("type") == "photo"
            if mid not in tl.media:
                tl.add_media(Media(
                    id=mid, path=path, kind="image" if is_photo else "video",
                    duration_us=int(mat.get("duration") or 0),
                    width=int(mat.get("width") or 0), height=int(mat.get("height") or 0),
                    # 剪映 videos 素材没有 has_audio 字段, 一律靠 ffprobe 探
                    has_audio=(not is_photo) and _has_audio(path) is not False,
                    name=mat.get("material_name") or os.path.basename(path),
                    missing=not os.path.exists(path),
                ))
            vol = float(seg.get("volume", 1) or 1)
            clip = seg.get("clip") or {}
            sc = clip.get("scale") or {}
            tr2 = clip.get("transform") or {}
            fl = clip.get("flip") or {}
            fit = _fit_factor(tl.width, tl.height,
                              int(mat.get("width") or 0), int(mat.get("height") or 0))
            tf = Transform(
                # 剪映: transform 单位为"半个画布宽/高", y 轴向上(负值向下)
                x=float(tr2.get("x", 0)) * tl.width / 2,
                y=-float(tr2.get("y", 0)) * tl.height / 2,
                # 剪映: scale 1.0 = 适应画布后的尺寸, 需乘适配系数还原成原生像素比例
                scale_x=float(sc.get("x", 1) or 1) * fit,
                scale_y=float(sc.get("y", 1) or 1) * fit,
                fit_x=fit, fit_y=fit,
                rotation=float(clip.get("rotation", 0) or 0),
                flip_h=bool(fl.get("horizontal")),
                flip_v=bool(fl.get("vertical")),
                opacity=float(clip.get("alpha", 1) or 1),
            )
            if not tf.is_identity():
                transformed += 1
            sp = ctx.speed_of(seg)
            fade_in, fade_out = ctx.fade_of(seg)
            v_in, v_out = ctx.vanim_of(seg)
            if v_in or v_out:
                vanim_n += 1
            kfs = _keyframes_of(seg, tl.width, tl.height, fit)
            if kfs:
                transformed += 1
            # 视频片段自带的声音(剪映: 视频轨片段本来就有音轨)
            # ⚠️ has_sound_separated 只在"分离音频"时由剪映打标, 用户把同一文件
            # 另外拖到音轨(提取音频/手动复制)时并不打标 —— 那种情况靠后面的
            # "同源覆盖"去重兜底, 否则同一段声音会响两遍。
            own_audio = (rep_audio and not is_photo and vol >= SILENT_VOL
                         and not mat.get("has_sound_separated")
                         and _has_audio(path) is not False)
            # 淡变是"这段声音"的属性: 声音单独成轨时, 淡变跟着音频片段走,
            # 视频片段本身不该再挂一个调不到东西的音量参数
            vfi, vfo = ((0, 0) if (own_audio and orig_audio == "track")
                        else (fade_in, fade_out))
            track.clips.append(Clip(
                media_id=mid, start=tstart, dur=tdur,
                src_in=src_in, src_dur=src_dur, speed=sp,
                transform=tf, volume=vol,
                fade_in=vfi, fade_out=vfo,
                vfade_in=v_in, vfade_out=v_out,
                own_audio=own_audio,
                grade=ctx.color_params(seg),
                effects=ctx.effects_of(seg),
                masks=ctx.masks_of(seg),
                label=mat.get("material_name") or "",
                source_app="jianying",
                kfs=kfs,
            ))
            tracked += 1

            # 模式 track: 同轨同位的独立音频片段, 后面单独成一条 "Vn 原声" 音轨
            if own_audio and orig_audio == "track":
                orig.clips.append(Clip(
                    media_id=mid, start=tstart, dur=tdur,
                    src_in=src_in, src_dur=src_dur, speed=sp,
                    volume=vol, fade_in=fade_in, fade_out=fade_out,
                    label=mat.get("material_name") or "",
                    source_app="jianying",
                ))
                orig_audio_n += 1
        tl.tracks.append(track)
        if orig.clips:
            orig_tracks.append(orig)

    # 音频轨
    for at in [t for t in d.get("tracks", []) if t.get("type") == "audio"]:
        track = Track(kind="audio", name=at.get("name", ""))
        for seg in sorted(at.get("segments", []),
                          key=lambda s: (s.get("target_timerange") or {}).get("start", 0)):
            tm = _seg_time(seg)
            if tm is None:
                skipped += 1
                continue
            src_in, src_dur, tstart, tdur = tm
            mat = ctx.audios.get(seg.get("material_id"))
            if not mat:
                skipped += 1
                continue
            path = ctx.path_of(mat)
            mid = mat["id"]
            if mid not in tl.media:
                tl.add_media(Media(
                    id=mid, path=path, kind="audio",
                    duration_us=int(mat.get("duration") or 0),
                    name=mat.get("name") or os.path.basename(path),
                    missing=not os.path.exists(path),
                ))
            fade_in, fade_out = ctx.fade_of(seg)
            # 音量关键帧(剪映的"音量包络"/自动闪避): 交换格式里表示成本高且兼容性差,
            # 这里只报出来让人手动补, 不做会错的近似。
            vpts = None
            for _kf in (seg.get("common_keyframes") or []):
                if _kf.get("property_type") == "KFTypeVolume":
                    vpts = [(int(p.get("time_offset") or 0), float((p.get("values") or [0])[0]))
                            for p in (_kf.get("keyframe_list") or []) if p.get("values")]
            if vpts:
                vol_kf += 1
                tl.manual_items.append(
                    f"音量关键帧 {os.path.basename(path)} @{tstart/1e6:.2f}s: "
                    + " → ".join(f"{t/1e6:.2f}s {v*100:.0f}%" for t, v in sorted(vpts)))
            track.clips.append(Clip(
                media_id=mid, start=tstart, dur=tdur,
                src_in=src_in, src_dur=src_dur, speed=ctx.speed_of(seg),
                volume=float(seg.get("volume", 1) or 1),
                fade_in=fade_in, fade_out=fade_out,
                label=mat.get("name") or "",
                source_app="jianying",
            ))
            tracked += 1
        tl.tracks.append(track)

    # ── 原声去重 ──────────────────────────────────────────────────────
    # 剪映里同一段声音可能有三种"已存在"的形态, 不加判断就会双声:
    #   ① 视频轨片段自带音轨 + 音轨上另有同源音频段(提取音频/手动拖入)
    #   ② 两条视频轨叠放同一素材(画中画), 两层都带声音
    #   ③ 音量被压到 0.001 的"静音"片段
    # 剪映音轨本身是用户主动排的, 一律不动; 只清理我们"补"出来的原声。
    def _np(p):
        return os.path.normcase(os.path.normpath(p or "")).replace("\\", "/")

    jy_src = []       # 剪映音轨: (路径, 起点, 终点, 源入点, clip)
    for t in tl.tracks:
        if t.kind != "audio":
            continue
        for c in t.clips:
            jy_src.append((_np(tl.media[c.media_id].path), c.start, c.end,
                           c.src_in, c))

    dropped_dup = 0   # 音轨上已有同源声音
    dropped_sil = 0   # 静音
    dropped_self = 0  # 与其他原声轨重复
    seen_orig = []    # 已成活的原声: (路径, 起点, 终点, 源入点)
    kept = []
    for ot in orig_tracks:
        new = []
        for c in ot.clips:
            np_ = _np(tl.media[c.media_id].path)
            if c.volume < SILENT_VOL:
                dropped_sil += 1
                continue
            cov = 0
            for ap, a0, a1, asrc, _ac in jy_src:
                if ap != np_ or abs(c.src_in - asrc) > DEDUP_SRC_TOL:
                    continue
                ov = min(c.end, a1) - max(c.start, a0)
                if ov > 0:
                    cov += ov
            if c.dur > 0 and cov / c.dur >= DEDUP_COVER:
                dropped_dup += 1
                continue
            if any(kp == np_ and abs(c.src_in - ksrc) <= DEDUP_SRC_TOL
                   and c.dur > 0
                   and (min(c.end, k1) - max(c.start, k0)) / c.dur >= DEDUP_SELF
                   for kp, k0, k1, ksrc in seen_orig):
                dropped_self += 1
                continue
            seen_orig.append((np_, c.start, c.end, c.src_in))
            new.append(c)
        ot.clips = new
        if new:
            kept.append(ot)
    orig_tracks = kept
    orig_audio_n = sum(len(t.clips) for t in orig_tracks)

    # 剪映音轨自己就存在同源叠放(同一素材、同一起点、两条轨都出 1.0 音量)。
    # 这是源工程的结构, 不擅自删 —— 有些是"叠一层加强"(音量 >1), 删了就变味。
    # 只列出来, 让人自己在剪映/达芬奇里决定。
    jy_dup = []
    drop_ids = set()
    for i in range(len(jy_src)):
        p1, s1, e1, r1, c1 = jy_src[i]
        for j in range(i + 1, len(jy_src)):
            p2, s2, e2, r2, c2 = jy_src[j]
            if p1 != p2 or abs(c1.speed - c2.speed) > 1e-3:
                continue
            # 判据是"同一时刻播的是不是同一段源内容", 不是源入点接近:
            # 剪映里把一段声音切成几刀后, 每刀的 src_in 都不同, 但内容是续着的。
            drift = (s2 - s1) - (r2 - r1) / max(c1.speed, 1e-6)
            if abs(drift) > 41_667:          # 偏离超过 1 帧 = 内容对不上
                continue
            ov = min(e1, e2) - max(s1, s2)
            if ov >= 500_000:
                jy_dup.append((p1, max(s1, s2), ov))
                # 只有"一条被另一条完全包住 + 处理参数一致"才敢删:
                #   部分交叠可能是刻意的交叉/叠化; 音量不同说明是另一种处理。
                inner = None
                if s1 >= s2 and e1 <= e2 and e1 - s1 < e2 - s2:
                    inner = c1
                elif s2 >= s1 and e2 <= e1 and e2 - s2 < e1 - s1:
                    inner = c2
                if (dedup_jy and inner is not None
                        and abs(c1.volume - c2.volume) < 0.05
                        and c1.fade_in == c2.fade_in and c1.fade_out == c2.fade_out):
                    drop_ids.add(id(inner))
    if drop_ids:
        for t in tl.tracks:
            t.clips = [c for c in t.clips if id(c) not in drop_ids]
        tracked -= len(drop_ids)
    if jy_dup:
        if drop_ids:
            tail = f"已删除其中 {len(drop_ids)} 条"
            rest = len(jy_dup) - len(drop_ids)
            if rest > 0:
                tail += f"; 另有 {rest} 处处理参数不同(音量/淡变/速度), 保留不动"
        else:
            tail = "已原样保留 —— 若听着有重影, 加 --dedup-jy 或去剪映里删掉其中一条"
        tl.warnings.append(
            f"剪映音轨本身有 {len(jy_dup)} 处同源叠放(同一素材同一段落在两条音轨上), "
            + tail + ": "
            + ", ".join(f"{s/1e6:.1f}s({ov/1e6:.1f}s)" for _, s, ov in
                        sorted(jy_dup, key=lambda x: x[1])[:6]))
    jy_dup_n = len(jy_dup)

    # 视频轨原声: 插到音轨区**最前**(A1..Ak), 剪映自己的音轨依次往后 —— 与剪映
    # 界面上下顺序一致, 且 IR 的 audio_tracks() 顺序 == 达芬奇 A 轨顺序,
    # fadesync/报告/轨道对账全部自动跟随(2026-11-16 用户要求, 不再追加到末尾)
    if orig_tracks:
        first_audio = next((i for i, t in enumerate(tl.tracks)
                            if t.kind == "audio"), len(tl.tracks))
        tl.tracks[first_audio:first_audio] = orig_tracks

    # 文本轨
    for tt in [t for t in d.get("tracks", []) if t.get("type") == "text"]:
        track = Track(kind="text", name=tt.get("name", ""))
        for seg in sorted(tt.get("segments", []),
                          key=lambda s: (s.get("target_timerange") or {}).get("start", 0)):
            tm = _seg_time(seg)
            if tm is None:
                continue
            _, _, tstart, tdur = tm
            mat = ctx.texts.get(seg.get("material_id"))
            if not mat:
                continue
            try:
                content = json.loads(mat.get("content", "{}"))
            except (json.JSONDecodeError, TypeError):
                content = {}
            track.clips.append(Clip(
                media_id=mat["id"], start=tstart, dur=tdur,
                src_in=0, src_dur=tdur, text=_strip_tags(content.get("text", "")),
                source_app="jianying", label="text",
            ))
        tl.tracks.append(track)

    # 剪映转场(推镜变焦/闪黑/闪白/叠化/炫光…)
    # ⚠️ 位置: 剪映把转场材料挂在**宿主片段的尾部**(= 与下一段的接缝), 不是起点。
    #    四条独立证据: V1#0 / V8#0 根本没有前段; V4 两处宿主起点前分别有
    #    13.2s / 9.0s 空档 —— 转场在起点物理上不可能。
    # 写法: 只有"叠化/溶解/闪黑"能用 FCPXML <transition> 表达(见 w_fcpxml), 其余
    #    (推镜变焦/放射/炫光/闪白)语义落在特效上, 只能人工或 BCC 重建。
    trs = {m.get("id"): m for m in (d.get("materials") or {}).get("transitions") or []}
    if trs:
        for t in d.get("tracks") or []:
            if t.get("type") != "video":
                continue
            for seg in t.get("segments") or []:
                for r in seg.get("extra_material_refs") or []:
                    m = trs.get(r)
                    if not m:
                        continue
                    tg = seg.get("target_timerange") or {}
                    st = tg.get("start") or 0
                    du = tg.get("duration") or 0
                    tl.transitions.append({
                        "at": st + du,
                        "dur": m.get("duration") or 0,
                        "name": m.get("name") or "转场",
                        "host_start": st,
                        "host_dur": du,
                    })
    if tl.transitions:
        fps_ = tl.fps or 24
        lst = "; ".join(
            f"{x['at'] / 1e6:.2f}s(帧{round(x['at'] * fps_ / 1e6)}) {x['name']}"
            for x in sorted(tl.transitions, key=lambda x: x["at"]))
        tl.warnings.append(
            f"剪映转场 {len(tl.transitions)} 处(挂在宿主段尾部): {lst}")

    # 提示
    if skipped:
        tl.warnings.append(f"跳过 {skipped} 个无效/无素材片段（剪映占位段或空轨）")
    if no_src_start:
        tl.warnings.append(
            f"{no_src_start} 个片段 source.start 缺省，已按 0 处理（剪映格式约定）")
    if vol_kf:
        tl.warnings.append(
            f"{vol_kf} 个音频片段带音量关键帧(音量包络/闪避), 未写入交换文件, "
            "已列在报告的待人工清单里")
    if jy_watermark:
        tl.warnings.append(
            f"已剔除剪映自带的推广水印层（materials/video/水印.png, {jy_watermark} 个片段）；"
            "需要保留时加 --keep-watermark")
    if orig_audio_n or dropped_dup or dropped_sil or dropped_self:
        msg = f"视频轨自带原声: 还原 {orig_audio_n} 个片段"
        if dropped_dup:
            msg += f"；{dropped_dup} 个因音轨上已有同源声音而跳过（否则同一段声音响两遍）"
        if dropped_self:
            msg += f"；{dropped_self} 个因两条视频轨叠放同一素材只保留一条"
        if dropped_sil:
            msg += f"；{dropped_sil} 个音量≈静音未还原"
        tl.warnings.append(msg)
    tl.stats = {"clips": tracked, "skipped": skipped, "transformed": transformed,
                "watermark_skipped": jy_watermark,
                "orig_audio": orig_audio_n, "dedup_audio": dropped_dup,
                "dedup_self": dropped_self, "silent_audio": dropped_sil,
                "jy_dup_audio": jy_dup_n, "vanim": vanim_n}
    return tl
