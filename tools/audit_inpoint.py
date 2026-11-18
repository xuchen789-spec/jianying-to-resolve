# -*- coding: utf-8 -*-
"""入点审计: 逐个片段比对 达芬奇源入点 vs 剪映草稿, 找出被倍速吞掉的入点误差.

用法:
    python tools/audit_inpoint.py -i "D:/剪映草稿/某工程" [-p 工程名]

判定依据:
    期望入点(帧) = 草稿 source_timerange.start(µs) / 1e6 × 素材池真实 FPS
    ⚠ 不能用草稿里的 fps 字段: 本项目的 488052b4 标称 30fps, 实际文件是 24fps.
"""
import sys, io, json, argparse, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nlebridge.fadesync import _connect


def load_draft(draft_dir):
    """支持加密草稿(剪映 11.x)与明文 json —— 不能直接 json.load。"""
    from nlebridge.jy_read import load_draft_json
    D, _base = load_draft_json(draft_dir)
    return D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True, help="剪映草稿目录")
    ap.add_argument("-p", "--project", default=None, help="达芬奇工程名(默认当前工程)")
    ap.add_argument("--tol", type=float, default=3.0, help="容差(帧), 超过则报错")
    a = ap.parse_args()

    D = load_draft(a.input)
    mats = D.get("materials", {})
    vmap = {m["id"]: m.get("material_name", "?") for m in mats.get("videos", [])
            if isinstance(m, dict) and "id" in m}

    segs = {}
    for ti, tr in enumerate([t for t in D["tracks"] if t.get("type") == "video"]):
        for s in tr.get("segments", []):
            tt = s.get("target_timerange") or {}
            # 从第 0 帧开始的段不写 start 键(剪映约定), 缺键按 0
            if tt.get("duration"):
                segs[(ti, round(tt.get("start", 0) / 1e6 * 24))] = s

    r = _connect()
    pm = r.GetProjectManager()
    if a.project:
        pm.LoadProject(a.project)
    tl = pm.GetCurrentProject().GetCurrentTimeline()
    print("工程: %s  时间线: %s  总长: %d 帧" % (pm.GetCurrentProject().GetName(), tl.GetName(), tl.GetEndFrame()))

    bad = []
    fusion_skip = 0
    for trk in range(1, 6):
        for it in sorted(tl.GetItemListInTrack("video", trk) or [], key=lambda x: x.GetStart()):
            nm = it.GetName() or ""
            if "交叉叠化" in nm:
                continue
            # 转成 Fusion Clip 后 GetSourceStartFrame() 一律返回 0(源入点不可读),
            # 不能当偏移报错; 这类段只能靠像素比对或转换前的记录校验。
            if nm.startswith("Fusion Clip"):
                fusion_skip += 1
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
            sr = s.get("source_timerange") or {}
            exp = sr.get("start", 0) / 1e6 * fps
            got = it.GetSourceStartFrame()
            if got is None:
                continue
            diff = got - exp
            if abs(diff) >= a.tol:
                spd = s.get("speed", 1.0)
                bad.append((trk, it.GetStart(), it.GetEnd(), vmap.get(s.get("material_id"), "?"),
                            spd, int(round(got)), int(round(exp)), diff / fps))
                print("V%d [%d-%d] %-22s %.2fx  in=%d 期望=%d  偏%+.0f帧(%+.2fs)" % (
                    trk, it.GetStart(), it.GetEnd(), vmap.get(s.get("material_id"), "?")[:22],
                    spd, got, round(exp), diff, diff / fps))
    print("\n异常段: %d   (跳过 Fusion 片段 %d 段: 转换后源入点不可读, 不参与判定)" % (len(bad), fusion_skip))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
