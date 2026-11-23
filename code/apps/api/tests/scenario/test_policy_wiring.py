"""Step 3 接线（F.3 接线点覆盖）：用**假 Session 断言出参**，零网络零 DB。

这一批是「接线正确性」的真正证据 —— 断言的是**出参**
（三条召回 SQL 的 `params["limit"]`、重排服务收到的 `top_k`、L1 闸门用的阈值），
而不是「代码看起来改对了」。

钉住四件事（错一件都是真事故）：

  1. `recall_top_k` 真的进到**三条**召回 SQL 与候选池截断（漏一条 = 少一路召回）；
  2. `recall_top_k` **绝不会**被当成重排的 `top_n`（§A.3 结论 2：重排在线分支
     不做二次截断，误传会让整池 50 条全进上下文，比正常多 42 条）；
  3. 阈值真的接在 L1 闸门上（不是算出来放着）；
  4. 链式多轮的**每一轮**都收到同一个 policy（漏一处 = 策略只对单趟生效，
     而链式是默认路径）。

夹具形态沿用 `tests/retrieve/test_governance_weight.py`（FakeSession / FakeEmbedding /
FakeRerank / Harness），只加一件事：**记录每次 `execute` 的绑定参数**。
"""
from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace

import pytest

from app.config import decisions
from app.services import retrieve as retrieve_mod
from app.services.retrieve import RetrievalService
from app.services.scenario.effective import effective_retrieval
from app.services.scenario.snapshot import ScenarioPolicy
from tests.fake_settings import fake_settings

pytestmark = pytest.mark.asyncio

TENANT = uuid.uuid4()
KB = uuid.uuid4()
DOC = uuid.uuid4()


def _row(cid: uuid.UUID, vector_score: float) -> SimpleNamespace:
    """构造一行召回结果（Row 替身：属性访问 + `_mapping` 双通道）。"""
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": f"内容 {cid}",
        "heading_path": "标题",  # 短标题 ⇒ 不触发章节补全（本文件不测那一路）
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "governance_weight": 0,
        "vector_score": vector_score,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001  (retrieve 代码读 row._mapping)
    return row


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class RecordingSession:
    """按 SQL 特征分发，并**记录每次执行的绑定参数**（接线断言的全部证据来源）。"""

    def __init__(self, harness: "Harness") -> None:
        self._h = harness
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        self.calls.append((self._kind(sql), dict(params or {})))
        if "<=>" in sql:
            return _Result(self._h.vector_rows)
        if "~*" in sql:
            return _Result([])
        if "ts_rank" in sql:
            return _Result([])
        if "filename" in sql:
            return _Result([])
        if "FROM kb_terms" in sql:
            return _Result([])
        if "v(document_id, heading_path)" in sql:
            # 章节上下文补全（第 7 步）：与本文件无关，返回空即跳过
            return _Result([])
        raise AssertionError(f"未识别的 SQL: {sql[:80]}")

    @staticmethod
    def _kind(sql: str) -> str:
        if "<=>" in sql:
            return "vector"
        if "~*" in sql:
            return "ilike"
        if "ts_rank" in sql:
            return "fts"
        if "filename" in sql:
            return "filename"
        return "other"

    def limits(self) -> dict[str, int]:
        """三条召回路各自的 `limit` 绑定值（没有该路 → 不在字典里）。"""
        return {
            kind: params["limit"]
            for kind, params in self.calls
            if kind in {"vector", "ilike", "fts"} and "limit" in params
        }


class _Embedding:
    async def embed_query(self, query: str) -> list[float]:  # noqa: ARG002
        return [0.01] * 8


class RecordingRerank:
    def __init__(self, harness: "Harness") -> None:
        self._h = harness

    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ARG002
        self._h.rerank_calls.append({"top_k": top_k, "docs": len(documents)})
        return list(self._h.rerank_result)


class Harness:
    def __init__(self, rows: list) -> None:
        self.vector_rows = rows
        self.rerank_result: list = []
        self.rerank_calls: list[dict] = []
        self.sessions: list[RecordingSession] = []

    def session(self) -> RecordingSession:
        s = RecordingSession(self)
        self.sessions.append(s)
        return s


@pytest.fixture
def build(monkeypatch):
    """构造替身齐全的 RetrievalService。

    `rerank_enabled` 可配：本文件同时要覆盖「平台开重排」与「平台关重排」两条路径。
    """

    def _make(harness: Harness, *, rerank_enabled: bool = False) -> RetrievalService:
        monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: _Embedding())
        monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: RecordingRerank(harness))
        monkeypatch.setattr(
            retrieve_mod,
            "get_settings",
            lambda: fake_settings(
                rerank_enabled=rerank_enabled,
                graph_enabled=False,   # 图召回与策略接线无关，关掉减少分支
                wiki_enabled=False,
            ),
        )
        return RetrievalService()

    return _make


async def _retrieve(service: RetrievalService, session, *, policy=None):
    return await service.retrieve(
        session, "报销制度", TENANT, [KB], 40, ["dept:it"], policy=policy
    )


# ══════════════════════════════════════════════════════════════════════
# D.3.2：recall_top_k 真的进了三条召回 SQL
# ══════════════════════════════════════════════════════════════════════


async def test_recall_top_k_reaches_all_three_recall_paths(build, monkeypatch):
    """三条召回 SQL 的 `limit` 绑定值都必须等于 `eff.recall_top_k`。

    ★ 用 7 而不是 50：50 是平台默认值，断言 `== 50` 无法区分
      「读了场景策略」与「什么也没改」。
    """
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", True)
    harness = Harness([_row(uuid.uuid4(), 0.9)])
    service = build(harness)
    session = harness.session()

    await _retrieve(service, session, policy=ScenarioPolicy(recall_top_k=7))

    limits = session.limits()
    assert set(limits) == {"vector", "ilike", "fts"}, f"三路召回必须都在：{limits}"
    assert limits == {"vector": 7, "ilike": 7, "fts": 7}


async def test_recall_top_k_platform_default_when_unset(build, monkeypatch):
    """没配 policy ⇒ 三条路的 limit 都等于平台当前值（现读，不是硬编码）。"""
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", True)
    monkeypatch.setattr(decisions, "TOP_K_RECALL", 37)
    harness = Harness([_row(uuid.uuid4(), 0.9)])
    service = build(harness)
    session = harness.session()

    await _retrieve(service, session, policy=None)

    assert session.limits() == {"vector": 37, "ilike": 37, "fts": 37}


async def test_candidate_pool_truncated_by_recall_top_k(build, monkeypatch):
    """候选池截断（读点 #4）同样受 `recall_top_k` 约束。

    构造 10 条 RRF 候选、`recall_top_k=3`：`filename` 查询收到的 `doc_ids`
    只应覆盖池里的 3 条（截断发生在池构造之后、取名之前）。
    """
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
    rows = [_row(uuid.uuid4(), 0.5) for _ in range(10)]
    harness = Harness(rows)
    service = build(harness)
    session = harness.session()

    await _retrieve(service, session, policy=ScenarioPolicy(recall_top_k=3))

    filename_params = [p for kind, p in session.calls if kind == "filename"]
    assert filename_params, "没有取 filename —— 池构造路径没走到"
    # 池里 3 条 → doc_ids 去重后至多 3 个（本用例 10 条同行同 doc，去重后 1 个）
    assert len(filename_params[0]["doc_ids"]) <= 3


# ══════════════════════════════════════════════════════════════════════
# D.3.3：recall_top_k 与 rerank(top_n) 解耦（**§A.3 结论 2**）
# ══════════════════════════════════════════════════════════════════════


async def test_recall_top_k_never_passed_as_rerank_top_n(build, monkeypatch):
    """把 `recall_top_k` 配成 50，重排收到的 `top_k` 仍必须是取用条数（默认 8）。

    ★ 为什么这条单独写：`recall_top_k`（池大小）与 `final_top_n`（进上下文的条数）
      都是"条数"，最容易被顺手互换。而重排**在线**分支不做二次截断
      （`retrieve/__init__.py` 的 `if use_rerank:` 分支直接采用 reranked_ids），
      于是误传的后果是「50 条全进上下文」—— 上下文与 citations 一起膨胀，
      且不会有任何报错或指标异常。
    """
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 8)
    harness = Harness([_row(uuid.uuid4(), 0.9)])
    # 重排返回空 ⇒ use_rerank 保持 True（空结果不触发降级），走在线分支
    harness.rerank_result = []
    service = build(harness, rerank_enabled=True)
    session = harness.session()

    await _retrieve(service, session, policy=ScenarioPolicy(recall_top_k=50))

    assert harness.rerank_calls, "重排开关开着却没调用重排服务"
    assert harness.rerank_calls[0]["top_k"] == 8, (
        "重排收到的 top_n 必须是取用条数 8，不能是池大小 50"
    )
    assert harness.rerank_calls[0]["docs"] <= 50, "送进重排的文档数受池大小约束"


async def test_final_top_n_follows_platform_when_unset(build, monkeypatch):
    """Step 3 的 `final_top_n` 恒等平台取用条数（ScenarioPolicy 尚无此字段）。"""
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 5)
    harness = Harness([_row(uuid.uuid4(), 0.9)])
    harness.rerank_result = []
    service = build(harness, rerank_enabled=True)
    session = harness.session()

    await _retrieve(service, session, policy=ScenarioPolicy(recall_top_k=50))

    assert harness.rerank_calls[0]["top_k"] == 5


async def test_rerank_off_path_truncates_by_final_top_n(build, monkeypatch):
    """重排关闭路径（读点 #7）按取用条数截断 —— 池可以大，进上下文的不能。"""
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 2)
    rows = [_row(uuid.uuid4(), 0.9) for _ in range(6)]
    harness = Harness(rows)
    service = build(harness, rerank_enabled=False)
    session = harness.session()

    result = await _retrieve(service, session, policy=ScenarioPolicy(recall_top_k=50))

    assert result.refused is False
    assert result.use_rerank is False
    assert len(result.chunks) == 2, "重排关闭时必须按取用条数截断"


# ══════════════════════════════════════════════════════════════════════
# D.3.5：阈值真的接在 L1 闸门上
# ══════════════════════════════════════════════════════════════════════


async def test_threshold_override_changes_refusal(build, monkeypatch):
    """top1 向量分 0.55：场景阈值 0.60 → 拒答；0.50 → 不拒答。

    ★ 两个值都刻意不是平台默认（0.5x 里只有一个相等），且**成对**断言：
      只测「拒答」无法区分「阈值接上了」与「阈值被写死成很大」。
    """
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)

    harness_strict = Harness([_row(uuid.uuid4(), 0.55)])
    service_strict = build(harness_strict, rerank_enabled=False)
    strict = await _retrieve(
        service_strict,
        harness_strict.session(),
        policy=ScenarioPolicy(relevance_threshold_vector=0.60),
    )
    assert strict.refused is True
    assert strict.score_source == "vector"

    harness_loose = Harness([_row(uuid.uuid4(), 0.55)])
    service_loose = build(harness_loose, rerank_enabled=False)
    loose = await _retrieve(
        service_loose,
        harness_loose.session(),
        policy=ScenarioPolicy(relevance_threshold_vector=0.50),
    )
    assert loose.refused is False


async def test_threshold_uses_vector_caliber_after_degrade(build, monkeypatch):
    """重排降级后 MUST 切回 vector 口径的阈值（不是继续用 rerank 口径）。

    ★ 出厂值上两套阈值**恰好相等**（`decisions.RELEVANCE_THRESHOLD_VECTOR` 与
      `_RERANK` 都是 0.50），所以「用错口径」在出厂值下根本不可观测 —— 这条用例
      若只依赖场景覆盖（0.10 / 0.99）也测不出来：0.55 同时高于 0.50 与 0.10，
      改错了照样不拒答。**必须把平台两套阈值改成不同值**，让「降级后仍用 rerank
      口径」唯一地表现为拒答。变异测试已验证：把 `eff.threshold_for(use_rerank=...)`
      改成单一口径时，旧写法全绿，新写法变红。
    """
    from app.services.rerank import RerankTimeout

    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_VECTOR", 0.20)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_RERANK", 0.90)

    harness = Harness([_row(uuid.uuid4(), 0.50)])  # 过 vector 0.20、不过 rerank 0.90
    harness.rerank_result = RerankTimeout("超时")  # type: ignore[assignment]
    service = build(harness, rerank_enabled=True)

    # 不覆盖阈值：走平台值，口径分流是否正确一目了然
    result = await _retrieve(service, harness.session(), policy=ScenarioPolicy())

    assert result.use_rerank is False
    assert result.score_source == "vector"
    assert result.refused is False, "降级后必须切回 vector 口径 0.20（用 rerank 口径 0.90 会拒掉）"


async def test_scenario_disabled_rerank_is_marked(build, monkeypatch):
    """平台开着、场景关掉 ⇒ `rag.rerank.disabled_by_scenario` 打点。

    ★ 为什么需要这个 counter：既有 `mode="off"` label 分不清「平台关」与「场景关」。
      而 label 取值不能改（改了会让既有看板出现断点，断点比不精确更难排查）。
    """
    from app.infra import metrics

    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
    before = metrics.get("rag.rerank.disabled_by_scenario")
    harness = Harness([_row(uuid.uuid4(), 0.9)])
    service = build(harness, rerank_enabled=True)

    await _retrieve(service, harness.session(), policy=ScenarioPolicy(rerank_enabled=False))

    assert metrics.get("rag.rerank.disabled_by_scenario") == before + 1
    assert not harness.rerank_calls, "场景关掉重排后不该再调用重排服务"


# ══════════════════════════════════════════════════════════════════════
# Z3-2 / Z3-3：零影响
# ══════════════════════════════════════════════════════════════════════


async def test_z3_2_retrieval_result_identical(build, monkeypatch):
    """`retrieve(policy=None)` 与 `retrieve(policy=ScenarioPolicy())` 逐字段相等。

    ★ 只排除 `stage_ms`（那是计时，逐次不同）。`RetrievalResult` 是 dataclass，
      `==` 会逐字段比较 —— 能真正发现顺序 / 分数 / refused 的漂移。
    """
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)

    def _strip(result):
        return dataclasses.replace(result, stage_ms={})

    # ★ 两次运行必须喂**同一组 chunk_id**：否则比较的是「两次随机构造的行」，
    #   任何 dataclass 相等都会因为 id 不同而失败 —— 那不是接线差异。
    ids = [uuid.uuid4(), uuid.uuid4()]
    scores = [0.55, 0.45]

    h_none = Harness([_row(i, s) for i, s in zip(ids, scores)])
    r_none = await _retrieve(build(h_none), h_none.session(), policy=None)

    h_empty = Harness([_row(i, s) for i, s in zip(ids, scores)])
    r_empty = await _retrieve(
        build(h_empty), h_empty.session(), policy=ScenarioPolicy()
    )

    assert _strip(r_none) == _strip(r_empty)


async def test_z3_3_unbound_app_policy_is_empty(monkeypatch):
    """未绑定场景的应用，走完 handler 后传给 `_chat_ask_impl` 的 policy 全 None。

    ★ 这是「未绑定 ⇒ 与改动前零差异」的 handler 级证据（Z3-3）：断言的是
      `declared() == {}`，而不是「policy is None」—— 因为 R5 分支返回的是
      一个**空的 ScenarioPolicy**，不是 None。若断言 None，这条用例就会假绿：
      它只证明「传了个 None」，不证明「传进去的覆盖集是空的」。
    """
    from fastapi.responses import StreamingResponse

    import app.api.chat as chat_mod
    from app.api import integrations as integrations_mod
    from app.schemas.integration import IntegrationAskRequest
    from tests.scenario.conftest import KB_A, FakeSession, build_row, make_application
    from tests.scenario.test_scenario_wiring import _no_rate_limit, _principal, _user

    captured: dict = {}

    async def _ask_impl(payload, user, session, integration=None, policy=None,
                        policy_caps=None):
        captured["policy"] = policy

        async def _events():
            yield chat_mod._sse("meta", {"conversation_id": None, "message_id": None})
            yield chat_mod._sse("done", {"usage": {}, "suggestions": []})

        return StreamingResponse(_events())

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    monkeypatch.setattr(integrations_mod, "_chat_ask_impl", _ask_impl)

    application = make_application(kb_ids=(str(KB_A),))  # scenario_id=None
    await integrations_mod.integration_ask(
        IntegrationAskRequest(question="住宿标准", kb_ids=[KB_A]),
        _principal(application, _user([KB_A])),
        FakeSession([build_row(is_builtin=True)]),
    )

    policy = captured["policy"]
    assert policy is not None, "handler 必须传 policy（不传等于 ask 接口拿不到策略）"
    assert policy.declared() == {}, "未绑定应用 ⇒ 稀疏覆盖集必须为空"
    eff = effective_retrieval(policy, platform_rerank_enabled=False)
    assert eff.recall_top_k == decisions.TOP_K_RECALL
    assert eff.clamped == ()


async def test_integration_ask_forwards_bound_policy(monkeypatch):
    """**绑定了**场景的应用：场景策略必须原样到达共用问答链路。

    ★ Z3-3 那条只证明「未绑定 ⇒ 空集」；空集在两种实现下都成立（传空 policy 与
      根本不传 policy 的 `declared()` 都是 `{}`），所以它**单独存在时抓不到**
      「忘了透传」这类漏改。这里给一个非空覆盖集：删掉
      `integrations.py` 的 `policy=snapshot.policy` 之后本用例立刻变红。
    """
    from fastapi.responses import StreamingResponse

    import app.api.chat as chat_mod
    from app.api import integrations as integrations_mod
    from app.schemas.integration import IntegrationAskRequest
    from tests.scenario.conftest import (
        KB_A,
        SCENARIO_ID,
        FakeSession,
        build_row,
        make_application,
    )
    from tests.scenario.test_scenario_wiring import _no_rate_limit, _principal, _user

    captured: dict = {}

    async def _ask_impl(payload, user, session, integration=None, policy=None,
                        policy_caps=None):
        captured["policy"] = policy
        captured["policy_caps"] = policy_caps
        captured["integration"] = integration

        async def _events():
            yield chat_mod._sse("meta", {"conversation_id": None, "message_id": None})
            yield chat_mod._sse("done", {"usage": {}, "suggestions": []})

        return StreamingResponse(_events())

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    monkeypatch.setattr(integrations_mod, "_chat_ask_impl", _ask_impl)

    application = make_application(scenario_id=SCENARIO_ID, kb_ids=(str(KB_A),))
    await integrations_mod.integration_ask(
        IntegrationAskRequest(question="住宿标准", kb_ids=[KB_A]),
        _principal(application, _user([KB_A])),
        FakeSession([build_row(policy={"recall_top_k": 7})]),
    )

    policy = captured["policy"]
    assert policy is not None
    assert policy.declared() == {"recall_top_k": 7}, "绑定场景的覆盖集必须原样透传"
    assert effective_retrieval(policy, platform_rerank_enabled=False).recall_top_k == 7
    # Step 7：caps 必须与 policy 同一次读取带下来（关闭治理时是空 caps）
    assert captured["policy_caps"] is not None
    assert captured["policy_caps"].is_empty()


async def test_integration_search_forwards_policy_to_retrieve(monkeypatch):
    """/integrations/search 也必须把 snapshot.policy 传进 `retrieve()`。

    ★ ask 与 search 是**两条**独立调用点。只接一条的症状是「问答按场景走、
      检索接口不按场景走」—— 对集成方而言这是两个接口给出两套口径。
    """
    from app.api import integrations as integrations_mod
    from app.schemas.integration import IntegrationSearchRequest
    from tests.scenario.conftest import (
        KB_A,
        SCENARIO_ID,
        FakeSession,
        build_row,
        make_application,
    )
    from tests.scenario.test_scenario_wiring import _no_rate_limit, _principal, _user

    captured: dict = {}

    class _RecordingRetrieval:
        async def retrieve(self, session, query, tenant_id, kb_ids, clearance, subjects, *,
                           policy=None, policy_caps=None):
            captured["policy"] = policy
            captured["policy_caps"] = policy_caps
            captured["kb_ids"] = list(kb_ids)
            from app.services.retrieve.base import RetrievalResult

            return RetrievalResult(chunks=[], stage_ms={})

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _no_rate_limit)
    monkeypatch.setattr(
        integrations_mod, "get_retrieval_service", lambda: _RecordingRetrieval()
    )

    application = make_application(scenario_id=SCENARIO_ID, kb_ids=(str(KB_A),))
    await integrations_mod.integration_search(
        IntegrationSearchRequest(query="住宿标准", kb_ids=[KB_A]),
        _principal(application, _user([KB_A])),
        FakeSession([build_row(policy={"recall_top_k": 7})]),
    )

    policy = captured["policy"]
    assert policy is not None, "search 不传 policy ⇒ 检索接口静默忽略场景配置"
    assert policy.declared() == {"recall_top_k": 7}
    # Step 7：caps 与 policy 同源，漏传会让 search 绕过租户收紧
    assert captured["policy_caps"] is not None
    assert captured["policy_caps"].is_empty()
    assert captured["kb_ids"] == [KB_A], "接管取范围仍来自请求 ∩ 应用授权，与场景无关"


# ══════════════════════════════════════════════════════════════════════
# D.3.4：链式多轮**每一轮**都收到同一个 policy
# ══════════════════════════════════════════════════════════════════════

async def test_chain_passes_policy_to_every_round(monkeypatch):
    """多轮子问题每一轮收到的 policy 与入口**同一个对象**。

    ★ 漏一处 = 场景策略只对单趟生效、对链式不生效 —— 而链式是默认路径
      （`CHAIN_RETRIEVAL_ENABLED` 默认为真）。这类漏改不会报错，只会让
      「我配的 recall_top_k 好像没用」变成没人能解释的现象。
    ★ 断言 `is policy`（同一对象）而不是 `==`：`ScenarioPolicy` 是 frozen dataclass，
      `==` 对「另一个内容相同的实例」也成立，那证明不了透传。
    """
    from app.services.retrieve import chain as chain_mod

    seen: list = []
    svc = RetrievalService()

    async def _stub(sess, query, tenant_id, kb_ids, clearance, subjects, *, policy=None, policy_caps=None):  # noqa: ANN001
        seen.append(policy)
        from app.services.retrieve.base import RetrievalResult, RetrievedChunk

        return RetrievalResult(
            chunks=[
                RetrievedChunk(
                    chunk_id=uuid.uuid4(), document_id=DOC, kb_id=KB, filename="制度.pdf",
                    content="住宿标准 500 元", heading_path="差旅", page_no=1,
                    level_rank=20, vector_score=0.9, keyword_score=None,
                    rrf_score=0.9, rerank_score=None, final_score=0.9,
                )
            ],
            stage_ms={},
        )

    svc.retrieve = _stub  # type: ignore[method-assign]

    async def _decompose(query, tenant_id):  # noqa: ANN001, ARG001
        return ["子问题一", "子问题二"]

    monkeypatch.setattr(chain_mod, "decompose_question", _decompose)
    monkeypatch.setattr(retrieve_mod, "decompose_question", _decompose)
    monkeypatch.setattr(decisions, "CHAIN_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(decisions, "CHAIN_MIN_SUBQUESTIONS", 2, raising=False)

    policy = ScenarioPolicy(recall_top_k=7)
    result = await svc.retrieve_chain(
        object(), "复杂问题", TENANT, [KB], 40, [], rewrite_score=10, policy=policy
    )

    assert len(seen) == 2, f"应有两轮子问题检索，实际 {len(seen)}"
    assert all(p is policy for p in seen), "每一轮都必须收到同一个 policy 对象"
    assert result.chain_mode == "multi:2"


# ══════════════════════════════════════════════════════════════════════
# D.3.6：`_chat_ask_impl` **内部**的转发（P0-2）
# ══════════════════════════════════════════════════════════════════════


async def test_chat_ask_impl_forwards_policy_to_retrieve_chain(monkeypatch):
    """`_chat_ask_impl` 内部 MUST 把收到的 `policy` 转给 `retrieve_chain`。

    ★ 为什么必须有这一条：`test_integration_ask_forwards_bound_policy` 与
      `test_z3_3_unbound_app_policy_is_empty` 都把 `_chat_ask_impl` **整个 monkeypatch
      掉了** —— 它们只覆盖「integrations handler → impl」这一段。把 `chat.py` 里那行
      `policy=policy` 改成 `policy=None`，那两条照样全绿（验证者实测：254 条全绿）。
      能覆盖「impl 内部转发」的写法只有一种 —— 驱动**真实**的 `_chat_ask_impl`。

    ★ 只打桩「外部世界」（DB / 改写 / 生成 / 审计 / 配额），链路本身全真：
      不调云端、不调 embedding、不连真库。
    """
    from app.api import chat as chat_mod
    from app.schemas.chat import ChatAskRequest
    from app.services.retrieve.base import RetrievalResult
    from tests.chat.test_sse_retrieval_observation import (
        _USER,
        _FakeGeneration,
        _FakeSession,
        _chunk,
    )

    seen: dict = {}

    class _RecordingRetrieval:
        async def retrieve_chain(  # noqa: ANN001
            self, session, query, tenant_id, kb_ids, clearance, subjects,
            rewrite_score=None, *, policy=None, policy_caps=None,
        ):
            seen["policy"] = policy
            seen["query"] = query
            return RetrievalResult(chunks=[_chunk()], stage_ms={}, chain_mode="single")

    # ── 外部世界全部打桩（与 tests/chat/test_sse_retrieval_observation.py 同款）──
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(chat_mod, "get_retrieval_service", lambda: _RecordingRetrieval())
    monkeypatch.setattr(chat_mod, "get_stream_generation_service", lambda: _FakeGeneration())
    monkeypatch.setattr(chat_mod, "rewrite_query", _fake_rewrite)
    monkeypatch.setattr(chat_mod, "build_system_prompt", _fake_system_prompt)
    monkeypatch.setattr(chat_mod, "snapshot_for_message", _fake_caliber)
    monkeypatch.setattr(chat_mod, "_persist_assistant", _noop_async)
    monkeypatch.setattr(chat_mod, "_update_user_memory", _noop_async)
    monkeypatch.setattr(chat_mod.audit, "record", _noop_async)
    monkeypatch.setattr(chat_mod.flag_service, "is_enabled", _flag_off)
    monkeypatch.setattr(chat_mod.quota_service, "_incr_usage", _noop_async)

    policy = ScenarioPolicy(recall_top_k=7)

    response = await chat_mod._chat_ask_impl(
        ChatAskRequest(kb_ids=[KB], question="住宿报销标准是多少？"),
        _USER,
        _FakeSession(kb_count=1),
        integration=None,
        policy=policy,
    )

    body = "".join(
        [piece if isinstance(piece, str) else piece.decode("utf-8")
         async for piece in response.body_iterator]
    )

    assert "event: done" in body, "流必须正常跑完（否则说明链路在某处提前 return）"
    assert seen.get("policy") is policy, (
        "impl 内部 MUST 把 policy 转给 retrieve_chain（`is` 同一对象，防「内容相同的新实例」）"
    )


async def _fake_rewrite(query, history, tenant_id):  # noqa: ANN001, ARG001
    return query, "skipped", 100


async def _fake_system_prompt(*args, **kwargs):  # noqa: ANN001, ARG001
    return "system"


async def _fake_caliber(tenant_id, chain_mode=None):  # noqa: ANN001, ARG001
    return {}


async def _noop_async(*args, **kwargs):  # noqa: ANN001, ARG001
    return None


async def _flag_off(*args, **kwargs):  # noqa: ANN001, ARG001
    return False


# ══════════════════════════════════════════════════════════════════════
# D.3.7：`retrieve_chain` 的**单趟**路径（P0-1）
# ══════════════════════════════════════════════════════════════════════


async def test_chain_single_path_passes_policy(monkeypatch):
    """`should_use_chain` 返回 False 时走 `_single()`，policy 也必须原样到达。

    ★ 为什么单趟必须单独钉：`test_chain_passes_policy_to_every_round` 只走了
      decompose → 多轮分支。而单趟是**常见路径**，不是边角：
      - `rewrite_score >= QUERY_REWRITE_SCORE_THRESHOLD`（自包含的简单问题）；
      - `rewrite_score is None`（跳过了改写流程）；
      - 开关 `CHAIN_RETRIEVAL_ENABLED=False`；
      - 拆解失败 / 拆解后只有 1 个子问题。
      前两种在线上是主要流量。`_single()` 里那行 `policy=policy` 被删掉，
      症状是「场景的召回条数对多轮生效、对单趟不生效」—— 完全不报错。
    ★ 这里**读真实判据**决定传什么 `rewrite_score`，不硬编码 90：
      阈值改了本用例跟着走（写死 90 的用例会在调阈值那天变成假绿）。
    """
    from app.services.retrieve.base import RetrievalResult
    from tests.scenario.conftest import KB_A, SCENARIO_ID  # noqa: F401

    seen: list = []
    svc = RetrievalService()

    async def _stub(sess, query, tenant_id, kb_ids, clearance, subjects, *, policy=None, policy_caps=None):  # noqa: ANN001
        seen.append(policy)
        from app.services.retrieve.base import RetrievedChunk

        return RetrievalResult(
            chunks=[
                RetrievedChunk(
                    chunk_id=uuid.uuid4(), document_id=DOC, kb_id=KB, filename="制度.pdf",
                    content="住宿标准 500 元", heading_path="差旅", page_no=1,
                    level_rank=20, vector_score=0.9, keyword_score=None,
                    rrf_score=0.9, rerank_score=None, final_score=0.9,
                )
            ],
            stage_ms={},
        )

    svc.retrieve = _stub  # type: ignore[method-assign]

    # 用真实判据算出「一定走单趟」的分数：阈值本身 + 1（自包含简单问题）
    score = decisions.QUERY_REWRITE_SCORE_THRESHOLD
    assert decisions.should_use_chain(score) is False, "前提：该分数必须走单趟"

    async def _must_not_decompose(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("单趟路径不该调用拆解")

    from app.services.retrieve import chain as chain_mod

    monkeypatch.setattr(chain_mod, "decompose_question", _must_not_decompose)
    monkeypatch.setattr(retrieve_mod, "decompose_question", _must_not_decompose)

    policy = ScenarioPolicy(recall_top_k=7)
    result = await svc.retrieve_chain(
        object(), "住宿标准是多少", TENANT, [KB], 40, [], rewrite_score=score, policy=policy
    )

    assert result.chain_mode == "single"
    assert len(seen) == 1, "单趟只该调用一次 retrieve"
    assert seen[0] is policy, "单趟路径 MUST 把同一个 policy 传给 retrieve()"


async def test_chain_single_path_when_rewrite_score_is_none(monkeypatch):
    """`rewrite_score is None`（跳过改写）同样走单趟，policy 同样必须到达。

    ★ None 是线上常态：改写超时 / 关闭改写功能时分数为 None。这条与上一条
      覆盖的是 `should_use_chain` 的两个不同分支，删任一条都会漏。
    """
    seen: list = []
    svc = RetrievalService()
    from app.services.retrieve.base import RetrievalResult

    async def _stub(sess, query, tenant_id, kb_ids, clearance, subjects, *, policy=None, policy_caps=None):  # noqa: ANN001
        seen.append(policy)
        return RetrievalResult(chunks=[], stage_ms={})

    svc.retrieve = _stub  # type: ignore[method-assign]

    assert decisions.should_use_chain(None) is False, "前提：None 必须走单趟"

    monkeypatch.setattr(decisions, "CHAIN_RETRIEVAL_ENABLED", True)

    policy = ScenarioPolicy(recall_top_k=7)
    result = await svc.retrieve_chain(
        object(), "住宿标准是多少", TENANT, [KB], 40, [], rewrite_score=None, policy=policy
    )

    assert result.chain_mode == "single"
    assert seen[0] is policy


# ══════════════════════════════════════════════════════════════════════
# D.3.5：折叠点位置 —— 必须在「空授权范围早退」之后
# ══════════════════════════════════════════════════════════════════════


async def test_empty_kb_scope_returns_before_reading_platform_settings(monkeypatch):
    """空授权范围 MUST 在读平台参数**之前**返回（零影响 Z3-2 的形式之一）。

    ★ 断言手段是「把 settings 替身削到连 `rerank_enabled` 都没有」：折叠块一旦
      上移到早退之前，本用例立刻 AttributeError 变红。这不是人为刁难 ——
      `tests/retrieve/` 里现存的 settings 替身就只提供部分字段
      （13 个预存在失败正是缺字段的桩），上移会让那一整类桩改错。
    """
    def _no_embedding():  # noqa: ANN202
        raise AssertionError("空授权范围不该走到 embedding")

    monkeypatch.setattr(retrieve_mod, "get_embedding_service", _no_embedding)
    monkeypatch.setattr(retrieve_mod, "get_settings", lambda: fake_settings())
    service = RetrievalService()

    result = await service.retrieve(
        None, "报销制度", TENANT, [], 40, ["dept:it"],  # type: ignore[arg-type]
        policy=ScenarioPolicy(recall_top_k=7),
    )

    assert result.refused is True
    assert result.chunks == []
