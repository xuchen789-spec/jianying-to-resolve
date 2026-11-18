# -*- coding: utf-8 -*-
"""转换报告 + SRT 导出"""
import json
import os
from collections import Counter

from .ir import Timeline

GRADE_MAP = {
    "brightness": ("亮度", "Offset"),
    "contrast": ("对比度", "Contrast (Pivot)"),
    "saturation": ("饱和度", "Saturation"),
    "natural_saturation": ("自然饱和度", "Color Boost"),
    "temperature": ("色温", "Temp"),
    "tone": ("色调", "Tint"),
    "highlight": ("高光", "Gain"),
    "shadow": ("阴影", "Lift"),
    "white": ("白色阶", "Gain Limit"),
    "black": ("黑色阶", "Lift Limit"),
    "sharpen": ("锐化", "Midtone Detail"),
    "clear": ("清晰度", "Midtone Detail (辅)"),
    "light_sensation": ("光感", "Gamma / Pivot"),
}


def _frames(us: int, fps: int) -> int:
    return int(round(us * fps / 1_000_000))


def report_frames(us: int, fps: int) -> int:
    return _frames(us, fps)


def srt_text(tl: Timeline) -> str:
    lines, i = [], 1
    for tr in tl.text_tracks():
        for c in sorted(tr.clips, key=lambda x: x.start):
            if not c.text:
                continue
            lines.append(f"{i}\n{_ts(c.start)} --> {_ts(c.end)}\n{c.text}\n")
            i += 1
    return "\n".join(lines)


def _ts(us: int) -> str:
    ms = max(us, 0) // 1000
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build(tl: Timeline, target: str) -> str:
    L = ["# 转换报告", "",
         f"- 来源: {tl.source_app}",
         f"- 规格: {tl.width}x{tl.height} @ {tl.fps}fps, 时长 {tl.duration/1e6:.2f}s",
         f"- 片段: {tl.clip_count()}  素材: {len(tl.media)}", ""]

    # 轨道表: 只列真正会生成的轨道(空轨在写出时被丢弃), 并标出目标端的轨号
    vt = [t for t in tl.tracks if t.kind == "video" and t.clips]
    at = [t for t in tl.tracks if t.kind == "audio" and t.clips]
    L += ["## 轨道", ""]
    if target == "resolve":
        L += ["| 源轨 | 名称 | 目标轨 | 类型 | 片段数 |", "|---|---|---|---|---|"]
        for i, t in enumerate(vt, 1):
            L.append(f"| 视频{i} | {t.name or '-'} | V{i} | video | {len(t.clips)} |")
        for i, t in enumerate(at, 1):
            L.append(f"| 音频{i} | {t.name or '-'} | A{i} | audio | {len(t.clips)} |")
        L += ["", f"> 源里另有 {len(tl.tracks) - len(vt) - len(at)} 条空轨/文本轨未写入时间线"
              "（文本已单独导出 SRT）。"]
    else:
        L += ["| # | 类型 | 片段数 |", "|---|---|---|"]
        for i, t in enumerate(tl.tracks, 1):
            L.append(f"| {i} | {t.kind} | {len(t.clips)} |")

    # 视频轨自带原声: 剪映的视频片段本来就有音轨, 这部分必须单独还原
    own = [c for t in tl.tracks if t.kind == "video" for c in t.clips
           if getattr(c, "own_audio", False)]
    if own:
        mode = getattr(tl, "video_audio_mode", "track")
        if mode == "track":
            where = "单独生成 'Vn 原声' 音轨（插在音轨区最前，A1 起依次是 V1/V2/V3 原声，剪映音频轨顺延）"
        elif mode == "video":
            where = "跟随视频片段（链接音频，不额外占音轨）"
        else:
            where = "已丢弃"
        L += ["", "## 视频轨自带原声", "",
              f"- 剪映视频片段带声音的: **{len(own)} 个** → {where}"]
        st = getattr(tl, "stats", {}) or {}
        kept = st.get("orig_audio")
        if kept is not None:
            L.append(f"- 实际写入原声片段: **{kept} 个**")
        if st.get("dedup_audio"):
            L.append(f"- 去重跳过 {st['dedup_audio']} 个: 剪映音轨上已有同源同段的音频，"
                     "再写一遍会双声")
        if st.get("dedup_self"):
            L.append(f"- 去重跳过 {st['dedup_self']} 个: 两条视频轨叠放同一素材（画中画），"
                     "声音只保留一条")
        if st.get("silent_audio"):
            L.append(f"- 跳过 {st['silent_audio']} 个: 音量被压到 0.001（≈静音）")
        if st.get("jy_dup_audio"):
            L.append(f"- ⚠️ 剪映音轨本身有 **{st['jy_dup_audio']} 处同源叠放**"
                     "（同一素材同一段落在两条音轨上），已原样保留；"
                     "如果听着有重影，需要在剪映里删掉其中一条")
        if st.get("vanim"):
            L.append(f"- 视频画面叠化（入场「渐显」/出场「渐隐」动画）: **{st['vanim']} 处**，"
                     "随导入后脚本一并写入（达芬奇导入 FCPXML 不带这些参数）")
        L.append("- 其余视频片段在剪映里已经「分离音频」或已静音，不再重复生成，"
                 "避免同一段声音出现两遍。")

    miss = [m for m in tl.media.values() if m.missing]
    if miss:
        L += ["", f"## 缺失素材 ({len(miss)})", "",
              "> 导入目标软件后需重链；路径为原始引用路径。", ""]
        L += [f"- `{m.path}`" for m in miss[:30]]
        if len(miss) > 30:
            L.append(f"- ... 其余 {len(miss)-30} 个")

    # 变换 / 变速
    tf_clips = [c for t in tl.tracks for c in t.clips if not c.transform.is_identity()]
    sp_clips = [c for t in tl.tracks for c in t.clips if abs(c.speed - 1) > 1e-4]
    L += ["", "## 已自动转换的属性", "",
          f"- 变换(位移/缩放/旋转/镜像): {len(tf_clips)} 个片段",
          f"- 变速: {len(sp_clips)} 个片段" +
          (f"（{', '.join(f'{s:.2f}x' for s in sorted({round(c.speed,2) for c in sp_clips}))}）" if sp_clips else ""),
          f"- 音量/淡变: {sum(1 for t in tl.tracks for c in t.clips if abs(c.volume-1)>1e-4)} 个音量调整, "
          f"{sum(1 for t in tl.tracks for c in t.clips if c.fade_in or c.fade_out)} 个淡变"]

    # 淡变明细: FCPXML 写成 adjust-volume 的 fadeIn/fadeOut 参数; 达芬奇不一定读,
    # 给出逐条清单, 配合 <名>_postimport.lua 一键补齐
    fade_clips = []
    for t in tl.tracks:
        if t.kind not in ("video", "audio"):
            continue
        for c in t.clips:
            if c.fade_in or c.fade_out:
                fade_clips.append((t.kind, c))
    if fade_clips:
        fade_head = ("## 淡变明细（FCPXML 写成 adjust-volume 的 fadeIn/fadeOut 参数; "
                     "若达芬奇导入后没有, 执行生成的 <名>_postimport.lua 补齐）"
                     if target == "resolve" else
                     "## 淡变明细（FCP7 XML 没有标准的淡变表示，PR XML 未写入；"
                     "需在 PR 里用淡化工具或脚本按此表补齐）")
        L += ["", fade_head, "",
              "| 类型 | 片段 | 时间线位置 | 淡入 | 淡出 |", "|---|---|---|---|---|"]
        for kind, c in fade_clips:
            m = tl.media.get(c.media_id)
            nm = (m.name if m else "") or ""
            L.append(f"| {kind} | {nm} | {c.start/1e6:.2f}s | "
                     f"{c.fade_in/1e6:.2f}s ({report_frames(c.fade_in, tl.fps)}帧) | "
                     f"{c.fade_out/1e6:.2f}s ({report_frames(c.fade_out, tl.fps)}帧) |")

    if tl.source_app == "jianying":
        L += ["", "## 剪映坐标/缩放是怎么换算的（核对时看这里）", "",
              "- **位移**：剪映 `transform` 的单位是「半个画布」，且 y 轴向上。"
              f"换算成像素：x = transform.x × {tl.width}/2，y = -transform.y × {tl.height}/2。",
              "- ⚠️ **剪映面板里那个「位置 X/Y」是实际像素的 2 倍**（它用的是整画布为基准的坐标），"
              "所以剪映面板写 X=1289 时，实际偏移是 645px，达芬奇里也应显示 ≈645。"
              "两者数字差 2 倍但画面位置一致，别被面板数字带偏。",
              "- **FCPXML 的 position 单位是「画面高度百分比」，不是像素**"
              f"（100 = 一整个画面高 {tl.height}px）。写像素会被放大 {tl.height/100:.1f} 倍、画面飞出画外。",
              "- ⚠️ **图片(贴图)片段另外还有一层补偿**：达芬奇把图片的**底边**放在 position 上，"
              "所以写出去的 Y 会比\"中心值\"多半个图片渲染高度（本工程多 83px）。"
              "在达芬奇里看到贴图的「位置 Y」比按上面公式算出来的大，是刻意的，画面位置才是对的。"
              "视频片段不受影响。",
              "- **缩放**：剪映里 `缩放 100%` 表示「素材已适应画布」，达芬奇/FCP 的默认 Image Scaling 也是"
              "「先 contain 再乘缩放」，语义相同，所以百分比数值**原样传递**（剪映 20% → 达芬奇 20%）。"
              "PR 不同：PR 的缩放以素材原始像素为 100%，转 PR 时会乘回适配系数。",
              "- 剪映有「等比缩放」时 scale.x = scale.y；达芬奇/PR 侧按同样的等比处理。"]
        if tf_clips:
            L += ["", "抽查用（取变换幅度最大的 5 个片段）：", "",
                  "| 片段 | 素材 | 时间线位置 | 位移(px) | 剪映面板 X/Y | 达芬奇里应显示 X/Y | 缩放 | 旋转 |",
                  "|---|---|---|---|---|---|---|---|"]
            for c in sorted(tf_clips, key=lambda c: -(abs(c.transform.x) + abs(c.transform.y)))[:5]:
                m = tl.media.get(c.media_id)
                name = os.path.basename(m.path) if m else c.media_id
                fit = c.transform.fit_x or 1.0
                dy = 0.0
                if m is not None and m.kind == "image" and m.height:
                    dy = m.height * abs(c.transform.scale_y) / 2.0
                L.append(f"| {c.label or '-'} | `{name}` | {c.start/1e6:.2f}s | "
                         f"x={c.transform.x:.0f} y={c.transform.y:.0f} | "
                         f"{c.transform.x*2:.0f} / {-c.transform.y*2:.0f} | "
                         f"{c.transform.x:.0f} / {-(c.transform.y + dy):.0f} | "
                         f"{c.transform.scale_x/fit*100:.1f}% | {c.transform.rotation:.1f}° |")

    # 关键帧
    kf_clips = [c for t in tl.tracks for c in t.clips if c.kfs]
    if kf_clips:
        L += ["", f"## 关键帧动画（{len(kf_clips)} 个片段）", "",
              "已写入交换文件（FCPXML 用 `<param><keyframeAnimation>`，PR XML 用 "
              "`<parameter><keyframe>`，时间相对片段起点）。若目标端 Inspector 里看不到"
              "关键帧，说明导入器不支持，只能照着下表手动重建。", "",
              "| 片段 | 时间线位置 | 属性 | 关键帧（片段内时间 → 值） |", "|---|---|---|---|"]
        for c in kf_clips[:10]:
            m = tl.media.get(c.media_id)
            name = os.path.basename(m.path) if m else c.media_id
            for k, pts in c.kfs.items():
                if k == "position":
                    s = " → ".join(f"{p[0]/1e6:.2f}s ({p[1]:.0f}, {-p[2]:.0f})" for p in pts)
                elif k == "scale":
                    fit = c.transform.fit_x or 1.0
                    s = " → ".join(f"{p[0]/1e6:.2f}s {p[1]/fit*100:.1f}%" for p in pts)
                elif k == "rotation":
                    s = " → ".join(f"{p[0]/1e6:.2f}s {p[1]:.1f}°" for p in pts)
                else:
                    s = " → ".join(f"{p[0]/1e6:.2f}s {p[1]*100:.0f}%" for p in pts)
                L.append(f"| `{name}` | {c.start/1e6:.2f}s | {k} | {s} |")

    # 调色
    groups = Counter()
    for t in tl.tracks:
        for c in t.clips:
            if c.grade:
                groups[json.dumps(c.grade, sort_keys=True, ensure_ascii=False)] += 1
    if groups:
        L += ["", f"## 调色参数（{sum(groups.values())} 个片段, {len(groups)} 组）", "",
              "已随时间线以注释/备注携带，需人工套用到目标端一级调色。", "",
              "| 组 | 次数 | 参数 |", "|---|---|---|"]
        for i, (k, n) in enumerate(groups.most_common(), 1):
            L.append(f"| {i} | {n} | `{k}` |")
        L += ["", "### 滑块映射（剪映 → 达芬奇）", "", "| 剪映 | 中文 | 达芬奇 |", "|---|---|---|"]
        used = set()
        for k in groups:
            used.update(json.loads(k).keys())
        for key in sorted(used):
            cn, dv = GRADE_MAP.get(key, (key, "—"))
            L.append(f"| {key} | {cn} | {dv} |")

    # 蒙版
    masks = [m for t in tl.tracks for c in t.clips for m in c.masks]
    L += ["", "## 蒙版（需人工重建）", ""]
    if masks:
        for m in masks:
            bz = "（贝塞尔轮廓）" if m.get("bezier") else ""
            L.append(f"- {m.get('name','')} w={m.get('width')} h={m.get('height')} "
                     f"feather={m.get('feather')}{bz}")
    else:
        L.append("- 无")

    # 特效
    fx = Counter(x for t in tl.tracks for c in t.clips for x in c.effects)
    L += ["", "## 片上特效（需人工重建）", ""]
    if fx:
        L += [f"- {k} × {v}" for k, v in fx.most_common()]
    else:
        L.append("- 无")

    # 其他需要人工补的
    if getattr(tl, "manual_items", None):
        L += ["", "## 待人工补齐", ""] + [f"- {x}" for x in tl.manual_items]

    if tl.warnings:
        L += ["", "## 转换提示", ""] + [f"- {w}" for w in tl.warnings]

    L += ["", "## 色彩空间", "",
          "剪映/达芬奇建议: DaVinci YRGB（非托管）, 时间线 Rec.709 Gamma 2.4；"
          "若来源为 PR 且已用 Lumetri，请复核色彩管理。"]

    # 导入后核对清单
    L += ["", "## 导入后核对清单", ""]
    if target == "resolve":
        items = []
        if tf_clips:
            c = sorted(tf_clips, key=lambda c: -(abs(c.transform.x) + abs(c.transform.y)))[0]
            m = tl.media.get(c.media_id)
            fit = c.transform.fit_x or 1.0
            dy = 0.0
            if m is not None and m.kind == "image" and m.height:
                dy = m.height * abs(c.transform.scale_y) / 2.0
            items.append(f"找到 `{c.label or (m.name if m else '')}`（时间线 {c.start/1e6:.2f}s）, "
                         f"看 Inspector→变换: 缩放 ≈ {c.transform.scale_x/fit*100:.0f}%, "
                         f"位置 X ≈ {c.transform.x:.0f}, Y ≈ {-(c.transform.y + dy):.0f}"
                         "（X 是剪映面板的一半, 因为剪映面板按整画布计数;"
                         " 图片片段还含\"底边对齐\"补偿, 见上一节; 若上下相反, "
                         "用 `--y-down` 重新导出）。")
            items.append(f"标定用: 上面这个片段 X 若显示成 ≈{c.transform.x*tl.width/tl.height:.0f}"
                         f"（而不是 ≈{c.transform.x:.0f}）, 说明这版达芬奇把 position 的 X 按"
                         "「宽度的百分比」解释, 反馈一下即可, 我把写出端换成按宽度。"
                         "缩放同理: 显示 ≈100% 说明它把 scale 当绝对缩放, 不带\"适应画布\"基准。")
        kf_n = sum(1 for t in tl.tracks for c in t.clips if c.kfs)
        if kf_n:
            items.append(f"{kf_n} 个片段带关键帧动画, 导入后逐一点开 Inspector 拖时间线, "
                         "确认关键帧被识别（Resolve 对 FCPXML 关键帧的支持不稳定, "
                         "没识别只能用报告里的数值手动重建）。")
        items += [f"轨道总数: 视频 {len(vt)} 条, 音频 {len(at)} 条（多出来的轨就是有片段被拆错了）。",
                  "导入前先把达芬奇里的旧时间线删掉，确认看的是刚导入的这条"
                  "（同项目里留两条同名时间线最容易看成\"剪辑错误\"）。",
                  "片头第一帧就要有画面, 时间线不应有空洞。",
                  "随机点一个变速片段, 右键→片段属性, 确认速度与剪映一致。",
                  f"音轨顺序: 剪映的「音频1」应落在 A1, 一直到「音频{len(at)}」落在 A{len(at)}。",
                  "图片片段(SRT 之外的 PNG/贴图)要能正常显示画面, 不是离线红框。",
                  "确认媒体池无离线素材。",
                  "想精确核对: 在达芬奇里 Ctrl+S 保存, 然后跑 `python cli.py doctor -p <项目名>`。"]
        L += [f"{i}. {s}" for i, s in enumerate(items, 1)]
    elif target == "pr":
        L += ["1. 导入后双击序列, 确认没有单帧片段。",
              "2. 选一个画中画片段, 效果控件里核对「位置/缩放」与剪映一致。",
              "3. 确认音频轨有波形, 且不是视频素材误入音频轨。",
              "4. 音量调整：视频片段自带音频的音量在禁用的备份轨里, 需要时手动启用。"]
    else:
        L += ["1. 复制生成的草稿目录到剪映草稿根目录, 重启剪映。",
              "2. 检查片段时间线位置与裁剪点。"]
    return "\n".join(L)


def write_all(tl: Timeline, out_dir: str, target: str, base: str):
    os.makedirs(out_dir, exist_ok=True)
    srt = srt_text(tl)
    paths = {}
    if srt.strip():
        p = os.path.join(out_dir, f"{base}.srt")
        open(p, "w", encoding="utf-8").write(srt)
        paths["srt"] = p
    tag = {"resolve": "resolve", "pr": "premiere", "jianying": "jianying"}.get(target, target)
    p = os.path.join(out_dir, f"转换报告_{tag}.md")
    open(p, "w", encoding="utf-8").write(build(tl, target))
    paths["report"] = p
    return paths
