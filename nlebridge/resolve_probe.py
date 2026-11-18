# -*- coding: utf-8 -*-
"""达芬奇工程库直读 —— 导入后的结构体检(诊断用)

为什么需要它: 达芬奇的 FCPXML 导入没法在命令行里跑, 只能导入完再肉眼看。
但导入结果就写在工程库的 SQLite 里:
    %APPDATA%/Blackmagic Design/DaVinci Resolve/Support/Resolve Project Library/
        Resolve Projects/Users/<user>/Projects/<项目>/Project.db
    Sm2TiTrack  = 时间线的轨道   (Type 0=视频 1=音频, rowid 即建立顺序)
    Sm2TiItem   = 轨道上的片段   (Start/Duration 为帧, MediaFilePath 为素材路径)

所以可以事后核对: 到底进了几条轨、每轨几个片段、音频有没有上下颠倒、
图片有没有被当成序列、素材有没有离线。全程只读, 不改任何东西。
"""
import glob
import os
import sqlite3

_LIB = os.path.join(os.environ.get("APPDATA", ""),
                    "Blackmagic Design", "DaVinci Resolve", "Support",
                    "Resolve Project Library", "Resolve Projects", "Users")


def find_db(project: str = None):
    """在达芬奇工程库里找 Project.db。project 可以是项目名(可部分匹配)或 .db 路径"""
    if project and project.lower().endswith(".db") and os.path.isfile(project):
        return project
    pats = [os.path.join(_LIB, "*", "Projects", "*", "Project.db")]
    cands = [p for pat in pats for p in glob.glob(pat)]
    if project:
        key = project.lower()
        hit = [p for p in cands if key in os.path.basename(os.path.dirname(p)).lower()]
        if hit:
            cands = hit
    if not cands:
        return None
    # 多个同名(不同用户)时取最近改动的
    return max(cands, key=os.path.getmtime)


def _conn(db: str):
    uri = "file:" + os.path.abspath(db).replace("\\", "/") + "?mode=ro&immutable=1"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return sqlite3.connect("file:" + os.path.abspath(db) + "?mode=ro", uri=True)


def _frames(v):
    """有时字段是 '51|80ffffff...' 这种复合串, 取前面整数"""
    try:
        return int(str(v).split("|")[0])
    except (TypeError, ValueError):
        return None


def _stem(p):
    return os.path.splitext(os.path.basename(p or ""))[0]


def load(db: str, timeline: str = None):
    """读出一个时间线的轨道结构。

    返回 {tracks: [{kind, order, items:[{start, dur, path, name}]}], timelines: [...]}
    """
    con = _conn(db)
    cur = con.cursor()
    timelines = [(r[0], r[1]) for r in cur.execute("select Sm2Timeline_id, Name from Sm2Timeline")]

    rows = cur.execute(
        "select Sm2TiTrack_id, Type, Sm2SequenceContainer_id from Sm2TiTrack").fetchall()
    by_container = {}
    for tid, typ, cid in rows:
        by_container.setdefault(cid, []).append((tid, typ))
    if not by_container:
        return {"tracks": [], "timelines": timelines, "container": None}

    # 选片段最多的容器(即刚导入的那条时间线); 若指定了时间线名则按名字过滤
    counts = {}
    for cid, trs in by_container.items():
        n = 0
        for tid, _t in trs:
            n += cur.execute("select count(*) from Sm2TiItem where Sm2TiTrack_id=?",
                             (tid,)).fetchone()[0]
        counts[cid] = n
    cid = max(counts, key=counts.get)

    tracks = []
    trs = cur.execute(
        "select rowid, Sm2TiTrack_id, Type from Sm2TiTrack where Sm2SequenceContainer_id=? "
        "order by Type, rowid", (cid,)).fetchall()
    for order, (rowid, tid, typ) in enumerate(trs, 1):
        items = []
        for start, dur, path, name in cur.execute(
                "select Start, Duration, MediaFilePath, Name from Sm2TiItem "
                "where Sm2TiTrack_id=?", (tid,)):
            s, d = _frames(start), _frames(dur)
            if s is None:
                continue
            items.append({"start": s, "dur": d, "path": path, "name": name})
        items.sort(key=lambda x: x["start"])
        tracks.append({"kind": "video" if typ == 0 else "audio",
                       "order": order, "rowid": rowid, "items": items})
    con.close()
    return {"tracks": tracks, "timelines": timelines, "container": cid}


def check(info, ir=None, fps: int = 24):
    """体检 + 与 IR 对照。返回 (lines, problems)"""
    out, bad, warn = [], [], []
    v = [t for t in info["tracks"] if t["kind"] == "video"]
    a = [t for t in info["tracks"] if t["kind"] == "audio"]
    out.append(f"达芬奇里这条时间线: 视频轨 {len(v)} 条, 音频轨 {len(a)} 条")

    seq_names = set()
    missing = {}
    for t in info["tracks"]:
        for it in t["items"]:
            nm = it["name"] or ""
            if "[" in nm and "]" in nm:
                seq_names.add(nm)
            p = it["path"]
            if p and not os.path.exists(p) and not p.startswith("\\\\"):
                missing.setdefault(p, 0)

    out.append("")
    out.append("轨道明细 (建立顺序 = 达芬奇的 V1/A1 顺序):")
    for kind, group, label in (("video", v, "V"), ("audio", a, "A")):
        k = 0
        for t in group:
            k += 1
            starts = [i["start"] for i in t["items"]]
            span = f"{min(starts)}-{max(i['start'] + i['dur'] for i in t['items'])}帧" if starts else "-"
            out.append(f"  {label}{k:<2} {kind:<5} 片段 {len(t['items']):>3}  范围 {span}")

    if seq_names:
        bad.append("有素材被当成图片序列读入(会显示离线/丢图): "
                   + ", ".join(sorted(seq_names)[:5]))
    if missing:
        bad.append(f"{len(missing)} 个素材在磁盘上找不到: "
                   + ", ".join(os.path.basename(p) for p in list(missing)[:5]))

    # ---- 与源时间线对照: 每条源轨最后落到达芬奇第几条 ----
    if ir is not None:
        fps = ir.fps or fps

        def fr(us):
            return int(round(us * fps / 1_000_000))

        src = []
        for kind in ("video", "audio"):
            for i, t in enumerate(ir.tracks):
                if t.kind != kind or not t.clips:
                    continue
                keys = set()
                for c in t.clips:
                    m = ir.media.get(c.media_id)
                    if m:
                        keys.add((fr(c.start), fr(c.dur), _stem(m.path)))
                src.append((kind, keys))
        dst = []
        for t in info["tracks"]:
            dst.append((t["kind"], {(i["start"], i["dur"], _stem(i["path"])) for i in t["items"]}))

        out.append("")
        out.append("源轨 → 达芬奇轨 对照(按片段内容匹配):")
        vk = ak = 0
        for kind, keys in src:
            pool = [(len(keys & dk), j + 1) for j, (k2, dk) in enumerate(dst) if k2 == kind]
            pool.sort(reverse=True)
            best = pool[0] if pool else (0, 0)
            if kind == "video":
                vk += 1
                label = f"V{vk}"
            else:
                ak += 1
                label = f"A{ak}"
            mark = "" if best[0] >= max(1, len(keys) * 0.6) else "  ← 匹配度低, 可能是零散/被拆分"
            out.append(f"  {label:<4} {len(keys):>3} 片段 -> 达芬奇第 {best[1]} 条 "
                       f"(对上 {best[0]}){mark}")

        nv_src = sum(1 for k, _ in src if k == "video")
        if len(v) > nv_src:
            warn.append(f"视频轨比源多 {len(v) - nv_src} 条: 通常是音频片段引用了含视频流的素材, "
                        f"被当成视频片段另开了轨")
        if ak:
            # 逐条比对: 源音频1 应当就是达芬奇的 A1(最上面那条), 依次往下。
            # 达芬奇建轨顺序 = 界面从上到下, 所以直接按序号对齐, 不看谁"最像"。
            src_audio = [keys for kind, keys in src if kind == "audio"]
            dst_audio = [dk for kind2, dk in dst if kind2 == "audio"]
            off = []
            for i, keys in enumerate(src_audio):
                if i >= len(dst_audio):
                    break
                hit = len(keys & dst_audio[i])
                if keys and hit < max(1, int(len(keys) * 0.5)):
                    best = max(((len(keys & dk), j + 1)
                                for j, dk in enumerate(dst_audio)), default=(0, 0))
                    off.append(f"源音频{i + 1}({len(keys)}段) 应在 A{i + 1}, 实际在 A{best[1]}")
            if off:
                warn.append("音轨顺序与源不一致: " + "; ".join(off[:4])
                            + "。FCPXML 里 lane -1 就是 A1, 音频第 i 条写 -i")

    return out, bad, warn


def render(info, ir=None):
    lines, bad, warn = check(info, ir)
    text = "\n".join(lines)
    if warn:
        text += "\n\n[注意]\n" + "\n".join("  - " + w for w in warn)
    if bad:
        text += "\n\n[问题]\n" + "\n".join("  - " + b for b in bad)
    if not bad and not warn:
        text += "\n\n结构与素材都正常。"
    return text
