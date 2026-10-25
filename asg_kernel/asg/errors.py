"""ASG 错误码 —— 对应《详细设计说明书》第 10 章目录的阶段 A 子集。"""
from __future__ import annotations

# 错误码目录（第 10 章表 25 的阶段 A 子集；其余码随阶段 B/C 补充）
ERROR_NAMES = {
    "E101": "SCHEMA_INVALID",
    "E102": "SCHEMA_VERSION",
    "E103": "KIND_NOT_ALLOWED",
    "E201": "HEAD_CONFLICT",
    "E202": "REQUEST_ID_REUSE",
    "E203": "REVISE_BASELINE",
    "E301": "REF_NOT_FOUND",
    "E302": "FUTURE_REF",
    "E303": "REF_NOT_READABLE",
    "E501": "BUDGET_EXCEEDED",
    "E502": "RESERVATION_LOST",
}


class AsgError(Exception):
    """统一错误返回结构 {error_code, message, retryable, details}（第 10 章）。"""

    def __init__(self, code: str, message: str, retryable: bool = False, details: dict | None = None):
        self.code = code
        self.name = ERROR_NAMES.get(code, "UNKNOWN")
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        super().__init__(f"{code} {self.name}: {message}")

    def to_dict(self) -> dict:
        return {
            "error_code": self.code,
            "name": self.name,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
