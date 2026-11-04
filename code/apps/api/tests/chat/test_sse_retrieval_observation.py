"""SSE 检索侧观测字段（`path_stats` / `graph_degraded_reason`）契约测试 —— §14.6 可测化。

钉住四件事：
  1. **纯追加**：`done` 与「检索之后」的 `refused` 载荷多出这两个键，
     而**既有键一个不少、事件顺序不变**（前端依赖 meta→stage→citations→delta→done）。
  2. 落点是 `done` / `refused`，**不是** `meta` —— meta 发在检索之前，
     那时还没有 path_stats；塞进 meta 只能靠「先检索再发 meta」，会牺牲 TTFB。
  3. 检索没跑完的拒答（检索异常 → VECTOR_STORE_DOWN）**不带**这两个键：
     没有结果就没有观测，不许编一个空壳字段。
  4. chat.py 里构造这两个键的地方**只有一处**（`_retrieval_observation`），
      谁在别处再手拼一次，这里就红 —— 两份载荷必然漂移。
"""
from __future__ import annotations

import inspect
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_mod
from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.rate_limit import check_chat_rate_limit
from app.main import app
from app.services.generate import StreamDelta
from app.services.retrieve.base import RetrievalResult, RetrievedChunk

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()

_USER = CurrentUser(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    username="tester",
    display_name="tester",
    clearance=40,  # admin：豁免库级交集校验，聚焦本文件的 SSE 载荷契约
    dept_path="",
    subjects=[],
    authorized_kb_ids=[],
    acl_epoch=1,
)


# ── 替身：DB ────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    """替身 AsyncSession：KB 存在性查询返回指定行数，其余（历史 / 画像）返回空。"""

    def __init__(self, kb_count: int = 0) -> None:
        self._kb_rows = [object() for _ in range(kb_count)]
        self.added: list = []

    async def execute(self, stmt) -> _FakeResult:  # noqa: ANN001
        return _FakeResult(self._kb_rows)

    async def get(self, model, key):  # noqa: ANN001
        return None

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    def add_all(self, objs) -> None:  # noqa: ANN001
        self.added.extend(objs)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:  # noqa: ANN001
        pass

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


# ── 替身：检索 / 生成 ────────────────────────────────────────────


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=_KB_ID,
        filename="费用报销制度.pdf",
        content="一线城市住宿报销标准为 500 元/晚",
        heading_path="",
        page_no=None,
        level_rank=1,
        vector_score=0.9,
        keyword_score=None,
        rrf_score=0.9,
        rerank_score=0.95,
        final_score=0.95,
    )


class _FakeRetrieval:
    """可配置替身：正常 / 检索拒答 / 抛异常（模拟 VECTOR_STORE_DOWN）。"""

    def __init__(self) -> None:
        self.result = RetrievalResult(
            chunks=[_chunk()],
            stage_ms={"retrieving": 1},
            path_stats={
                "vector_covered": 1,
                "graph_covered": 2,
                "graph_unique": 1,
            },
            graph_degraded_reason="timeout",
        )
        self.raise_on_retrieve = False

    async def retrieve(self, session, query, tenant_id, kb_ids, clearance, subjects):  # noqa: ANN001
        if self.raise_on_retrieve:
            raise RuntimeError("pgvector 不可用")
        return self.result

    async def retrieve_chain(self, session, query, tenant_id, kb_ids, clearance, subjects,
                             rewrite_score=None):  # noqa: ANN001
        result = await self.retrieve(session, query, tenant_id, kb_ids, clearance, subjects)
        result.chain_mode = "single"
        return result


class _FakeGeneration:
    """可配置替身：正常 done 或生成层拒答（UNGROUNDED）。"""

    def __init__(self) -> None:
        self.refuse_reason = ""

    def stream_generate(self, query, chunks, history=None, memory_prompt="",  # noqa: ANN001
                        tenant_id=None, rewrite_score=None):
        async def _gen():
            if self.refuse_reason:
                yield StreamDelta(
                    type="refused", refused=True, refuse_reason=self.refuse_reason,
                )
                return
            yield StreamDelta(type="delta", text="住宿报销标准为 500 元/晚 [1]\n")
            yield StreamDelta(
                type="done",
                full_text="住宿报销标准为 500 元/晚 [1]\n",
                suggestions=["追问 A"],
                usage={"prompt_tokens": 1, "completion_tokens": 2},
            )

        return _gen()


class _Harness:
    def __init__(self) -> None:
        self.client: TestClient | None = None
        self.retrieval = _FakeRetrieval()
        self.generation = _FakeGeneration()
        self.db_session = _FakeSession(kb_count=1)


@pytest.fixture
def harness(monkeypatch):
    h = _Harness()

    app.dependency_overrides[get_current_user] = lambda: _USER

    async def _override_get_db():
        yield h.db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[check_chat_rate_limit] = lambda: None

    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(chat_mod, "get_retrieval_service", lambda: h.retrieval)
    monkeypatch.setattr(chat_mod, "get_stream_generation_service", lambda: h.generation)

    async def _fake_rewrite(query, history, tenant_id):  # noqa: ANN001
        return query, "skipped", 100

    monkeypatch.setattr(chat_mod, "rewrite_query", _fake_rewrite)

    async def _fake_system_prompt(*args, **kwargs):  # noqa: ANN001
        return "system"

    monkeypatch.setattr(chat_mod, "build_system_prompt", _fake_system_prompt)

    async def _fake_caliber(tenant_id, chain_mode=None):  # noqa: ANN001
        return {}

    monkeypatch.setattr(chat_mod, "snapshot_for_message", _fake_caliber)

    async def _fake_persist(*args, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod, "_persist_assistant", _fake_persist)

    async def _fake_memory(user_id, user_msg, assistant_msg):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod, "_update_user_memory", _fake_memory)

    async def _fake_audit(*args, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod.audit, "record", _fake_audit)

    async def _flag_off(*args, **kwargs):  # noqa: ANN001
        return False

    monkeypatch.setattr(chat_mod.flag_service, "is_enabled", _flag_off)

    async def _noop_incr(*args, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod.quota_service, "_incr_usage", _noop_incr)

    # ★ 不用 `with TestClient(app)`（不触发 lifespan）：
    #   lifespan 会真连一次 DB，asyncpg 连接池绑定到第一个事件循环；
    #   第二个用例的 TestClient 起了新循环，池里残留连接就是
    #   「attached to a different loop」→ 整个文件只有首个用例能过。
    #   本文件对 DB/redis/生成/审计全部打了桩，lifespan 初始化的东西一样都不需要。
    #   （test_ask_with_image.py 的 6 条环境依赖 error 就是同一个坑，别再复制。）
    client = TestClient(app)
    h.client = client
    try:
        yield h
    finally:
        app.dependency_overrides.clear()


# ── 工具 ────────────────────────────────────────────────────────


def _events(body: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        event = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if event:
            out.append((event, json.loads(data) if data else {}))
    return out


def _ask(h) -> list[tuple[str, dict]]:  # noqa: ANN001
    resp = h.client.post(
        "/api/chat/ask",
        json={"kb_ids": [str(_KB_ID)], "question": "住宿报销标准是多少？"},
    )
    assert resp.status_code == 200
    return _events(resp.text)


# ── 用例 ────────────────────────────────────────────────────────


def test_done_carries_retrieval_observation_and_keeps_existing_keys(harness):
    """成功轮：done 多两个键，既有四个键一个不少，事件顺序不变。"""
    events = _ask(harness)
    kinds = [e for e, _ in events]
    # 事件顺序 MUST 保持 meta→stage→citations→delta...→done
    assert kinds[0] == "meta"
    assert kinds.index("citations") < kinds.index("delta") < kinds.index("done")

    done = events[-1][1]
    assert done["path_stats"] == {
        "vector_covered": 1, "graph_covered": 2, "graph_unique": 1,
    }
    assert done["graph_degraded_reason"] == "timeout"
    # 既有键 MUST 一字不动（前端依赖）
    assert done["finish_reason"] == "stopped"
    assert done["grounding"] == {"stripped_sentences": 0}
    assert done["usage"] == {"prompt_tokens": 1, "completion_tokens": 2}
    assert done["suggestions"] == ["追问 A"]


def test_meta_does_not_carry_observation(harness):
    """meta 发在检索之前，MUST NOT 带检索观测 —— 带了只能是编的。"""
    events = _ask(harness)
    meta = events[0][1]
    assert "path_stats" not in meta
    assert "graph_degraded_reason" not in meta
    # meta 既有键照旧
    assert meta["stage"] == "retrieving"
    assert meta["modality"] == "text"


def test_retrieval_refused_also_carries_observation(harness):
    """检索阶段就拒答：能拒答说明检索跑完了，观测同样有效。"""
    harness.retrieval.result = RetrievalResult(
        refused=True,
        refuse_reason="NO_RELEVANT_CONTENT",
        path_stats={"graph_covered": 0, "graph_unique": 3},
        graph_degraded_reason="no_neighbors",
    )
    events = _ask(harness)
    kinds = [e for e, _ in events]
    assert kinds[-1] == "refused"
    payload = events[-1][1]
    assert payload["reason"] == "NO_RELEVANT_CONTENT"
    assert payload["message"] == "知识库中未找到相关内容"
    assert payload["path_stats"] == {"graph_covered": 0, "graph_unique": 3}
    assert payload["graph_degraded_reason"] == "no_neighbors"


def test_generation_refused_also_carries_observation(harness):
    """生成层拒答：检索已完成，观测照带。"""
    harness.generation.refuse_reason = "UNGROUNDED"
    events = _ask(harness)
    payload = events[-1][1]
    assert events[-1][0] == "refused"
    assert payload["reason"] == "UNGROUNDED"
    assert payload["graph_degraded_reason"] == "timeout"
    assert payload["path_stats"]["graph_unique"] == 1


def test_retrieval_exception_refused_has_no_observation(harness):
    """检索没跑完（异常 → VECTOR_STORE_DOWN）MUST NOT 带观测：没有结果就没有观测。"""
    harness.retrieval.raise_on_retrieve = True
    events = _ask(harness)
    assert events[-1][0] == "refused"
    payload = events[-1][1]
    assert payload["reason"] == "VECTOR_STORE_DOWN"
    assert "path_stats" not in payload
    assert "graph_degraded_reason" not in payload


def test_graph_off_still_emits_both_keys(harness):
    """关图态：两个键仍在（值为空 / 空串）—— 缺键会让「没开图」和「老版本」无法区分。"""
    harness.retrieval.result = RetrievalResult(
        chunks=[_chunk()], stage_ms={"retrieving": 1}, path_stats={},
    )
    events = _ask(harness)
    done = events[-1][1]
    assert done["path_stats"] == {}
    assert done["graph_degraded_reason"] == ""


def test_observation_constructed_in_one_place_only():
    """chat.py 里构造这两个键的地方只有一处 —— 手拼第二份必然漂移。"""
    src = inspect.getsource(chat_mod)
    assert src.count('"path_stats"') == 1, "path_stats 被多处构造，载荷会漂移"
    assert src.count('"graph_degraded_reason"') == 1, (
        "graph_degraded_reason 被多处构造，载荷会漂移"
    )
    assert "def _retrieval_observation(" in src
