# -*- coding: utf-8 -*-
"""淡变同步 / 对账(达芬奇 21.1+ 脚本 API)

FCPXML 导入达芬奇会丢掉两类东西:
  1. 声音淡变(audio_fades 材料)     —— FCPXML 里写了 adjust-volume 参数, 达芬奇不读
  2. 画面叠化(剪映 动画 的「渐显/渐隐」) —— FCPXML 根本没有对应的写法
另外视频片段若没有自带声音(图片/无音频流素材), 它的声音淡变也挂在视频片段上。

Resolve 21.1 起提供 TimelineItem.SetFades/GetFades, 本模块用它:
  - sync():  按剪映源把全部淡变写进当前时间线, 写完逐条读回核对
  - audit(): 只读对账 —— 淡变逐条比对 + 片段启用状态(GetClipEnabled)

需要达芬奇正在运行且已打开目标时间线; 首选项 > 系统 > 常规 >
「外部脚本使用」需设为「本地」。
"""
import os
import sys


def _connect():
    os.environ.setdefault(
        "RESOLVE_SCRIPT_API",
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting")
    os.environ.setdefault(
        "RESOLVE_SCRIPT_LIB",
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
    sys.path.append(os.environ["RESOLVE_SCRIPT_API"] + "\\Modules")
    import DaVinciResolveScript as dvr  # noqa: E402
    r = dvr.scriptapp("Resolve")
    if r is None:
        raise RuntimeError(
            "连不上达芬奇: 请确认达芬奇已启动, 且 首选项>系统>常规>"
            "「外部脚本使用」设为「本地」(改完需重启达芬奇)")
    return r


def _expected(tl_src, fps):
    """期望淡变表: (轨类型, 轨号1起, 起点帧) -> (淡入帧, 淡出帧)"""
    fr = lambda us: int(round(us * fps / 1_000_000))
    exp = {}
    for i, t in enumerate([x for x in tl_src.video_tracks() if x.clips], 1):
        for c in t.clips:
            # ⚠️ 视频片段自身的 fade_in/fade_out 是"声音淡化"。track 模式下
            # 声音已由独立音轨承载(hasAudio=0), 片段上残留的淡变是剪映的
            # 死数据 —— 播放不生效(2026-11-16 用户实测确认), 写上去会让
            # 达芬奇凭空生成画面淡化手柄。画面淡化只认 vfade(动画面板
            # 「渐显/渐隐」)。
            fi = fr(getattr(c, "vfade_in", 0))
            fo = fr(getattr(c, "vfade_out", 0))
            if fi or fo:
                exp[("video", i, fr(c.start))] = (fi, fo)
    for i, t in enumerate([x for x in tl_src.audio_tracks() if x.clips], 1):
        for c in t.clips:
            fi, fo = fr(c.fade_in), fr(c.fade_out)
            if fi or fo:
                exp[("audio", i, fr(c.start))] = (fi, fo)
    return exp


def _iter_timeline(tl):
    for tt in ("video", "audio"):
        for n in range(1, 41):
            try:
                items = tl.GetItemListInTrack(tt, n)
            except Exception:
                break
            if items is None:
                break
            for it in items:
                yield tt, n, it


def sync(tl_src, audit_only=False, log=print):
    """把剪映源的淡变写进达芬奇当前时间线; audit_only=True 时只对账不动手。

    tl_src: nlebridge IR Timeline (jy_read.read 的返回值)
    返回 (成功数, 失败数, 日志行列表)
    """
    r = _connect()
    proj = r.GetProjectManager().GetCurrentProject()
    tl = proj.GetCurrentTimeline()
    if tl is None:
        raise RuntimeError("达芬奇里没有打开的时间线 —— 先双击目标时间线")
    log(f"工程: {proj.GetName()}  时间线: {tl.GetName()}  达芬奇: {r.GetVersionString()}")

    fps = tl_src.fps
    exp = _expected(tl_src, fps)
    log(f"源里带淡变的片段: {len(exp)}")

    idx = {}
    trans = {}                     # (轨类型, 轨号, start, end) -> 转场 item
    fb = {}                        # (轨类型, start) -> [(轨号, item)] 轨号错位时的退路
    for tt, n, it in _iter_timeline(tl):
        try:
            is_trans = it.GetType() == "transition"
        except Exception:
            is_trans = False
        if is_trans:
            trans[(tt, n, int(it.GetStart()), int(it.GetEnd()))] = it
        else:
            idx[(tt, n, int(it.GetStart()))] = it
            fb.setdefault((tt, int(it.GetStart())), []).append((n, it))
    log(f"时间线片段总数: {len(idx)}  (另有转场 {len(trans)} 个)")

    def _video_trans_match(key, fi, fo):
        """视频淡变 -> 检查起/止转场是否存在且时长一致; 返回 (命中数, 缺失描述)"""
        hit, miss = 0, []
        it = idx.get(key)
        seg_end = int(it.GetEnd()) if it else None
        if fi:
            t = trans.get((key[0], key[1], key[2], key[2] + fi))
            if t is not None:
                hit += 1
            else:
                miss.append(f"入场转场{fi}帧缺失")
        if fo:
            t = trans.get((key[0], key[1], seg_end - fo, seg_end)) \
                if seg_end else None
            if t is not None:
                hit += 1
            else:
                miss.append(f"出场转场{fo}帧缺失")
        return hit, miss

    ok = fail = 0
    for key, (fi, fo) in sorted(exp.items()):
        it = idx.get(key)
        if it is None:
            # 轨号错位: 草稿里只有 1 段的短音轨转换时可能被并进别的轨,
            # 使后续轨整体前移。退化为「同起点、轨号最接近」匹配。
            cands = fb.get((key[0], key[2])) or []
            if cands:
                n2, it2 = min(cands, key=lambda x: abs(x[0] - key[1]))
                it = it2
                log(f"  · 轨号校正 {key[0]}{key[1]}@{key[2]} -> {key[0]}{n2}")
            else:
                log(f"  ✗ 未找到 {key[0]}{key[1]} @{key[2]}")
                fail += 1
                continue
        if key[0] == "video":
            # ⚠️ 达芬奇 21.1 的 SetFades 对视频片段不持久化(保存工程后丢失,
            # 2026-11-16 实测), 画面渐显/渐隐一律用 Cross Dissolve 转场。
            hit, miss = _video_trans_match(key, fi, fo)
            if audit_only:
                if not miss:
                    ok += hit
                else:
                    log(f"  ✗ {key[0]}{key[1]} @{key[2]}  " + "; ".join(miss))
                    fail += 1
                continue
            added = True
            jobs = []
            if fi:
                jobs.append((key[2], key[2] + fi, fi, "start"))
            if fo:
                e = int(it.GetEnd())
                jobs.append((e - fo, e, fo, "end"))
            for (t0, t1, dur, pos) in jobs:
                if (key[0], key[1], t0, t1) in trans:
                    continue
                res = it.AddTransition({"type": "Cross Dissolve",
                                        "category": "simple",
                                        "position": pos,
                                        "alignment":
                                            "right" if pos == "start" else "left",
                                        "duration": dur})
                if res is None and (key[0], key[1], t0, t1) not in trans:
                    added = False
                    log(f"  ✗ {key[0]}{key[1]} @{key[2]} {pos}转场{dur}帧写入失败")
            if added:
                ok += 1
            else:
                fail += 1
            continue
        if audit_only:
            got = it.GetFades() or {}
            gi = int(got.get("FadeIn", 0) or 0)
            go = int(got.get("FadeOut", 0) or 0)
            if (gi, go) == (fi, fo):
                ok += 1
            else:
                log(f"  ✗ 不一致 {key[0]}{key[1]} @{key[2]} 期望({fi},{fo}) 实际({gi},{go})")
                fail += 1
            continue
        cur = it.GetFades() or {}
        want = {"FadeIn": int(cur.get("FadeIn", 0) or 0),
                "FadeOut": int(cur.get("FadeOut", 0) or 0)}
        if fi:
            want["FadeIn"] = fi
        if fo:
            want["FadeOut"] = fo
        okset = it.SetFades(want)
        back = it.GetFades() or {}
        gi = int(back.get("FadeIn", 0) or 0)
        go = int(back.get("FadeOut", 0) or 0)
        if okset and gi == want["FadeIn"] and go == want["FadeOut"]:
            ok += 1
        else:
            log(f"  ✗ {key[0]}{key[1]} @{key[2]} 写入={okset} "
                f"读回 in={gi}/{want['FadeIn']} out={go}/{want['FadeOut']}")
            fail += 1
    log(f"淡变: {'对账' if audit_only else '写入'}完成: 一致 {ok} / 问题 {fail}")
    return ok, fail, idx


def audit_enabled(idx, log=print):
    """报告片段启用状态(21.1 GetClipEnabled): 停用的片段灰显、不发声不发画面"""
    disabled = []
    for key, it in idx.items():
        try:
            if it.GetClipEnabled() is False:
                disabled.append(key)
        except Exception:
            pass
    if disabled:
        log(f"\n片段启用状态: {len(disabled)} 个被停用(时间线上灰显, 播放/导出无画面无声音):")
        for tt, n, st in sorted(disabled):
            nm = ""
            try:
                nm = idx[(tt, n, st)].GetName()
            except Exception:
                pass
            log(f"  - {tt}{n} @{st}  {nm[:40]}")
        log("  (若为有意停用可忽略; 若不是: 选中片段右键 > 启用片段)")
    else:
        log("\n片段启用状态: 全部启用, 无停用片段")
    return disabled


def audit_structure(tl_src, idx, fps, log=print):
    """顺带对账结构: 逐轨起点+时长集合比对, 给出差异"""
    fr = lambda us: int(round(us * fps / 1_000_000))
    diffs = 0
    for kind, tracks in (("video", tl_src.video_tracks()),
                         ("audio", tl_src.audio_tracks())):
        tracks = [t for t in tracks if t.clips]
        for i, t in enumerate(tracks, 1):
            src = sorted((fr(c.start), fr(c.dur)) for c in t.clips)
            got = sorted((int(it.GetStart()), int(it.GetEnd()) - int(it.GetStart()))
                         for (tt, n, _), it in idx.items() if tt == kind and n == i)
            if src != got:
                diffs += 1
                log(f"  ✗ {kind.upper()}{i} [{t.name or ' unnamed'}]: "
                    f"{len(got)}/{len(src)} 段, 与源不一致")
    if diffs == 0:
        log("结构对账: 全部轨道逐条一致")
    return diffs


def run(src, orig_audio="track", audit_only=False, log=print, fps=None):
    """入口: src=剪映草稿目录; 先读 IR 再连达芬奇执行"""
    from nlebridge import jy_read
    tl_src = jy_read.read(src, orig_audio=orig_audio, fps=fps)
    ok, fail, idx = sync(tl_src, audit_only=audit_only, log=log)
    audit_enabled(idx, log=log)
    try:
        audit_structure(tl_src, idx, tl_src.fps, log=log)
    except Exception as e:  # 结构对账失败不影响淡变结果
        log(f"结构对账出错(忽略): {e}")
    return ok, fail
