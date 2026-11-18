#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打客户端: 拼 Electron 运行时 + 便携 Python + nlebridge, 产出可直接双击的文件夹和 zip。

用法:
    python build_client.py --electron-dist <解压的 electron dist> \
                           --python-home   <便携 Python 3.12 目录> \
                           --crypto-wheel  <pycryptodome-*.whl> \
                           [--out release]

产物:
    release/NLE Bridge 1.0.0/          双击 NLE Bridge.exe 即可用
    release/NLE-Bridge-1.0.0-win64.zip
"""
import argparse
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "NLE Bridge"
APP_VER = "1.0.0"

PY_FILES = ["python.exe", "pythonw.exe", "python3.dll", "python312.dll",
            "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"]


def copy_app(resources):
    app_dir = os.path.join(resources, "app")
    os.makedirs(app_dir, exist_ok=True)
    src_client = os.path.join(ROOT, "client", "electron")
    for name in ("main.js", "preload.js", "index.html", "renderer.js", "package.json"):
        shutil.copyfile(os.path.join(src_client, name), os.path.join(app_dir, name))
    shutil.copyfile(os.path.join(ROOT, "client", "worker.py"),
                    os.path.join(app_dir, "worker.py"))
    shutil.copytree(os.path.join(ROOT, "nlebridge"), os.path.join(app_dir, "nlebridge"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return app_dir


def copy_python(resources, python_home, crypto_wheel):
    rt = os.path.join(resources, "python")
    os.makedirs(rt, exist_ok=True)
    for name in PY_FILES:
        p = os.path.join(python_home, name)
        if os.path.isfile(p):
            shutil.copyfile(p, os.path.join(rt, name))
    shutil.copytree(os.path.join(python_home, "DLLs"), os.path.join(rt, "DLLs"),
                    ignore=shutil.ignore_patterns("__pycache__", "*_test*", "*.lib"))
    shutil.copytree(os.path.join(python_home, "Lib"), os.path.join(rt, "Lib"),
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__",
                                                  "test", "tests", "idlelib",
                                                  "ensurepip", "*.pyc"))
    site = os.path.join(rt, "Lib", "site-packages")
    os.makedirs(site, exist_ok=True)
    if crypto_wheel:
        tmp = os.path.join(rt, "_whl")
        os.makedirs(tmp, exist_ok=True)
        with zipfile.ZipFile(crypto_wheel) as z:
            z.extractall(tmp)
        for name in os.listdir(tmp):
            if name.startswith("Crypto"):
                shutil.move(os.path.join(tmp, name), os.path.join(site, name))
        shutil.rmtree(tmp, ignore_errors=True)
    # 显式路径表: 不依赖注册表/环境里的 PYTHONHOME
    with open(os.path.join(rt, "python312._pth"), "w", encoding="ascii") as f:
        f.write("Lib\nDLLs\nLib/site-packages\nimport site\n")
    return rt


def build(electron_dist, python_home, crypto_wheel, out_root):
    dest = os.path.join(out_root, f"{APP_NAME} {APP_VER}")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    # Electron 运行时(去掉默认 app, 换成我们的)
    shutil.copytree(electron_dist, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("default_app.asar"))
    exe_src = os.path.join(dest, "electron.exe")
    exe_dst = os.path.join(dest, f"{APP_NAME}.exe")
    if os.path.isfile(exe_src):
        os.replace(exe_src, exe_dst)

    resources = os.path.join(dest, "resources")
    os.makedirs(resources, exist_ok=True)
    copy_app(resources)
    copy_python(resources, python_home, crypto_wheel)

    for doc in ("README.md", "使用说明.md"):
        p = os.path.join(ROOT, doc)
        if os.path.isfile(p):
            shutil.copyfile(p, os.path.join(dest, doc))
    with open(os.path.join(dest, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(f"{APP_NAME} {APP_VER}\n")

    zp = os.path.join(out_root, f"NLE-Bridge-{APP_VER}-win64.zip")
    if os.path.isfile(zp):
        os.remove(zp)
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, _dirs, files in os.walk(dest):
            for name in files:
                full = os.path.join(base, name)
                z.write(full, os.path.relpath(full, out_root))
    return dest, zp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--electron-dist", required=True)
    ap.add_argument("--python-home", required=True)
    ap.add_argument("--crypto-wheel", default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "release"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    dest, zp = build(a.electron_dist, a.python_home, a.crypto_wheel, a.out)
    size = sum(os.path.getsize(os.path.join(b, f))
               for b, _d, fs in os.walk(dest) for f in fs)
    print(f"[ok] {dest}  ({size / 1e6:.0f} MB)")
    print(f"[ok] {zp}  ({os.path.getsize(zp) / 1e6:.0f} MB)")


if __name__ == "__main__":
    sys.exit(main())
