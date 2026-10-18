"""API 公共响应格式。

统一为：
    成功 {"ok": true, "data": ...}
    失败 {"ok": false, "error": {"code": "...", "message": "..."}}
前端只需判断 ok 字段。
"""
from __future__ import annotations

from typing import Any

from flask import jsonify


def ok(data: Any = None, status: int = 200, **extra: Any):
    payload = {"ok": True, "data": data}
    payload.update(extra)
    if status != 200:
        return jsonify(payload), status
    return jsonify(payload)


def fail(code: str, message: str, status: int = 400, **extra: Any):
    payload = {"ok": False, "error": {"code": code, "message": message}}
    payload.update(extra)
    return jsonify(payload), status
