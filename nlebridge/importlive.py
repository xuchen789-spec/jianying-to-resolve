# -*- coding: utf-8 -*-
"""把转换产物直接导入达芬奇时间线（在线写操作）。

convert 一直只产离线文件，导入是手工三步：

    删同名旧时间线  ->  File > Import > Timeline  ->  在媒体池里双击打开

这三步没有任何反馈：删错、导入成 "xxx 1"、导入后开着的是别的时间线，
界面照旧一切正常。本模块把它们串起来，每步都回读确认。

用到三个此前从未调用过的 API（21.1 只读探测均为 callable，但从未真跑过）：
    Project.SetCurrentTimeline(timeline)
    MediaPool.ImportTimelineFromFile(path, {options})
    MediaPool.DeleteTimelines([timeline, ...])

**这是写操作**：会在当前打开的工程里新建时间线、并删除同名旧时间线。
已手工精修过的工程不要重复跑 —— 会多出一条同名（或 "xxx 1"）的时间线，
而且旧的那条（带调色/转场/蒙版的）已经被删掉了。
"""
from __future__ import annotations

import os
import re
import time


def connect(log=print):
    """连到达芬奇并取到 (resolve, projectManager, project, mediaPool)。

    与 postsync 的区别：这里**不要求已经打开时间线** —— 空工程也能导入。
    """
    from .fadesync import _connect
    try:
        r = _connect()
    except Exception as e:
        log("✗ 连不上达芬奇: %s" % e)
        return None
    if r is None:
        log("✗ 连不上达芬奇 (脚本 API 未就绪)。")
        log("  检查: 偏好设置 → 系统 → 常规 → 外部脚本使用 = 本地。")
        return None
    try:
        pm = r.GetProjectManager()
        proj = pm.GetCurrentProject() if pm else None
    except Exception as e:
        log("✗ 取不到项目管理器: %s" % e)
        return None
    if proj is None:
        log("✗ 达芬奇里没有打开的项目。先打开目标工程再导入。")
        return None
    mp = proj.GetMediaPool()
    if mp is None:
        log("✗ 取不到 MediaPool。")
        return None
    return r, pm, proj, mp


def _api_ok(obj, name):
    """坑 22: dir() 里有名字不代表能调, 必须 callable。"""
    return callable(getattr(obj, name, None))


def list_timelines(proj):
    """[{name, start, end, obj}]；拿不到就返回空表。"""
    rows = []
    try:
        n = int(proj.GetTimelineCount())
    except Exception:
        return rows
    for i in range(1, n + 1):
        try:
            t = proj.GetTimelineByIndex(i)
            if t is None:
                continue
            rows.append({"name": t.GetName(),
                         "start": int(t.GetStartFrame()),
                         "end": int(t.GetEndFrame()),
                         "obj": t})
        except Exception:
            continue
    return rows


def _current(proj):
    try:
        return proj.GetCurrentTimeline()
    except Exception:
        return None


def _dup_names(rows, name):
    """达芬奇重复导入会在名字后面加 " 1"/" 2"（也可能是 " (1)"）。"""
    pat = re.compile(r"^%s(?:\s+\d+|\s*\(\d+\))$" % re.escape(name))
    return [x for x in rows if pat.match(x["name"])]


def _delete_timeline(proj, mp, tl, log=print):
    """删一条时间线；删的是当前打开那条时先切走（否则容易失败/留下怪状态）。"""
    name = tl.GetName()
    cur = _current(proj)
    try:
        if cur is not None and cur.GetName() == name:
            others = [x for x in list_timelines(proj) if x["name"] != name]
            if others and _api_ok(proj, "SetCurrentTimeline"):
                if not proj.SetCurrentTimeline(others[0]["obj"]):
                    log("  ! 切到 %r 失败, 删当前时间线可能不成功" % others[0]["name"])
            else:
                log("  ! %r 是工程里唯一的时间线, 且正开着; 直接尝试删除" % name)
    except Exception:
        pass

    ok = False
    err = ""
    f = getattr(mp, "DeleteTimelines", None)
    if callable(f):
        for arg in ([tl], tl):        # 新版本吃 list, 老写法单个
            try:
                ok = bool(f(arg))
                if ok:
                    break
            except Exception as e:
                err = str(e)
    else:
        err = "MediaPool 没有 DeleteTimelines（需 18.6+）"

    gone = not any(x["name"] == name for x in list_timelines(proj))
    return gone, ("" if gone else (err or "API 返回成功但回读仍在"))


def _backup(r, tl, near_path, log=print):
    """删之前把旧时间线导出成 .drt（保 Fusion comp / 转场 / 调色），退而求其次 fcpxml。

    为什么必须备份：删旧和导入是两次调用，中间没有事务。旧的一删掉、导入再失败，
    用户工程里那条时间线就没了 —— 这不是"重跑一次"能恢复的。
    返回备份文件路径，失败返回 None（此时调用方应中止，不动旧时间线）。
    """
    name = "?"
    try:
        name = tl.GetName()
    except Exception:
        pass
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    d = os.path.dirname(os.path.abspath(near_path)) or "."
    ts = time.strftime("%Y%m%d-%H%M%S")
    cands = []
    for attr, ext in (("EXPORT_DRT", ".drt"), ("EXPORT_FCPXML_1_10", ".fcpxml"),
                      ("EXPORT_FCPXML_1_9", ".fcpxml")):
        t = getattr(r, attr, None)
        if t is not None:
            cands.append((t, ext))
    if not cands:
        log("  ! 这版 Resolve 没有可用的导出常量, 无法备份旧时间线")
        return None
    for etype, ext in cands:
        path = os.path.join(d, "_backup_%s_%s%s" % (safe, ts, ext))
        try:
            ok = tl.Export(path, etype)
        except Exception as e:
            log("  ! 备份失败(%s): %s" % (ext, e))
            continue
        if ok and os.path.isfile(path) and os.path.getsize(path) > 0:
            log("  已备份旧时间线 → %s" % path)
            return path
        log("  ! 备份没产出文件(%s)" % ext)
    return None


def import_file(path, name=None, keep_old=False, expect_frames=None,
                expect_tracks=None, backup_dir=None, log=print):
    """导入一个时间线文件（fcpxml / xml / aaf / otio）到当前工程并打开它。

    path          转换产物（如 xxx_resolve.fcpxml）
    name          期望的时间线名；默认取文件名里的主体
    keep_old      True = 不删同名旧时间线（导入会变成 "名字 1"，一般不该用）
    expect_frames 期望帧数（草稿总帧）；对不上只警告 —— 但通常是导入漏段
    expect_tracks (视频轨数, 音频轨数)；对不上只警告
    backup_dir    删旧前的备份放哪；默认跟产物同目录

    返回 {"ok", "name", "frames", "deleted": [...], "backup", "warnings": [...], "error"}
    """
    res = {"ok": False, "name": "", "frames": 0, "deleted": [], "backup": None,
           "restored": False, "warnings": [], "error": None}

    if not os.path.isfile(path):
        res["error"] = "找不到文件: %s" % path
        return res

    name = name or os.path.splitext(os.path.basename(path))[0]

    conn = connect(log=log)
    if conn is None:
        res["error"] = "连不上达芬奇"
        return res
    r, pm, proj, mp = conn

    if not _api_ok(mp, "ImportTimelineFromFile"):
        res["error"] = ("这版达芬奇的 MediaPool 没有 ImportTimelineFromFile"
                        "（需 18.6+），只能手工 File > Import > Timeline")
        return res

    def _try_restore():
        """导入失败时把删掉的旧时间线救回来。救不回也要把备份路径报出来。"""
        if not res["backup"]:
            return
        log("  旧时间线已删除, 尝试从备份恢复: %s" % res["backup"])
        try:
            t = mp.ImportTimelineFromFile(res["backup"],
                                          {"timelineName": name,
                                           "importSourceClips": False})
            if t is not None or any(x["name"] == name for x in list_timelines(proj)):
                log("  ✓ 已恢复旧时间线（内容以备份为准）")
                res["restored"] = True
                return
        except Exception as e:
            log("  ! 恢复失败: %s" % e)
        log("  ! 没能自动恢复。备份文件还在, 手工 File > Import > Timeline 导回去:")
        log("      %s" % res["backup"])

    before = list_timelines(proj)
    log("工程: %s   现有时间线 %d 条" % (_proj_name(pm), len(before)))

    # ---- 1) 删同名旧时间线（不删的话导入会自动改名成 "xxx 1"）----
    same = [x for x in before if x["name"] == name]
    dups = _dup_names(before, name)
    if same and keep_old:
        log("  ! 已有同名时间线 %r, --keep-old 所以不删；导入后会变成 %r"
            % (name, name + " 1"))
    for x in same if not keep_old else []:
        log("  删同名旧时间线: %r (%d 帧)" % (x["name"], x["end"] - x["start"]))
        bk = _backup(r, x["obj"], backup_dir or path, log=log)
        if not bk:
            res["error"] = ("备份旧时间线失败，为安全起见没有删它。"
                            "先手工导出/确认这条旧时间线不再需要，再加 --keep-old 或换个 -n")
            log("  ✗ " + res["error"])
            return res
        res["backup"] = bk
        gone, why = _delete_timeline(proj, mp, x["obj"], log=log)
        if not gone:
            res["error"] = "删不掉同名旧时间线 %r: %s" % (x["name"], why)
            log("  ✗ " + res["error"])
            log("    先手工在媒体池里删掉它，或换一个 -n 名称，再重试")
            return res
        res["deleted"].append(x["name"])
    if dups:
        log("  ! 还有 %d 条同源的重复时间线（%s），没动它们"
            % (len(dups), ", ".join(repr(d["name"]) for d in dups[:3])))

    # ---- 2) 导入 ----
    log("  导入: %s" % path)
    tl_new = None
    try:
        opts = {"timelineName": name, "importSourceClips": True}
        tl_new = mp.ImportTimelineFromFile(path, opts)
    except Exception as e:
        res["error"] = "ImportTimelineFromFile 抛异常: %s" % e
        log("  ✗ " + res["error"])
        _try_restore()
        return res

    # 返回值不可信（坑 4/15）：以"导入前后名字集合的差"为准
    after = list_timelines(proj)
    old_names = {x["name"] for x in before} - {x["name"] for x in before
                                                if x["name"] in res["deleted"]}
    fresh = [x for x in after if x["name"] not in old_names]
    if tl_new is not None and getattr(tl_new, "GetName", None):
        try:
            picked = [x for x in after if x["name"] == tl_new.GetName()]
            if picked:
                fresh = picked
        except Exception:
            pass
    if not fresh:
        res["error"] = ("导入没有产生新时间线（返回值 %r）。"
                        "常见原因: 素材路径不可达 / fcpxml 结构被拒。"
                        "看达芬奇界面有没有报错弹窗" % (tl_new,))
        log("  ✗ " + res["error"])
        _try_restore()
        return res

    new = fresh[0]
    tl_obj = new["obj"]
    frames = new["end"] - new["start"]
    res["name"] = new["name"]
    res["frames"] = frames
    log("  已导入: %r  %d 帧 (%d-%d)"
        % (new["name"], frames, new["start"], new["end"]))
    if new["name"] != name:
        res["warnings"].append("改名了: 期望 %r 实际 %r" % (name, new["name"]))
        log("  ! " + res["warnings"][-1])

    # ---- 3) 回读校验 ----
    if expect_frames:
        diff = frames - int(expect_frames)
        if abs(diff) > max(2, int(expect_frames * 0.005)):
            res["warnings"].append(
                "总帧对不上: 导入 %d / 草稿 %d (差 %+d)" % (frames, expect_frames, diff))
            log("  ! " + res["warnings"][-1])
            log("    常见原因: 素材缺失被跳过 / 变速段被合并")
    if expect_tracks:
        got = _track_counts(tl_obj)
        if got != tuple(expect_tracks):
            res["warnings"].append("轨数对不上: 导入 视频%d/音频%d, 草稿 视频%d/音频%d"
                                   % (got[0], got[1], expect_tracks[0], expect_tracks[1]))
            log("  ! " + res["warnings"][-1])

    # ---- 4) 打开它（后续 post 就是对着它写）----
    if _api_ok(proj, "SetCurrentTimeline"):
        try:
            if not proj.SetCurrentTimeline(tl_obj):
                res["warnings"].append("SetCurrentTimeline 返回 False，请手工双击打开")
                log("  ! " + res["warnings"][-1])
        except Exception as e:
            res["warnings"].append("SetCurrentTimeline 抛异常: %s" % e)
            log("  ! " + res["warnings"][-1])
    else:
        res["warnings"].append("这版没有 Project.SetCurrentTimeline，导入后请在媒体池双击打开")
        log("  ! " + res["warnings"][-1])

    res["ok"] = True
    return res


def _proj_name(pm):
    try:
        p = pm.GetCurrentProject()
        return p.GetName()
    except Exception:
        return "?"


def _track_counts(tl):
    out = []
    for kind in ("video", "audio"):
        try:
            out.append(int(tl.GetTrackCount(kind)))
        except Exception:
            out.append(0)
    return tuple(out)


def ir_counts(tl):
    """从 IR 数出 (视频轨数, 音频轨数, 总帧数)，用于导入后对照。"""
    nv = len([t for t in tl.tracks if t.kind == "video" and t.clips])
    na = len([t for t in tl.tracks if t.kind == "audio" and t.clips])
    frames = int(round(tl.duration / 1e6 * tl.fps)) if tl.duration else 0
    return nv, na, frames
