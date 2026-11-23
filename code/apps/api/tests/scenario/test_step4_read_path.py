"""Step 4A 对**读路径**的影响（T4-R5 / T4-R6）：归档后的可见性、发布后的缓存时效。

★ 这两个用例是本批次唯一「写接口 vs 读路径」的接口面。它们不经过 `/ask`，
  直接调 `resolver`（设计 §D.1.3）：本机没有 Redis、没有 embedding / rerank 服务，
  走 `/ask` 会让用例在**进链路之后**因为与场景无关的原因失败，
  于是最该被验证的「解析结果」反而没人验。

★ R6 的反向对照是关键（设计 §D.5 的推导）：
  只断言「发布后读到新版本」无法区分「`invalidate` 生效」与「TTL 恰好过期」。
  做法：把 `CACHE_TTL_SECONDS` monkeypatch 成 3600，同一进程内连续读；
  再把 `invalidate` 换成 no-op 跑一遍 —— 那时必须读到**旧**版本。
  没有这一半，`invalidate` 整行删掉用例照样绿。
"""
from __future__ import annotations

import uuid

import pytest

from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    committed_application,
    committed_rows,
    committed_scalar,
)

# ★ 不用模块级 pytestmark：本文件里的用例全是 async，但统一用装饰器逐个标注，
#   与 `test_step4_constraints.py` 的做法一致 —— 一个模块里混进同步用例时，
#   模块级 mark 会让它变成「标记了 asyncio 但不是 async」的静默不跑。
_SKIP = pytest.mark.skipif(
    not SCENARIO_TEST_DB_URL,
    reason="needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)",
)


async def _mk_scenario(api, key: str = "ct_scene") -> uuid.UUID:
    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": key, "name": "读路径用例场景"}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _publish_version(api, scenario_id: uuid.UUID, *, policy: dict | None = None) -> dict:
    """新建 + 校验 + 发布一版，返回该版本的响应体（含 version / checksum）。"""
    created = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": policy or {},
            "kb_ids": [str(kb) for kb in api.kb_ids],
            "change_note": "读路径用例",
        },
    )
    assert created.status_code == 201, created.text
    row = created.json()
    for action in ("validate", "publish"):
        response = await api.client.post(
            f"/api/scenarios/{scenario_id}/versions/{row['version']}/{action}",
            headers={"If-Match": f'"{row["checksum"]}"'},
        )
        assert response.status_code == 200, response.text
    return row


async def _bind(api, application_id: uuid.UUID, scenario_id: uuid.UUID | None) -> None:
    response = await api.client.put(
        f"/api/integrations/applications/{application_id}/scenario",
        json={"scenario_id": str(scenario_id) if scenario_id else None},
    )
    assert response.status_code == 200, response.text


async def _resolve(api, application_id: uuid.UUID):
    """走**真实 resolver** 解析一次（不经 HTTP，见模块 docstring）。"""
    from app.models import IntegrationApplication
    from app.services.scenario.resolver import resolve_scenario

    async with api.session_factory() as session:
        application = await session.get(IntegrationApplication, application_id)
        assert application is not None
        return await resolve_scenario(
            session,
            tenant_id=api.tenant_id,
            application=application,
            requested_kb_ids=[api.kb_ids[0]],
        )


# ══════════════════════════════════════════════════════════════════════
# T4-R5：归档后 `_load_published` 返回 None（不是抛错、不是旧行）
# ══════════════════════════════════════════════════════════════════════


@_SKIP
@pytest.mark.asyncio
async def test_r5_load_published_returns_none_after_archive(api):
    """T4-R5：归档后 `_load_published` 必须返回 `None`。

    ★ 「返回 None」是 R1 分支的**唯一**输入。若它改成抛错，`resolve_scenario`
      会变成 500 而不是 `UNRESOLVABLE`，集成侧拿到的错误码与文档不符；
      若它返回旧行，归档就等于没生效（读路径继续用已下线的口径）。
      `None` 与「旧行」这两种实现的差别，只有在归档**之后**读一次才能观测到。
    """
    from app.services.scenario.resolver import _load_published

    scenario_id = await _mk_scenario(api)
    row = await _publish_version(api, scenario_id)

    async with api.session_factory() as session:
        before = await _load_published(
            session, tenant_id=api.tenant_id, scenario_id=scenario_id
        )
    assert before is not None and before.version == row["version"]

    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/{row['version']}/archive",
        headers={"If-Match": f'"{row["checksum"]}"'},
    )
    assert response.status_code == 200, response.text

    async with api.session_factory() as session:
        after = await _load_published(
            session, tenant_id=api.tenant_id, scenario_id=scenario_id
        )
    assert after is None, "归档后 `_load_published` 仍返回行 —— 归档对读路径无效"
    # 库里确实是 archived（排除「其实没归档，只是缓存被清」）
    state = await committed_scalar(
        "SELECT state FROM scenario_versions WHERE scenario_id = $1 AND version = $2",
        scenario_id,
        row["version"],
    )
    assert state == "archived"


# ══════════════════════════════════════════════════════════════════════
# T4-R6：发布后立刻读到新版本（`invalidate` 在提交后被调用）
# ══════════════════════════════════════════════════════════════════════


@_SKIP
@pytest.mark.asyncio
async def test_r6_publish_is_visible_immediately_despite_long_ttl(api, monkeypatch):
    """T4-R6：把 TTL 拉到 3600s 后，发布 v2 之后立刻解析必须读到 **v2**。

    ★ 为什么必须把 TTL 改大：出厂 TTL 是 60s，而用例总耗时远小于 60s ——
      不改大的话「读到新版本」既可能是 `invalidate` 的功劳，也可能是「缓存里本来
      就没有过这一行」。改大之后，唯一能让读到 v2 的机制就是 `invalidate`。
    """
    from app.services.scenario import resolver

    monkeypatch.setattr(resolver, "CACHE_TTL_SECONDS", 3600.0)

    scenario_id = await _mk_scenario(api)
    v1 = await _publish_version(api, scenario_id)
    application_id = await committed_application(api.tenant_id, created_by=api.user_id)
    await _bind(api, application_id, scenario_id)

    first = await _resolve(api, application_id)
    assert first.version == v1["version"]
    assert first.scenario_key == "ct_scene"

    v2 = await _publish_version(api, scenario_id, policy={"recall_top_k": 30})
    assert v2["version"] == 2

    second = await _resolve(api, application_id)
    assert second.version == 2, (
        "发布后仍解析到旧版本 —— `resolver.invalidate` 没被调用（或调用在提交之前）"
    )
    assert second.policy.declared() == {"recall_top_k": 30}, "读到的是新版本身份、旧版内容"


@_SKIP
@pytest.mark.asyncio
async def test_r6b_without_invalidate_the_cache_serves_the_old_version(api, monkeypatch):
    """T4-R6 反向对照：把 `invalidate` 换成 no-op → 发布 v2 后仍解析到 **v1**。

    ★ 这一半才是 R6 的证明力所在：它证明上一条用例的绿**来自 `invalidate`**，
      而不是来自「缓存恰好没命中」。同时它也是「删掉 `invalidate` 那行会不会被抓」
      这个问题的直接答案：会（本条会退化成读到 v2，从而失败）。
    ★ 用**独立场景**（新 key）而不是复用上一个用例的场景：用例之间的缓存已被
      conftest 的 autouse 夹具清空，但库里的版本号是累积的，复用会让断言依赖执行顺序。
    """
    from app.services.scenario import resolver, write_service

    monkeypatch.setattr(resolver, "CACHE_TTL_SECONDS", 3600.0)
    monkeypatch.setattr(write_service.resolver, "invalidate", lambda *a, **k: None)

    scenario_id = await _mk_scenario(api, key="ct_scene_cache")
    v1 = await _publish_version(api, scenario_id)
    application_id = await committed_application(api.tenant_id, created_by=api.user_id)
    await _bind(api, application_id, scenario_id)

    first = await _resolve(api, application_id)
    assert first.version == v1["version"]

    v2 = await _publish_version(api, scenario_id, policy={"recall_top_k": 40})
    assert v2["version"] == 2

    second = await _resolve(api, application_id)
    assert second.version == 1, (
        "`invalidate` 已停用却仍读到新版本 —— 说明上一条用例的绿不来自 invalidate，"
        "R6 是假绿的"
    )
    # 库里确实是 v2 published：证明「读到旧版本」是缓存造成的，不是发布失败
    published = await committed_rows(
        "SELECT version FROM scenario_versions "
        "WHERE scenario_id = $1 AND state = 'published'",
        scenario_id,
    )
    assert [row["version"] for row in published] == [2]
