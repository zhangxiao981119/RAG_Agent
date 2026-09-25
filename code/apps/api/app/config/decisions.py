"""全局口径的代码投影 —— 由《知识库问答 Agent · AI Coding 开发手册》§3 + §8 生成。

修改流程: 手册 §3 → 本文件 → 启动自检
MUST NOT 手动偏离；偏离即为口径违规。

约定：判定逻辑 MUST 通过 `decisions.X` 或本模块级名字读取常量，
MUST NOT 在业务文件里写 `from .decisions import X` 那种把值拷进命名空间的形式 ——
否则单元测试无法用 monkeypatch 锁死口径（见 tests/acl/test_visibility.py::test_10）。
"""
from __future__ import annotations

from enum import Enum
from typing import Final

CONTRACT_VERSION: Final[str] = "v1.1"


# ── 回答边界 ──────────────────────────────────────────────
RELEVANCE_THRESHOLD: Final[float] = 0.35        # L1 闸门；M2 结束前必须标定
GENERATION_TEMPERATURE: Final[float] = 0.1      # L2
GROUNDING_CHECK_ENABLED: Final[bool] = True     # L3；MUST NOT 置 False
TOP_K_RECALL: Final[int] = 50                   # 每路召回条数
TOP_K_RERANK: Final[int] = 8                    # 重排后取用条数
RRF_K: Final[int] = 60
MAX_HISTORY_TURNS: Final[int] = 6               # 短期记忆：最近 6 条历史消息（3 轮问答）
MEMORY_COMPRESS_THRESHOLD: Final[int] = 12      # 历史超过 12 条时触发压缩（条数兜底，超限 token 也会触发）
MEMORY_MAX_FACTS: Final[int] = 20                # 长期记忆：用户画像最多缓存 20 条关键事实
MEMORY_MAX_PROFILE_CHARS: Final[int] = 800      # 长期记忆：用户画像文本上限

# ── Query 改写（RAG 前置增强）─────────────────────────
QUERY_REWRITE_ENABLED: Final[bool] = True
"""检索前对用户问题做意图识别 + 改写（处理口语化/指代/多义词）。
改写后的 query 只用于 retrieval，原始 question 仍用于 generation（标准 HyDE 模式）。"""
QUERY_REWRITE_TIMEOUT_SECONDS: Final[float] = 5.0
"""改写 LLM 调用超时。超时或失败 → 静默回退原始 question，不阻塞主流程。"""

# ── 上下文窗口上限（单次请求 prompt 总 token 数）────────
CONTEXT_WINDOW_LIMIT_TOKENS: Final[int] = 256_000
"""单次请求 context（system + memory + history + chunks + question）token 上限。
超限则递归压缩 history 并裁剪 chunks 直到达标。
★ 值 MUST 与实际 LLM 模型的 context window 对齐（deepseek-chat 实际 128K，
此处 256K 为用户指定上限，若模型窗口更小会在 LLM 调用时报错）。"""
CONTEXT_OUTPUT_RESERVE_TOKENS: Final[int] = 4_000
"""为 LLM 输出预留的 token 数（从 CONTEXT_WINDOW_LIMIT 里扣掉）。"""


# ── 分块 ──────────────────────────────────────────────────
CHUNK_TARGET_TOKENS: Final[int] = 400
CHUNK_MAX_TOKENS: Final[int] = 800
CHUNK_OVERLAP_TOKENS: Final[int] = 60


# ── 密级 ──────────────────────────────────────────────────
class Level(int, Enum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    SECRET = 3


def level_rank(level: Level) -> int:
    """密级 → 下推数值。API 层用 enum，检索下推用数值。"""
    return (level + 1) * 10          # 10 / 20 / 30 / 40


LEVEL_RANK_PUBLIC: Final[int] = 10
LEVEL_RANK_INTERNAL: Final[int] = 20
LEVEL_RANK_CONFIDENTIAL: Final[int] = 30
LEVEL_RANK_SECRET: Final[int] = 40


# ── 可见性口径（DEC-22 · 已冻结）──────────────────────────
class GrantModel(str, Enum):
    KB = "kb"
    TAG = "tag"
    ORG = "org"


VISIBILITY_GRANTS: Final[frozenset[GrantModel]] = frozenset(
    {GrantModel.KB, GrantModel.TAG}
)
"""★ 已冻结。grant_kb 是必要条件（G2），grant_tag 做文档级细化（G4）。
grant_org MUST NOT 进入判定 —— 组织架构只作标签来源。"""

DEPT_VISIBLE_DIRECTION: Final[str] = "up_and_down"
"""上级能看下级。对应 Q3 = 能。"""

USER_SUBJECT_DESCENDANT_EXPANSION: Final[bool] = True
"""A2: 用户主体展开子孙部门。"""

DOC_TAG_ANCESTOR_EXPANSION: Final[bool] = False
"""★ 文档标签 MUST NOT 展开祖先。改成 True 会让全公司互通（手册 §3.2.3）。"""

ACL_PUBLIC_MUTEX: Final[bool] = True
"""public MUST 单独存在，MUST NOT 与其他标签共存。"""

ACL_LEVEL_TAGS_ALLOWED: Final[bool] = False
"""acl_tags MUST NOT 含 level: 标签（会让 G4 绕过 G1）。"""

PUBLIC_KB_ENABLED: Final[bool] = True
PUBLIC_KB_MAX_COUNT: Final[int] = 1
NEW_USER_VISIBLE_SCOPE: Final[str] = "public_kb_only"
SUBJECT_EXPANSION_SOFT_LIMIT: Final[int] = 300
"""用户主体数量软上限；超过时记录告警（提示组织树过深，考虑预计算闭包表）。"""

ACL_CACHE_TTL_SECONDS: Final[int] = 300


# ── 多租户（§8 D-01）──────────────────────────────────────
TENANT_ID_REQUIRED: Final[bool] = True
"""所有业务表 MUST 带 tenant_id 并出现在每个查询条件里。"""

MULTI_TENANT_ENABLED: Final[bool] = False
"""私有化交付 = 单租户（默认 False）；SaaS 场景置 True。
★ 注意：这是"是否开放多租户注册"，MUST NOT 用来跳过 tenant_id 过滤。"""


# ── 上传限制（§8 D-04 / D-16）────────────────────────────
MAX_UPLOAD_MB: Final[int] = 100
ALLOWED_EXT: Final[frozenset[str]] = frozenset({"pdf", "md", "txt", "xls", "xlsx"})
OCR_ENABLED: Final[bool] = False
"""扫描件 OCR 明确不做（§1.4 Non-Goals）。带 OCR 需求的文件 MUST 在解析层显式拒绝并提示用户。"""


# ── 合规与保留（§8 D-15）─────────────────────────────────
AUDIT_RETENTION_DAYS: Final[int] = 365


# ── 脱敏（M5 任务 2）──────────────────────────────────
MASK_PII_ENABLED: Final[bool] = True
"""输出层 PII 脱敏。命中手机号/身份证/银行卡的片段打码。
MUST NOT 对 citations.snippet 打码（原文是原文）。"""


# ── 配额管理（M6 续篇 — 手册第 14 步）──────────────────
QUOTA_SOFT_THRESHOLD: Final[float] = 0.9
"""达到配额 90% 时触发四级降级：按用户画像→历史→压缩→检索片段 顺序裁剪，
绝不丢当前轮的检索片段（手册第 14 步硬性要求）。"""

QUOTA_TOKEN_ESTIMATE_CHARS_PER_TOKEN: Final[int] = 2
"""token 估算系数：len(text) // 2 ≈ token 数（中文保守估计）。"""

QUOTA_FEATURE_KEY: Final[str] = "quota"
"""配额管理的 feature flag key，关闭即跳过配额检查。"""


# ── 敏感词过滤（M6 续篇）──────────────────────────────
SENSITIVE_FILTER_ENABLED: Final[bool] = True
"""输入侧 + 输出侧双向命中检测。命中即拒答（走 refused 事件）。
★ 与 PII 脱敏职责不同：PII 是隐私数据打码（保留语义），敏感词是政策性禁用词（直接拒答）。"""

SENSITIVE_FEATURE_KEY: Final[str] = "sensitive_filter"
"""敏感词过滤的 feature flag key，关闭即放行。"""


def self_check() -> None:
    """应用启动时调用。任何一条不成立 → 抛异常，拒绝启动（fail-closed）。"""
    if not GROUNDING_CHECK_ENABLED:
        raise RuntimeError("GROUNDING_CHECK_ENABLED MUST be True —— L3 出口校验 MUST 开启")
    if DOC_TAG_ANCESTOR_EXPANSION:
        raise RuntimeError(
            "DOC_TAG_ANCESTOR_EXPANSION MUST be False（§3.2.3）—— "
            "文档标签展开祖先将导致全公司互通。"
        )
    if GrantModel.KB not in VISIBILITY_GRANTS:
        raise RuntimeError("VISIBILITY_GRANTS MUST 含 KB（G2 是必要条件）")
    if GrantModel.ORG in VISIBILITY_GRANTS:
        raise RuntimeError("VISIBILITY_GRANTS MUST NOT 含 ORG（组织架构只作标签来源）")
    if not ACL_PUBLIC_MUTEX:
        raise RuntimeError("ACL_PUBLIC_MUTEX MUST be True（§3.2.4）")
    if ACL_LEVEL_TAGS_ALLOWED:
        raise RuntimeError("ACL_LEVEL_TAGS_ALLOWED MUST be False（§3.2.4）")
    if PUBLIC_KB_MAX_COUNT != 1:
        raise RuntimeError("PUBLIC_KB_MAX_COUNT MUST be 1")
    if OCR_ENABLED:
        raise RuntimeError(
            "OCR_ENABLED MUST be False —— 扫描件 OCR 是明确不做项（§1.4 / §8 D-04）。"
            "要打开它，先改手册并重估 M2 排期。"
        )
    if MAX_UPLOAD_MB <= 0:
        raise RuntimeError("MAX_UPLOAD_MB MUST > 0")
    if level_rank(Level.SECRET) != LEVEL_RANK_SECRET:
        raise RuntimeError("level_rank 与 LEVEL_RANK_* 常量不一致")
    if level_rank(Level.PUBLIC) != LEVEL_RANK_PUBLIC:
        raise RuntimeError("level_rank 与 LEVEL_RANK_* 常量不一致")


if __name__ == "__main__":
    self_check()
    print(f"self_check OK · CONTRACT_VERSION={CONTRACT_VERSION}")
