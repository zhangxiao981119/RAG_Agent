"""wiki 生成服务（services/wiki/build.py）的单测 —— LLM 一律打桩。

钉住五件事（设计文档 §9.4 幂等 + §9.6 ③ 的产物契约）：
  1. content_hash = sha1(输入文本 + 模型 + prompt 版本)[:12]：文本/模型/版本任一
     变化 → hash 变化（换模型后旧页必须被判为待重建，而不是被复用）；
  2. 幂等判定：已有页 (ready, 同 hash, 同口径) → 跳过；status='failed' 的行
     **不可**当幂等命中（否则失败的页永远不被重试）；
  3. 输入截断是**整片粒度**（切半个片段会让模型引用看不到结尾的内容）；
  4. 归一在代码侧：编造的 chunk_id 丢弃、无合法证据的小节整节丢弃、
     全页 key_terms sorted 去重、sections 超 WIKI_MAX_SECTIONS 截断；
  5. 两次输出都非法 → WikiBuildError（job 层负责重试与留痕）。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.wiki import build as build_module
from app.services.wiki.build import (
    WikiBuildError,
    WikiInput,
    _normalize_sections,
    _truncate_chunks,
    compute_content_hash,
    generate_page_content,
)

TENANT = uuid.uuid4()
KB = uuid.uuid4()
DOC = uuid.uuid4()


def _chunk(n: int, content: str = "正文" * 50) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.UUID(int=n), heading_path=f"第{n}章", content=content)


# ── content_hash（§9.4）─────────────────────────────────────────


def test_content_hash_stable_and_caliber_sensitive():
    text = "输入文本"
    h1 = compute_content_hash(text, "m1", "wprompt-v1")
    assert h1 == compute_content_hash(text, "m1", "wprompt-v1")
    assert len(h1) == 12
    # 换模型 / bump prompt → hash 必须变（口径变化 = 旧页待重建）
    assert compute_content_hash(text, "m2", "wprompt-v1") != h1
    assert compute_content_hash(text, "m1", "wprompt-v2") != h1
    assert compute_content_hash(text + "!", "m1", "wprompt-v1") != h1


# ── 输入截断（整片粒度）─────────────────────────────────────────


def test_truncate_keeps_whole_chunks_only():
    chunks = [_chunk(i, content="x" * 100) for i in range(10)]
    kept = _truncate_chunks(chunks, max_chars=350)
    assert 1 <= len(kept) < 10
    # 整片粒度：预算内能放下的片段必须完整保留（不存在被切的半片）
    assert all(len(c.content) == 100 for c in kept)
    # 每多放一片都要算总量（不超预算太多）
    assert len(kept) >= 2


def test_truncate_always_keeps_at_least_one_chunk():
    """单个片段超预算也必须保留一片 —— 空输入只会得到 LLM 编造。"""
    chunks = [_chunk(0, content="y" * 1000)]
    kept = _truncate_chunks(chunks, max_chars=10)
    assert len(kept) == 1


# ── 幂等判定（打桩 session 走真实 prepare_wiki_input，不复制判定式）──


class _ExecuteResult:
    def __init__(self, first=None, rows=None) -> None:
        self._first = first
        self._rows = list(rows or [])

    def first(self):
        return self._first

    def all(self):
        return self._rows


class _InputSession:
    """按序出栈：get(Document) → execute(页行) → execute(chunk 行)。"""

    def __init__(self, page_row) -> None:
        self._page_row = page_row

    async def get(self, model, pk):
        return SimpleNamespace(id=pk)  # 文档存在即可

    async def execute(self, stmt, params=None):
        if "wiki_pages" in str(stmt):
            return _ExecuteResult(first=self._page_row)
        return _ExecuteResult(rows=[SimpleNamespace(id=uuid.UUID(int=n),
                                                    heading_path=f"第{n}章",
                                                    content="正文" * 20)
                                     for n in range(3)])


@pytest.fixture()
def _stub_profile(monkeypatch):
    monkeypatch.setattr(
        build_module, "current_wiki_profile",
        lambda: SimpleNamespace(generator_model="gen", prompt_version="wprompt-v1"),
    )
    monkeypatch.setattr(
        build_module, "get_settings",
        lambda: SimpleNamespace(wiki_input_max_chars=10**9),
    )


def _prepare(page_row):
    import asyncio

    _, idempotent = asyncio.run(
        build_module.prepare_wiki_input(_InputSession(page_row), document_id=DOC)
    )
    return idempotent


def test_idempotent_semantics(_stub_profile):
    """ready + 同 hash + 同口径 才命中；failed/stale 绝不命中（走真实函数）。"""
    # 第一遍（无已有页）拿真实算出的 content_hash，第二遍用它构造"已存在页"
    import asyncio

    result = asyncio.run(
        build_module.prepare_wiki_input(_InputSession(None), document_id=DOC)
    )
    real_hash = result[0].content_hash  # result = (WikiInput, idempotent)

    def _row(status, hash_, model="gen", pv="wprompt-v1"):
        return SimpleNamespace(status=status, generator_model=model,
                               prompt_version=pv, content_hash=hash_)

    assert _prepare(_row("ready", real_hash)) is True
    # failed 行不可当幂等命中 —— 否则失败的页永远不被重试
    assert _prepare(_row("failed", real_hash)) is False
    # stale 行同理
    assert _prepare(_row("stale", real_hash)) is False
    # 口径不同 / 内容不同都不命中
    assert _prepare(_row("ready", real_hash, model="gen2")) is False
    assert _prepare(_row("ready", real_hash, pv="wprompt-v2")) is False
    assert _prepare(_row("ready", "b" * 12)) is False
    assert _prepare(None) is False


# ── 小节归一（归一在代码侧，不信任模型）─────────────────────────


def test_normalize_sections_drops_fabricated_chunk_ids():
    valid = {uuid.UUID(int=1), uuid.UUID(int=2)}
    raw = [
        {"heading_path": "第一章", "summary": "s1",
         "key_terms": ["术语"], "chunk_ids": [str(uuid.UUID(int=1))]},
        {"heading_path": "第二章", "summary": "s2",
         "key_terms": [], "chunk_ids": [str(uuid.UUID(int=999))]},  # 编造 id
        {"heading_path": "第三章", "summary": "s3",
         "key_terms": [], "chunk_ids": []},  # 无合法证据 → 整节丢弃
        {"heading_path": "", "summary": "s4", "key_terms": [],
         "chunk_ids": [str(uuid.UUID(int=2))]},  # 空 heading → 丢弃
    ]
    sections, used = _normalize_sections(raw, valid)
    assert len(sections) == 1
    assert sections[0]["chunk_ids"] == [uuid.UUID(int=1)]
    assert used == [uuid.UUID(int=1)]


def test_normalize_sections_caps_and_dedups_terms():
    valid = {uuid.UUID(int=1)}
    terms = [f"术语{i:02d}" for i in range(20)]
    raw = [{
        "heading_path": "第一章", "summary": "s",
        "key_terms": ["术语00", "术语01", "术语00", "很长" * 30] + terms,
        "chunk_ids": [str(uuid.UUID(int=1))],
    }]
    sections, _ = _normalize_sections(raw, valid)
    section = sections[0]
    assert len(section["key_terms"]) <= 12          # 单节术语上限
    assert len(set(section["key_terms"])) == len(section["key_terms"])  # 去重
    assert all(len(t) <= 40 for t in section["key_terms"])              # 超长截除


def test_normalize_sections_caps_section_count():
    from app.services.wiki.prompts import WIKI_MAX_SECTIONS

    valid = {uuid.UUID(int=1)}
    raw = [
        {"heading_path": f"第{i}章", "summary": f"s{i}",
         "key_terms": [], "chunk_ids": [str(uuid.UUID(int=1))]}
        for i in range(WIKI_MAX_SECTIONS + 5)
    ]
    sections, _ = _normalize_sections(raw, valid)
    assert len(sections) == WIKI_MAX_SECTIONS


# ── LLM 调用与产物（LLM 打桩）──────────────────────────────────


class _FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, temperature, *, model=None, timeout_seconds=None,
                   max_tokens=None):
        self.calls += 1
        return self.responses.pop(0)


_VALID_JSON = """
{"title": "差旅报销制度",
 "summary": "说明差旅报销的范围、标准与流程。适用全体员工。",
 "sections": [
   {"heading_path": "第一章 总则", "summary": "定义适用范围。",
    "key_terms": ["差旅报销", "适用范围"], "chunk_ids": ["%s"]},
   {"heading_path": "第二章 标准", "summary": "住宿与交通标准。",
    "key_terms": ["住宿标准", "差旅报销"], "chunk_ids": ["%s", "%s"]}
 ]}
""" % (uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3))


@pytest.fixture
def wiki_env(monkeypatch):
    get_settings = build_module.get_settings
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_generate_page_content_normalizes(wiki_env, monkeypatch):
    fake = _FakeLLM([f"```json{_VALID_JSON}```"])
    monkeypatch.setattr(build_module, "get_llm_service", lambda: fake)

    wiki_input = WikiInput(
        text="prompt", content_hash="a" * 12,
        chunks=[_chunk(1), _chunk(2), _chunk(3)],
    )
    page = await generate_page_content(wiki_input)

    assert fake.calls == 1
    assert page.title == "差旅报销制度"
    assert len(page.sections) == 2
    # 编造的 id（int=999 不在输入里）被丢弃
    assert all(cid in {uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)}
               for s in page.sections for cid in s["chunk_ids"])
    # 全页术语 sorted 去重
    assert page.key_terms == sorted(set(page.key_terms))
    assert "差旅报销" in page.key_terms
    assert page.source_chunk_ids == [uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)]


@pytest.mark.asyncio
async def test_generate_page_content_retries_then_raises(wiki_env, monkeypatch):
    fake = _FakeLLM(["完全不是 JSON", "也不是 JSON"])
    monkeypatch.setattr(build_module, "get_llm_service", lambda: fake)

    wiki_input = WikiInput(text="prompt", content_hash="a" * 12, chunks=[_chunk(1)])
    with pytest.raises(WikiBuildError):
        await generate_page_content(wiki_input)
    assert fake.calls == 2, "非法输出应原地重试一次"


@pytest.mark.asyncio
async def test_generate_page_content_rejects_missing_title(wiki_env, monkeypatch):
    bad = '{"summary": "只有摘要", "sections": []}'
    fake = _FakeLLM([bad, bad])
    monkeypatch.setattr(build_module, "get_llm_service", lambda: fake)

    with pytest.raises(WikiBuildError):
        await generate_page_content(
            WikiInput(text="p", content_hash="a" * 12, chunks=[_chunk(1)])
        )


@pytest.mark.asyncio
async def test_generate_llm_error_becomes_wiki_build_error(wiki_env, monkeypatch):
    """LLM 层失败（超时/限流/推理模型空响应）统一转 WikiBuildError：

    调用方 build_and_store 按「生成失败」留痕（record_failed_page）+ 重试，
    不该看到 LLM 客户端的异常面 —— 若 LLMError 穿透，失败行永远不落库
    （真库联调实证：推理模型空响应 → FAIL 但 wiki_pages 没有留痕）。
    """
    from app.services.llm import LLMError

    class _FailingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, temperature, *, model=None,
                       timeout_seconds=None, max_tokens=None):
            self.calls += 1
            raise LLMError("LLM 只产出了推理内容，content 为空")

    fake = _FailingLLM()
    monkeypatch.setattr(build_module, "get_llm_service", lambda: fake)

    with pytest.raises(WikiBuildError, match="LLM 调用两次失败"):
        await generate_page_content(
            WikiInput(text="p", content_hash="a" * 12, chunks=[_chunk(1)])
        )
    assert fake.calls == 2, "LLM 层失败也应原地重试一次"


@pytest.mark.asyncio
async def test_generate_llm_error_recovered_on_second_attempt(wiki_env, monkeypatch):
    """第一次 LLM 失败、第二次成功 → 正常产页（瞬时故障被原地重试吸收）。"""
    from app.services.llm import LLMError

    class _FlakyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, temperature, *, model=None,
                       timeout_seconds=None, max_tokens=None):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("timeout")
            return f"```json{_VALID_JSON}```"

    monkeypatch.setattr(build_module, "get_llm_service", lambda: _FlakyLLM())

    generated = await generate_page_content(
        WikiInput(text="p", content_hash="a" * 12, chunks=[_chunk(1)])
    )
    assert generated.title
