"""配置指纹的跨角色口径（api 与 worker 必须打印同一个 config_fingerprint）。

背景（真机踩到的）：dev 编排里 worker 段没有 `RERANK_BASE_URL`（它不重排），
而指纹原先哈希**全量**快照 → api 的 `rerank_base_url` 是 URL、worker 是 None，
两侧指纹恒定不同；可启动横幅却写着「另一侧必须打印同一个指纹」。
检查当场变成假警 —— 而假警比不做检查更糟：运维学会忽略之后，
真正的「开关不一致」也没人看了。

所以指纹只哈希 CROSS_ROLE_FIELDS 白名单：进白名单的判据是
「两侧取不同值会导致行为或数据不可解释」，而不是「这个字段存在」。
"""
from __future__ import annotations

from app.config.config_profile import (
    CROSS_ROLE_FIELDS,
    FINGERPRINT_LEN,
    config_fingerprint,
    runtime_config_snapshot,
)


def _snapshot() -> dict:
    return dict(runtime_config_snapshot())


def test_role_specific_fields_do_not_affect_fingerprint() -> None:
    """只在角色专属字段上不同的两份快照，必须得到同一个指纹。

    这是本次真机缺陷的回归钉子：worker 没有重排端点，不该因此被判成配置不一致。
    """
    api_side = _snapshot()
    worker_side = _snapshot()

    api_side["rerank_base_url"] = "http://reranker:8000/v1"
    worker_side["rerank_base_url"] = None
    api_side["chat_rate_limit_per_minute"] = 20
    worker_side["chat_rate_limit_per_minute"] = 60
    api_side["arq_max_attempts"] = 3
    worker_side["arq_max_attempts"] = 5

    assert config_fingerprint(api_side) == config_fingerprint(worker_side)


def test_cross_role_field_difference_changes_fingerprint() -> None:
    """能力开关不一致必须改变指纹 —— 否则比对机制就是一句空话。"""
    left = _snapshot()
    right = _snapshot()
    right["graph_enabled"] = not left["graph_enabled"]

    assert config_fingerprint(left) != config_fingerprint(right)


def test_embedding_caliber_difference_changes_fingerprint() -> None:
    """嵌入口径不同 = 两个向量空间，必须被抓出来。"""
    left = _snapshot()
    right = _snapshot()
    right["embedding_model"] = f"{left['embedding_model']}-other"

    assert config_fingerprint(left) != config_fingerprint(right)


def test_fingerprint_is_order_independent_and_fixed_length() -> None:
    """字段顺序无关、长度固定，且只由白名单子集决定。"""
    snap = _snapshot()
    reordered = {key: snap[key] for key in reversed(list(snap))}

    assert config_fingerprint(snap) == config_fingerprint(reordered)
    assert len(config_fingerprint(snap)) == FINGERPRINT_LEN


def test_allowlist_fields_exist_in_snapshot() -> None:
    """白名单里的键必须真的存在于快照 —— 拼错键名会让它静默失效。"""
    snap = _snapshot()
    missing = [key for key in CROSS_ROLE_FIELDS if key not in snap]

    assert missing == [], f"CROSS_ROLE_FIELDS 里这些键不在快照中：{missing}"
