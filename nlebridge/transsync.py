# -*- coding: utf-8 -*-
"""把剪映转场写进达芬奇当前时间线（21.1+ 的 AddTransition）。

背景（2026-11-18 实测，见 references/坑位库.md）：
  - 剪映把转场挂在**宿主片段尾部**（= 与下一段的接缝），不是起点
  - 达芬奇 21.1 起有 `TimelineItem.AddTransition({type, category, position, alignment, duration})`
  - `category`: simple / fusion / ofx；`type` 是界面显示名（中文界面就写中文名）
  - 入点没有余量（source.start == 0，含静帧）时居中/右对齐会返回 None，只能退 left

映射原则：能贴原片的用达芬奇自带，风格差异大的才上 BCC+。
    叠化     -> simple / Cross Dissolve
    闪白     -> fusion / Brightness Flash
    闪黑     -> fusion / Inverse Flash
    推镜变焦 -> ofx / BCC+交叉变焦溶解
    放射     -> ofx / BCC+光线溶解
    炫光 III -> ofx / BCC+镜头光晕溶解
未识别的效果不猜，进人工清单。
"""
import time

# 剪映名 -> [(category, 达芬奇显示名, 备注)]，按优先级排列，第一个失败试下一个
TRANSITION_MAP = {
    "叠化": [
        ("simple", "Cross Dissolve", "达芬奇自带"),
    ],
    "闪白": [
        ("fusion", "Brightness Flash", "亮度闪白，比 BCC Lens Flash 更贴原片"),
    ],
    "闪黑": [
        ("fusion", "Inverse Flash", "反向亮度闪 = 压黑"),
    ],
    "推镜变焦": [
        ("ofx", "BCC+交叉变焦溶解", "Continuum BCC+；英文界面试 BCC Cross Zoom Dissolve"),
        ("ofx", "BCC Cross Zoom Dissolve", ""),
        ("ofx", "BCC Crash Zoom Dissolve", "经典组备选"),
    ],
    "放射": [
        ("ofx", "BCC+光线溶解", "光心要对准主体"),
        ("ofx", "BCC Rays Dissolve", "经典组备选"),
    ],
    "炫光": [
        ("ofx", "BCC+镜头光晕溶解", "默认光晕在画面中心，需调位置"),
        ("ofx", "BCC Lens Flare Dissolve", "经典组备选"),
        ("ofx", "BCC+漏光溶解", "备选"),
    ],
}


def match(name):
    """按名字前缀匹配（剪映会带 ' III' 之类后缀）。返回 (key, candidates) 或 (None, [])。"""
    n = (name or "").strip()
    if n in TRANSITION_MAP:
        return n, TRANSITION_MAP[n]
    for k in sorted(TRANSITION_MAP, key=len, reverse=True):
        if n.startswith(k):
            return k, TRANSITION_MAP[k]
    return None, []


def build_plan(draft_dir, fps=None, tl_fps=None):
    """只读：从草稿生成转场落地计划（不连达芬奇）。

    fps    草稿帧率，不给就按片段边界自动推断
    tl_fps 达芬奇时间线帧率，不给视为与草稿相同（不同时必须给，帧号要换算）
    """
    from .jy_read import load_draft_json, infer_fps
    d, _base = load_draft_json(draft_dir)
    if not fps:
        fps = float(infer_fps(d)[0])
    tlf = float(tl_fps or fps)
    trs = {m.get("id"): m for m in (d.get("materials") or {}).get("transitions") or []}
    rows = []
    for track in d.get("tracks") or []:
        if track.get("type") != "video":
            continue
        for seg in track.get("segments") or []:
            for ref in seg.get("extra_material_refs") or []:
                m = trs.get(ref)
                if not m:
                    continue
                tg = seg.get("target_timerange") or {}
                st = tg.get("start") or 0
                du = tg.get("duration") or 0
                key, cand = match(m.get("name"))
                rows.append({
                    "name": m.get("name") or "转场",
                    "key": key,
                    "cands": cand,
                    # duration 单位是微秒，帧号按时间线帧率折算（坑 29 的同类问题）
                    "dur_frames": max(1, round((m.get("duration") or 0) * tlf / 1e6)),
                    "host_frame": round(st * tlf / 1e6),
                    "host_dur": max(1, round(du * tlf / 1e6)),
                    "at_frame": round((st + du) * tlf / 1e6),
                })
    rows.sort(key=lambda r: (r["host_frame"], r["name"]))
    return rows


def _find_host(tl, frame, dur, tol=1):
    """在所有视频轨里找宿主片段：起点 + 时长对得上，且不是转场本身。"""
    hits = []
    for t in range(1, tl.GetTrackCount("video") + 1):
        for it in tl.GetItemListInTrack("video", t) or []:
            if it.GetType() == "transition":
                continue
            if abs(it.GetStart() - frame) <= tol and \
                    abs((it.GetEnd() - it.GetStart()) - dur) <= tol:
                hits.append((t, it))
    return hits


def _has_transition_at(tl, track, frame):
    for it in tl.GetItemListInTrack("video", track) or []:
        if it.GetType() == "transition" and it.GetStart() <= frame <= it.GetEnd():
            return True
    return False


def _entry_has_handles(item):
    """入口片段有没有余量：source 起点 > 0 才能居中。读不到（Fusion/静帧）算没有。"""
    try:
        s = item.GetSourceStartFrame()
        return bool(s) and int(s) > 0
    except Exception:
        return False


def apply(tl, rows, go=False, log=print):
    """逐条写入。go=False 只预演（不调 AddTransition）。返回结果行。"""
    results = []
    for r in rows:
        rec = dict(r, track=None, alignment=None, used=None, done=False, note="")
        if not r["cands"]:
            rec["note"] = "未识别，进人工清单"
            results.append(rec)
            log("  ? %-10s @%-6d %-4d帧  未识别 → 人工" %
                (r["name"], r["host_frame"], r["dur_frames"]))
            continue

        hits = _find_host(tl, r["host_frame"], r["host_dur"])
        if not hits:
            rec["note"] = "时间线上找不到宿主片段"
            results.append(rec)
            log("  ✗ %-10s @%-6d 找不到宿主片段（先确认导入的是同一条时间线）" %
                (r["name"], r["host_frame"]))
            continue
        if len(hits) > 1:
            rec["note"] = f"命中 {len(hits)} 处，需人工指定轨道"
            results.append(rec)
            log("  ! %-10s @%-6d 命中 %d 处（V%s），跳过" %
                (r["name"], r["host_frame"], len(hits),
                 ",".join(str(t) for t, _ in hits)))
            continue

        track, host = hits[0]
        rec["track"] = track
        if _has_transition_at(tl, track, r["host_frame"]):
            rec["note"] = "已有转场"
            rec["done"] = True
            results.append(rec)
            log("  = V%d %-10s @%-6d 已有转场，跳过" %
                (track, r["name"], r["host_frame"]))
            continue

        # 尾部下一个片段 = 入口段，决定能不能居中
        entry = None
        for it in tl.GetItemListInTrack("video", track) or []:
            if it.GetType() != "transition" and abs(it.GetStart() - host.GetEnd()) <= 1:
                entry = it
                break
        alignment = "center" if entry is not None and _entry_has_handles(entry) else "left"
        rec["alignment"] = alignment

        if not go:
            cat, typ, _ = r["cands"][0]
            log("  → V%d %-10s @%-6d %-4d帧  %s/%s  对齐=%s%s" %
                (track, r["name"], r["host_frame"], r["dur_frames"],
                 cat, typ, alignment,
                 "" if alignment == "center" else "（入口无余量）"))
            results.append(rec)
            continue

        ok = False
        for cat, typ, note in r["cands"]:
            try:
                res = host.AddTransition({
                    "type": typ, "category": cat, "position": "end",
                    "alignment": alignment, "duration": r["dur_frames"],
                })
            except Exception as e:
                res = None
                rec["note"] = f"{type(e).__name__}: {e}"
            time.sleep(0.35)
            if res:
                rec.update(done=True, used=f"{cat}/{typ}")
                log("  ✓ V%d %-10s @%-6d %-4d帧  %s  %s" %
                    (track, r["name"], r["host_frame"], r["dur_frames"], typ,
                     "" if alignment == "center" else "(left)"))
                ok = True
                break
            if alignment == "center":
                # 居中失败常见原因是入口无余量，退 left 再试同一效果
                try:
                    res = host.AddTransition({
                        "type": typ, "category": cat, "position": "end",
                        "alignment": "left", "duration": r["dur_frames"],
                    })
                except Exception:
                    res = None
                time.sleep(0.35)
                if res:
                    rec.update(done=True, used=f"{cat}/{typ}", alignment="left")
                    log("  ✓ V%d %-10s @%-6d 居中失败改 left  %s" %
                        (track, r["name"], r["host_frame"], typ))
                    ok = True
                    break
        if not ok:
            rec["note"] = rec["note"] or "候选效果全部失败（插件未装/授权/显示名不匹配）"
            log("  ✗ V%d %-10s @%-6d 全部候选失败" %
                (track, r["name"], r["host_frame"]))
        results.append(rec)
    return results
