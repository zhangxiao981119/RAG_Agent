"""记忆管理单测（§4.20.5 可查看 / 可删除）。

覆盖三条关键约束：
  1. memory_id **确定性** —— 同一内容必须稳定映射到同一 ID，
     否则用户拿到的删除 ID 刷新一次就失效。
  2. **物理删除** —— 删掉的内容 MUST NOT 残留在返回结构里。
  3. **profile 与 facts 同源重建** —— 只改一个会让读路径与展示路径不一致。
"""
from __future__ import annotations

from app.services import memory as memory_service
from app.services.memory import MemoryItem


def _mem(*items: MemoryItem) -> dict:
    return memory_service.rebuild_memory(list(items))


# ── memory_id 确定性 ────────────────────────────────────
def test_memory_id_is_deterministic():
    a = MemoryItem(content="常用技术中心知识库")
    b = MemoryItem(content="常用技术中心知识库", kind="correction", confidence=0.3)
    assert a.memory_id == b.memory_id, "同一内容 MUST 派生出同一 ID"


def test_memory_id_differs_by_content():
    assert MemoryItem(content="A").memory_id != MemoryItem(content="B").memory_id


def test_memory_id_stable_across_calls():
    item = MemoryItem(content="关注信息安全领域")
    assert item.memory_id == item.memory_id


def test_to_dict_excludes_memory_id():
    """落库结构不含 memory_id —— 它是派生的，存下来只会带来不一致风险。"""
    assert "memory_id" not in MemoryItem(content="x").to_dict()


def test_to_public_dict_includes_memory_id():
    item = MemoryItem(content="x")
    assert item.to_public_dict()["memory_id"] == item.memory_id


# ── 列表 ────────────────────────────────────────────────
def test_list_items_returns_public_shape():
    mem = _mem(MemoryItem(content="常用技术中心知识库", kind="preference"))
    items = memory_service.list_memory_items(mem)
    assert len(items) == 1
    assert set(items[0]) == {"memory_id", "content", "kind", "confidence", "source_trace_id"}


def test_list_items_handles_none_and_empty():
    assert memory_service.list_memory_items(None) == []
    assert memory_service.list_memory_items({}) == []


def test_list_items_reads_legacy_string_facts():
    """★★ 旧格式 `facts: list[str]` 必须能读出来 ——
    不能因为升级格式就丢掉用户已有画像（历史遗留盲区，见记忆层设计说明）。"""
    legacy = {"profile": "用户背景：\n- 常用技术中心知识库", "facts": ["常用技术中心知识库"]}
    items = memory_service.list_memory_items(legacy)
    assert len(items) == 1
    assert items[0]["content"] == "常用技术中心知识库"
    assert items[0]["memory_id"]  # 旧数据无 ID，仍能派生出稳定 ID


# ── 单条删除 ────────────────────────────────────────────
def test_delete_removes_item_physically():
    """★ 物理删除：删掉的 MUST NOT 出现在返回结构里（标记删除等于没删）。"""
    target = MemoryItem(content="待删除项")
    mem = _mem(target, MemoryItem(content="保留项"))

    new_mem, deleted = memory_service.delete_memory_item(mem, target.memory_id)

    assert deleted is True
    assert [i["content"] for i in memory_service.list_memory_items(new_mem)] == ["保留项"]
    assert "待删除项" not in str(new_mem), "内容不应残留（含 profile 字段）"


def test_delete_missing_item_returns_false():
    mem = _mem(MemoryItem(content="保留项"))
    new_mem, deleted = memory_service.delete_memory_item(mem, "not-a-real-id")
    assert deleted is False
    assert new_mem == mem, "未删除时 MUST 原样返回，不应产生副作用"


def test_delete_keeps_profile_in_sync():
    """删完 profile 与 facts 必须一致 —— 否则展示路径会显示已删除的内容。"""
    target = MemoryItem(content="待删除项")
    mem = _mem(target, MemoryItem(content="保留项"))
    new_mem, _ = memory_service.delete_memory_item(mem, target.memory_id)

    assert "待删除项" not in new_mem["profile"]
    assert all("待删除项" not in str(f) for f in new_mem["facts"])
    assert len(new_mem["facts"]) == 1


def test_delete_works_on_legacy_format():
    """旧格式（纯字符串 facts）也能按派生 ID 删除。"""
    legacy = {"profile": "用户背景：\n- 待删除项", "facts": ["待删除项", "保留项"]}
    mid = MemoryItem(content="待删除项").memory_id
    new_mem, deleted = memory_service.delete_memory_item(legacy, mid)
    assert deleted is True
    assert [i["content"] for i in memory_service.list_memory_items(new_mem)] == ["保留项"]


# ── 清空 ────────────────────────────────────────────────
def test_clear_returns_empty_memory():
    mem = _mem(MemoryItem(content="a"), MemoryItem(content="b"))
    cleared = memory_service.clear_memory()
    assert cleared["facts"] == []
    assert cleared["profile"] == ""
    assert mem["facts"], "清空 MUST NOT 就地修改原对象"


# ── rebuild 一致性 ──────────────────────────────────────
def test_rebuild_profile_matches_facts():
    mem = _mem(MemoryItem(content="常用技术中心知识库"), MemoryItem(content="关注信息安全"))
    for item in mem["facts"]:
        assert item["content"] in mem["profile"]


def test_rebuild_truncates_profile():
    many = [MemoryItem(content="偏好" * 30 + str(i)) for i in range(50)]
    mem = memory_service.rebuild_memory(many)
    from app.config import decisions

    assert len(mem["profile"]) <= decisions.MEMORY_MAX_PROFILE_CHARS + 3


def test_rebuild_empty_gives_empty_profile():
    mem = memory_service.rebuild_memory([])
    assert mem == {"profile": "", "facts": []}
