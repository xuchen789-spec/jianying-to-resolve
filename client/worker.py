# -*- coding: utf-8 -*-
"""客户端后台: 收一份 JSON 请求, 跑一次转换, 逐行吐 JSON 日志给 Electron。

协议(每行一个 JSON):
    请求  {"src":..., "src_fmt":..., "dst":..., "out":..., "opt":{...}}
    日志  {"type":"log","text":"...","kind":"ok|warn|err|dim|null"}
    结束  {"type":"done","ok":true,"main":"...","report":"...","problems":0}
          {"type":"done","ok":false,"error":"..."}

不依赖 tkinter, 只用到 nlebridge 包和标准库, 这样能被便携 Python 直接跑。
"""
import io
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
# 打包后 worker.py 与 nlebridge/ 同级; 开发时在 client/ 下, 包在上一级
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

APP_NAME = "NLE Bridge"
APP_VER = "1.0.0"


OUT = sys.stdout          # 真 stdout 只走 JSON, 其余 print 会被 LogStream 收走


def emit(obj):
    OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    OUT.flush()


def log(text, kind=None):
    emit({"type": "log", "text": str(text), "kind": kind})


def run(req):
    import nlebridge as nb
    from nlebridge import report

    src = req["src"]
    src_fmt = req.get("src_fmt") or "auto"
    dst = req["dst"]
    out_dir = req["out"]
    opt = req.get("opt") or {}

    if src_fmt == "auto":
        src_fmt = nb.detect(src)

    log(f"{APP_NAME} {APP_VER}", "dim")
    log(f"源    ：{src}")
    log(f"目标  ：{dst}")
    log(f"输出  ：{out_dir}")
    log("-" * 62, "dim")

    tl = nb.read(src, src_fmt, keep_watermark=opt.get("keep_watermark", False))
    log(f"读取成功：{tl.summary()}", "ok")
    for w in tl.warnings:
        log("  ! " + w, "warn")

    base = tl.name
    target_dir = os.path.join(out_dir, base) if dst == "jianying" else out_dir
    os.makedirs(target_dir, exist_ok=True)

    if dst in ("resolve", "pr"):
        fixed = nb.mediaprobe.apply(tl, target_dir)
        if fixed:
            log(f"  ! {len(fixed)} 个素材的扩展名与容器不符（剪映曲库常见），"
                f"已生成后缀正确的副本", "warn")

    problems = []
    main_path = None
    if dst == "resolve":
        main_path = nb.w_fcpxml.write(
            tl, os.path.join(target_dir, f"{base}_resolve.fcpxml"),
            y_up=not opt.get("y_down", False),
            rename_stills=not opt.get("no_still_rename", False),
            scale_mode=opt.get("scale_mode", "fit"),
            still_anchor="center" if opt.get("still_center") else "bottom",
            connect="absolute")
        problems, stats = nb.verify.check_fcpxml(main_path)
        log(f"写出 {os.path.basename(main_path)}：素材 {stats['assets']}，"
            f"视频片段 {stats['video_clips']}，音频片段 {stats['audio_clips']}", "ok")
    elif dst == "pr":
        n_fade = sum(1 for t in tl.tracks for c in t.clips if c.fade_in or c.fade_out)
        if n_fade:
            log(f"  ! {n_fade} 处音频淡变未写入（FCP7 XML 无标准淡变表示），"
                f"请在 PR 里用淡化工具补", "warn")
        if not opt.get("no_still_rename", False):
            nb.mediaprobe.apply_stills(tl, target_dir)
        main_path = nb.w_pr_xml.write(tl, os.path.join(target_dir, f"{base}_premiere.xml"))
        problems, stats = nb.verify.check_pr_xml(main_path)
        log(f"写出 {os.path.basename(main_path)}：素材 {stats['files']}，"
            f"视频片段 {stats['video_clips']}，音频片段 {stats['audio_clips']}", "ok")
    else:
        main_path = nb.w_jianying.write(tl, target_dir, base)
        log(f"写出剪映草稿目录：{target_dir}", "ok")
        log("把整个目录复制到剪映草稿目录，重启剪映即可看到工程", "dim")

    for q in problems[:15]:
        log("  ! " + q, "warn")
    if len(problems) > 15:
        log(f"  ! 共 {len(problems)} 个问题，详见报告", "warn")
    if not problems:
        log("自检通过：没有发现问题", "ok")

    rep = report.write_all(tl, target_dir, dst, base)
    rep_path = rep.get("report")
    if rep_path:
        log(f"报告：{os.path.basename(rep_path)}", "ok")

    emit({"type": "done", "ok": True, "main": main_path,
          "report": rep_path, "problems": len(problems),
          "outdir": target_dir})


class _LogStream(io.TextIOBase):
    """把 nlebridge 里的 print 转成 JSON 日志行, 免得污染协议"""

    def write(self, s):
        text = str(s).rstrip("\n")
        if text.strip():
            log(text)
        return len(str(s))

    def flush(self):
        pass


def main():
    raw = sys.stdin.read()
    sys.stdout = _LogStream()
    try:
        req = json.loads(raw)
        run(req)
    except Exception as e:                    # noqa: BLE001  界面要看到全部信息
        log("转换失败：" + str(e), "err")
        for line in traceback.format_exc().splitlines()[-8:]:
            log("    " + line, "dim")
        emit({"type": "done", "ok": False, "error": str(e)})


if __name__ == "__main__":
    main()
