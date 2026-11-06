"""F6 回归：达上限 × 传别名的交叉用例（C4 报告 §3.7 的两条机器锚点）。

⚠ 这两条在**修复前是红断言**（`base == extended`，别名被静默吞掉），
  MUST 与 F6 独立预算修复同时落地、不得单独进主分支 —— 否则直接破坏 CI。
  本次随 N6 裁决与第 4 步段 A 同批合入：先存 Golden 快照 → 补这两条断言 →
  改 `build_fts_query` 的 `:145/:146` 两行 → 一起转绿，形成完整证据链。

★ 交叉盲区（C4 §1.4 实测）：`test_tsquery.py:117/124` 测了「词元/窗口达 24」
  但都不传别名；`test_terms.py:135/139` 传了别名但 `windows=1`。
  「达上限 × 传别名」是 F6 静默缺陷唯一能被证伪的机器证据。

★ 修复口径：别名走**独立预算**（`LEXICON_FTS_ALIAS_MAX_TERMS`），
  不再把 `windows + 别名` 重截回 `FTS_QUERY_MAX_TERMS`；原 query 窗口一个不少。
"""
from app.services.keyword_text import build_fts_query


def test_fts_query_keeps_cjk_alias_when_windows_hit_cap():
    """中文侧：26 个互异汉字 → 恰好 24 个 3 字窗口（§2.4 实测阈值 L=26→W=24）。

    :145 把 windows+别名重截回 24 时，_dedupe 保序使原窗口在前，别名被全部吞掉。
    """
    q = "".join(chr(0x4E00 + i) for i in range(26))
    base = build_fts_query(q)                # 不带别名
    extended = build_fts_query(q, ["访问"])  # 带别名
    assert base is not None and extended is not None
    assert extended != base, "别名被静默吞掉：带/不带别名输出逐字节相同"
    assert "访问" in extended, "别名 '访问' 未进入 tsquery"
    # 独立预算契约：原 query 窗口一个不少
    for win in base.strip("()").split(" | "):
        assert win in extended, f"原窗口 {win} 被挤掉"


def test_fts_query_keeps_ascii_alias_when_ascii_items_hit_cap():
    """ASCII 侧：40 个词元 → :138 截到 24；:146 同型重截吞掉 ascii 别名。"""
    q = " ".join(f"token{i}" for i in range(40))
    base = build_fts_query(q)
    extended = build_fts_query(q, ["accesstoken"])
    assert base is not None and extended is not None
    assert extended != base, "ASCII 别名被静默吞掉：带/不带别名输出逐字节相同"
    assert "accesstoken" in extended, "别名 'accesstoken' 未进入 tsquery"
    for item in base.strip("()").split(" | "):
        assert item in extended, f"原 ASCII 词元 {item} 被挤掉"
