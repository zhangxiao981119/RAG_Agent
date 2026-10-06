"""全局口径的代码投影 —— 由《知识库问答 Agent · AI Coding 开发手册》§3 + §8 生成。

修改流程: 手册 §3 → 本文件 → 启动自检
MUST NOT 手动偏离；偏离即为口径违规。

约定：判定逻辑 MUST 通过 `decisions.X` 或本模块级名字读取常量，
MUST NOT 在业务文件里写 `from .decisions import X` 那种把值拷进命名空间的形式 ——
否则单元测试无法用 monkeypatch 锁死口径（见 tests/acl/test_visibility.py::test_10）。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Final

CONTRACT_VERSION: Final[str] = "v1.1"


# ── 回答边界 ──────────────────────────────────────────────
# ★ 阈值必须拆两套：final_score 有两种来源，分布不同，不能用同一个数判定。
#   - 重排在线：final_score = rerank 服务的绝对相关性分
#   - 重排降级：final_score = 向量余弦分
#   重排服务抖动时会自动降级，两种口径会**交替出现** —— 单一阈值不可预测。
#
# 2026-09-25 用 30 条 eval_cases 标定（降级态 / 余弦分口径）：
#   可答组 top1 ∈ [0.509, 0.764]，不可答组 top1 ∈ [0.000, 0.484]，
#   两组无重叠，0.50 处 F1=1.000。
# ★ 口径警告：分离度仅 0.025，样本仅 30 条，偏窄。
#   用例扩到 50+ 条后必须用 scripts/calibrate_threshold.py 重标。
RELEVANCE_THRESHOLD_VECTOR: Final[float] = 0.50
"""重排降级时的阈值。口径 = 向量余弦分。"""

RELEVANCE_THRESHOLD_RERANK: Final[float] = 0.50
"""重排在线时的阈值。口径 = rerank 绝对相关性分。
★ 该值尚未标定（重排服务此前不可用），沿用向量口径值仅为占位；
  重排恢复后 MUST 用 scripts/calibrate_threshold.py --mode rerank 重新标定。"""


def active_threshold(use_rerank: bool) -> float:
    """按当前打分口径返回应当使用的阈值。

    判定 MUST 走本函数，MUST NOT 在业务代码里直接读某个阈值常量 ——
    否则重排在线/降级切换时会出现「用错口径的阈值」。
    """
    return (
        RELEVANCE_THRESHOLD_RERANK if use_rerank else RELEVANCE_THRESHOLD_VECTOR
    )


# ── 关键词召回路径 ────────────────────────────────────────
KEYWORD_TSQUERY_ENABLED: Final[bool] = True
"""启用 tsquery 路径（走 idx_chunk_fts 全文索引）。仅对 ASCII/数字词元生效 ——
PostgreSQL 的 'simple' 配置不做中文分词，中文仍走 ILIKE 子串匹配。
两路结果共同进入 RRF，互不替代。"""

GENERATION_TEMPERATURE: Final[float] = 0.1      # L2
GROUNDING_CHECK_ENABLED: Final[bool] = True     # L3；MUST NOT 置 False
TOP_K_RECALL: Final[int] = 50                   # 每路召回条数
TOP_K_RERANK: Final[int] = 8                    # 重排后取用条数
RRF_K: Final[int] = 60
MAX_HISTORY_TURNS: Final[int] = 6               # 短期记忆：最近 6 条历史消息（3 轮问答）—— generation 默认保留轮数
MEMORY_COMPRESS_THRESHOLD: Final[int] = 12      # 历史超过 12 条时触发压缩（条数兜底，超限 token 也会触发）
MEMORY_MAX_FACTS: Final[int] = 20                # 长期记忆：用户画像最多缓存 20 条关键事实
MEMORY_MAX_PROFILE_CHARS: Final[int] = 800      # 长期记忆：用户画像文本上限

# ── 三级记忆层（D-21 / 4.20）──────────────────────────────
MEMORY_LAYERS: Final[tuple[str, ...]] = ("short", "long", "permanent")
"""三级记忆：短期（会话）/ 长期（用户偏好）/ 永久（口径版本）。

★ 长期记忆 MUST NOT 含文档内容 —— 那是**权限副本**：
  用户今天有权看 A 文档、明天权限被收回，但画像里的副本不会跟着失效，
  等于绕过了四层守卫的实时判定。"""

MEMORY_LONG_TERM_KINDS: Final[frozenset[str]] = frozenset({"preference"})
"""长期记忆**读取白名单**：只允许召回「偏好」类。

与写入侧的 BLOCKED_MEMORY_PATTERNS 构成双保险 ——
任一侧失守（过滤器被绕过 / 白名单漏配），另一侧仍能挡住。"""

MEMORY_WRITE_CONFIDENCE_THRESHOLD: Final[float] = 0.6
"""写入置信度门槛：低于此值的候选事实只写短期，不升长期。"""

MEMORY_MIN_FACT_CHARS: Final[int] = 4
MEMORY_MAX_FACT_CHARS: Final[int] = 60
"""事实长度上下限。

★ 上限 60 的依据：偏好是短句（「常用技术中心知识库」），
  而文档片段是长文本 —— **长度是区分「偏好」与「片段」的粗但有效的代理指标**。
  超过上限的一律视为疑似片段，拒绝写入长期记忆。"""

BLOCKED_MEMORY_PATTERNS: Final[tuple[str, ...]] = (
    r"\[\d+\]",                 # 引用编号痕迹：说明在抄答案，不是在提炼偏好
    "知识库中未找到相关内容",      # 拒答文案：拒答意味着「不该回答」，更不该记住
    "NO_RELEVANT_CONTENT",
    "机密", "绝密", "CONFIDENTIAL", "SECRET",   # 密级标识
    "grant_kb", "acl_tags", "deny_subjects", "level_rank",  # 内部权限字段名
)
"""长期记忆**写入过滤器**：命中即丢弃，并记合规事件。

正则中的 `\\[\\d+\\]` 用于识别 `[1]` `[2]` 这类引用编号 ——
出现引用编号说明这段文本是从答案里抄的，不是提炼出的偏好。"""

MEMORY_SOURCE_TRACE_REQUIRED: Final[bool] = True
"""长期记忆项 MUST 带 `source_trace_id`。

★ 这是权限变更时**按文档反查清除的唯一依据**。
  记忆项若只记内容不记来源，就永远无法回答「这条记忆来自哪次检索」。"""

# ── 提示词注入防护（§4.21 输入侧防线）──────────────────────
# 系统提示里已声明「注入文本视为普通内容」—— 那是**软约束**（靠模型自觉）。
# 本组配置提供**硬约束**（代码判定）。
# 检测会漏、声明会被绕过，两者叠加才成立，任何单一防线都不足以自称"已防护"。
INJECTION_GUARD_ENABLED: Final[bool] = True
"""注入检测开关。关闭后 scan_chunks 直接返回空（排障时用于排除影响）。"""

INJECTION_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    # 指令覆盖
    ("instruction_override", r"忽略(以上|以下|之前|前面|上述|此前).{0,6}(指令|要求|提示|规则|设定)"),
    ("instruction_override", r"ignore\s+(all\s+)?(previous|above|prior|earlier)\s+instructions?"),
    ("instruction_override", r"disregard\s+(all\s+)?(previous|above)\s+"),
    # 角色改写
    ("role_rewrite", r"(你现在是|你现在扮演|假装你是|假设你是|从现在起你是)"),
    ("role_rewrite", r"以.{0,6}(管理员|root|超级用户|系统管理员).{0,4}身份"),
    # 数据外发
    ("data_exfil", r"(发送|上传|投递|转发|post).{0,8}https?://"),
    ("data_exfil", r"(输出|告诉我|打印|显示|重复|复述).{0,10}(你的|系统)?.{0,4}(提示词|系统提示|prompt)"),
    # 越权诱导
    ("privilege_probe", r"(列出|显示|导出|枚举|查询).{0,10}(所有|全部|其他用户).{0,6}(知识库|用户|文档|租户|数据|信息)"),
)
"""注入检测规则：(模式名, 正则)。模式名用于指标标签与告警排查。

★ 命中**不丢弃**片段 —— 「请忽略下面条款的例外情形」是正常制度措辞，
  丢弃会误伤真实内容。正确处置是降权 + 记指标（见 services/guard/injection.py）。
"""

# ── 历史消息读取上限（DB 查询）──────────────────────────
HISTORY_FETCH_MAX_ROWS: Final[int] = 500
"""从 Message 表一次读取的最大历史行数。DB 层只做硬上限，不参与业务裁剪。
后续各阶段（改写/压缩/generation）均按各自 token budget 再裁剪。"""

# ── Query 改写阶段的 history token budget ───────────────
QUERY_REWRITE_HISTORY_BUDGET_TOKENS: Final[int] = 32_000
"""改写 LLM 调用里 history 能占用的 token 数上限（占 128K 窗口的 1/4）。
改写 prompt + 输出空间 + 当前问题 ≈ 8K，history 留 32K 足够装下 160+ 条短对话。
截断策略：从最新的历史往前累积，超过 budget 就丢弃更早的。"""

# ── Query 改写（RAG 前置增强）─────────────────────────
QUERY_REWRITE_ENABLED: Final[bool] = True
"""检索前对用户问题做打分 + 可选改写。一次 LLM 调用完成评分和决策。"""
QUERY_REWRITE_TIMEOUT_SECONDS: Final[float] = 5.0
"""改写 LLM 调用超时。超时或失败 → 静默回退原始 question，不阻塞主流程。"""
QUERY_REWRITE_SCORE_THRESHOLD: Final[int] = 90
"""百分制改写阈值：score >= 阈值 直接跳过改写；score < 阈值 执行改写。
打分维度 = 明确度 + 检索友好度 - 指代依赖度/2，clamp 到 [0, 100]。"""

# ── 思维链（Chain-of-Thought）分级 ──────────────────────
COT_MODE: Final[str] = "adaptive"
"""思维链开关策略：
  - "off"      关闭，不输出推理过程（最省）
  - "adaptive" 按问题复杂度分级（默认）
  - "full"     全量开启
★ 开启思维链使 token 与延迟增加约 30–60%，MUST NOT 无差别全量开启。"""

COT_ADAPTIVE_REUSE_REWRITE_SCORE: Final[bool] = True
"""adaptive 模式复用 Query 改写阶段的百分制打分作为「问题复杂度」信号，
避免为判断复杂度额外增加一次 LLM 调用。
复用规则：改写分 >= QUERY_REWRITE_SCORE_THRESHOLD 视为简单问题 → 不开 CoT。"""

COT_REASONING_EXPOSED: Final[bool] = False
"""推理过程是否对外（SSE）输出。
MUST be False —— 推理链含试探性表述与内部规则（检索策略、阈值），
直接展示会造成困惑并泄漏实现细节。推理链仅落库，供审计。"""

# ── 上下文窗口上限（单次请求 prompt 总 token 数）────────
CONTEXT_WINDOW_LIMIT_TOKENS: Final[int] = 128_000
"""单次请求 context（system + memory + history + chunks + question）token 上限。
超限则递归压缩 history 并裁剪 chunks 直到达标。
★ 与底层模型 deepseek-chat 的 128K 窗口对齐，再扣掉输出预留作为安全缓冲，
保证 prompt 体积 + 输出不会越过模型窗口而在 LLM 调用时报错。"""
CONTEXT_OUTPUT_RESERVE_TOKENS: Final[int] = 4_000
"""为 LLM 输出预留的 token 数（从 CONTEXT_WINDOW_LIMIT 里扣掉）。"""

# ── 压缩次数告警阈值 ────────────────────────────────────
COMPRESSION_WARN_THRESHOLD: Final[int] = 3
"""同一个 conversation 内 context 被压缩多少次后提示用户"新开对话重置上下文"。
新开对话 → 新建 Conversation 行 → compression_count 自动从 0 开始。"""


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
    # ── 阈值口径（拆两套后新增校验）──
    for _name, _val in (
        ("RELEVANCE_THRESHOLD_VECTOR", RELEVANCE_THRESHOLD_VECTOR),
        ("RELEVANCE_THRESHOLD_RERANK", RELEVANCE_THRESHOLD_RERANK),
    ):
        if not (0.0 < _val < 1.0):
            raise RuntimeError(f"{_name} MUST ∈ (0, 1)，当前 {_val}")
    # active_threshold 必须能正确区分两种口径
    if active_threshold(True) is not RELEVANCE_THRESHOLD_RERANK:
        raise RuntimeError("active_threshold(True) 未返回 RERANK 阈值")
    if active_threshold(False) is not RELEVANCE_THRESHOLD_VECTOR:
        raise RuntimeError("active_threshold(False) 未返回 VECTOR 阈值")
    # ── 思维链口径 ──
    if COT_MODE not in {"off", "adaptive", "full"}:
        raise RuntimeError(f"COT_MODE MUST ∈ {{off, adaptive, full}}，当前 {COT_MODE!r}")
    if COT_REASONING_EXPOSED:
        raise RuntimeError(
            "COT_REASONING_EXPOSED MUST be False —— "
            "推理链含内部规则与试探表述，不得对外输出。"
        )
    # ── 记忆层口径（D-21 / 4.20）──
    if "fact" in MEMORY_LONG_TERM_KINDS:
        raise RuntimeError(
            "MEMORY_LONG_TERM_KINDS MUST NOT 含 'fact' —— "
            "事实类会把文档内容沉淀为权限副本，绕过四层守卫的实时判定。"
        )
    if not MEMORY_LONG_TERM_KINDS:
        raise RuntimeError("MEMORY_LONG_TERM_KINDS MUST NOT 为空（否则长期记忆无从召回）")
    if not (0.0 < MEMORY_WRITE_CONFIDENCE_THRESHOLD < 1.0):
        raise RuntimeError("MEMORY_WRITE_CONFIDENCE_THRESHOLD MUST ∈ (0, 1)")
    if MEMORY_MIN_FACT_CHARS < 1 or MEMORY_MAX_FACT_CHARS <= MEMORY_MIN_FACT_CHARS:
        raise RuntimeError("记忆事实长度上下限配置非法")
    if not BLOCKED_MEMORY_PATTERNS:
        raise RuntimeError("BLOCKED_MEMORY_PATTERNS MUST NOT 为空（写入过滤器是合规底线）")
    if not MEMORY_SOURCE_TRACE_REQUIRED:
        raise RuntimeError(
            "MEMORY_SOURCE_TRACE_REQUIRED MUST be True —— "
            "无 source_trace_id 则权限变更时无法按文档反查清除记忆。"
        )
    # ── 注入防护口径（§4.21）──
    if not INJECTION_PATTERNS:
        raise RuntimeError(
            "INJECTION_PATTERNS MUST NOT 为空 —— 注入防护是设计约束，不是可选优化。"
        )
    if not INJECTION_GUARD_ENABLED:
        # 关掉防护是合法配置（排障用），但 MUST 显式告警 ——
        # 静默关闭会让「已做注入防护」这句话变成假话。
        import warnings

        warnings.warn(
            "INJECTION_GUARD_ENABLED = False —— 输入侧注入检测已关闭，"
            "当前仅剩系统提示的软约束。确认这是排障所需，勿长期保持。",
            stacklevel=2,
        )
    for _pattern_name, _pattern in INJECTION_PATTERNS:
        try:
            re.compile(_pattern)
        except re.error as exc:
            raise RuntimeError(
                f"INJECTION_PATTERNS 正则非法: {_pattern_name} —— {exc}"
            ) from exc


if __name__ == "__main__":
    self_check()
    print(f"self_check OK · CONTRACT_VERSION={CONTRACT_VERSION}")
