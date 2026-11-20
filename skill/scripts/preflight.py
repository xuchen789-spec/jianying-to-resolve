# -*- coding: utf-8 -*-
"""开工前自检。查不通就别往下走 —— 这些条件不满足时, 下游全是静默失败。

    python scripts/preflight.py                          # 基础项
    python scripts/preflight.py --draft "<草稿目录>"      # 顺带验证草稿可读
    python scripts/preflight.py --resolve                # 顺带验证达芬奇脚本端口
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

OK, BAD, WARN = "✓", "✗", "!"


def find_root():
    env = os.environ.get("NLE_BRIDGE_HOME")
    if env and os.path.isdir(env):
        return pathlib.Path(env)
    cfg = pathlib.Path(__file__).resolve().parent.parent / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return pathlib.Path(data["root"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", default=None)
    ap.add_argument("--resolve", action="store_true")
    a = ap.parse_args()

    fails = []
    root = find_root()
    print(f"仓库: {root}")

    def check(cond, msg, hard=True):
        print(f"  {OK if cond else (BAD if hard else WARN)} {msg}")
        if not cond and hard:
            fails.append(msg)
        return cond

    print("\n[1] 源码")
    check(root.is_dir() and (root / "cli.py").exists(), f"cli.py 存在（{root}）")
    check((root / "nlebridge" / "w_fcpxml.py").exists(), "nlebridge/ 完整")
    check((root / "tools" / "audit_export.py").exists(), "tools/audit_export.py 存在")

    print("\n[2] 解释器")
    check(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}")
    try:
        import Crypto  # noqa: F401
        ok = True
    except Exception:
        ok = False
    check(ok, "pycryptodome 可用（剪映加密草稿必需）")

    if a.draft:
        print("\n[3] 草稿")
        try:
            sys.path.insert(0, str(root))
            from nlebridge.jy_read import load_draft_json
            d, base = load_draft_json(a.draft)
            n_tr = len(d.get("tracks", []))
            n_seg = sum(len(t.get("segments", [])) for t in d.get("tracks", []))
            check(True, f"可读：{base}（{n_tr} 轨 / {n_seg} 段）")
        except Exception as e:
            check(False, f"草稿读不出来：{e}")

    if a.resolve:
        print("\n[4] 达芬奇")
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Resolve.exe"],
                                 capture_output=True, text=True).stdout
            running = "Resolve.exe" in out
        except Exception:
            running = True     # 非 Windows 就让下一步去判
        if check(running, "进程在跑"):
            try:
                sys.path.insert(0, str(root))
                from nlebridge.fadesync import _connect
                r = _connect()
                pm = r.GetProjectManager()
                p = pm.GetCurrentProject()
                tl = p.GetCurrentTimeline()
                check(True, f"脚本端口通：{p.GetName()} / {tl.GetName()} / {tl.GetEndFrame()} 帧")
            except Exception as e:
                check(False, f"连不上：{e}\n"
                             "       → 首选项 > 系统 > 常规 > 外部脚本使用 = 本地（改完重启达芬奇）")

    print()
    if fails:
        print(f"{BAD} {len(fails)} 项不满足，先修再跑")
        return 1
    print(f"{OK} 可以开工")
    return 0


if __name__ == "__main__":
    sys.exit(main())
