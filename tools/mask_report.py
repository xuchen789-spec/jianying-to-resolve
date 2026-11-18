"""列出草稿里所有"挂了蒙版"的段，以及能读到 / 读不到的信息。

回答一个具体问题：剪映草稿有没有记录"这个蒙版在工程里到底用没用"？

结论：**没有**。同一个工程里，确认要用的段和确认不该用的段，字段结构完全一样。
能读到的只有三种间接证据：

  1. enable_video_mask is False      → 用户手动关掉了（明确否定）
  2. enable_adjust_mask 存在          → 这是"调节蒙版"，只限定调色区，不裁画面（否定）
  3. 该段下方没有任何重叠的视频层      → 蒙版让画面透明，没有下层只能露黑（物理否定）

所以判定只能靠 1/2/3 的组合，读不到一个直接的 "is_used"。

用法:
    python tools/mask_report.py -i "<草稿目录>" [--json]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nlebridge.jy_read import load_draft_json


def _bbox_stats(draft_dir, mask):
    """AI 抠像序列的有效帧占比（bbox 宽高 > 0.1%）。读不到返回 None。"""
    ws = mask.get("loader_work_space") or ""
    if "adjust_mask/" not in ws:
        return None
    uid = ws.split("adjust_mask/")[-1].strip("/\\")
    ddir = os.path.join(draft_dir, "adjust_mask", uid)
    data_p = os.path.join(ddir, "data.json")
    if not os.path.exists(data_p):
        return None
    try:
        data = json.load(open(data_p, encoding="utf-8")).get("data") or []
    except Exception:
        return None
    n = len(data)
    if not n:
        return (0, 0)
    nz = sum(1 for x in data
             if (x.get("right", 0) - x.get("left", 0)) > 0.001
             and (x.get("bottom", 0) - x.get("top", 0)) > 0.001)
    return (nz, n)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True, help="剪映草稿目录")
    ap.add_argument("--fps", type=float, default=24.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    d, base = load_draft_json(a.input)
    mats = {}
    for k, v in d.get("materials", {}).items():
        if isinstance(v, list):
            for m in v:
                mats[m.get("id")] = (k, m)
    videos = {m["id"]: m for m in d.get("materials", {}).get("videos", [])}
    fps = a.fps

    # 各视频轨段区间（轨道号从 1 起，1 为最底）
    spans, vtracks = [], []
    vno = 0
    for tr in d.get("tracks", []):
        if tr.get("type") != "video":
            continue
        vno += 1
        segs = tr.get("segments", [])
        vtracks.append((vno, segs))
        for seg in segs:
            tt = seg.get("target_timerange") or {}
            # 坑 12: 从第 0 帧开始的段不写 start 键, 缺键按 0 处理
            if "duration" in tt:
                st0 = tt.get("start", 0)
                spans.append((vno, st0, st0 + tt["duration"]))

    def below(v, s, dur):
        e = s + max(0, dur)
        out = []
        for o, s0, s1 in spans:
            if o >= v:
                continue
            if s1 > s and s0 < e:
                out.append(o)
        return sorted(set(out))

    rows = []
    for v, segs in vtracks:
        for seg in segs:
            masks = []
            for r in seg.get("extra_material_refs") or []:
                k, m = mats.get(r, (None, {}))
                if k == "common_mask":
                    masks.append(m)
            if not masks:
                continue
            tt = seg.get("target_timerange") or {}
            st = tt.get("start", 0)
            dur = tt.get("duration", 0)
            vinfo = videos.get(seg.get("material_id"), {})
            name = vinfo.get("material_name") or os.path.basename(vinfo.get("path") or "")
            evm = seg.get("enable_video_mask")
            ead = seg.get("enable_adjust_mask")
            bl = below(v, st, dur)

            reasons, verdict = [], "套用"
            if evm is False:
                reasons.append("enable_video_mask=False（手动关闭）")
                verdict = "跳过"
            if ead is not None:
                reasons.append("存在 enable_adjust_mask（调节蒙版，不裁画面）")
                verdict = "跳过"
            if not bl:
                reasons.append("下方无重叠层（透明即露黑）")
                verdict = "跳过"

            ai = []
            for m in masks:
                s = _bbox_stats(a.input, m)
                if s:
                    ai.append(f"{s[0]}/{s[1]}")

            rows.append({
                "frame": int(round(st / 1e6 * fps)),
                "dur": int(round(dur / 1e6 * fps)),
                "track": v,
                "name": name[:34],
                "masks": [m.get("name") for m in masks],
                "evm": evm, "ead": ead,
                "below": bl,
                "ai": ",".join(ai) or "-",
                "verdict": verdict,
                "reason": "；".join(reasons) or "有下层承接，蒙版有意义",
            })

    rows.sort(key=lambda x: x["frame"])
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    print(f"草稿: {os.path.basename(os.path.normpath(a.input))}   带蒙版段: {len(rows)}\n")
    print(f"{'帧':>6} {'时长':>4} {'轨':>3}  {'蒙版':14s} {'evm':>5} {'ead':>5} "
          f"{'下层':>4} {'AI有效':>8}  {'判定':4}  素材 / 理由")
    print("-" * 118)
    for r in rows:
        ev = "-" if r["evm"] is None else str(r["evm"])
        ea = "-" if r["ead"] is None else str(r["ead"])
        bl = ",".join(f"V{x}" for x in r["below"]) or "无"
        mk = "+".join(r["masks"])[:14]
        print(f"{r['frame']:6d} {r['dur']:4d} V{r['track']:<2} {mk:14s} {ev:>5} {ea:>5} "
              f"{bl:>4} {r['ai']:>8}  {r['verdict']:4}  {r['name']}")
        if r["verdict"] == "跳过":
            print(f"{'':32s}└ {r['reason']}")
    n_skip = sum(1 for r in rows if r["verdict"] == "跳过")
    print(f"\n套用 {len(rows) - n_skip} 段 / 跳过 {n_skip} 段")
    print("注：草稿没有记录「蒙版是否启用」的布尔值，判定全部来自上面三类间接证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
