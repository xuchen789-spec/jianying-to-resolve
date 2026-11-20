#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""nle-bridge CLI

用法:
  python cli.py inspect -i <输入>                    # 只查看时间线结构
  python cli.py convert -i <输入> -t resolve|pr|jianying|drt [-o 输出目录] [-n 名称]
  python cli.py doctor -p <达芬奇项目名> [-i <源输入>]  # 导入后核对达芬奇里的实际结构

输入自动识别:
  剪映草稿目录 / draft_content.json / *.xml(Premiere xmeml) / *.fcpxml(达芬奇导出)

转换矩阵:
  剪映 → Premiere XML / 达芬奇 FCPXML / 达芬奇原生 .drp
  Premiere XML → 达芬奇 FCPXML / 剪映草稿
  达芬奇 FCPXML → Premiere XML / 剪映草稿
  (剪映 → 剪映 可用于草稿规范化/降级为明文)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nlebridge as nb
from nlebridge import report


def main():
    ap = argparse.ArgumentParser(description="剪映 / Premiere / 达芬奇 时间线互转")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ins = sub.add_parser("inspect", help="查看时间线结构")
    p_ins.add_argument("-i", "--input", required=True)
    p_ins.add_argument("-f", "--from", dest="fmt", default="auto",
                       choices=["auto", "jianying", "pr", "fcpxml"])

    p_con = sub.add_parser("convert", help="转换")
    p_con.add_argument("-i", "--input", required=True)
    p_con.add_argument("-f", "--from", dest="fmt", default="auto",
                       choices=["auto", "jianying", "pr", "fcpxml"])
    p_con.add_argument("-t", "--to", required=True,
                       choices=["resolve", "pr", "jianying", "drt"])
    p_con.add_argument("-o", "--out", default="out")
    p_con.add_argument("-n", "--name", default=None)
    p_con.add_argument("--y-down", action="store_true",
                       help="FCPXML 的 position.y 按向下为正写出(默认向上, 画面上下镜像时用)")
    p_con.add_argument("--no-still-rename", action="store_true",
                       help="不复制静帧(默认会复制成纯字母文件名, 避免达芬奇当图片序列读)")
    p_con.add_argument("--scale-mode", default="fit", choices=["fit", "native"],
                       help="fcpxml 缩放语义: fit=相对适应画布达芬奇默认会自动适配(默认); "
                            "native=相对素材原始像素(项目里关了 Image Scaling 时用)")
    p_con.add_argument("--still-anchor", default="bottom", choices=["bottom", "center"],
                       help="静帧 position.y 的对齐基准: bottom=达芬奇实测按图片底边对齐, "
                            "写出时补半个高度(默认); center=目标端按图片中心对齐(不补)")
    p_con.add_argument("--connect", default="absolute",
                       choices=["absolute", "parent", "anchor0"],
                       help="挂靠片段的挂载方式: absolute=全部挂到第一个故事线元素下、"
                            "offset 写绝对时间(达芬奇唯一正确的方式, 默认; anchor0 是旧别名); "
                            "parent=标准 FCPX 写法, offset 相对父元素(达芬奇会读错位置, "
                            "只有给 FCPX 或严格按规范读的工具时才用)")
    p_con.add_argument("--keep-watermark", action="store_true",
                       help="保留剪映自带的推广水印层(默认剔除 materials/video/水印.png)")
    p_con.add_argument("--orig-audio", default="track",
                       choices=["track", "video", "off"],
                       help="剪映视频轨片段自带的声音怎么处理: "
                            "track=单独生成 'Vn 原声' 音轨(默认, 剪辑时最好调); "
                            "video=跟着视频片段走(成片链接音频, 少几条音轨); "
                            "off=丢弃")

    p_con.add_argument("--keep-jy-dup", action="store_true",
                       help="保留剪映音轨里完全重复的叠放(同一素材同一段落在两条轨上)。"
                            "默认会删掉被包住的那条, 刻意加强音量(>1)或有不同处理的不动")

    p_con.add_argument("--transitions", action="store_true",
                       help="把主故事线上的纯叠化/闪黑写成 FCPXML <transition>"
                            "(叠层轨与推镜/放射/炫光/闪白仍留在人工清单)")
    p_con.add_argument("--fps", type=int, default=None,
                       help="剪映草稿帧率。新版剪映不写 fps 字段, 默认按片段边界自动推断; "
                            "推断有疑问时(报告里会提示对齐率)用它强制指定")

    p_doc = sub.add_parser("doctor", help="导入后核对达芬奇工程库里的实际结构")
    p_doc.add_argument("-p", "--project", required=True,
                       help="达芬奇项目名(可部分匹配) 或 Project.db 路径")
    p_doc.add_argument("-i", "--input", default=None,
                       help="可选: 源时间线, 用来对照每条轨道落到哪")

    p_fade = sub.add_parser("fades",
                            help="把剪映的淡变(声音淡变+画面渐显/渐隐)写进达芬奇当前时间线"
                                 "(需达芬奇 21.1+ 且已打开目标时间线)")
    p_fade.add_argument("-i", "--input", required=True, help="剪映草稿目录")
    p_fade.add_argument("--orig-audio", default="track", choices=["track", "video", "off"],
                        help=argparse.SUPPRESS)
    p_fade.add_argument("--keep-jy-dup", action="store_true", help=argparse.SUPPRESS)
    p_fade.add_argument("--audit", action="store_true",
                        help="只读对账: 不写入, 逐条核对淡变+启用状态+结构")
    p_fade.add_argument("--fps", type=int, default=None,
                        help="草稿帧率(默认自动推断, 与 convert 保持一致)")

    p_mask = sub.add_parser("masks",
                            help="把剪映的形状蒙版(钢笔/矩形)+文字层写进达芬奇当前时间线"
                                 "(需达芬奇 21.1+ 且已打开目标时间线)")
    p_mask.add_argument("-i", "--input", required=True, help="剪映草稿目录")
    p_mask.add_argument("--audit", action="store_true",
                        help="只读对账: 不写入, 导出 comp 核对蒙版点位/接线/文字内容")
    p_mask.add_argument("--allow-bottom", action="store_true",
                        help="忠实模式: 该段在最底层也照草稿套蒙版(默认跳过, 因为透明区无下层可露, 成片必黑)")

    p_fix = sub.add_parser("fix-shell",
                           help="清掉时间线上草稿里不需要的 Fusion comp(空壳): 删片段→按草稿重放→回填变换/变速")
    p_fix.add_argument("-i", "--input", required=True, help="剪映草稿目录(唯一事实源)")
    p_fix.add_argument("--go", action="store_true", help="执行(默认只预演, 不动时间线)")
    p_fix.add_argument("--only", type=int, default=None, help="只处理指定起始帧(调试)")
    p_fix.add_argument("--force", action="store_true",
                       help="该段有调色节点(GetNumNodes>1)也重建 —— 会丢调色, 慎用")
    p_fix.add_argument("--enable", action="store_true", help="顺带把被禁用的片段恢复启用")

    p_tr = sub.add_parser("trans",
                          help="把剪映转场写进达芬奇当前时间线(21.1+ AddTransition; 叠化/闪白/闪黑走自带, 推镜/放射/炫光走 BCC+)")
    p_tr.add_argument("-i", "--input", required=True, help="剪映草稿目录")
    p_tr.add_argument("--audit", action="store_true",
                      help="只读: 只列计划, 不连达芬奇(离线可用)")
    p_tr.add_argument("--go", action="store_true", help="执行(默认只预演, 不动时间线)")
    p_tr.add_argument("--fps", type=int, default=None,
                      help="草稿帧率(默认取达芬奇时间线帧率; --audit 时默认 24)")

    p_post = sub.add_parser("post",
                            help="导入达芬奇后一次做完: 转场 -> 淡变 -> 蒙版(默认只预演)")
    p_post.add_argument("-i", "--input", required=True, help="剪映草稿目录(唯一事实源)")
    p_post.add_argument("--go", action="store_true", help="执行(默认只预演, 不动时间线)")
    p_post.add_argument("--audit", action="store_true",
                        help="纯离线: 只从草稿数出转场/淡变/蒙版各有多少, 不连达芬奇")
    p_post.add_argument("--only", default="trans,fades,masks",
                        help="只跑其中几步, 逗号分隔, 顺序仍按 转场->淡变->蒙版 "
                             "(默认全部; 例: --only fades,masks)")
    p_post.add_argument("--allow-bottom", action="store_true",
                        help="蒙版忠实模式: 最底层也照草稿套(默认跳过)")
    p_post.add_argument("--fix-shell", action="store_true",
                        help="开头先清掉历史空壳 comp(会删段重放, 必须最先)")
    p_post.add_argument("--fps", type=int, default=None,
                        help="草稿帧率(默认自动推断, 与 convert 一致)")

    a = ap.parse_args()

    if a.cmd == "post":
        from nlebridge import postsync
        if a.audit:
            postsync.offline_audit(a.input, fps=a.fps,
                                   allow_bottom=bool(a.allow_bottom), log=print)
            sys.exit(0)
        steps = [s.strip() for s in (a.only or "").split(",") if s.strip()]
        bad = [s for s in steps if s not in ("trans", "fades", "masks")]
        if bad:
            print("未知步骤: %s (可选 trans / fades / masks)" % ", ".join(bad))
            sys.exit(2)
        res = postsync.run(a.input, go=bool(a.go), steps=steps,
                           allow_bottom=bool(a.allow_bottom),
                           fix_shell=bool(a.fix_shell), fps=a.fps, log=print)
        sys.exit(0 if res["ok"] else 1)

    if a.cmd == "trans":
        from nlebridge import transsync
        if a.audit:
            rows = transsync.build_plan(a.input, fps=a.fps)
            if not rows:
                print("草稿里没有转场")
                sys.exit(0)
            print("%-10s %-7s %-6s %-5s %s" % ("转场", "宿主帧", "宿主长", "转场长", "目标效果"))
            for r in rows:
                cat, typ = (r["cands"][0][0], r["cands"][0][1]) if r["cands"] else ("-", "未识别→人工")
                print("%-10s %-7d %-6d %-5d %s/%s" %
                      (r["name"], r["host_frame"], r["host_dur"], r["dur_frames"], cat, typ))
            sys.exit(0)

        from nlebridge.fadesync import _connect
        r = _connect()
        proj = r.GetProjectManager().GetCurrentProject()
        tl = proj.GetCurrentTimeline()
        fps = a.fps
        if not fps:
            try:
                v = tl.GetFrameRate
                fps = int(round(v() if callable(v) else v))
            except Exception:
                fps = 24
        print("时间线: %s  %sfps  %d 帧" % (tl.GetName(), fps, tl.GetEndFrame()))
        rows = transsync.build_plan(a.input, fps=a.fps, tl_fps=float(fps))
        res = transsync.apply(tl, rows, go=bool(a.go))
        ok = sum(1 for x in res if x["done"])
        manual = [x for x in res if not x["done"]]
        print("\n共 %d 处: 就绪 %d, 待人工 %d" % (len(res), ok, len(manual)))
        if a.go:
            print("保存:", proj.GetProjectManager().SaveProject())
        elif rows:
            print("（预演，未写入。确认后加 --go）")
        sys.exit(0 if not manual else 0)

    if a.cmd == "fix-shell":
        from nlebridge import shellfix
        n_ok, n_tot, _ = shellfix.fix(a.input, go=bool(a.go), only=a.only,
                                      force=bool(a.force), enable=bool(a.enable),
                                      log=print)
        sys.exit(0 if (n_ok == n_tot or not a.go) else 1)

    if a.cmd == "masks":
        from nlebridge import masklive
        ok, fail, _ = masklive.run(a.input, audit_only=bool(a.audit), log=print,
                                   allow_bottom=bool(getattr(a, "allow_bottom", False)))
        sys.exit(0 if fail == 0 else 1)

    if a.cmd == "doctor":
        run_doctor(a)
        return

    if a.cmd == "fades":
        from nlebridge import fadesync
        ok, fail = fadesync.run(a.input,
                                orig_audio=getattr(a, "orig_audio", "track"),
                                audit_only=bool(getattr(a, "audit", False)),
                                log=print, fps=getattr(a, "fps", None))
        sys.exit(0 if fail == 0 else 1)

    tl = nb.read(a.input, a.fmt, keep_watermark=getattr(a, "keep_watermark", False),
                 orig_audio=getattr(a, "orig_audio", "track"),
                 dedup_jy=not getattr(a, "keep_jy_dup", False),
                 fps=getattr(a, "fps", None))
    print(tl.summary())
    for w in tl.warnings:
        print("  !", w)
    if a.cmd == "inspect":
        return

    base = a.name or tl.name
    out_dir = os.path.join(a.out, base) if a.to == "jianying" else a.out
    os.makedirs(out_dir, exist_ok=True)

    # 容器容错: 剪映曲库缓存会把 M4A 存成 .mp3, 达芬奇按后缀选解码器会失败
    if a.to in ("resolve", "pr"):
        n_fixed = len(nb.mediaprobe.apply(tl, out_dir))
        if n_fixed:
            print(f"  ! {n_fixed} 个素材的扩展名与实际容器不符, 已生成后缀正确的副本")

    if a.to == "resolve":
        # 静帧改名在写出器内部完成(达芬奇会把带数字的 PNG 当图片序列读)
        p = nb.w_fcpxml.write(tl, os.path.join(out_dir, f"{base}_resolve.fcpxml"),
                              y_up=not a.y_down,
                              rename_stills=not a.no_still_rename,
                              scale_mode=a.scale_mode,
                              still_anchor=a.still_anchor,
                              connect=a.connect,
                              with_transitions=getattr(a, "transitions", False))
        print("[ok]", p)
        problems, stats = nb.verify.check_fcpxml(p)
        print(f"[自检] 素材 {stats['assets']} 个, 视频片段 {stats['video_clips']}, "
              f"音频片段 {stats['audio_clips']}")
        for q in problems[:10]:
            print("  !", q)
        if problems:
            print(f"  ! 共 {len(problems)} 个问题")
        # 淡变后处理脚本: 达芬奇 FCPXML 导入不一定保留 audioFadeIn/Out,
        # 画面渐显/渐隐更是完全没有对应写法 —— 生成 Lua 供控制台一键补齐
        # (含声音淡变 + 画面叠化 + 无声片段的声音淡变)
        n_fade = len(nb.lua_post.fade_entries(tl))
        p_lua = nb.lua_post.generate(tl, os.path.join(out_dir, f"{base}_postimport.lua"))
        print("[ok]", p_lua, f"(淡变 {n_fade} 处, 含画面渐显/渐隐; 老路: 工作区>脚本>Utility 里点一下)")
        print("     导入达芬奇并打开目标时间线后, 一条命令做完 转场/淡变/蒙版:")
        print(f"       python cli.py post -i \"{a.input}\"          # 预演")
        print(f"       python cli.py post -i \"{a.input}\" --go     # 写入")
    elif a.to == "pr":
        n_fade = sum(1 for t in tl.tracks for c in t.clips if c.fade_in or c.fade_out)
        if n_fade:
            print(f"  ! {n_fade} 处音频淡变未写入 PR XML（FCP7 XML 无标准淡变表示），请在 PR 中用淡化工具补")
        if not a.no_still_rename:
            nb.mediaprobe.apply_stills(tl, out_dir)
        p = nb.w_pr_xml.write(tl, os.path.join(out_dir, f"{base}_premiere.xml"))
        print("[ok]", p)
        problems, stats = nb.verify.check_pr_xml(p)
        print(f"[自检] 素材 {stats['files']} 个, 视频片段 {stats['video_clips']}, "
              f"音频片段 {stats['audio_clips']}")
        for q in problems[:10]:
            print("  !", q)
        if problems:
            print(f"  ! 共 {len(problems)} 个问题")
    elif a.to == "drt":
        # 达芬奇原生工程包(.drp): 直接引用原始素材路径, 不做容器副本
        out_path = os.path.join(out_dir, f"{base}.drp")
        p = nb.w_drt.write(tl, out_path)
        print("[ok]", p)
        print(f"     素材池 {p['pool']} 项, 缺失 {len(p['missing'])} 个, "
              f"变速片段 {p['speed_clips']} 个(恒等 timemap 占位, 变速暂不生效)")
        for m in p["missing"][:10]:
            print("  ! 缺失素材:", m)
        if p["missing"]:
            print(f"  ! 共 {len(p['missing'])} 个缺失素材")
        print("     达芬奇 File > Import Project > 导入该 .drp, 检查轨序/位置/淡变/媒体在线")
    elif a.to == "jianying":
        p = nb.w_jianying.write(tl, out_dir, base)
        print("[ok]", p)
        print("     将整个目录复制到剪映草稿目录, 重启剪映即可看到工程")
    for k, p in report.write_all(tl, out_dir, a.to, base).items():
        print(f"[ok] {k}:", p)


def run_doctor(a):
    db = nb.resolve_probe.find_db(a.project)
    if not db:
        print("找不到达芬奇工程库。用 -p 指定项目名, 或直接给 Project.db 路径。")
        return
    print("工程库:", db)
    info = nb.resolve_probe.load(db)
    if not info["tracks"]:
        print("这个工程里没有已导入的时间线(或还没保存)。请先在达芬奇里 Ctrl+S 保存。")
        return
    print("时间线:", ", ".join(n for _i, n in info["timelines"]))
    ir = None
    if a.input:
        ir = nb.read(a.input, "auto")
    print()
    print(nb.resolve_probe.render(info, ir))


if __name__ == "__main__":
    main()
