"""T4-R1 ~ T4-R4：**归档一个有应用引用的场景**之后，那批应用实际拿到什么。

★ 为什么要这个文件（本步最像生产事故的一条）：
  `archive` 是 UPDATE，`fk_integration_app_scenario` 的 RESTRICT **完全不参与**。
  所以「归档 → 集成侧立刻全 500」不是理论风险，而是只要绕过 `IN_USE` 前置检查
  就会发生的确定结局。§D.3 的 S16 只证明了「正常路径会被 409 拦下」，
  它证明不了「万一没拦住，客户端看到的是什么」—— 本文件补的就是这一半。

★ 为什么**必须**走 `/integrations/ask` 的真实 HTTP 路径（而不是调 service）：
  要验的恰恰是**路由层到 service 层之间的错误映射**：
  `ScenarioError(UNRESOLVABLE)` → `_resolve_or_reject` → `HTTPException(500, 固定文案)`。
  中间任何一环把码换成 404、把 `message` 换成 `diagnostic`、或者退回默认场景，
  都只在 HTTP 层可见。断言 service 返回值会让这三类错误全部漏过。

★ 只打桩「外部世界」（检索 / 生成 / 落库 / 改写 / 配额 / 限流），**链路本身全真**：
  不调云端、不调 embedding、不连 Redis。打桩清单与
  `tests/scenario/test_policy_wiring.py::test_chat_ask_impl_forwards_policy_to_retrieve_chain`
  同一套（直接 import 复用，避免两份「外部世界」定义各自漂移）。
"""

from __future__ import annotations

import dataclasses
import logging
import uuid

import pytest
import pytest_asyncio

from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    committed_application,
    committed_builtin_scenario,
    committed_rows,
    committed_scalar,
)

_SKIP = pytest.mark.skipif(
    not SCENARIO_TEST_DB_URL,
    reason="needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)",
)


# ══════════════════════════════════════════════════════════════════════
# 写侧工具（与 test_step4_api.py 同一套语义，这里只需要最少的几条）
# ══════════════════════════════════════════════════════════════════════


async def _mk_scenario(api, key: str = "ct_scene") -> uuid.UUID:
    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": key, "name": "集成侧归档用例"}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _publish_once(api, scenario_id: uuid.UUID, *, policy: dict | None = None) -> dict:
    """新建 + 校验 + 发布一版，返回该版本响应体。"""
    created = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": policy or {},
            "kb_ids": [str(kb) for kb in api.kb_ids],
            "change_note": "集成侧用例",
        },
    )
    assert created.status_code == 201, created.text
    row = created.json()
    for action in ("validate", "publish"):
        response = await api.client.post(
            f"/api/scenarios/{scenario_id}/versions/{row['version']}/{action}",
            headers={"If-Match": f'"{row["checksum"]}"'},
        )
        assert response.status_code == 200, response.text
    return row


async def _publish_n_versions(api, scenario_id: uuid.UUID, n: int) -> dict:
    """连续发布 n 版，返回**最后一版**的响应体（前几版会被自动归档）。"""
    last: dict = {}
    for index in range(1, n + 1):
        last = await _publish_once(api, scenario_id, policy={"recall_top_k": 10 + index})
    return last


# ══════════════════════════════════════════════════════════════════════
# 夹具：把 `/integrations/ask` 的外部世界打桩，其余全真
# ══════════════════════════════════════════════════════════════════════


class IntegrationEnv:
    """一个「已认证的应用」+ 若干便捷动作。"""

    def __init__(self, api, application_id: uuid.UUID) -> None:
        self.api = api
        self.application_id = application_id
        self.captured: dict = {}

    @property
    def kb_ids(self) -> list[uuid.UUID]:
        return self.api.kb_ids

    async def ask(self, question: str = "住宿报销标准是多少？"):
        """走**真实** `POST /api/integrations/ask`（HTTP 层，含全部错误映射）。"""
        return await self.api.client.post(
            "/api/integrations/ask",
            json={"question": question, "kb_ids": [str(kb) for kb in self.kb_ids]},
        )

    async def bind(self, scenario_id: uuid.UUID | None) -> None:
        response = await self.api.client.put(
            f"/api/integrations/applications/{self.application_id}/scenario",
            json={"scenario_id": str(scenario_id) if scenario_id else None},
        )
        assert response.status_code == 200, response.text


@pytest_asyncio.fixture()
async def integration(api, monkeypatch):
    """`/integrations/ask` 的端到端夹具。

    ★ 鉴权依赖的实际形态（`app/api/integrations.py` 的 `get_integration_principal`）：
      它是一个 `Depends`，签名是
        `(user: CurrentUser = Depends(get_current_user),
          session: AsyncSession = Depends(get_db),
          application_id_raw: str | None = Header(alias="X-Application-Id"),
          application_key: str | None = Header(alias="X-Application-Key"))`
      → 返回 `IntegrationPrincipal(user=..., application=...)`。
      **我们没有发明请求头**，而是 override 整个依赖：内部用被 override 的 `get_db`
      重新读一次 `integration_applications` 行（不用夹具里那个可能过期的 ORM 对象 ——
      R2 的解绑会改 `scenario_id`，捕获一份快照会让「解绑」看不见）。
    """
    from app.api import chat as chat_mod
    from app.api import integrations as integ_mod
    from app.api.deps import get_db
    from app.api.integrations import IntegrationPrincipal, get_integration_principal
    from app.main import app
    from app.models import IntegrationApplication
    from app.services.retrieve.base import RetrievalResult
    from fastapi import Depends
    from tests.chat.test_sse_retrieval_observation import (
        _FakeGeneration,
        _FakeSession,
        _chunk,
    )
    from tests.scenario.test_policy_wiring import (
        _fake_caliber,
        _fake_rewrite,
        _fake_system_prompt,
        _flag_off,
        _noop_async,
    )

    # ── ① 限流：本机没有 Redis，`check_integration_rate_limit` 会直接 503。
    #    它是**函数调用**而不是 `Depends`，所以只能 monkeypatch 模块属性。
    #    限流本身的行为由 `tests/test_integrations.py` 覆盖，不属于本文件要验的东西。
    async def _no_limit(*args, **kwargs):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(integ_mod, "check_integration_rate_limit", _no_limit)

    # ── ② 检索桩：固定返回一条 chunk（不碰 embedding / pgvector / rerank）。
    #    同时记录 policy，顺带证明「场景快照真的被透传下去了」。
    captured: dict = {}

    class _StubRetrieval:
        async def retrieve_chain(  # noqa: ANN001
            self,
            session,
            query,
            tenant_id,
            kb_ids,
            clearance,
            subjects,
            rewrite_score=None,
            *,
            policy=None,
        ):
            captured["policy"] = policy
            captured["kb_ids"] = list(kb_ids)
            return RetrievalResult(chunks=[_chunk()], stage_ms={}, chain_mode="single")

        async def retrieve(  # noqa: ANN001
            self,
            session,
            query,
            tenant_id,
            kb_ids,
            clearance,
            subjects,
            *,
            policy=None,
        ):
            captured["policy"] = policy
            return RetrievalResult(chunks=[_chunk()], stage_ms={})

    # ── ③ 生成 / 改写 / 落库 / 配额 / 灰度：全部打桩（与 policy_wiring 同一套）
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(chat_mod, "get_retrieval_service", lambda: _StubRetrieval())
    monkeypatch.setattr(chat_mod, "get_stream_generation_service", lambda: _FakeGeneration())
    monkeypatch.setattr(chat_mod, "rewrite_query", _fake_rewrite)
    monkeypatch.setattr(chat_mod, "build_system_prompt", _fake_system_prompt)
    monkeypatch.setattr(chat_mod, "snapshot_for_message", _fake_caliber)
    monkeypatch.setattr(chat_mod, "_persist_assistant", _noop_async)
    monkeypatch.setattr(chat_mod, "_update_user_memory", _noop_async)
    monkeypatch.setattr(chat_mod.audit, "record", _noop_async)
    monkeypatch.setattr(chat_mod.flag_service, "is_enabled", _flag_off)
    monkeypatch.setattr(chat_mod.quota_service, "_incr_usage", _noop_async)
    # ★ `check_application_quota` 内部要连 Redis（`quota.py:254` 的 pipeline.execute）。
    #   本机没有 Redis，它会抛 `ConnectionError` → 被 `event_stream` 的兜底捕获 →
    #   流变成一条 error 事件。虽然最终仍是 200，但那已经不是「正常问答」了：
    #   后面任何针对 answer / meta 的断言都建在一具尸检报告上。
    monkeypatch.setattr(chat_mod.quota_service, "check_application_quota", _noop_async)
    # ★ `check_and_degrade` 会走 `get_limits` → 真 DB + `get_usage` → 真 Redis。
    #   不桩它的话，本机无 Redis 时它在 event_stream 内部抛异常 → 同样被兜底捕获 →
    #   流退化成一条 error 事件。必须返回**不降级、不拒绝**的 plan，才是「正常问答」。
    #   签名与 `tests/test_integrations.py:412-414` 的官方桩一致（原样回传三段）。
    from app.services.quota import DegradationPlan

    async def _no_degrade(  # noqa: ANN001, ARG001
        tenant_id, user_id, question, history, memory_prompt, chunks
    ):
        return DegradationPlan(history=history, memory_prompt=memory_prompt, chunks=chunks)

    monkeypatch.setattr(chat_mod.quota_service, "check_and_degrade", _no_degrade)

    application_id = await committed_application(
        api.tenant_id,
        created_by=api.user_id,
        # ★ 应用白名单必须含本租户的知识库：集成模式取「应用白名单 ∩ 用户授权 ∩
        #   请求集合」的三重交集（`chat.py:311`），空白名单 ⇒ 每次 ask 都 403。
        kb_ids=api.kb_ids,
    )

    # 用户授权集合同理（三重交集的第二项）。`CurrentUser` 是 frozen dataclass，用 replace 派生。
    principal_user = dataclasses.replace(api.user, authorized_kb_ids=list(api.kb_ids))

    # ── ④ 鉴权：override 整个依赖，内部按请求重新读库
    async def _principal(session=Depends(get_db)):  # noqa: ANN001, B008
        row = await session.get(IntegrationApplication, application_id)
        assert row is not None, "集成应用行丢了（夹具建的应用被谁删了？）"
        return IntegrationPrincipal(user=principal_user, application=row)

    app.dependency_overrides[get_integration_principal] = _principal
    try:
        env = IntegrationEnv(api, application_id)
        env.captured = captured
        yield env
    finally:
        app.dependency_overrides.pop(get_integration_principal, None)


# ══════════════════════════════════════════════════════════════════════
# T4-R1：绑定生效 —— meta 里的 key / version 就是绑定的那一版
# ══════════════════════════════════════════════════════════════════════


@_SKIP
@pytest.mark.asyncio
async def test_r1_bound_scenario_key_and_version_reach_meta(integration):
    """T4-R1：绑定到 **v3** 的场景后，`meta.scenario_version` 必须是 3（不是 1）。

    ★ 为什么特意发三版：只发一版时 `version == 1` 与「meta 里写死 1」不可区分 ——
      这种假绿在本项目已经出现过（Step 3 的「两套阈值都等于 0.50」）。
      发三版之后，能拿到 3 只可能是「真的读了 published 那一行」。
    ★ 同时断言 `captured["policy"]` 里的 `recall_top_k`：证明快照不只在 meta 里
      好看，还真的透传进了检索链路（`integrations.py` 的 `policy=snapshot.policy`）。
    """
    scenario_id = await _mk_scenario(integration.api)
    last = await _publish_n_versions(integration.api, scenario_id, 3)
    assert last["version"] == 3
    await integration.bind(scenario_id)

    response = await integration.ask()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["scenario_key"] == "ct_scene"
    assert body["meta"]["scenario_version"] == 3
    # 绑定生效而不是靠「默认场景恰好也是这个 key」—— 默认场景的 key 是 default
    assert body["meta"]["scenario_key"] != "default"
    # 检索链路真的拿到了这一版的策略（第 3 版写入的是 recall_top_k=13）
    policy = integration.captured["policy"]
    assert policy is not None, "场景快照的 policy 没有透传进检索链路"
    # `policy` 是 `ScenarioPolicy`（稀疏覆盖集对象），不是 dict —— 用属性而不是 `.get`
    assert policy.recall_top_k == 13, f"透传下去的是 {policy!r}；第 3 版写入的是 recall_top_k=13"


# ══════════════════════════════════════════════════════════════════════
# T4-R4：绕过 W6 直写 SQL 归档 → 集成侧 500 + 固定文案（含反向对照）
# ══════════════════════════════════════════════════════════════════════


@_SKIP
@pytest.mark.asyncio
async def test_r4_archived_scenario_gives_500_with_fixed_message(integration, caplog):
    """T4-R4：把 published 直接改成 archived（绕过 W6）→ `/ask` 返回 **500 + 固定文案**。

    这条用例是本步「最像生产事故」的观测点，断言四层：
      ① **反向对照**：归档前同一请求必须 200 且 `scenario_version == 1` ——
         否则「归档后 500」无法排除「这个请求本来就会 500」（前三轮各出过一次这类假绿）；
      ② 归档后状态码必须 **500**（不是 404：404 会让客户端以为路径错、去重试；
         不是 200：那等于静默用已下线的口径继续答）；
      ③ 响应体 `detail` 必须恰好是固定文案，且**不含** internal 诊断
         （`scenario_not_found` / scenario_id / `SCENARIO_UNRESOLVABLE` 一律不许出现）；
      ④ 诊断只进日志：WARN 里有 `reason=scenario_not_found` —— 这条同时证明
         「响应体没泄露」不是因为它没被记录，而是因为**分流是对的**。

    ★ 为什么走 SQL 而不走 W6：W6 会被 `IN_USE` 前置检查拦下（S16 已证）。
      这里要造的是**已经坏了的数据**（运维直连库 / 另一条脚本路径 / 删场景的前置检查被绕过），
      那是 R1 分支唯一可能真实发生的入口。
    ★ 为什么写完 SQL 要 `clear_cache()`：行缓存是**进程内**设施（TTL 60s）。
      不清缓存的话第二次请求读到的是缓存里的旧行 → 仍然 200，
      于是这条用例会以「归档后居然还是 200」的形式变红，而真因是本进程缓存。
      `clear_cache()` 在这里代表「TTL 到期 / 请求落到另一台实例」，
      是**归档这条路线的正常形态**，不是为了让测试好过（§C.4 的推导同理）。
    """
    from app.services.scenario.resolver import clear_cache

    scenario_id = await _mk_scenario(integration.api)
    published = await _publish_once(integration.api, scenario_id)
    assert published["version"] == 1
    await integration.bind(scenario_id)

    # ── ① 反向对照：坏之前必须正常 ──────────────────────────────
    before = await integration.ask()
    assert before.status_code == 200, f"归档前的正常请求就已经失败：{before.text}"
    assert before.json()["meta"]["scenario_version"] == 1
    assert before.json()["meta"]["scenario_key"] == "ct_scene"

    # ── 造坏数据：绕过 W6 的 IN_USE 前置检查，直接 UPDATE ─────────
    await committed_rows(
        "UPDATE scenario_versions SET state = 'archived', archived_at = now() "
        "WHERE scenario_id = $1 AND state = 'published' RETURNING version",
        scenario_id,
    )
    archived = await committed_scalar(
        "SELECT state FROM scenario_versions WHERE scenario_id = $1 AND version = $2",
        scenario_id,
        published["version"],
    )
    assert archived == "archived", "造数据失败：published 没被改成 archived"
    clear_cache()

    # ── ②③ 归档后：500 + 固定文案 + 不泄露诊断 ─────────────────
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.api.integrations"):
        after = await integration.ask()

    assert after.status_code == 500, (
        f"归档后返回 {after.status_code}（期望 500）；"
        "404 会让客户端以为路径错去重试，200 等于静默用已下线的口径继续答。"
        f"响应体：{after.text}"
    )
    assert after.json()["detail"] == "场景配置不可解析"

    raw = after.text
    for leaked in (
        "scenario_not_found",
        str(scenario_id),
        str(integration.api.tenant_id),
        "SCENARIO_UNRESOLVABLE",
        "scenario_versions",
    ):
        assert leaked not in raw, (
            f"响应体泄露了内部诊断 {leaked!r} —— 集成侧是外部系统，"
            "只应拿到固定文案（`errors.py` 的模块纪律）。实际：{raw}"
        )
    # `detail` 必须是**单个字符串**，不是 {"code":..., "diagnostic":...} 那种结构
    assert isinstance(after.json()["detail"], str)

    # ── ④ 诊断只进日志 ────────────────────────────────────────
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    # ★ 设计 §D.5 的 R4 写的是「日志含 `reason=scenario_not_found`」。
    #   实测格式是 `detail={'reason': 'scenario_not_found', ...}`
    #   （`integrations.py` 用 `%s` 打 `exc.diagnostic` 这个 dict）——
    #   语义一致、写法不同。断言按**实测**写，并在这里留痕，避免下一个人
    #   照着文档的字面去改日志格式（那会把结构化诊断降级成一行拼接字符串）。
    assert "scenario_not_found" in warnings, (
        f"日志里没有定位线索（运维无从判断是「场景被归档」还是「查询炸了」）。"
        f"实际 WARN：{warnings!r}"
    )
    assert "'reason'" in warnings, "诊断字典里丢了 reason 键（只剩 code 就没有定位能力）"
    assert "code=SCENARIO_UNRESOLVABLE" in warnings
    assert "trace_id=" in warnings, "WARN 没有 trace_id —— 无法与请求日志对齐"


# ══════════════════════════════════════════════════════════════════════
# T4-R2：解绑 → 回到内置行（不是「继续用旧场景」，也不是 UNRESOLVABLE）
# ══════════════════════════════════════════════════════════════════════


@_SKIP
@pytest.mark.asyncio
async def test_r2_unbind_falls_back_to_builtin_row(integration):
    """T4-R2：W7 解绑后再 ask → `scenario_key == "default"`、`scenario_version == 1`。

    ★ 与 R4 成对：R4 是「绑定指向的东西坏了」，R2 是「不绑定了」。两者的正确
      结局**相反**（一个 500 fail-closed，一个 200 用平台默认）。只测其中一个时，
      「`scenario_id is None` 与 `row is None` 被实现混作一谈」这个错误
      （R1 会因此把未绑定的应用也判成 UNRESOLVABLE）不可能被发现。
    ★ 为什么要在库里造内置行：`api` 夹具的租户是在 0028 迁移**之后**建的，
      迁移的种子 INSERT 不会为它补行。没有内置行时解绑会走 `builtin_fallback`
      （`version == 0`）——那是 R3 要测的分支，不是 R2。两者用 `version` 区分（1 vs 0）。
    ★ `source` 字段**不在**契约的 `meta` 里（`_scenario_meta` 只给 4 个键），
      所以 HTTP 层只能断言 key + version；`source` 在同一个用例里用 resolver 复核，
      并明确标注它是**补充证据**而非端到端断言。
    """
    from tests.scenario.pg_helpers import committed_rows as _rows  # noqa: F401

    from app.models import IntegrationApplication
    from app.services.scenario.resolver import clear_cache, resolve_scenario

    await committed_builtin_scenario(integration.api.tenant_id, version=1)

    scenario_id = await _mk_scenario(integration.api)
    await _publish_n_versions(integration.api, scenario_id, 2)  # v2 published
    await integration.bind(scenario_id)

    bound = await integration.ask()
    assert bound.status_code == 200, bound.text
    assert bound.json()["meta"]["scenario_key"] == "ct_scene"
    assert bound.json()["meta"]["scenario_version"] == 2

    await integration.bind(None)  # W7 解绑
    clear_cache()

    after = await integration.ask()

    assert after.status_code == 200, (
        f"解绑后返回 {after.status_code}：`scenario_id is None` 被当成「解析不出」了"
        f"（R1 与 R5 混作一谈）。响应体：{after.text}"
    )
    meta = after.json()["meta"]
    assert meta["scenario_key"] == "default"
    assert meta["scenario_version"] == 1, (
        "解绑后应当读到内置行的 v1；0 意味着落到了 builtin_fallback（内置行缺失分支）"
    )
    # ★ 补充证据（非端到端）：`source` 不在 meta 契约里，只能从 resolver 侧确认
    async with integration.api.session_factory() as session:
        application = await session.get(IntegrationApplication, integration.application_id)
        assert application is not None and application.scenario_id is None
        snapshot = await resolve_scenario(
            session,
            tenant_id=integration.api.tenant_id,
            application=application,
            requested_kb_ids=integration.kb_ids,
        )
    assert snapshot.source == "builtin_row", (
        f"source={snapshot.source}；解绑应当命中本租户的内置行，而不是同值兜底"
    )
    clear_cache()


# ══════════════════════════════════════════════════════════════════════
# T4-R3（设计标注「可选」）：内置行缺失 → builtin_fallback 的同值兜底
# ══════════════════════════════════════════════════════════════════════


# 「身份 / 审计标识」允许不同的四个字段；其余一律不许不同。
# 这个口径直接抄自 `test_scenario_wiring.py::test_builtin_row_equals_builtin_fallback`
# 的 `identity_only`（那是本仓既有的、绿的、设计文档自己点名的锁），
# 不在这里自创第二套。
_IDENTITY_ONLY = frozenset({"scenario_id", "source", "version", "checksum"})


def _behaviour_fields(snapshot) -> dict:  # noqa: ANN001
    """快照里所有**行为相关**字段（排除身份/审计标识）。

    `fingerprint` 是 property 不是 dataclass field，因此不会出现在这里 ——
    它单独断言（见用例正文的冲突说明）。
    """
    return {
        field.name: getattr(snapshot, field.name)
        for field in dataclasses.fields(snapshot)
        if field.name not in _IDENTITY_ONLY
    }


@_SKIP
@pytest.mark.asyncio
async def test_r3_missing_builtin_row_falls_back_with_same_caliber(integration):
    """T4-R3：删掉内置行后再 ask → **200** 且 `scenario_version == 0`，口径不变。

    ★ 这条要验的真实承诺是「漏补种不产生行为差异」（`snapshot.py` 的
      `_builtin_fallback_identity`，:326-343），而**不是**「meta 四个键全部相等」。
      两者唯一的差别恰好在 `scenario_fingerprint` 上，而这一点
      **设计文档与实现真的冲突**，必须逐条留痕：

      ① §C.4.3 表格（R3 那一行）写：「`meta.scenario_fingerprint` 与有行时
         **逐字段相等**（`test_builtin_row_equals_builtin_fallback` 锁死）」；
      ② 但 `snapshot.py:38-43` 把兜底版本号定成 0 的**原文理由**是：
         「1 留给真实的第一个版本行，否则 `fingerprint` 会与真实 v1 撞车
         （审计时无法区分「有 v1 行」与「漏补种」）」；
      ③ 而 §C.4.3 引用的那条测试
         `tests/scenario/test_scenario_wiring.py::test_builtin_row_equals_builtin_fallback`
         在 :529 **恰恰断言 `with_row.fingerprint != fallback.fingerprint`** ——
         也就是「文档引用的测试锁死的是文档结论的反面」。

      【实测】`fingerprint` 的公式是
      `sha1(key|version|index.fingerprint|canonical_policy)[:12]`（`snapshot.py:259-273`），
      **含 version**；所以 version=1（`builtin_row`）与 version=0
      （`builtin_fallback`）必然不同，实测 `'19c470227277'` vs `'834546219ea3'`。
      这不是本用例写错，也不是实现 bug —— 是设计 §C.4.3 那一行与 §C.6 /
      `snapshot.py:41` 的自我矛盾。**已按「不改设计、不改实现、先报实际」
      的要求上报，等裁决。**

    ★ 因此本用例按「实现的实际语义 + 既有绿测的口径」断言：
      · 身份/审计标识**允许**不同：`version` 1→0、`checksum` None、
        `scenario_id` None、`source`；
      · 行为相关字段**必须全等**：`scenario_key` / `index` / `graph` / `wiki` /
        `policy` / `binding_mode` / `binding_kb_ids` / `kb_ids` / `db_binding` /
        `is_builtin`；
      · `scenario_fingerprint` **必须不同**，且不同只来自 version —— 这条反向
        断言是「审计上能区分漏补种」的唯一保证；反过来断言相等，等于要求实现
        把「有没有行」这件事从指纹里抹掉，那才是真的制造静默降级。

    ★ 为什么仍然值得写：`builtin_fallback` 是**唯一**允许「没有行也算成功」的
      分支，它一旦被实现成 fail-closed，所有还没补种的租户直接不可用（R2 的
      反面对照）；而它一旦口径漂了，表现形式是**答准率缓慢下滑**（日志无红、
      指标无异常），只能靠逐字段对比观测出来。
    """
    from app.models import IntegrationApplication
    from app.services.scenario.resolver import clear_cache, resolve_scenario

    async def _snapshot():  # noqa: ANN202
        """用 resolver 直接取一次快照（补充证据，非端到端）。"""
        clear_cache()
        async with integration.api.session_factory() as session:
            application = await session.get(IntegrationApplication, integration.application_id)
            assert application is not None
            return await resolve_scenario(
                session,
                tenant_id=integration.api.tenant_id,
                application=application,
                requested_kb_ids=integration.kb_ids,
            )

    builtin_id = await committed_builtin_scenario(integration.api.tenant_id, version=1)

    # ── 前半：有内置行（R2 的形态） ───────────────────────────────
    with_row = await integration.ask()
    assert with_row.status_code == 200, with_row.text
    meta_before = with_row.json()["meta"]
    assert meta_before["scenario_version"] == 1
    assert meta_before["scenario_key"] == "default"

    row_snapshot = await _snapshot()
    assert row_snapshot.source == "builtin_row"
    assert row_snapshot.version == 1

    # ── 删掉内置行（版本行跟着 CASCADE）──────────────────────────
    await committed_rows("DELETE FROM scenarios WHERE id = $1 RETURNING id", builtin_id)
    gone = await committed_scalar("SELECT count(*) FROM scenarios WHERE id = $1", builtin_id)
    assert gone == 0, "造数据失败：内置行没被删掉"
    # ★ 行缓存是**进程内**设施（TTL 60s，key 含 tenant）。不 clear 的话第二次 ask
    #   读到的还是前半截缓存的 v1 快照 → 这条用例会以「兜底居然是 version=1」变红，
    #   而真因是本进程缓存。`clear_cache()` 在这里代表「TTL 到期 / 请求落到另一台实例」，
    #   是内置行缺失这条路的**正常形态**（与 R4 里同一处理同理）。
    clear_cache()

    # ── 后半：内置行缺失 → 同值兜底 ──────────────────────────────
    without_row = await integration.ask()

    assert without_row.status_code == 200, (
        f"内置行缺失时返回 {without_row.status_code}："
        "R5 分支（同值兜底）被实现成了 fail-closed，"
        f"所有还没补种的租户直接不可用。响应体：{without_row.text}"
    )
    meta_after = without_row.json()["meta"]

    # ① 版本号约定：0 = 「没有版本行」的显式表达
    assert meta_after["scenario_version"] == 0, (
        f"兜底分支的版本号约定是 0；实测 {meta_after['scenario_version']}"
    )
    # ② key 与契约版本必须一模一样（这两项漂了 = 契约破了）
    assert meta_after["scenario_key"] == meta_before["scenario_key"] == "default"
    assert meta_after["contract_version"] == meta_before["contract_version"]
    # ③ 指纹**必须不同**，且差异只允许来自 version（见文档冲突说明）
    assert meta_after["scenario_fingerprint"] != meta_before["scenario_fingerprint"], (
        "兜底态与有行态的 fingerprint 相同 —— 这反而是 bug："
        "`fingerprint` 含 version（snapshot.py:259-273），1 与 0 必然不同；"
        "若相同说明指纹公式被改掉了，「有 v1 行」与「漏补种」从此无法区分"
    )

    # ④ 补充证据（非端到端，`source` 不在 meta 契约里）：行为字段逐字段相等
    fallback_snapshot = await _snapshot()
    assert fallback_snapshot.source == "builtin_fallback"
    assert fallback_snapshot.version == 0
    assert fallback_snapshot.scenario_id is None
    assert fallback_snapshot.checksum is None
    assert _behaviour_fields(row_snapshot) == _behaviour_fields(fallback_snapshot), (
        "兜底快照的行为字段与有行时不同 —— 「同值兜底」的承诺破了，"
        "而这只会表现为答准率缓慢下滑，不会有任何报警"
    )
    # 显式再钉三个最容易漂的（断言消息比 dict diff 可读）
    assert row_snapshot.index == fallback_snapshot.index
    assert row_snapshot.policy == fallback_snapshot.policy
    assert row_snapshot.kb_ids == fallback_snapshot.kb_ids
    # 指纹差异的**唯一**合法来源也是 version：指纹的四个输入里
    # `scenario_key`（上面已断言相等）、`index` / `policy`（`_behaviour_fields`
    # 相等 ⇒ 这两个也相等）都已钉死，剩下能造成差异的只有 version。
    assert row_snapshot.fingerprint != fallback_snapshot.fingerprint
    clear_cache()
