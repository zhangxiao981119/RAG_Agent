"""WU-M-15 四维审查测试 —— 判据 / 合成 / 任务语义 / 不变量检查点。

边界全替身：SessionLocal、arq 池、LLM、提示词解析全部 monkeypatch，
零网络零 DB。钉住十二件事：
  1. 时效性规则：过期 0 分 / 未生效低分 / created_at 分档衰减
  2. weight 合成：四维加权均值 -50，范围 [-50, +50]
  3. 审查成功：四维分数 + 依据落 documents，governance_report 有依据（DoD）
  4. chunks.governance_weight 同步更新（检索加权数据源）
  5. 无内容文档：代码短路不调 LLM，三维 0 分 + NO_ANSWER_TEXT 契约
  6. LLM 输出无 JSON：抛 GovernanceError，治理字段不被污染
  7. 审查失败重试耗尽：job dead + 文档 failed（INV-M-12 降级未审查）
  8. 幂等：文档已 done 时重复 job 直接成功，不再调 LLM
  9. cron 扫描：pending 文档建 job + 入队，enqueue 先于 commit（无 zombie）
  10. INV-M-10：run_parse_job 源码零治理引用 + 治理任务独立注册
  11. INV-M-11：审查只写治理列（update 仅 governance_weight），不改原文
  12. governance_report 含四维依据（DoD：结论有 governance_report 依据）
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Select, Update

from app.config.prompts import NO_ANSWER_TEXT
from app.models import Document, GovernanceJob
from app.services import governance
from app.workers.parse_job import WorkerSettings, run_parse_job, run_sync_all, scan_and_enqueue

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
TENANT = uuid.uuid4()
DOC_ID = uuid.uuid4()

_LLM_JSON = (
    '{"authority": {"score": 80, "basis": "正式制度文件口吻"},'
    ' "consistency": {"score": 90, "basis": "前后数字一致"},'
    ' "completeness": {"score": 70, "basis": "末尾章节有截断迹象"}}'
)


def _doc(**overrides) -> Document:
    """构造内存态文档（不落库）。治理字段默认未审查。"""
    defaults: dict = {
        "id": DOC_ID,
        "tenant_id": TENANT,
        "kb_id": uuid.uuid4(),
        "doc_group_id": uuid.uuid4(),
        "version": 1,
        "is_latest": True,
        "filename": "报销制度.pdf",
        "ext": "pdf",
        "size_bytes": 1024,
        "storage_key": "k/doc",
        "status": "indexed",
        "level_rank": 20,
        "acl_tags": [],
        "deny_subjects": [],
        "created_at": NOW - timedelta(days=30),
        "governance_status": "running",
        "governance_report": {},
    }
    defaults.update(overrides)
    return Document(**defaults)


def _job(doc: Document, **overrides) -> GovernanceJob:
    defaults: dict = {
        "id": uuid.uuid4(),
        "tenant_id": doc.tenant_id,
        "document_id": doc.id,
        "status": "queued",
        "attempts": 0,
        "max_attempts": 3,
        "last_error": None,
    }
    defaults.update(overrides)
    return GovernanceJob(**defaults)


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class FakeSession:
    """极简 session 替身：get 查共享对象表，select 按队列吐行，update 记录参数。"""

    def __init__(self, h: "Harness") -> None:
        self._h = h

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        return None

    async def get(self, model, obj_id):  # noqa: ANN001
        return self._h.objects.get((model, obj_id))

    async def execute(self, stmt):  # noqa: ANN001
        if isinstance(stmt, Update):
            self._h.updates.append(dict(stmt.compile().params))
            self._h.events.append("update")
            return None
        assert isinstance(stmt, Select), f"未知语句类型: {type(stmt)}"
        rows = self._h.select_rows.pop(0) if self._h.select_rows else []
        return FakeResult(rows)

    def add(self, obj) -> None:  # noqa: ANN001
        self._h.added.append(obj)

    async def flush(self) -> None:
        for obj in self._h.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        self._h.commits += 1
        self._h.events.append("commit")


class FakeLLM:
    def __init__(self, h: "Harness") -> None:
        self._h = h

    async def chat(self, messages, temperature) -> str:  # noqa: ANN001, ARG002
        self._h.llm_calls.append(messages)
        if isinstance(self._h.llm_output, Exception):
            raise self._h.llm_output
        return self._h.llm_output


class FakePool:
    def __init__(self, h: "Harness") -> None:
        self._h = h

    async def enqueue_job(self, *args) -> None:  # noqa: ANN002
        self._h.enqueued.append(args)
        self._h.events.append("enqueue")


class Harness:
    """一次审查 / 扫描运行的全部替身与记录。"""

    def __init__(self) -> None:
        self.objects: dict[tuple, object] = {}
        self.select_rows: list[list] = []
        self.updates: list[dict] = []
        self.added: list = []
        self.enqueued: list = []
        self.llm_calls: list = []
        self.llm_output: str | Exception = _LLM_JSON
        self.commits = 0
        self.events: list[str] = []  # enqueue / commit / update 事件序

    def put(self, *objs) -> None:
        for obj in objs:
            self.objects[(type(obj), obj.id)] = obj


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness()

    def _session():
        # SessionLocal 是同步工厂：调用即返回 AsyncSession（生产同款语义），
        # 不能写成 async 函数 —— 那会返回 coroutine，async with 直接炸
        return FakeSession(h)

    async def _pool():
        return FakePool(h)

    def _llm():
        return FakeLLM(h)

    async def _resolve(key, tenant_id=None):  # noqa: ANN001, ARG001
        return "审查提示词模板 {content}"

    monkeypatch.setattr(governance, "SessionLocal", _session)
    monkeypatch.setattr(governance, "get_arq_pool", _pool)
    monkeypatch.setattr(governance, "get_llm_service", _llm)
    monkeypatch.setattr(governance, "resolve_prompt", _resolve)
    return h


# ── 1. 时效性规则 ─────────────────────────────────────────


def test_freshness_expired_is_zero() -> None:
    score, basis = governance.freshness_score(
        None, NOW - timedelta(days=1), NOW - timedelta(days=30), NOW
    )
    assert score == 0
    assert "expire_at" in basis


def test_freshness_not_effective_is_low() -> None:
    score, basis = governance.freshness_score(
        NOW + timedelta(days=7), None, NOW - timedelta(days=1), NOW
    )
    assert score == governance.decisions.GOVERNANCE_FRESHNESS_FUTURE_SCORE
    assert "尚未生效" in basis


def test_freshness_tiers() -> None:
    cases = [(30, 90), (100, 75), (200, 55), (400, 35), (800, 15)]
    for age, expected in cases:
        score, basis = governance.freshness_score(
            None, None, NOW - timedelta(days=age), NOW
        )
        assert score == expected, f"age={age}"
        assert str(age) in basis


# ── 2. weight 合成 ────────────────────────────────────────


def test_compose_weight_bounds() -> None:
    assert governance.compose_weight(100, 100, 100, 100) == 50
    assert governance.compose_weight(0, 0, 0, 0) == -50


def test_compose_weight_mixed() -> None:
    # 90*0.35 + 80*0.30 + 90*0.20 + 70*0.15 = 84.0 → 84 - 50 = 34
    assert governance.compose_weight(90, 80, 90, 70) == 34


# ── 3~6. 审查主流程 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_review_success(harness: Harness) -> None:
    doc = _doc()
    harness.put(doc)
    harness.select_rows.append([("制度正文", "费用报销"), ("附则", None)])

    report = await governance.review_document(doc.id, doc.tenant_id)

    # 四维分数落库（INV-M-11：只写治理字段）
    assert doc.freshness_score == 90
    assert doc.authority_score == 80
    assert doc.consistency_score == 90
    assert doc.completeness_score == 70
    assert doc.governance_status == "done"
    # weight 同步到 chunks（检索加权数据源）：update 限定该文档且只写治理列
    assert len(harness.updates) == 1
    params = harness.updates[0]
    assert params.get("governance_weight") == 34
    assert str(doc.id) in {str(v) for v in params.values()}
    # DoD：governance_report 有依据
    assert report["dims"]["authority"]["basis"] == "正式制度文件口吻"
    assert report["dims"]["freshness"]["basis"].startswith("创建至今")
    assert report["governance_weight"] == 34
    # LLM 收到了带 heading 前缀的审查文本
    sent = harness.llm_calls[0][0]["content"]
    assert "[费用报销] 制度正文" in sent


@pytest.mark.asyncio
async def test_review_empty_content_short_circuits_llm(harness: Harness) -> None:
    doc = _doc()
    harness.put(doc)
    harness.select_rows.append([])  # 无 chunk

    report = await governance.review_document(doc.id, doc.tenant_id)

    assert harness.llm_calls == []  # 代码短路，不调 LLM
    for dim in ("authority", "consistency", "completeness"):
        assert report["dims"][dim]["score"] == 0
        assert report["dims"][dim]["basis"] == NO_ANSWER_TEXT


@pytest.mark.asyncio
async def test_review_bad_llm_output_leaves_fields_clean(harness: Harness) -> None:
    doc = _doc()
    harness.put(doc)
    harness.select_rows.append([("正文", None)])
    harness.llm_output = "抱歉，我无法输出 JSON"

    with pytest.raises(governance.GovernanceError):
        await governance.review_document(doc.id, doc.tenant_id)

    # 治理字段不被污染（异常发生在落库前）
    assert doc.governance_status == "running"
    assert doc.authority_score is None
    assert harness.updates == []


@pytest.mark.asyncio
async def test_review_llm_missing_dim_rejected(harness: Harness) -> None:
    doc = _doc()
    harness.put(doc)
    harness.select_rows.append([("正文", None)])
    harness.llm_output = '{"authority": {"score": 80, "basis": "x"}}'  # 缺两个维度

    with pytest.raises(governance.GovernanceError, match="consistency"):
        await governance.review_document(doc.id, doc.tenant_id)


# ── 7~9. 任务语义 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_job_retry_exhausted_degrades(harness: Harness) -> None:
    """INV-M-12：重试耗尽 → job dead + 文档 failed（降级未审查，不写治理分）。"""
    doc = _doc()
    job = _job(doc, attempts=0, max_attempts=1)
    harness.put(doc, job)
    harness.select_rows.append([("正文", None)])
    harness.llm_output = governance.GovernanceError("LLM 不可用")

    result = await governance.execute_governance_job({}, str(job.id))

    assert result == "dead"
    assert job.status == "dead"
    assert doc.governance_status == "failed"
    assert "GovernanceError" in job.last_error
    assert harness.updates == []  # 未写任何治理加权


@pytest.mark.asyncio
async def test_execute_job_idempotent_when_done(harness: Harness) -> None:
    doc = _doc(governance_status="done")
    job = _job(doc)
    harness.put(doc, job)

    result = await governance.execute_governance_job({}, str(job.id))

    assert result == "succeeded"
    assert job.status == "succeeded"
    assert harness.llm_calls == []  # 不重复审查


@pytest.mark.asyncio
async def test_scan_enqueues_pending_docs(harness: Harness) -> None:
    doc = _doc(governance_status="pending")
    harness.put(doc)
    harness.select_rows.append([(doc.id, doc.tenant_id)])

    count = await governance.scan_and_enqueue({})

    assert count == 1
    assert len(harness.added) == 1
    job = harness.added[0]
    assert isinstance(job, GovernanceJob)
    assert job.document_id == doc.id
    # 入队参数与 flush 后的 job.id 一致，且 enqueue 先于 commit（无 zombie）
    assert harness.enqueued == [("execute_governance_job", str(job.id), harness.enqueued[0][2])]
    assert harness.events.index("enqueue") < harness.events.index("commit")


@pytest.mark.asyncio
async def test_scan_no_pending_is_noop(harness: Harness) -> None:
    harness.select_rows.append([])

    assert await governance.scan_and_enqueue({}) == 0
    assert harness.enqueued == []


# ── 10~11. 不变量检查点 ───────────────────────────────────


def test_inv_m_10_parse_job_has_no_governance_call() -> None:
    """run_parse_job 函数体零治理引用；治理任务独立注册进 functions/cron。"""
    source = inspect.getsource(run_parse_job)
    assert "governance" not in source.lower()
    names = [fn.__name__ for fn in WorkerSettings.functions]
    assert "execute_governance_job" in names
    assert "scan_and_enqueue" in names
    # arq CronJob 的数据类字段名是 coroutine
    cron_funcs = [getattr(job, "coroutine", None) for job in WorkerSettings.cron_jobs]
    assert run_sync_all in cron_funcs
    assert scan_and_enqueue in cron_funcs


def test_inv_m_11_only_governance_columns_written() -> None:
    """审查落库只碰治理列：update 仅 governance_weight，无任何原文赋值。"""
    source = Path(governance.__file__).read_text(encoding="utf-8")
    assert ".values(" in source
    assert source.count(".values(") == 1  # 唯一的 update：chunks 治理加权
    assert "governance_weight=weight" in source
    assert ".content =" not in source  # MUST NOT 改写 chunk.content（INV-M-11）
    assert ".filename =" not in source
