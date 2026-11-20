# -*- coding: utf-8 -*-
"""把仓库里的 skill/ 同步到本机技能目录（单一来源：仓库）。

    python tools/sync_skill.py                 # 同步（仓库 -> 技能目录）
    python tools/sync_skill.py --check         # 只检查，有差异返回 1（CI/hook 用）
    python tools/sync_skill.py --target <目录>  # 指定技能目录
    python tools/sync_skill.py --force         # 目标端更新过也覆盖
    python tools/sync_skill.py --prune         # 删掉目标端已废弃的旧文件

规则：
  - 只同步 SKILL.md / references/*.md / scripts/*.py
  - 不同步 config.json（本机私有：root 路径、达芬奇安装位置）
  - 目标端内容更新且与源不同 = 冲突，默认不覆盖并告警（防止把手工改动冲掉）
"""
import argparse
import filecmp
import os
import pathlib
import shutil
import sys

SRC_REL = "skill"
DEFAULT_TARGET = pathlib.Path.home() / ".workbuddy" / "skills" / "nle-bridge"
SUFFIXES = (".md", ".py")
SKIP_NAMES = {"config.json", "config.example.json"}

C_SAME, C_FILL, C_UPD, C_CONFLICT, C_STALE = "same", "new", "update", "conflict", "stale"


def _iter(base):
    if not base.is_dir():
        return []
    out = []
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix not in SUFFIXES or p.name in SKIP_NAMES:
            continue
        if "__pycache__" in p.parts:
            continue
        out.append(p.relative_to(base))
    return sorted(out)


def plan(src, dst):
    """返回 [(rel, kind)]，kind 见 C_* 常量。"""
    rows = []
    src_files = _iter(src)
    for rel in src_files:
        s, d = src / rel, dst / rel
        if not d.exists():
            rows.append((rel, C_FILL))
        elif filecmp.cmp(s, d, shallow=False):
            rows.append((rel, C_SAME))
        elif s.stat().st_mtime > d.stat().st_mtime:
            rows.append((rel, C_UPD))
        else:
            rows.append((rel, C_CONFLICT))
    known = set(src_files)
    for rel in _iter(dst):
        if rel not in known:
            rows.append((rel, C_STALE))
    return rows


def sync(src, dst, force=False, prune=False, quiet=False, log=print):
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    if not src.is_dir():
        log(f"✗ 源目录不存在: {src}")
        return 1
    dst.mkdir(parents=True, exist_ok=True)

    rows = plan(src, dst)
    did = {"write": 0, "conflict": 0, "stale": 0}
    for rel, kind in rows:
        s, d = src / rel, dst / rel
        if kind in (C_FILL, C_UPD) or (kind == C_CONFLICT and force):
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s, d)
            # 复制后把 mtime 对齐到源，避免下次又被判成"目标更新"
            try:
                st = s.stat()
                os.utime(d, (st.st_atime, st.st_mtime))
            except OSError:
                pass
            did["write"] += 1
            if not quiet:
                log(f"  {'+' if kind == C_FILL else '~'} {rel}")
        elif kind == C_CONFLICT:
            did["conflict"] += 1
            log(f"  ! {rel}  技能目录里的版本更新（可能手工改过），未覆盖；"
                f"确要覆盖加 --force")
        elif kind == C_STALE:
            did["stale"] += 1
            if prune:
                d.unlink()
                log(f"  - {rel}  已删除（仓库里没有）")
            elif not quiet:
                log(f"  ? {rel}  仅存在于技能目录，仓库里没有（--prune 可删）")

    if did["write"] == 0 and did["conflict"] == 0:
        if not quiet:
            log("✓ 已是最新，无需同步")
    elif quiet:
        log(f"· 技能已从仓库同步 {did['write']} 个文件"
            + (f"，{did['conflict']} 个冲突未覆盖" if did["conflict"] else ""))
    else:
        log(f"✓ 同步完成：写入 {did['write']} 个文件"
            + (f"，冲突 {did['conflict']} 个" if did["conflict"] else ""))
    return 1 if did["conflict"] else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不写（有差异返回 1）")
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--src", default=None, help="默认 <仓库>/skill")
    a = ap.parse_args()

    here = pathlib.Path(__file__).resolve().parent.parent   # 仓库根
    src = pathlib.Path(a.src) if a.src else here / SRC_REL
    dst = pathlib.Path(a.target)

    if a.check:
        rows = plan(src, dst)
        diff = [(r, k) for r, k in rows if k != C_SAME]
        if not diff:
            print(f"✓ 技能目录与仓库一致（{len(rows)} 个文件）")
            return 0
        print(f"! 技能目录与仓库有 {len(diff)} 处差异：")
        for rel, kind in diff:
            print(f"    {kind:9s} {rel}")
        print("  跑 python tools/sync_skill.py 同步")
        return 1

    return sync(src, dst, force=a.force, prune=a.prune)


if __name__ == "__main__":
    sys.exit(main())
