"""hit_snippet（引用片段按命中位置开窗）测试。

钉住四件事（对齐任务规格）：
  1. 命中在片中部：snippet 包含答案词，且窗口从命中点附近开始（不是从片头）；
  2. 未命中：与 `content[:200]` **逐字节一致**（回退 = 现行为，基线不漂移）；
  3. 中文 query（「上班时间」）命中中文 content；
  4. content ≤ width：原样返回（开窗对短片段没有意义）。
另加三条契约：总长 ≤ width、左对齐句号边界、这是纯展示函数（不在检索路径）。
"""
from __future__ import annotations

from app.services.keyword_text import hit_snippet

# 200+ 字的长 chunk：前 200 字是「报销流程概述」，证据在中段
_FILLER = "报销流程概述。" * 40          # 7 字 × 40 = 280 字，覆盖前 200 字窗口
_LONG_CHUNK = _FILLER + "员工上班时间为 9:00-12:00，午休 1 小时。" + "附则。" * 30


def test_hit_in_middle_starts_near_match():
    snippet = hit_snippet(_LONG_CHUNK, "公司几点上班")
    assert "上班时间" in snippet, "窗口必须挪到证据附近，而不是停在片头"
    assert snippet.startswith("…"), "命中点在片中部，开头应有省略号"
    # 命中点在 280 字之后：旧的 content[:200] 根本看不到证据
    assert "上班时间" not in _LONG_CHUNK[:200]
    assert hit_snippet(_LONG_CHUNK, "公司几点上班") != _LONG_CHUNK[:200]


def test_no_hit_falls_back_byte_identical():
    content = "差旅费报销需要提供行程单与发票。" * 20
    assert hit_snippet(content, "完全无关的查询词") == content[:200]
    assert hit_snippet(content, "完全无关的查询词") == content[:200]  # 逐字节一致


def test_chinese_query_hits_chinese_content():
    snippet = hit_snippet(_LONG_CHUNK, "上班时间")
    assert "上班时间为 9:00-12:00" in snippet


def test_short_content_returned_verbatim():
    content = "上班时间为 9:00-12:00。"
    assert hit_snippet(content, "几点上班") == content
    # 未命中且 content 短：同样原样（content[:200] == content）
    assert hit_snippet(content, "无关词") == content


def test_output_never_exceeds_width():
    snippet = hit_snippet(_LONG_CHUNK, "公司几点上班")
    assert len(snippet) <= 200, "总长（含省略号）MUST ≤ width，与回退行为同界"


def test_window_aligns_to_sentence_boundary():
    # 命中点前 30 字回看区内有一个句号 → 窗口应从句号后（句子开头）起，
    # 而不是把上一句的尾巴切一半。
    # 两个填充块用不同词，才能分辨「对齐后从哪块开始」。
    content = "第一句话。" + "补充" * 110 + "长期铺垫。" + "铺垫" * 10 + "上班时间为 9:00。" + "尾" * 30
    snippet = hit_snippet(content, "几点上班")
    assert "上班时间为 9:00" in snippet
    # 回看区内的句号在「长期铺垫。」之后 → 对齐后窗口从「铺垫」块开始；
    # 不对齐时起点落在「补充」块中部（max(0, 命中-30)）
    assert snippet.startswith("…铺垫"), f"窗口没有对齐到句号边界: {snippet[:12]}"


def test_width_override_respected():
    snippet = hit_snippet(_LONG_CHUNK, "公司几点上班", width=50)
    assert len(snippet) <= 50
    assert "上班时间" in snippet


def test_pure_display_not_in_retrieval_path():
    """纯展示函数：不许 import 任何检索/DB 模块（保持叶子模块）。"""
    import app.services.keyword_text as m

    assert not hasattr(m, "RetrievalService")
    assert not hasattr(m, "SessionLocal")
