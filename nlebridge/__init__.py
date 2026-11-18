# -*- coding: utf-8 -*-
"""nle-bridge: 剪映 / Premiere / 达芬奇 时间线互转"""
__version__ = "0.1.0"

from . import (ir, jy_read, pr_read, fcpxml_read, w_pr_xml, w_fcpxml, w_jianying,
               w_drt, report, verify, mediaprobe, resolve_probe, lua_post)  # noqa: F401

READERS = {
    "jianying": jy_read.read,
    "pr": pr_read.read,
    "fcpxml": fcpxml_read.read,
}


def read(path: str, fmt: str = "auto", **kw):
    if fmt == "auto":
        fmt = detect(path)
    if fmt == "jianying":
        return jy_read.read(path, **kw)
    return READERS[fmt](path)


def detect(path: str) -> str:
    import os
    if os.path.isdir(path):
        return "jianying"
    lower = path.lower()
    if lower.endswith(".fcpxml"):
        return "fcpxml"
    if lower.endswith(".json"):
        return "jianying"
    if lower.endswith(".xml"):
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(2048)
        return "fcpxml" if "<fcpxml" in head else "pr"
    raise ValueError(f"无法识别输入格式: {path}")
