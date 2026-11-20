# -*- coding: utf-8 -*-
"""把「导入达芬奇之后要做的三件事」串成一次执行。

    trans  -> 转场        (AddTransition, 非破坏性)
    fades  -> 淡变        (声音 SetFades + 画面 Cross Dissolve)
    masks  -> 蒙版 / 文字 (建 Fusion comp)

这三步都必须在达芬奇里对**已打开的目标时间线**操作, 无法塞进离线的 convert。
以前要手动依次跑三条命令, 漏一条时间线上就少一样东西 —— 这个模块就是为了
不再漏。

顺序为什么是 转场 -> 淡变 -> 蒙版:
  - trans 先: 真实转场优先占位。fades 的视频淡变也是 Cross Dissolve,
    它会读「已有转场集合」并避让 (fadesync.sync 里的 trans 集合)。
    反过来先跑 fades, 该用 BCC+ 的切点会被普通叠化占掉。
  - masks 最后: 它建 Fusion comp, 前两步都是非破坏性的转场操作,
    先做不会影响后续 comp; 反之先建 comp 再动转场没必要。
  - fix_shell 必须最前: 它删片段重放, 会丢掉已经挂上去的转场和 comp。
"""
from __future__ import annotations

import time


def _connect_or_die(log):
    from .fadesync import _connect
    try:
        r = _connect()
    except Exception as e:
        log("✗ 连不上达芬奇: %s" % e)
        log("  请先启动达芬奇, 打开项目, 并让目标时间线处于激活状态。")
        return None
    if r is None:
        log("✗ 连不上达芬奇 (脚本 API 未就绪)。")
        log("  检查: 偏好设置 → 系统 → 常规 → 外部脚本使用 = 本地。")
        return None
    proj = r.GetProjectManager().GetCurrentProject()
    if proj is None:
        log("✗ 达芬奇里没有打开的项目。")
        return None
    tl = proj.GetCurrentTimeline()
    if tl is None:
        log("✗ 项目里没有打开的时间线。")
        return None
    return r, proj, tl


def _tl_fps(tl, default=24.0):
    """Timeline.GetFrameRate 在部分版本是 None 属性(坑 30 同类), 要探测调用。"""
    try:
        v = tl.GetFrameRate
        fps = v() if callable(v) else v
        if fps:
            return float(fps)
    except Exception:
        pass
    try:
        st = tl.GetSetting("timelineFrameRate")
        if st:
            return float(st)
    except Exception:
        pass
    return float(default)


def _draft_fps(draft_dir, fallback):
    """草稿帧率: 与 convert 保持一致, 优先让 jy_read 自己推断。"""
    try:
        from . import jy_read
        tl = jy_read.read(draft_dir)
        if getattr(tl, "fps", None):
            return float(tl.fps)
    except Exception:
        pass
    return float(fallback)


def offline_audit(draft_dir, fps=None, allow_bottom=False, log=print):
    """完全不连达芬奇: 从草稿算出 post 将要写回多少东西。

    用来回答两个问题 ——「草稿里到底有没有这些效果」和「是不是漏跑了」。
    """
    from . import jy_read, transsync
    log("=" * 68)
    log("离线审计(不连达芬奇): %s" % draft_dir)
    log("=" * 68)

    tl = jy_read.read(draft_dir, fps=fps)
    f = float(getattr(tl, "fps", None) or fps or 24)
    dur_us = float(getattr(tl, "duration", 0) or 0)     # IR 里 duration 是微秒
    log("草稿: %.3gfps  %.2fs (%d 帧)" % (f, dur_us / 1e6, int(dur_us * f / 1e6)))

    # --- 转场 ---
    try:
        rows = transsync.build_plan(draft_dir, fps=int(round(f)))
        if rows:
            from collections import Counter
            c = Counter(r["name"] for r in rows)
            unknown = [r for r in rows if not r["cands"]]
            log("\n转场    %d 处" % len(rows))
            for name, n in c.most_common():
                log("        %-10s x%d" % (name, n))
            if unknown:
                log("        ! 未识别(需人工): %s"
                    % ", ".join(sorted({r["name"] for r in unknown})))
        else:
            log("\n转场    0 处 (草稿里没有转场)")
    except Exception as e:
        log("\n转场    !! 读不出: %s" % e)

    # --- 淡变 ---
    try:
        from .fadesync import _expected
        exp = _expected(tl, f)
        nv = sum(1 for k in exp if k[0] == "video")
        na = sum(1 for k in exp if k[0] == "audio")
        log("\n淡变    %d 处 (画面 %d / 声音 %d)" % (len(exp), nv, na))
        if not exp:
            log("        (草稿里没有淡变)")
    except Exception as e:
        log("\n淡变    !! 算不出: %s" % e)

    # --- 蒙版 / 文字 ---
    try:
        from . import masklive
        skips = []
        res = masklive.build_plan(draft_dir, f, allow_bottom=allow_bottom, skips=skips)
        plan = res[0] if isinstance(res, tuple) else res
        log("\n蒙版    %d 段要套 (跳过 %d 段)" % (len(plan), len(skips)))
        for p in plan:
            kind = "钢笔" if p.get("rt") == "contour" else "矩形"
            log("        T%s@%-6d %-4d帧 %-4s %s点 feather %.2f  %s"
                % (p.get("jy_track"), p.get("frame"), p.get("dur_frames", 0), kind,
                   p.get("n", "?"), float(p.get("feather") or 0),
                   p.get("clip") or "(复合/无文件名)"))
        if skips:
            from collections import Counter
            for reason, n in Counter(s["reason"] for s in skips).most_common(4):
                log("        跳过 %-46s %d" % (reason, n))
        if not plan:
            log("        (草稿里没有启用的蒙版/文字)")
    except Exception as e:
        log("\n蒙版    !! 算不出: %s" % e)

    log("\n以上是导入前就能算出来的数量。导入达芬奇后跑:")
    log("  python cli.py post -i \"<草稿目录>\"          # 预演")
    log("  python cli.py post -i \"<草稿目录>\" --go     # 写入")
    log("=" * 68)


def run(draft_dir, go=False, steps=("trans", "fades", "masks"),
        allow_bottom=False, fix_shell=False, fps=None, log=print):
    """依次执行; go=False 时全部只读预演。

    返回 dict: {"ok": bool, "steps": {name: {...}}, "saved": bool}
    """
    steps = tuple(s for s in steps if s)
    t0 = time.time()

    log("=" * 68)
    log("后处理: %s   [%s]" % (draft_dir, "写入" if go else "预演 / 只读"))
    log("顺序: %s" % " → ".join((["fix-shell"] if fix_shell else []) + list(steps)))
    log("=" * 68)

    conn = _connect_or_die(log)
    if conn is None:
        return {"ok": False, "steps": {}, "saved": False}
    r, proj, tl = conn

    tl_fps = _tl_fps(tl)
    draft_fps = _draft_fps(draft_dir, tl_fps)
    try:
        log("时间线: %s  %.3gfps  %d 帧 (起点 %d)"
            % (tl.GetName(), tl_fps, tl.GetEndFrame(), tl.GetStartFrame()))
    except Exception:
        pass
    log("草稿帧率: %.3g  时间线帧率: %.3g" % (draft_fps, tl_fps))
    if abs(draft_fps - tl_fps) > 0.01:
        log("  ! 两者不一致, 帧位按草稿帧率折算到时间线(convert 也是这么做的)")

    out = {"ok": True, "steps": {}, "saved": False}
    seq = 0          # 执行序号: --only 传参顺序与执行顺序无关, 编号要按实际执行

    # ---------- 0) 清空壳(可选, 且必须最先) ----------
    if fix_shell:
        log("\n[0/%d] 清空壳 comp" % len(steps))
        try:
            from . import shellfix
            n_ok, n_tot, _ = shellfix.fix(draft_dir, go=go, log=log)
            out["steps"]["fix-shell"] = {"ok": n_ok, "total": n_tot}
            if go and n_ok != n_tot:
                log("✗ 空壳清理未全部成功, 后面的步骤容易写错位置, 中止。")
                out["ok"] = False
                return out
        except Exception as e:
            log("✗ fix-shell 失败: %s" % e)
            out["ok"] = False
            return out

    # ---------- 1) 转场 ----------
    if "trans" in steps:
        seq += 1
        log("\n[%d/%d] 转场" % (seq, len(steps)))
        try:
            from . import transsync
            rows = transsync.build_plan(draft_dir, fps=fps, tl_fps=tl_fps)
            if not rows:
                log("  草稿里没有转场, 跳过")
                out["steps"]["trans"] = {"total": 0, "done": 0, "manual": 0}
            else:
                log("  计划 %d 处" % len(rows))
                res = transsync.apply(tl, rows, go=go, log=log)
                done = sum(1 for x in res if x["done"])
                manual = len(res) - done
                out["steps"]["trans"] = {"total": len(res), "done": done, "manual": manual}
                if go:
                    log("  写入 %d / %d, 待人工 %d" % (done, len(res), manual))
                else:
                    log("  就绪 %d / %d (预演)" % (done, len(res)))
        except Exception as e:
            log("✗ 转场失败: %s" % e)
            out["steps"]["trans"] = {"error": str(e)}
            out["ok"] = False

    # ---------- 2) 淡变 ----------
    if "fades" in steps:
        seq += 1
        log("\n[%d/%d] 淡变" % (seq, len(steps)))
        try:
            from . import fadesync
            ok_n, fail_n = fadesync.run(draft_dir, audit_only=not go, log=log,
                                        fps=fps or int(round(draft_fps)))
            out["steps"]["fades"] = {"ok": ok_n, "fail": fail_n}
            if fail_n:
                out["ok"] = False
        except Exception as e:
            log("✗ 淡变失败: %s" % e)
            out["steps"]["fades"] = {"error": str(e)}
            out["ok"] = False

    # ---------- 3) 蒙版 / 文字 ----------
    if "masks" in steps:
        seq += 1
        log("\n[%d/%d] 蒙版 / 文字" % (seq, len(steps)))
        try:
            from . import masklive
            ok_n, fail_n, _ = masklive.run(draft_dir, audit_only=not go, log=log,
                                           allow_bottom=allow_bottom)
            out["steps"]["masks"] = {"ok": ok_n, "fail": fail_n}
            if fail_n:
                out["ok"] = False
        except Exception as e:
            log("✗ 蒙版失败: %s" % e)
            out["steps"]["masks"] = {"error": str(e)}
            out["ok"] = False

    # ---------- 保存 ----------
    if go:
        try:
            out["saved"] = bool(proj.GetProjectManager().SaveProject())
        except Exception as e:
            log("✗ 保存失败: %s" % e)
        log("\n保存: %s" % ("已存盘" if out["saved"] else "未存盘(检查工程只读?)"))

    log("\n" + "-" * 68)
    log("汇总 (%.1fs)" % (time.time() - t0))
    for name, st in out["steps"].items():
        if "error" in st:
            log("  %-10s ✗ %s" % (name, st["error"]))
        elif name == "trans":
            log("  %-10s 写入/就绪 %d, 待人工 %d" % (name, st.get("done", 0), st.get("manual", 0)))
        elif name == "fix-shell":
            log("  %-10s 处理 %d/%d" % (name, st.get("ok", 0), st.get("total", 0)))
        else:
            log("  %-10s 成功 %d, 失败 %d" % (name, st.get("ok", 0), st.get("fail", 0)))
    if not go:
        log("\n以上为预演, 时间线未改动。确认后加 --go。")
    log("-" * 68)
    return out
