# -*- coding: utf-8 -*-
"""剪映新版加密草稿解密 —— 可选模块。

用途边界
========
本模块只用于读取 **你自己拥有版权、并且由你本机的剪映客户端生成** 的草稿。
剪映从 10.x 起把 `draft_content.json` 加密存储，这里只是把本机文件还原成明文
以便转换，不涉及绕过授权、不涉及解密他人作品、不涉及分发受保护内容。

**本模块可以整文件删除。** 删掉之后 `nlebridge/jy_read.py` 会自动降级为
「只读未加密草稿」，转换/审计/蒙版等全部功能不受影响 —— 你只要在剪映里
用「另存为 / 导出草稿」拿到未加密版本即可。

格式备忘（剪映 10.x 实测 2026-11）
=================================
- 算法：AES-256-GCM
- 密钥材料内嵌在 Base64 原文的 12 个固定偏移处，每个偏移取 4 个字符；
  按偏移顺序拼起来，前 32 字符作 key、后 16 字符作 nonce。
- 这 12 段取值字符要从原文里剔除，剩下的部分做 Base64 解码；
  解码结果尾部 16 字节是 GCM tag。
- 判据：文件开头 64 字符不以 `{` 开头，即视为加密。
"""

from __future__ import annotations

import base64

# 密钥材料所在的固定字符偏移（每个取 4 字符）
KEY_POS = [0, 7, 20, 33, 40, 47, 59, 66, 76, 89, 99, 127]


def is_encrypted(path: str) -> bool:
    """草稿文件是否为加密格式。"""
    with open(path, encoding="utf-8", errors="ignore") as f:
        return not f.read(64).lstrip().startswith("{")


def decrypt_draft_text(path: str) -> str:
    """解密整份草稿，返回明文 JSON 文本。"""
    try:
        from Crypto.Cipher import AES
    except ImportError as e:      # pragma: no cover
        raise RuntimeError(
            "读取加密草稿需要 pycryptodome：pip install pycryptodome\n"
            "或者把草稿在剪映里另存为未加密版本。") from e

    raw = open(path, encoding="utf-8").read().strip()
    keymat, removed = "", set()
    for p in KEY_POS:
        keymat += raw[p:p + 4]
        removed.update(range(p, p + 4))
    key, iv = keymat[:32].encode(), keymat[32:].encode()
    rest = "".join(c for i, c in enumerate(raw) if i not in removed)
    data = base64.b64decode(rest + "=" * (-len(rest) % 4))
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)      # nonce = 完整 16 字节 IV
    return cipher.decrypt_and_verify(data[:-16], data[-16:]).decode("utf-8")
