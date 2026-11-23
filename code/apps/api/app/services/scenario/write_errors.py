"""场景**写侧**错误码 —— 与 `errors.py` 的三码**不合并**（受众不同）。

`errors.py` 的三个码（`SCENARIO_NOT_ALLOWED` / `MISMATCH` / `UNRESOLVABLE`）的读者是
**外部集成系统**：契约要求「响应体只有固定文案、诊断只进日志」（`errors.py:1-14`）。
写接口的读者是**本租户管理员**：它需要 `field` 与 `context` 才能自助修配置。
两者受众、文案策略、可泄露内容全都不同 —— 合成一个枚举会让将来每个码都要写
「这个码能不能带 context」的特例。

★ 13 个码，每个对应客户端的一种**不同处置**（这是「能不能合并」的唯一判据）。
  第 13 个（`SCENARIO_KB_EMPTY_BUT_INDEXED`，Step 4B-1 新增）的处置是
  「去查解析/迁移链路」—— 责任方与其余 12 个（改配置 / 改内容 / 重试）都不同。
  `SCENARIO_NOT_PUBLISHABLE` 是三处 T4 拒因的同一个码，靠 `field` 区分 —— 客户端
  对它们的处置完全相同（去改内容或改平台开关），拆成三个码只会让客户端写三个 case。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Final

from sqlalchemy.exc import IntegrityError


class WriteCode(str, Enum):
    """写侧失败码。值即响应体 `detail.code`（稳定机器键）。"""

    WRITE_INVALID = "SCENARIO_WRITE_INVALID"
    NOT_PUBLISHABLE = "SCENARIO_NOT_PUBLISHABLE"
    NOT_FOUND = "SCENARIO_NOT_FOUND"
    KEY_RESERVED = "SCENARIO_KEY_RESERVED"
    KEY_EXISTS = "SCENARIO_KEY_EXISTS"
    BUILTIN_IMMUTABLE = "SCENARIO_BUILTIN_IMMUTABLE"
    STATE_CONFLICT = "SCENARIO_STATE_CONFLICT"
    CHECKSUM_MISMATCH = "SCENARIO_CHECKSUM_MISMATCH"
    PRECONDITION_REQUIRED = "SCENARIO_PRECONDITION_REQUIRED"
    IN_USE = "SCENARIO_IN_USE"
    VERSION_CONFLICT = "SCENARIO_VERSION_CONFLICT"
    PUBLISH_CONFLICT = "SCENARIO_PUBLISH_CONFLICT"
    # Step 4B-1：唯一新增码。判据仍是「客户端的一种不同处置」——
    # 本码的处置是「去查数据管道（解析 / 迁移）」，责任方与上面 12 个都不同
    # （上面全是"改配置 / 改内容 / 重试"），所以不能并入 SCENARIO_NOT_PUBLISHABLE。
    KB_EMPTY_BUT_INDEXED = "SCENARIO_KB_EMPTY_BUT_INDEXED"


_HTTP: Final[dict[WriteCode, int]] = {
    WriteCode.WRITE_INVALID: 400,
    WriteCode.NOT_PUBLISHABLE: 400,
    WriteCode.NOT_FOUND: 404,
    WriteCode.KEY_RESERVED: 409,
    WriteCode.KEY_EXISTS: 409,
    WriteCode.BUILTIN_IMMUTABLE: 409,
    WriteCode.STATE_CONFLICT: 409,
    WriteCode.CHECKSUM_MISMATCH: 409,
    # 「你没给条件」≠「条件不满足」：混成 409 后客户端无法区分「我拿的是旧值」
    # 与「我漏发头」（设计 §C.2.3）。
    WriteCode.PRECONDITION_REQUIRED: 428,
    WriteCode.IN_USE: 409,
    WriteCode.VERSION_CONFLICT: 409,
    WriteCode.PUBLISH_CONFLICT: 409,
    # 409 而非 400：请求体完全合法，是**世界的状态**与这个请求冲突（同 STATE_CONFLICT）。
    # 400 会让客户端以为改请求能解决，而它必须去查解析/迁移链路。
    WriteCode.KB_EMPTY_BUT_INDEXED: 409,
}

_MSG: Final[dict[WriteCode, str]] = {
    WriteCode.WRITE_INVALID: "请求内容不合法",
    WriteCode.NOT_PUBLISHABLE: "当前内容在现平台配置下不可发布",
    WriteCode.NOT_FOUND: "场景不存在",
    WriteCode.KEY_RESERVED: "该 key 为内置场景保留",
    WriteCode.KEY_EXISTS: "本租户已存在同名 key 的场景",
    WriteCode.BUILTIN_IMMUTABLE: "内置场景不可修改",
    WriteCode.STATE_CONFLICT: "当前状态不允许该操作",
    WriteCode.CHECKSUM_MISMATCH: "内容已被修改，请重新读取后再提交",
    WriteCode.PRECONDITION_REQUIRED: "缺少 If-Match 头",
    WriteCode.IN_USE: "仍有集成应用绑定该场景，请先换绑",
    WriteCode.VERSION_CONFLICT: "版本号被并发占用，请重试",
    WriteCode.PUBLISH_CONFLICT: "并发发布冲突，请重试",
    WriteCode.KB_EMPTY_BUT_INDEXED: "场景绑定的知识库有已索引文档但没有任何 chunk",
}


def http_status(code: WriteCode) -> int:
    return _HTTP[code]


def message(code: WriteCode) -> str:
    return _MSG[code]


class WriteError(Exception):
    """写侧失败。路由层把它转成 `{"detail": {code, message, field, context}}`。

    ★ 与 `PolicyValidationError` 一样属于**可预期**失败：`main.py` 全局处理器不会拦它
      （只拦 `ValueError` 与未捕获异常），因此路由层必须显式捕获。
    """

    code: WriteCode
    field: str | None
    context: dict[str, Any]

    def __init__(
        self,
        code: WriteCode,
        *,
        message_override: str | None = None,
        field: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.field = field
        self.context = dict(context or {})
        super().__init__(message_override or _MSG[code])

    @property
    def message(self) -> str:
        return str(self)


def constraint_name(exc: IntegrityError) -> str | None:
    """从 `IntegrityError` 里提取**具体约束名**（没有就返回 `None`）。

    ★ 为什么必须是显式函数并被单独测（设计 §B.5.3）：asyncpg 把约束名藏在
      `exc.orig.__cause__` 上，SQLAlchemy 不统一暴露。而**绝不许**写
      `except IntegrityError: 409` —— 那会把 CHECK 违反、FK 违反全变成「并发冲突」，
      是本项目最容易造成假绿的一类写法（`test_0028_pg_constraints.py:11-18` 的纪律）。
    """
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    text = str(orig or exc)
    match = re.search(r'constraint "([^"]+)"', text)
    if match:
        return match.group(1)
    match = re.search(r"违反检查约束 \"([^\"]+)\"|violates check constraint \"([^\"]+)\"", text)
    if match:
        return match.group(1) or match.group(2)
    return None
