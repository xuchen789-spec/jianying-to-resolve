#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""NLE Bridge 客户端（第一版）

剪映 / Premiere / 达芬奇 时间线互转的桌面界面。所有转换逻辑都在 nlebridge 包里，
这个文件只负责收集参数、跑转换线程、把日志显示出来。

启动:
    python nlebridge_gui.py
"""
import os
import queue
import sys
import threading
import traceback
import webbrowser

APP_NAME = "NLE Bridge"
APP_VER = "1.0.0"

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nlebridge as nb
from nlebridge import report

SRC_KINDS = [
    ("jianying", "剪映草稿目录"),
    ("pr", "Premiere XML"),
    ("fcpxml", "达芬奇 FCPXML"),
]
DST_KINDS = [
    ("resolve", "达芬奇（FCPXML）"),
    ("pr", "Premiere（XML）"),
    ("jianying", "剪映草稿（反向）"),
]

MONO = ("Consolas", 9) if sys.platform == "win32" else ("Menlo", 11)


def convert(src, src_fmt, dst, out_dir, opt, log):
    """跑一次转换。log(str, kind) 输出一行日志。返回 (主文件路径, 报告路径, 问题列表)"""
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

    main_path, problems, stats = None, [], {}
    if dst == "resolve":
        main_path = nb.w_fcpxml.write(
            tl, os.path.join(target_dir, f"{base}_resolve.fcpxml"),
            y_up=not opt.get("y_down", False),
            rename_stills=not opt.get("no_still_rename", False),
            scale_mode=opt.get("scale_mode", "fit"),
            still_anchor="center" if opt.get("still_center") else "bottom",
            connect=opt.get("connect", "absolute"))
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
    return main_path, rep_path, problems


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VER} — 剪映 / Premiere / 达芬奇 时间线互转")
        self.geometry("980x680")
        self.minsize(860, 560)
        self.q = queue.Queue()
        self.worker = None
        self.last_dir = None
        self.last_report = None

        self.src_fmt = tk.StringVar(value="jianying")
        self.src_path = tk.StringVar()
        self.dst_fmt = tk.StringVar(value="resolve")
        self.out_dir = tk.StringVar(value=os.path.join(
            os.path.expanduser("~"), "Desktop", "nle-bridge-out"))
        self.keep_watermark = tk.BooleanVar(value=False)
        self.still_center = tk.BooleanVar(value=False)
        self.no_still_rename = tk.BooleanVar(value=False)
        self.y_down = tk.BooleanVar(value=False)
        self.scale_mode = tk.StringVar(value="fit")
        self.status = tk.StringVar(value="就绪")

        self._build()
        self.after(80, self._drain)
        self._on_dst_change()

    # ---------- 界面 ----------
    def _build(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        st.configure("Big.TButton", padding=(16, 6))

        pad = {"padx": 10, "pady": 6}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, **pad)

        # ---- 源 ----
        f1 = ttk.LabelFrame(root, text=" 1. 源工程 ")
        f1.pack(fill="x")
        row = ttk.Frame(f1)
        row.pack(fill="x", padx=10, pady=(8, 4))
        for i, (val, label) in enumerate(SRC_KINDS):
            ttk.Radiobutton(row, text=label, value=val, variable=self.src_fmt,
                            command=self._on_src_change).pack(side="left", padx=(0, 14))
        row2 = ttk.Frame(f1)
        row2.pack(fill="x", padx=10, pady=(0, 10))
        self.e_src = ttk.Entry(row2, textvariable=self.src_path)
        self.e_src.pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="浏览…", command=self._pick_src).pack(side="left", padx=(8, 0))

        # ---- 目标 ----
        f2 = ttk.LabelFrame(root, text=" 2. 目标 ")
        f2.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(f2)
        row.pack(fill="x", padx=10, pady=8)
        for val, label in DST_KINDS:
            ttk.Radiobutton(row, text=label, value=val, variable=self.dst_fmt,
                            command=self._on_dst_change).pack(side="left", padx=(0, 14))

        # ---- 输出 ----
        f3 = ttk.LabelFrame(root, text=" 3. 输出目录 ")
        f3.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(f3)
        row.pack(fill="x", padx=10, pady=8)
        ttk.Entry(row, textvariable=self.out_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._pick_out).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="打开", command=self._open_out).pack(side="left", padx=(6, 0))

        # ---- 高级 ----
        f4 = ttk.LabelFrame(root, text=" 高级选项 ")
        f4.pack(fill="x", pady=(10, 0))
        g = ttk.Frame(f4)
        g.pack(fill="x", padx=10, pady=8)
        self.cb_wm = ttk.Checkbutton(g, text="保留剪映推广水印", variable=self.keep_watermark)
        self.cb_still = ttk.Checkbutton(g, text="静帧按中心对齐（默认按达芬奇实测的底边对齐）",
                                        variable=self.still_center)
        self.cb_sc = ttk.Checkbutton(g, text="平移按 Y 向下（画面上下镜像时才勾）",
                                     variable=self.y_down)
        self.cb_wm.grid(row=0, column=0, sticky="w", padx=(0, 18))
        self.cb_still.grid(row=0, column=1, sticky="w", padx=(0, 18))
        self.cb_sc.grid(row=0, column=2, sticky="w")
        ttk.Label(g, text="缩放语义").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cb_scale = ttk.Combobox(g, textvariable=self.scale_mode, width=24, state="readonly",
                                     values=["fit（相对适应画布，默认）", "native（相对素材原始像素）"])
        self.cb_scale.current(0)
        self.cb_scale.grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(g, text="（缩放语义只影响达芬奇；项目里关掉 Image Scaling 才用 native）",
                  foreground="#666").grid(row=1, column=2, columnspan=2, sticky="w",
                                          padx=(10, 0), pady=(8, 0))

        # ---- 操作 ----
        f5 = ttk.Frame(root)
        f5.pack(fill="x", pady=(12, 0))
        self.btn_run = ttk.Button(f5, text="开始转换", style="Big.TButton",
                                  command=self._run)
        self.btn_run.pack(side="left")
        self.btn_rep = ttk.Button(f5, text="打开报告", command=self._open_report,
                                  state="disabled")
        self.btn_rep.pack(side="left", padx=(8, 0))
        ttk.Button(f5, text="清空日志", command=self._clear).pack(side="left", padx=(8, 0))

        # ---- 日志 ----
        f6 = ttk.LabelFrame(root, text=" 日志 ")
        f6.pack(fill="both", expand=True, pady=(10, 0))
        self.txt = tk.Text(f6, wrap="word", font=MONO, height=14, relief="flat",
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4")
        sb = ttk.Scrollbar(f6, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.txt.tag_configure("ok", foreground="#6fcf97")
        self.txt.tag_configure("warn", foreground="#e5c07b")
        self.txt.tag_configure("err", foreground="#e06c75")
        self.txt.tag_configure("dim", foreground="#8a8a8a")
        self.txt.configure(state="disabled")

        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(side="left", padx=10, pady=4)
        ttk.Label(bar, text=f"v{APP_VER}", foreground="#888").pack(side="right", padx=10)

    # ---------- 事件 ----------
    def _on_src_change(self):
        self.src_path.set("")

    def _on_dst_change(self):
        is_resolve = self.dst_fmt.get() == "resolve"
        state = "normal" if is_resolve else "disabled"
        self.cb_still.configure(state=state)
        self.cb_sc.configure(state=state)
        self.cb_scale.configure(state="readonly" if is_resolve else "disabled")

    def _pick_src(self):
        kind = self.src_fmt.get()
        if kind == "jianying":
            p = filedialog.askdirectory(title="选择剪映草稿目录（里面有 draft_content.json）")
        else:
            p = filedialog.askopenfilename(
                title="选择交换文件",
                filetypes=[("FCPXML / XML", "*.fcpxml *.xml"), ("所有文件", "*.*")])
        if p:
            self.src_path.set(os.path.normpath(p))

    def _pick_out(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.out_dir.set(os.path.normpath(p))

    def _open_out(self):
        d = self.out_dir.get()
        if os.path.isdir(d):
            self._open(d)
        else:
            messagebox.showinfo(APP_NAME, "输出目录还不存在，转换一次后会自动创建。")

    def _open_report(self):
        if self.last_report and os.path.isfile(self.last_report):
            self._open(self.last_report)

    @staticmethod
    def _open(path):
        try:
            os.startfile(path)          # noqa: S606  (Windows 专用)
        except AttributeError:
            webbrowser.open("file://" + path)

    def _clear(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _log(self, msg, kind=None):
        self.q.put((msg, kind))

    def _drain(self):
        try:
            while True:
                msg, kind = self.q.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", msg + "\n", kind or ())
                self.txt.see("end")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain)

    # ---------- 转换 ----------
    def _opts(self):
        scale = self.scale_mode.get().split("（")[0].strip()
        return {
            "keep_watermark": self.keep_watermark.get(),
            "still_center": self.still_center.get(),
            "no_still_rename": self.no_still_rename.get(),
            "y_down": self.y_down.get(),
            "scale_mode": scale if scale in ("fit", "native") else "fit",
            "connect": "absolute",
        }

    def _run(self):
        if self.worker and self.worker.is_alive():
            return
        src = self.src_path.get().strip()
        if not src:
            messagebox.showwarning(APP_NAME, "先选一个源工程（剪映草稿目录 / XML / FCPXML）。")
            return
        if not os.path.exists(src):
            messagebox.showerror(APP_NAME, f"路径不存在：\n{src}")
            return
        out = self.out_dir.get().strip() or os.path.join(
            os.path.expanduser("~"), "Desktop", "nle-bridge-out")
        self.out_dir.set(out)

        self._clear()
        self.btn_run.configure(state="disabled", text="转换中…")
        self.btn_rep.configure(state="disabled")
        self.status.set("正在转换…")
        self._log(f"{APP_NAME} {APP_VER}", "dim")
        self._log(f"源    ：{src}")
        self._log(f"目标  ：{dict(DST_KINDS)[self.dst_fmt.get()]}")
        self._log(f"输出  ：{out}")
        self._log("-" * 60, "dim")

        args = (src, self.src_fmt.get(), self.dst_fmt.get(), out, self._opts())
        self.worker = threading.Thread(target=self._job, args=args, daemon=True)
        self.worker.start()

    def _job(self, src, src_fmt, dst, out, opt):
        try:
            main_path, rep, problems = convert(src, src_fmt, dst, out, opt, self._log)
            self.last_dir = os.path.dirname(main_path or out)
            self.last_report = rep
            self.after(0, self._done, main_path, problems, None)
        except Exception as e:                      # noqa: BLE001  界面上要看到全部信息
            self._log("转换失败：" + str(e), "err")
            for line in traceback.format_exc().splitlines()[-6:]:
                self._log("    " + line, "dim")
            self.after(0, self._done, None, [], e)

    def _done(self, main_path, problems, err):
        self.btn_run.configure(state="normal", text="开始转换")
        if err is not None:
            self.status.set("转换失败")
            messagebox.showerror(APP_NAME, f"转换失败：\n{err}")
            return
        if self.last_report:
            self.btn_rep.configure(state="normal")
        if problems:
            self.status.set(f"完成，但有 {len(problems)} 个待确认项（见日志/报告）")
            messagebox.showwarning(
                APP_NAME, f"转换完成，自检发现 {len(problems)} 个待确认项。\n\n"
                          f"输出：{os.path.dirname(main_path or '')}\n"
                          "点“打开报告”看详情。")
        else:
            self.status.set("完成")
            messagebox.showinfo(APP_NAME, f"转换完成，自检无问题。\n\n"
                                          f"输出：{os.path.dirname(main_path or '')}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-v", "--version"):
        print(f"{APP_NAME} {APP_VER}")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
