# -*- coding: utf-8 -*-
"""变速段入点修复: 删段(含链接音频) -> 1x 精确放置 -> SetSpeed, 逐段校验.

用法:
    # 1) 先审计, 拿到偏帧列表
    python tools/audit_inpoint.py -i "<草稿目录>"
    # 2) 预演(不动任何东西)
    python tools/fix_inpoint.py -i "<草稿目录>"
    # 3) 执行
    python tools/fix_inpoint.py -i "<草稿目录>" --go

原理: 达芬奇导入交换文件时, 会把变速片段的源入点再乘一次倍速
      (in_effective = in_export × speed). 修法是"重放"—— 按草稿的真实入点
      以 1x 放置, 再对整段 SetSpeed, SetSpeed 只压缩源消耗、不动入点.
注意: 每段只尝试一次; 放置失败多数是音频轨同位置被孤儿音频占用, 先清理再试.
"""
import sys, io, json, time, argparse, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nlebridge.fadesync import _connect


def load_draft(draft_dir):
    for name in ("draft_content.json", "draft_content_decrypted.json"):
        p = os.path.join(draft_dir, name)
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
    raise SystemExit("找不到 draft_content*.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True)
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--tol", type=float, default=3.0)
    a = ap.parse_args()

    D = load_draft(a.input)
    mats = D.get("materials", {})
    segs = {}
    for ti, tr in enumerate([t for t in D["tracks"] if t.get("type") == "video"]):
        for s in tr.get("segments", []):
            tt = s.get("target_timerange") or {}
            if "start" in tt:
                segs[(ti, round(tt["start"] / 1e6 * 24))] = s

    r = _connect()
    pm = r.GetProjectManager()

    def tl():
        return pm.GetCurrentProject().GetCurrentTimeline()

    # 找出需要修的段
    todo = []
    for trk in range(1, 6):
        for it in sorted(tl().GetItemListInTrack("video", trk) or [], key=lambda x: x.GetStart()):
            nm = it.GetName() or ""
            if "交叉叠化" in nm or nm.startswith("Fusion Clip"):
                continue
            s = segs.get((trk - 1, it.GetStart()))
            if s is None:
                continue
            src = it.GetMediaPoolItem()
            fps = 24.0
            if src:
                try:
                    fps = float(src.GetClipProperty("FPS") or 24.0)
                except Exception:
                    pass
            exp = (s.get("source_timerange") or {}).get("start", 0) / 1e6 * fps
            got = it.GetSourceStartFrame()
            if got is None or abs(got - exp) < a.tol:
                continue
            todo.append((trk, it.GetStart(), it.GetEnd(), int(round(exp)),
                         int(round(s.get("speed", 1.0) * 100))))

    print("待修 %d 段" % len(todo))
    for t in todo:
        print("  ", t)
    if not a.go:
        print("\n[dry-run] 加 --go 执行")
        return 0

    for trk, st, en, exp, pct in todo:
        it = None
        for x in (tl().GetItemListInTrack("video", trk) or []):
            if x.GetStart() == st and x.GetEnd() == en:
                it = x
        if it is None:
            print("!! V%d[%d-%d] 找不到, 停" % (trk, st, en))
            break
        src = it.GetMediaPoolItem()
        aud = []
        for atrk in range(1, 10):
            for x in (tl().GetItemListInTrack("audio", atrk) or []):
                if x.GetStart() == st and (x.GetName() or "") == (it.GetName() or ""):
                    aud.append(x)
        print("V%d[%d-%d] 删段+音频%d -> 放 %d..%d -> %d%%" % (
            trk, st, en, len(aud), exp, exp + (en - st), pct))
        tl().DeleteClips([it] + aud, False)
        time.sleep(0.6)
        pm.GetCurrentProject().GetMediaPool().AppendToTimeline([{
            "mediaPoolItem": src, "startFrame": exp, "endFrame": exp + (en - st),
            "recordFrame": st, "trackIndex": trk}])
        time.sleep(0.8)
        new = None
        for x in (tl().GetItemListInTrack("video", trk) or []):
            if x.GetStart() == st:
                new = x
        if new is None:
            print("  !! 放置失败, 停" )
            break
        if pct != 100:
            new.SetSpeed({"Percentage": pct})
            time.sleep(0.6)
        for x in (tl().GetItemListInTrack("video", trk) or []):
            if x.GetStart() == st:
                new = x
        ok = abs((new.GetSourceStartFrame() or 0) - exp) <= 2 and new.GetEnd() == en
        print("  结果 in=%s 期望=%d [%d-%d] %s" % (new.GetSourceStartFrame(), exp,
                                                    new.GetStart(), new.GetEnd(),
                                                    "OK" if ok else "CHECK"))
    print("save:", pm.SaveProject())
    return 0


if __name__ == "__main__":
    sys.exit(main())
