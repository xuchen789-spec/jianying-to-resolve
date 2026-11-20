# -*- coding: utf-8 -*-
"""NLE Bridge 命令行入口（技能侧薄壳）。

    python scripts/nb.py inspect -i "<草稿目录>"
    python scripts/nb.py convert -i "<草稿目录>" -t resolve -o <输出目录>
    python scripts/nb.py fades   -i "<草稿目录>" [--audit]
    python scripts/nb.py masks   -i "<草稿目录>" [--audit]
    python scripts/nb.py fix-shell -i "<草稿目录>" [--go]

等价于在源码仓库里直接跑 `python cli.py ...`，只是自动定位仓库路径。
"""
import json
import os
import sys
import pathlib


def find_root():
    env = os.environ.get("NLE_BRIDGE_HOME")
    if env and os.path.isdir(env):
        return pathlib.Path(env)
    cfg = pathlib.Path(__file__).resolve().parent.parent / "config.json"
    if cfg.exists():
        try:
            root = pathlib.Path(json.loads(cfg.read_text(encoding="utf-8"))["root"])
            if (root / "cli.py").exists():
                return root
        except Exception:
            pass
    raise SystemExit(
        "找不到 NLE Bridge 源码仓库。\n"
        "请在 skill 目录的 config.json 里把 root 改成本机仓库路径"
        "（形如 D:/work/nle-bridge）。")


def main():
    root = find_root()
    os.chdir(root)
    sys.path.insert(0, str(root))
    if not os.path.exists(os.path.join(root, "cli.py")):
        raise SystemExit(f"{root} 里没有 cli.py，root 指错了？")
    import cli
    try:
        cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        sys.exit(code)


if __name__ == "__main__":
    main()
