"""运行时配置口径 —— 记录「这次运行是按什么部署配置跑的」（项3：运行时配置版本）。

与另两份口径互补，三者各管一段：
  · index_profile   管「向量怎么算」—— 决定向量是否同构
  · config_profile  管「运行参数」—— 阈值 / 超时 / 批量 / 预算等**部署可变项**
  · caliber         把上面两者 + 模型 / 提示词版本聚合成每条答案的审计快照

契约级口径（decisions 里的冻结常量）由 decisions.CONTRACT_VERSION 单独表达，
本模块只覆盖可由环境变量覆盖的运行时配置，避免两处重复维护。

★ 为什么必须脱敏：
  快照会写进启动日志与每条消息的 caliber，里面有 LLM_BASE_URL 这类可能带账号密码的串。
  指纹的目的是「可比对 / 可复现」，不是「可还原」—— 脱敏后仍能判断「两次运行的配置
  是否一致」，但泄漏不了密钥。MUST NOT 把原始密钥 / 连接串明文写进日志或落库
  （启动自检会校验这一点，见 decisions.self_check）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from app.config.settings import get_settings

FINGERPRINT_LEN: Final[int] = 12
"""配置指纹长度（sha1 十六进制前 12 位，与索引指纹口径一致）。"""


def _redact_url(url: str | None) -> str | None:
    """去掉 URL 中的账号密码，只留 scheme/host/port/path。

    指纹需要的是「端点是不是同一个」，而不是连接凭据 —— 凭据留在 .env 里即可。
    """
    if not url:
        return url
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunsplit(parsed)


CROSS_ROLE_FIELDS: Final[tuple[str, ...]] = (
    # 会话与租户
    "app_env",
    "multi_tenant",
    # 生成：同一模型、同一端点、同一输出上限，两侧才算「答同一套」
    "llm_model",
    "llm_base_url",
    "llm_max_output_tokens",
    # 向量：模型 / 维度 / 端点决定「向量是否同构」，两侧不一致等于算出两个向量空间
    "embedding_model",
    "embedding_dim",
    "embedding_base_url",
    # 解析白名单：api 放行的扩展名，worker 必须能解析，否则上传成功、解析必失败
    "allowed_ext",
    # 能力开关与派生数据口径
    "graph_enabled",
    "graph_store_backend",
    "graph_extractor_model",
    "graph_retrieve_timeout_ms",
    "graph_extract_timeout_seconds",
    "wiki_enabled",
    "wiki_scope",
    "wiki_generator_model",
    "wiki_generate_timeout_seconds",
    "wiki_input_max_chars",
)
"""跨角色必须一致的字段白名单 —— `config_fingerprint` **只**对这份子集求指纹。

★ 为什么不直接哈希整个快照：
  快照里有一批**只属于某一个角色**的配置（重排端点/超时只有 api 用；解析超时、arq 重试、
  嵌入批大小只有 worker 用；限流、上传上限、审计保留只有 api 用）。拿全量哈希去比两侧，
  这些「合法的角色差异」会被误报成「两侧配置不一致」——
  实测就踩过：worker 编排里没有 `RERANK_BASE_URL`，`rerank_base_url` 一个是 URL 一个是
  None，两侧指纹恒定不同，而横幅还写着「另一侧必须打印同一个指纹」，检查当场变成假警。
  假警的代价比不做检查更大：运维学会忽略它之后，真正的开关不一致也就没人看。

★ 判定标准：一个字段进白名单，当且仅当「两侧取不同值会导致行为或数据不可解释」。
  只是「这个角色用不到」的，一律不进。
  新增配置项时 MUST 过一遍这个判定：进快照是默认动作，进本白名单需要理由。
"""
def runtime_config_snapshot() -> dict:
    """当前运行时配置的**脱敏**快照（纯 settings，不含密钥明文）。

    MUST 作为唯一的取值入口 —— 启动日志、caliber 快照、snapshot_config 脚本都从这里读，
    避免多处各拼一份、改了一处漏一处。
    """
    s = get_settings()
    return {
        "app_env": s.app_env,
        "multi_tenant": s.multi_tenant,
        # ── LLM ──
        "llm_model": s.llm_model,
        "llm_base_url": _redact_url(s.llm_base_url),
        "llm_max_output_tokens": s.llm_max_output_tokens,
        "llm_first_token_timeout_seconds": s.llm_first_token_timeout_seconds,
        "llm_total_timeout_seconds": s.llm_total_timeout_seconds,
        # ── 嵌入 / 重排 ──
        "embedding_model": s.embedding_model,
        "embedding_dim": s.embedding_dim,
        "embedding_base_url": _redact_url(s.embedding_base_url),
        "embedding_batch_size": s.embedding_batch_size,
        "rerank_model": s.rerank_model,
        "rerank_base_url": _redact_url(s.rerank_base_url),
        "rerank_timeout_seconds": s.rerank_timeout_seconds,
        # ── 上传 / 审计 / 限流 / 异步任务 ──
        "max_upload_mb": s.max_upload_mb,
        "allowed_ext": sorted(s.allowed_ext),
        "audit_retention_days": s.audit_retention_days,
        "chat_rate_limit_per_minute": s.chat_rate_limit_per_minute,
        "parse_job_timeout_seconds": s.parse_job_timeout_seconds,
        "arq_max_attempts": s.arq_max_attempts,
        # ── 能力开关（GraphRAG 图召回 / LLM wiki 导航）──
        # ★ 必须进快照（且必须进 CROSS_ROLE_FIELDS）：api 与 worker 的能力一致性**只**靠
        #   config_fingerprint 比对发现（两侧运行期不通信，见设计文档 §7.4）。少了这几项，
        #   两侧开关不同也会算出同一个指纹 —— 比对机制就变成了一句空话。
        # ★ 这里放的是**原始配置值**（GRAPH_EXTRACTOR_MODEL 可能是空串 = 跟随主模型）。
        #   不解析成实际模型名：解析结果已由 graph_profile 的指纹单独表达，
        #   混进来会让「跟随主模型」与「显式指定同一模型」生成不同指纹 ——
        #   两者行为完全一致，不该被判为配置不一致。
        "graph_enabled": s.graph_enabled,
        "graph_store_backend": s.graph_store_backend,
        "graph_extractor_model": s.graph_extractor_model,
        "graph_retrieve_timeout_ms": s.graph_retrieve_timeout_ms,
        "graph_extract_timeout_seconds": s.graph_extract_timeout_seconds,
        "wiki_enabled": s.wiki_enabled,
        "wiki_scope": s.wiki_scope,
        "wiki_generator_model": s.wiki_generator_model,
        "wiki_generate_timeout_seconds": s.wiki_generate_timeout_seconds,
        "wiki_input_max_chars": s.wiki_input_max_chars,
    }


def config_fingerprint(snapshot: dict | None = None) -> str:
    """配置指纹 = sha1(**跨角色一致字段**的排序快照)[:12]。

    传入 snapshot 可对给定快照求指纹（便于测试与成对比较）；不传则取当前配置。
    sort_keys 保证字段顺序无关，同一组配置恒得同一指纹。

    ★ 只哈希 `CROSS_ROLE_FIELDS` 子集，不哈希全量快照 —— 理由见该常量的 docstring：
      全量哈希会把「合法的角色差异」（如 worker 没有重排端点）误报成配置不一致。
      需要「完整配置」的场合（日志、caliber 审计）仍用 runtime_config_snapshot() 全量。
    """
    snap = runtime_config_snapshot() if snapshot is None else snapshot
    subset = {key: snap.get(key) for key in CROSS_ROLE_FIELDS if key in snap}
    raw = json.dumps(subset, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]