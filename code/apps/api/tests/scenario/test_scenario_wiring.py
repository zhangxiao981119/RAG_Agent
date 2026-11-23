"""F.2/F.3（NO-DB 层）：接线点的零影响证据 —— Z-3、SQL 租户隔离、meta 只填 4 个字段、MCP 契约。

这一层的每一条都是**可执行的**断言，不需要 PG，也不需要大模型：

  · `test_effective_kb_ids_unchanged`（Z-3）：与 `chat.py:243-255` 改动前的实现
    逐组对照，顺序与内容都必须相同；
  · `test_lookup_sql_tenant_scoped`：解析 SQL 必须带 `tenant_id` 且 `OR` 有显式括号；
  · `test_mcp_contract_backward_compat`（Z-4 / B-1）：MCP 工具签名逐字不变；
  · `test_meta_required_fields_only_step2`（B-4）：`meta` 只有 Step 2 能保证为真的 4 个键。
"""
from __future__ import annotations

import ast
import hashlib
import pathlib
import random
import uuid

import pytest

import app.api.integrations as integrations_mod
from app.api.deps import CurrentUser
from app.infra import metrics
from app.services.scenario import ScenarioError, resolve_scenario
from app.services.scenario.resolver import LOOKUP_SQL
from app.services.retrieve.base import RetrievalResult, RetrievedChunk
from tests.scenario.conftest import (
    KB_A,
    KB_B,
    SCENARIO_ID,
    TENANT_ID,
    FakeSession,
    build_row,
    make_application,
)

_APPS_DIR = pathlib.Path(__file__).resolve().parents[3]

# T-5-17：LOOKUP_SQL 的逐字节金标准（2026-11-22 实测）。正当变更流程同 T-5-14：
# 改 resolver.LOOKUP_SQL → 跑本文件拿到新值 → 更新此字面量并说明理由。
_STEP5_LOOKUP_SQL_SHA256 = "05536f35c66c2c2e9c1f3b1b8efacc076379252a582fbabdcb00046325c1e005"


# ══════════════════════════════════════════════════════════════════════
# Z-3：三重交集与改动前逐字节相同
# ══════════════════════════════════════════════════════════════════════


def _legacy_integration_kb_ids(
    requested_kb_ids: list[uuid.UUID],
    user: CurrentUser,
    application,
) -> list[uuid.UUID]:
    """`chat.py:243-255` 改动**前**的实现，逐字复制作为对照基准。

    ★ 复制而不是引用：引用生产函数就等于「拿实现给自己做证」，
      实现被改坏时两边一起错、测试仍绿。
    """
    application_kb_ids = {uuid.UUID(kb_id) for kb_id in application.kb_ids}
    authorized_kb_ids = set(user.authorized_kb_ids)
    return [
        kb_id
        for kb_id in requested_kb_ids
        if kb_id in application_kb_ids and kb_id in authorized_kb_ids
    ]


def _user(authorized: list[uuid.UUID]) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("77777777-7777-7777-7777-777777777777"),
        tenant_id=TENANT_ID,
        username="real-user",
        display_name="真实用户",
        clearance=40,
        dept_path="/总部/",
        subjects=["user:x"],
        authorized_kb_ids=authorized,
        acl_epoch=1,
    )


@pytest.mark.asyncio
async def test_effective_kb_ids_unchanged():
    """10 组随机 (requested, app.kb_ids, authorized)：输出顺序与内容均相同（Z-3）。

    走的是真实路径：`resolve_scenario()` 产出快照 → `list(snapshot.kb_ids)` →
    `_integration_kb_ids`，与改动前对 `payload.kb_ids` 直接求交集对照。
    """
    from app.api.chat import _integration_kb_ids

    rng = random.Random(20261120)
    pool = [uuid.uuid4() for _ in range(6)]
    for _ in range(10):
        requested = rng.sample(pool, rng.randint(1, len(pool)))
        app_kb_ids = rng.sample(pool, rng.randint(1, len(pool)))
        authorized = rng.sample(pool, rng.randint(1, len(pool)))
        user = _user(authorized)
        application = make_application(kb_ids=tuple(str(k) for k in app_kb_ids))

        snapshot = await resolve_scenario(
            FakeSession([build_row(is_builtin=True)]),
            tenant_id=TENANT_ID,
            application=application,  # scenario_id=None —— 存量应用的形态
            requested_kb_ids=requested,
        )
        new = _integration_kb_ids(list(snapshot.kb_ids), user, application)
        old = _legacy_integration_kb_ids(requested, user, application)
        assert new == old, f"交集结果变了：requested={requested}"
        assert [str(x) for x in new] == [str(x) for x in old]


@pytest.mark.asyncio
async def test_effective_kb_ids_is_still_a_pure_intersection():
    """场景**不额外限制**请求集合：快照的 kb_ids 恒等于请求集合（R5/R6）。"""
    requested = [KB_A, KB_B]
    snapshot = await resolve_scenario(
        FakeSession([build_row(is_builtin=True)]),
        tenant_id=TENANT_ID,
        application=make_application(),
        requested_kb_ids=requested,
    )
    assert snapshot.kb_ids == tuple(requested)


# ══════════════════════════════════════════════════════════════════════
# SQL：租户条件与显式括号
# ══════════════════════════════════════════════════════════════════════


def _or_is_parenthesized(sql: str) -> bool:
    """判断 `OR` 是否处在一个括号组内（扫描到 OR 时深度 > 0）。"""
    depth = 0
    index = 0
    upper = sql
    while index < len(upper):
        char = upper[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth > 0 and upper.startswith("OR ", index) and (
            index == 0 or not upper[index - 1].isalnum()
        ):
            return True
        index += 1
    return False


def test_lookup_sql_tenant_scoped():
    """解析 SQL 必须带 `s.tenant_id = :tenant_id`；`OR` 两侧必须有显式括号。

    缺 tenant 条件 = 跨租户读（唯一防线）；缺显式括号 = 任何人后续插一个 `AND`
    都会改变语义（`AND` 优先级高于 `OR`）。
    """
    assert "s.tenant_id = :tenant_id" in LOOKUP_SQL
    # 版本行也按租户过滤（加严）
    assert "v.tenant_id = :tenant_id" in LOOKUP_SQL
    assert _or_is_parenthesized(LOOKUP_SQL), "OR 没有显式括号"
    # 括号必须配平
    assert LOOKUP_SQL.count("(") == LOOKUP_SQL.count(")")
    # 只读：解析路径 MUST NOT 写库
    for keyword in ("INSERT", "UPDATE", "DELETE"):
        assert keyword not in LOOKUP_SQL.upper()


# ══════════════════════════════════════════════════════════════════════
# resolver 的接线形状：meta 只填 4 个字段（B-4）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_meta_required_fields_only_step2():
    """`meta` 恰为 Step 2 能保证为真的 4 个键，且不含 Step 4 才有的字段。"""
    from app.config import decisions

    snapshot = await resolve_scenario(
        FakeSession([build_row(is_builtin=True)]),
        tenant_id=TENANT_ID,
        application=make_application(),
        requested_kb_ids=[KB_A],
    )
    meta = integrations_mod._scenario_meta(snapshot)
    assert set(meta) == {
        "scenario_key",
        "scenario_version",
        "scenario_fingerprint",
        "contract_version",
    }
    assert meta["scenario_key"] == "default"
    assert meta["scenario_version"] == 1
    assert meta["contract_version"] == decisions.CONTRACT_VERSION
    assert meta["scenario_fingerprint"] == snapshot.fingerprint
    # Step 4 才有意义的字段一个都不许现在填（填 None = 在契约里承诺一个假值）
    for later in (
        "dna_fingerprint",
        "score_source",
        "use_rerank",
        "policy_applied",
    ):
        assert later not in meta


def test_response_models_expose_meta_additively():
    """`meta` 是**纯增量**字段（默认 None），旧响应契约字段全部保留。"""
    from app.schemas.integration import IntegrationAskResponse, IntegrationSearchResponse

    ask_fields = IntegrationAskResponse.model_fields
    assert {"conversation_id", "message_id", "answer", "citations", "refused",
            "refuse_reason", "message", "usage", "suggestions"} <= set(ask_fields)
    assert ask_fields["meta"].default is None
    assert IntegrationSearchResponse.model_fields["meta"].default is None
    assert "items" in IntegrationSearchResponse.model_fields


# ══════════════════════════════════════════════════════════════════════
# 接线：两个 handler 都过场景解析，失败转 HTTP 错误（fail-closed）
# ══════════════════════════════════════════════════════════════════════


def _principal(application, user):
    return integrations_mod.IntegrationPrincipal(user, application)


async def _no_rate_limit(user, application):
    return None


class _Retrieval:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        # Step 3：桩必须与生产签名一致（`retrieve(..., *, policy=None)`），
        # 否则「策略有没有传到检索层」无法被断言。只记录，不改任何既有断言。
        self.policies: list = []

    async def retrieve(self, session, query, tenant_id, kb_ids, clearance, subjects,
                       *, policy=None):
        self.calls.append((query, tenant_id, list(kb_ids), clearance, subjects))
        self.policies.append(policy)
        return RetrievalResult(chunks=[_chunk()])


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=KB_A,
        filename="制度.pdf",
        content="住宿标准 500 元",
        heading_path="差旅",
        page_no=1,
        level_rank=20,
        vector_score=0.8,
        keyword_score=None,
        rrf_score=0.8,
        rerank_score=0.9,
        final_score=0.9,
    )


@pytest.mark.asyncio
async def test_search_handler_fills_meta_and_keeps_kb_ids(monkeypatch):
    """`/integrations/search`：meta 有 4 个键，检索入参的 kb_ids 与改动前一致。"""
    from app.schemas.integration import IntegrationSearchRequest

    async def _audit(*args, **kwargs):
        return None

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    monkeypatch.setattr(integrations_mod.audit, "record", _audit)
    retrieval = _Retrieval()
    monkeypatch.setattr(integrations_mod, "get_retrieval_service", lambda: retrieval)

    application = make_application(kb_ids=(str(KB_A), str(KB_B)))
    user = _user([KB_A, KB_B])
    result = await integrations_mod.integration_search(
        IntegrationSearchRequest(query="住宿标准", kb_ids=[KB_A, KB_B]),
        _principal(application, user),
        FakeSession([build_row(is_builtin=True)]),
    )
    assert set(result.meta) == {
        "scenario_key", "scenario_version", "scenario_fingerprint", "contract_version",
    }
    assert retrieval.calls[0][2] == [KB_A, KB_B]
    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_t5_17_search_handler_issues_exactly_one_lookup(monkeypatch):
    """T-5-17（适配版）：`/integrations/search` 对 session 只发 1 条语句，且逐字等于 LOOKUP_SQL。

    ★ 与设计 §D.4 的偏差（如实记录）：设计要求抓「全链 SQL 多重集」并与 golden json 相等。
      本仓 handler 级用例里检索服务是替身（`_Retrieval`），抓不到检索三段 SQL；
      而检索 SQL 已由 4B-9 的逐字节哈希钉死。这里钉的是 Step 5 的真实新增风险 ——
      「路由实现又查了一次库」：handler 里任何额外 `session.execute` 都会让计数从 1 变 2。
    ★ 破坏方式（必须变红）：在 `integration_search` 里 resolve 之后加一条
      `await session.execute(...)`，或改动 `resolver.LOOKUP_SQL` 的文本。
    """
    from app.schemas.integration import IntegrationSearchRequest

    async def _audit(*args, **kwargs):
        return None

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    monkeypatch.setattr(integrations_mod.audit, "record", _audit)
    monkeypatch.setattr(integrations_mod, "get_retrieval_service", lambda: _Retrieval())

    application = make_application(kb_ids=(str(KB_A),))
    session = FakeSession([build_row(is_builtin=True)])
    await integrations_mod.integration_search(
        IntegrationSearchRequest(query="住宿标准", kb_ids=[KB_A]),
        _principal(application, _user([KB_A])),
        session,
    )

    assert len(session.statements) == 1, (
        "路由层多发了一条 SQL：Step 5 不得为「认库」额外查库。"
        f"实发 {len(session.statements)} 条"
    )
    statement = session.statements[0]
    actual_text = getattr(statement, "text", str(statement))
    actual = hashlib.sha256(actual_text.encode("utf-8")).hexdigest()
    assert actual == _STEP5_LOOKUP_SQL_SHA256, (
        "解析 SQL 的文本变了 —— Step 5 不得改 LOOKUP_SQL。"
        f"实测 {actual}；期望 {_STEP5_LOOKUP_SQL_SHA256}"
    )


@pytest.mark.asyncio
async def test_search_handler_rejects_unresolvable_scenario(monkeypatch):
    """R1 在 handler 上表现为 500，**不是**「退回默认场景继续跑」。"""
    from fastapi import HTTPException

    from app.schemas.integration import IntegrationSearchRequest

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    application = make_application(scenario_id=SCENARIO_ID)
    with pytest.raises(HTTPException) as exc_info:
        await integrations_mod.integration_search(
            IntegrationSearchRequest(query="x", kb_ids=[KB_A]),
            _principal(application, _user([KB_A])),
            FakeSession([]),  # 引用的场景查不到
        )
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "场景配置不可解析"


@pytest.mark.asyncio
async def test_search_handler_rejects_out_of_scope_request(monkeypatch):
    """R4 在 handler 上表现为 400 `SCENARIO_MISMATCH` 的固定文案。"""
    from fastapi import HTTPException

    from app.schemas.integration import IntegrationSearchRequest

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    application = make_application(scenario_id=SCENARIO_ID, kb_ids=(str(KB_A), str(KB_B)))
    row = build_row(
        scenario_id=SCENARIO_ID, is_builtin=False, status="active",
        binding_mode="explicit", kb_ids=(str(KB_A),),
    )
    with pytest.raises(HTTPException) as exc_info:
        await integrations_mod.integration_search(
            IntegrationSearchRequest(query="x", kb_ids=[KB_B]),
            _principal(application, _user([KB_B])),
            FakeSession([row]),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "请求的知识库不在场景范围内"


@pytest.mark.asyncio
async def test_ask_handler_fills_meta(monkeypatch):
    """`/integrations/ask`：meta 附加在共用的 SSE 消费结果上（不改消费函数本身）。"""
    from fastapi.responses import StreamingResponse

    import app.api.chat as chat_mod

    class _FakeAskResponse(integrations_mod.IntegrationAskResponse):
        pass

    async def _ask_impl(payload, user, session, integration=None, policy=None):
        async def _events():
            yield chat_mod._sse("meta", {"conversation_id": None, "message_id": None})
            yield chat_mod._sse("delta", {"text": "500 元"})
            yield chat_mod._sse("done", {"usage": {}, "suggestions": []})

        return StreamingResponse(_events())

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    monkeypatch.setattr(integrations_mod, "_chat_ask_impl", _ask_impl)

    from app.schemas.integration import IntegrationAskRequest

    application = make_application(kb_ids=(str(KB_A),))
    result = await integrations_mod.integration_ask(
        IntegrationAskRequest(question="住宿标准", kb_ids=[KB_A]),
        _principal(application, _user([KB_A])),
        FakeSession([build_row(is_builtin=True)]),
    )
    assert result.answer == "500 元"
    assert set(result.meta) == {
        "scenario_key", "scenario_version", "scenario_fingerprint", "contract_version",
    }
    assert result.meta["scenario_key"] == "default"
    assert _FakeAskResponse  # 引用一次，避免 ruff 视为未使用


@pytest.mark.asyncio
async def test_ask_handler_rejects_disabled_scenario(monkeypatch):
    """R3 在 ask 入口也生效（顺序：限流 → 场景解析 → 共用链路）。"""
    from fastapi import HTTPException

    from app.schemas.integration import IntegrationAskRequest

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    application = make_application(scenario_id=SCENARIO_ID, kb_ids=(str(KB_A),))
    row = build_row(scenario_id=SCENARIO_ID, is_builtin=False, status="disabled",
                    binding_mode="explicit", kb_ids=(str(KB_A),))
    with pytest.raises(HTTPException) as exc_info:
        await integrations_mod.integration_ask(
            IntegrationAskRequest(question="x", kb_ids=[KB_A]),
            _principal(application, _user([KB_A])),
            FakeSession([row]),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "场景不可用"


# ══════════════════════════════════════════════════════════════════════
# Z-4 / B-1：MCP 契约零改动
# ══════════════════════════════════════════════════════════════════════


def _mcp_functions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse((_APPS_DIR / "mcp" / "main.py").read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"knowledge_ask", "knowledge_search"}
    }


def test_mcp_contract_backward_compat():
    """`knowledge_ask(question, kb_ids, conversation_id=None)` / `knowledge_search(query, kb_ids)`。

    用 AST 读源码而不是 import：MCP 服务有独立依赖集（fastmcp），API 的测试环境里
    未必装得上 —— 而**契约**是源码事实，不需要运行时依赖才能断言。
    """
    functions = _mcp_functions()
    assert set(functions) == {"knowledge_ask", "knowledge_search"}

    ask = functions["knowledge_ask"]
    assert [arg.arg for arg in ask.args.args] == ["question", "kb_ids", "conversation_id"]
    assert len(ask.args.defaults) == 1
    assert isinstance(ask.args.defaults[0], ast.Constant)
    assert ask.args.defaults[0].value is None
    assert not ask.args.kwonlyargs

    search = functions["knowledge_search"]
    assert [arg.arg for arg in search.args.args] == ["query", "kb_ids"]
    assert search.args.defaults == []
    assert not search.args.kwonlyargs


def test_mcp_declares_no_scenario_parameter():
    """`scenario` 不进入任何 MCP 工具签名（身份即场景，见 §D.1）。"""
    for node in _mcp_functions().values():
        names = {arg.arg for arg in node.args.args}
        assert "scenario" not in names
        assert "scenario_key" not in names


def test_mcp_error_passthrough():
    """MCP 适配层把非 2xx 统一转 `ToolError` —— 新增的 400/500 自动走既有通路。

    断言的是**接线形状**：`_post_integration` 用 `raise_for_status()`，
    且没有对状态码做白名单判断（若做了白名单，新增的 400/500 会被静默放过）。
    真正跑一遍 HTTP 需要 fastmcp 的 `get_http_headers()` 上下文，属集成测试范畴。
    """
    source = (_APPS_DIR / "mcp" / "main.py").read_text(encoding="utf-8")
    assert "response.raise_for_status()" in source
    assert "status_code" not in source
    assert "ToolError" in source


def test_scenario_error_messages_are_fixed_text():
    """三类失败的响应文案固定，且不含内部 ID / 场景真实集合（防枚举）。"""
    from app.services.scenario import ScenarioCode

    cases = {
        ScenarioCode.NOT_ALLOWED: (400, "场景不可用"),
        ScenarioCode.MISMATCH: (400, "请求的知识库不在场景范围内"),
        ScenarioCode.UNRESOLVABLE: (500, "场景配置不可解析"),
    }
    for code, (status_code, message) in cases.items():
        error = ScenarioError(code, diagnostic={"scenario_id": str(SCENARIO_ID)})
        assert (error.http_status, error.message) == (status_code, message)
        assert str(SCENARIO_ID) not in error.message
        assert "kb" not in error.message.lower() or code is ScenarioCode.MISMATCH


def test_builtin_row_missing_metric_incremented(monkeypatch):
    """内置行缺失不是静默兜底：`rag.scenario.builtin_row_missing` 必须 +1。"""
    import asyncio

    before = metrics.get("rag.scenario.builtin_row_missing")
    asyncio.run(
        resolve_scenario(
            FakeSession([]),
            tenant_id=uuid.UUID("88888888-8888-8888-8888-888888888888"),
            application=make_application(
                tenant_id=uuid.UUID("88888888-8888-8888-8888-888888888888")
            ),
            requested_kb_ids=[KB_A],
        )
    )
    assert metrics.get("rag.scenario.builtin_row_missing") == before + 1


@pytest.mark.asyncio
async def test_builtin_row_equals_builtin_fallback():
    """同一请求分别走「内置行存在」与「内置行删除」：`kb_ids` / 策略 / DNA 全等。

    `builtin_fallback` 兜底**不产生任何行为差异**（否则它就变成了静默降级）。

    ★ 比较范围比 §F.2 原文更窄，因为设计文档自相矛盾（已在交付报告登记）：
      · §F.2 说「除 source 与 scenario_id 外全等」；
      · §C.6 明确兜底态 `checksum=None`、`version=0`（0 是为了不与真实 v1 撞指纹）；
      · §C.2 也确认「scenario_id / checksum 为 None 只发生在 builtin_fallback」。
      按 §C.2 / §C.6 落地：`checksum` 与 `version` **是会不同的标识**，
      而**行为相关字段（DNA / policy / binding / kb_ids）必须全等** —— 这才是
      「兜底不产生行为差异」的真正含义。
    """
    import dataclasses

    import app.services.scenario.resolver as resolver_mod

    tenant_with_row = uuid.UUID("99999999-9999-9999-9999-999999999991")
    tenant_without_row = uuid.UUID("99999999-9999-9999-9999-999999999992")
    # 只有「身份/审计标识」允许不同；行为相关字段一律不许不同
    identity_only = {"scenario_id", "source", "version", "checksum"}

    def comparable(snapshot):
        return {
            field.name: getattr(snapshot, field.name)
            for field in dataclasses.fields(snapshot)
            if field.name not in identity_only
        }

    with_row = await resolve_scenario(
        FakeSession([build_row(tenant_id=tenant_with_row, is_builtin=True)]),
        tenant_id=tenant_with_row,
        application=make_application(tenant_id=tenant_with_row),
        requested_kb_ids=[KB_A, KB_B],
    )
    resolver_mod.clear_cache()
    fallback = await resolve_scenario(
        FakeSession([]),
        tenant_id=tenant_without_row,
        application=make_application(tenant_id=tenant_without_row),
        requested_kb_ids=[KB_A, KB_B],
    )

    assert with_row.source == "builtin_row"
    assert fallback.source == "builtin_fallback"
    assert fallback.scenario_id is None
    assert fallback.version == 0
    assert fallback.checksum is None
    # 行为相关字段（DNA / policy / binding_mode / kb_ids / is_builtin / scenario_key）全等
    assert comparable(with_row) == comparable(fallback)
    assert with_row.index == fallback.index
    assert with_row.policy == fallback.policy
    assert with_row.kb_ids == fallback.kb_ids
    # 指纹在兜底态同样可用（version=0 与真实 v1 不撞车）
    assert with_row.fingerprint != fallback.fingerprint
