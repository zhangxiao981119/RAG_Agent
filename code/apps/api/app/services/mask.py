"""PII 脱敏 —— 输出层对最终回答文本打码。

位置：generate/__init__.py 返回 GenerationResult 前调用
范围：只处理 assistant 的最终回答文本，citations.snippet MUST NOT 打码
规则：
  - 手机号（11 位，1 开头）  → 138****5678
  - 身份证（18 位含 X 结尾）  → 510123********1234
  - 身份证（15 位）          → 510123******123
  - 银行卡（16 或 19 位）    → 622202****7890

★ 按长度从长到短匹配，避免短规则先吃掉长串（正则 | 按左到右贪心）。
"""
from __future__ import annotations

import re

# —— 脱敏规则：replacement 是函数，接收 Match 返回打码字符串 ——

def _mask_phone(m: re.Match) -> str:
    s = m.group(0)
    # 13812345678 → 138****5678
    return s[:3] + "****" + s[-4:]


def _mask_id18(m: re.Match) -> str:
    s = m.group(0)
    # 510123199001011234 → 510123********1234
    return s[:6] + "*" * 8 + s[-4:]


def _mask_id15(m: re.Match) -> str:
    s = m.group(0)
    # 510123900101123 → 510123******123
    return s[:6] + "*" * 6 + s[-3:]


def _mask_bank(m: re.Match) -> str:
    s = m.group(0)
    # 6222021234567890 → 622202****7890  (16 位)
    # 6222021234567890123 → 622202********1234 (19 位)
    if len(s) == 19:
        return s[:6] + "*" * 8 + s[-4:]
    return s[:6] + "****" + s[-4:]


# 顺序：长 → 短，避免 18 位被 16 位规则先吃掉（正则 | 左到右优先级）
_PII_RULES: list[tuple[re.Pattern, callable]] = [  # type: ignore[name-defined]
    # 身份证 18 位（含 X 结尾）
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), _mask_id18),
    # 身份证 15 位
    (re.compile(r"(?<!\d)\d{15}(?!\d)"), _mask_id15),
    # 银行卡 19 位
    (re.compile(r"(?<!\d)\d{19}(?!\d)"), _mask_bank),
    # 银行卡 16 位
    (re.compile(r"(?<!\d)\d{16}(?!\d)"), _mask_bank),
    # 手机号 11 位（1 开头，第二位 3-9）
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), _mask_phone),
]


def mask_pii(text: str) -> str:
    """对文本中的 PII 打码。返回打码后的新字符串。"""
    if not text:
        return text
    result = text
    for pattern, replacer in _PII_RULES:
        result = pattern.sub(replacer, result)
    return result
