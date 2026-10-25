"""标识与摘要 —— 对应《详细设计说明书》3.1 节。

ID：UUIDv7（时间有序、不可猜测），由写入服务分配；
摘要：SHA-256 十六进制，内容绑定用，不对外公开低熵内容摘要（9.5 节）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid


def uuid7() -> str:
    """生成 UUIDv7 字符串。不依赖运行时是否提供 uuid.uuid7，手工构造。"""
    ms = time.time_ns() // 1_000_000
    rand = os.urandom(10)
    ba = bytearray(ms.to_bytes(6, "big") + rand)
    ba[6] = (ba[6] & 0x0F) | 0x70  # version 7
    ba[8] = (ba[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(ba)))


def canonical_json(obj) -> str:
    """规范化 JSON 文本：键排序、紧凑分隔。JCS 思路的最小实现（9.1 节在此基础上另有编码规则）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_obj(obj) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
