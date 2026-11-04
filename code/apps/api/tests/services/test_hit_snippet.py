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


def test_width_override_respected():
    snippet = hit_snippet(_LONG_CHUNK, "公司几点上班", width=50)
    assert len(snippet) <= 50
    assert "上班时间" in snippet


# ── P2-1：width 边界 ────────────────────────────────────────────


def test_tiny_width_clamped_and_never_exceeds():
    """width=1 旧实现返回 '……'（len=2 > width）：钳到 4 后不再产生负数切片。"""
    snippet = hit_snippet(_LONG_CHUNK, "公司几点上班", width=1)
    assert len(snippet) == 4  # 左省略号 + 2 字符 + 右省略号
    assert snippet.startswith("…")


def test_nonpositive_width_falls_back():
    """width 非正直接走回退（content[:负数] 是从尾部去字符的错误切片，必须挡住）。"""
    assert hit_snippet(_LONG_CHUNK, "公司几点上班", width=0) == ""
    assert hit_snippet(_LONG_CHUNK, "公司几点上班", width=-5) == ""


# ── P1：型号词元探测（与检索路 ILIKE 同源同语义）────────────────


_MODEL_CHUNK = "产品说明书。" * 60 + "AX3000 路由器支持 Mesh 组网。" + "附则。" * 30


def test_model_code_hit_in_middle():
    """型号在 300 字处：窗口必须落在型号附近，而不是回退到片头。"""
    assert "AX3000" not in _MODEL_CHUNK[:200]  # 旧口径根本看不到型号
    snippet = hit_snippet(_MODEL_CHUNK, "AX3000")
    assert snippet != _MODEL_CHUNK[:200], "型号词元探测失败，整体回退了旧行为"
    assert snippet.startswith("…")
    assert "AX3000" in snippet


def test_model_code_spelling_variants_all_probe():
    """ax-3000 / ax_3000 / ax 3000 三种写法都要能探测到 AX3000
    —— 检索路 ILIKE 容错正则能命中，开窗探测必须同样能命中（P1 缺口的判据）。"""
    for q in ("ax-3000", "ax_3000", "ax 3000"):
        snippet = hit_snippet(_MODEL_CHUNK, q)
        assert "AX3000" in snippet, f"query={q!r} 探测不到型号"


def test_model_code_lowercase_content_case_insensitive():
    """content 里是小写、query 里是大写（或反之）也要命中 —— IGNORECASE 契约。"""
    chunk = "产品说明书。" * 60 + "ax3000 路由器。" + "附则。" * 30
    assert "ax3000" in hit_snippet(chunk, "AX3000")


# ── P2-5：多词元长词元优先（逐词元 search，不做 OR 最左）──────────


def test_multi_term_prefers_longer_term_not_leftmost():
    """query「上班时间 报销」：短词元「报销」在片头 pos 0 先出现，
    窗口仍必须锚到长词元「上班时间」附近的证据，而不是静默退回片头
    —— 若把词元 OR 成一条正则，re.search 取最左命中就会命中这里。"""
    content = "报销流程。" * 60 + "上班时间为 9:00。"
    assert "报销" in content[:200] and "上班时间" not in content[:200]
    snippet = hit_snippet(content, "上班时间 报销")
    assert snippet.startswith("…上班时间"), f"窗口锚到了片头: {snippet[:16]}"


def test_multi_term_longer_term_later_in_content_wins():
    """反向用例：两词元都在片中、长词元位置靠后 —— 仍选长词元的位置
    （优先序按词元长度，不按出现位置先后）。"""
    content = "报销说明。" * 30 + "补充" * 100 + "上班时间为 9:00。" + "尾" * 30
    snippet = hit_snippet(content, "上班时间 报销")
    assert "上班时间为 9:00" in snippet, "长词元靠后时窗口没有锚到长词元"


# ── P2-4：回看与对齐两个契约分开各钉一条 ────────────────────────


def test_lookback_window_starts_exactly_30_before_hit():
    """回看契约：命中点前 30 字内**无**换行/句号边界时，
    窗口起点必须恰为 max(0, 命中点 - 30)（不被对齐挪动）。"""
    content = "片" * 300 + "上班时间为 9:00。" + "尾" * 50
    hit = content.find("上班")
    assert hit == 300
    snippet = hit_snippet(content, "几点上班")
    # 无边界 → start = 300 - 30 = 270，开头省略号后紧跟 content[270:]
    assert snippet == "…" + content[270:]


def test_alignment_snaps_to_sentence_boundary():
    """对齐契约：回看区内有句号时，窗口必须从句号后（句子开头）起，
    而不是把上一句的尾巴切一半（与回看契约分开钉，删一条不失另一条）。"""
    # 两个填充块用不同词，才能分辨「对齐后从哪块开始」
    content = "第一句话。" + "补充" * 110 + "长期铺垫。" + "铺垫" * 10 + "上班时间为 9:00。" + "尾" * 30
    snippet = hit_snippet(content, "几点上班")
    assert "上班时间为 9:00" in snippet
    # 回看区内的句号在「长期铺垫。」之后 → 对齐后窗口从「铺垫」块开始；
    # 不对齐时起点落在「补充」块中部（max(0, 命中-30)）
    assert snippet.startswith("…铺垫"), f"窗口没有对齐到句号边界: {snippet[:12]}"


def test_pure_display_not_in_retrieval_path():
    """纯展示函数：不许 import 任何检索/DB 模块（保持叶子模块）。"""
    import app.services.keyword_text as m

    assert not hasattr(m, "RetrievalService")
    assert not hasattr(m, "SessionLocal")
