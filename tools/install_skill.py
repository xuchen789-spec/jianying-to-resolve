# -*- coding: utf-8 -*-
"""把本仓库的技能包装到 WorkBuddy 技能目录，并生成 config.json。

    python tools/install_skill.py                  # 装到 ~/.workbuddy/skills/nle-bridge
    python tools/install_skill.py --target "<目录>"  # 装到别处（多版本并存 / 测试）
    python tools/install_skill.py --reset-config     # 覆盖已有的 config.json

做三件事：
  1. 把 skill/ 复制到技能目录（scripts / references / SKILL.md）
  2. 生成 config.json，root 指向本仓库（技能是薄壳，逻辑留在仓库里）
  3. 跑一遍 preflight 自检

之后升级只要 `git pull` —— 下次运行技能时会自动把 skill/ 对齐（tools/sync_skill.py）。
"""
import argparse
import json
import pathlib
import subprocess
import sys

DEFAULT_TARGET = pathlib.Path.home() / ".workbuddy" / "skills" / "nle-bridge"

RESOLVE_API = [
    "C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting",
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
    "/opt/resolve/Developer/Scripting",
]
RESOLVE_LIB = [
    "C:/Program Files/Blackmagic Design/DaVinci Resolve/fusionscript.dll",
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    "/opt/resolve/libs/Fusion/fusionscript.so",
]


def first_exist(paths):
    for p in paths:
        if pathlib.Path(p).exists():
            return p
    return None


def write_config(target, repo, reset=False):
    cfg = target / "config.json"
    if cfg.exists() and not reset:
        try:
            old = json.loads(cfg.read_text(encoding="utf-8")).get("root", "")
        except Exception:
            old = ""
        if pathlib.Path(old).is_dir():
            print(f"· 保留已有 config.json（root={old}）")
            return
        print(f"! 已有 config.json 的 root 失效（{old}），改写为新仓库路径"
              f"（要保留旧值请先备份）")
    data = {
        "root": repo.as_posix(),
        "resolve_script_api": first_exist(RESOLVE_API) or RESOLVE_API[0],
        "resolve_script_lib": first_exist(RESOLVE_LIB) or RESOLVE_LIB[0],
        "default_out": (pathlib.Path.home() / "nle-bridge-out").as_posix(),
    }
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"· 已写 config.json → root={data['root']}")
    if not first_exist(RESOLVE_API) and not first_exist(RESOLVE_LIB):
        print("  （没找到达芬奇安装目录，只有 fades / masks 需要它；装了达芬奇后改这两行）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--reset-config", action="store_true")
    a = ap.parse_args()

    repo = pathlib.Path(__file__).resolve().parent.parent
    target = pathlib.Path(a.target).expanduser()

    print(f"仓库: {repo}")
    print(f"技能: {target}\n")

    if not (repo / "cli.py").exists() or not (repo / "skill" / "SKILL.md").exists():
        print("✗ 这个脚本要放在仓库的 tools/ 里运行（找不到 cli.py 或 skill/SKILL.md）")
        return 1
    if sys.version_info < (3, 9):
        print(f"✗ Python {sys.version.split()[0]} 太旧，需要 3.9+")
        return 1

    sys.path.insert(0, str(repo / "tools"))
    import sync_skill

    target.mkdir(parents=True, exist_ok=True)
    rc = sync_skill.sync(repo / "skill", target)
    if rc:
        print("✗ 有文件冲突，未覆盖。确认后加 --force 重跑")
        return 1
    write_config(target, repo, reset=a.reset_config)

    print("\n[自检]")
    rc2 = subprocess.run([sys.executable, str(target / "scripts" / "preflight.py")]).returncode
    if rc2:
        print("! 自检有项没通过（上面标 ✗ 的那条），技能已装好但先修完再跑转换")

    print(f"""
装好了。打开 WorkBuddy（技能列表里应出现 nle-bridge，没有就重启一次），
或者直接命令行验证：
    python "{target / 'scripts' / 'nb.py'}" --help

升级：在仓库目录 git pull 即可 —— 下次运行技能时自动对齐 skill/。
反馈：https://github.com/xuchen789-spec/jianying-to-resolve/issues
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
