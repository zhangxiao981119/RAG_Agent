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
    }


def config_fingerprint(snapshot: dict | None = None) -> str:
    """配置指纹 = sha1(排序后的脱敏快照)[:12]。

    传入 snapshot 可对给定快照求指纹（便于测试与成对比较）；不传则取当前配置。
    sort_keys 保证字段顺序无关，同一组配置恒得同一指纹。
    """
    snap = runtime_config_snapshot() if snapshot is None else snapshot
    raw = json.dumps(snap, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]