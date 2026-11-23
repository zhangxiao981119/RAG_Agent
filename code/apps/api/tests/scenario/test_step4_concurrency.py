"""Step 4A 并发用例（T4-P1 ~ T4-P5）：锁纪律与第 3 道闸的**可观测**证据。

★ 并发断言与单请求断言的差别：单请求用例里 `FOR UPDATE`、部分唯一索引、
  `AND state='validated'` 这三个护栏**全部不参与**（没有第二个事务）。
  于是「把锁去掉」「把状态条件去掉」两种破坏在单请求下完全不可见 ——
  这正是本文件存在的理由。

★ 每个用例都用 `api` 夹具的**独立 session**（`NullPool`，每请求一条连接）：
  两条请求必须各持一条连接，否则测的是连接池而不是锁。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    committed_rows,
    committed_scalar,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not SCENARIO_TEST_DB_URL,
        reason="needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)",
    ),
]


async def _mk_scenario(api, key: str = "ct_scene") -> uuid.UUID:
    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": key, "name": "并发用例场景"}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _mk_version(api, scenario_id: uuid.UUID, *, policy: dict | None = None) -> dict:
    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": policy or {},
            "kb_ids": [str(kb) for kb in api.kb_ids],
            "change_note": "并发用例",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _if_match(checksum: str) -> dict[str, str]:
    return {"If-Match": f'"{checksum}"'}


async def _db_versions(scenario_id: uuid.UUID) -> list[dict]:
    rows = await committed_rows(
        "SELECT version, state, checksum, policy, archived_at "
        "FROM scenario_versions WHERE scenario_id = $1 ORDER BY version",
        scenario_id,
    )
    for row in rows:
        row["policy"] = json.loads(row["policy"])
    return rows


async def _published_count(scenario_id: uuid.UUID) -> int:
    return int(
        await committed_scalar(
            "SELECT count(*) FROM scenario_versions "
            "WHERE scenario_id = $1 AND state = 'published'",
            scenario_id,
        )
    )


def _code(response) -> str | None:
    try:
        return response.json()["detail"]["code"]
    except Exception:  # noqa: BLE001 — 非契约形状的失败在断言里单独体现
        return None


def _install_overlap(monkeypatch, *, seconds: float = 0.2) -> None:
    """在**拿到场景锁之后**插入一小段 `await sleep`，强制两次请求真正重叠。

    ★ 为什么必须这么做（不是「为了让测试好过」）：
      `asyncio.gather` **不保证**两个请求交错执行。ASGI 直连的两次请求如果没有
      一个足够长的 await 点，第一个会整条跑完再轮到第二个 —— 此时「并发用例」
      测的仍是**串行**路径，而且它照样会绿（`changed` 一 true 一 false 的顺序
      断言在串行下也成立）。也就是说：不插桩时 P3 是**假绿**，它证明不了锁在起作用。
      实测证据：不插桩时 P5（去掉 FOR UPDATE）得到两个 200 —— 因为第二个请求
      根本没和第一个重叠，它看到的是已提交的 `published` 并走了幂等分支。
    ★ 插桩点在 `_lock_scenario` **之后**：那里是事务里第一个「我已经持有行锁」
      的时刻，sleep 在那儿才能把「持锁窗口」拉长。插在锁之前只是让两个请求
      同时变慢，仍然可能一个先跑完。
    ★ 只 sleep 0.2s，不影响任何判据：它不改变语句、不改变顺序、不改变返回值。
    """
    from app.services.scenario import write_service

    original = write_service._lock_scenario

    async def _slow_lock(*args, **kwargs):
        result = await original(*args, **kwargs)
        await asyncio.sleep(seconds)
        return result

    monkeypatch.setattr(write_service, "_lock_scenario", _slow_lock)


# ══════════════════════════════════════════════════════════════════════
# T4-P1：并发建版本 —— 版本号不允许重复
# ══════════════════════════════════════════════════════════════════════


async def test_p1_concurrent_create_version_allocates_distinct_numbers(api, monkeypatch):
    """T4-P1：`gather` 两个 W2 → 两个版本号**不同且连续**（1,2），库里无重复。"""
    _install_overlap(monkeypatch)
    scenario_id = await _mk_scenario(api)

    async def _one(note: str):
        return await api.client.post(
            f"/api/scenarios/{scenario_id}/versions",
            json={
                "policy": {},
                "kb_ids": [str(kb) for kb in api.kb_ids],
                "change_note": note,
            },
        )

    first, second = await asyncio.gather(_one("并发甲"), _one("并发乙"))

    assert {first.status_code, second.status_code} == {201}, (
        f"并发建版本不是两个 201：{first.status_code}/{second.status_code} "
        f"{first.text} {second.text}"
    )
    versions = sorted([first.json()["version"], second.json()["version"]])
    assert versions == [1, 2], f"版本号不连续：{versions}（MAX+1 未在场景锁下分配）"

    rows = await _db_versions(scenario_id)
    assert [row["version"] for row in rows] == [1, 2], "库里的版本号与响应不一致（有丢写）"
    distinct = await committed_scalar(
        "SELECT count(DISTINCT version) FROM scenario_versions WHERE scenario_id = $1",
        scenario_id,
    )
    assert int(distinct) == 2


# ══════════════════════════════════════════════════════════════════════
# T4-P2：并发改同一 draft —— 后到者必须撞 checksum，不允许「混合内容」
# ══════════════════════════════════════════════════════════════════════


async def test_p2_concurrent_replace_same_draft_rejects_the_late_one(api, monkeypatch):
    """T4-P2：同 draft、同 `If-Match` 并发 W3 → 一个 200、一个 409 CHECKSUM_MISMATCH。

    ★ 关键的第二条断言：库里的 `policy` 必须与**某一个请求体逐键相等**。
      若并发控制写错（读旧值→合并→写回），会得到一个「两个请求的混合体」——
      那种内容谁都没提交过，却会被发布出去。
    """
    _install_overlap(monkeypatch)
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    checksum = row["checksum"]
    body_a = {"recall_top_k": 11}
    body_b = {"relevance_threshold_vector": 0.33}

    async def _one(policy: dict):
        return await api.client.put(
            f"/api/scenarios/{scenario_id}/versions/1",
            json={
                "policy": policy,
                "kb_ids": [str(kb) for kb in api.kb_ids],
                "change_note": "并发改",
            },
            headers=_if_match(checksum),
        )

    first, second = await asyncio.gather(_one(body_a), _one(body_b))

    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], (
        f"并发改同一 draft 的状态码是 {first.status_code}/{second.status_code}："
        f"{first.text} {second.text}"
    )
    rejected = first if first.status_code == 409 else second
    assert _code(rejected) == "SCENARIO_CHECKSUM_MISMATCH"

    rows = await _db_versions(scenario_id)
    assert rows[0]["policy"] in (body_a, body_b), (
        f"库里是混合内容 {rows[0]['policy']} —— 两个请求都没有提交过它"
    )


# ══════════════════════════════════════════════════════════════════════
# T4-P3 / T4-P5：并发发布同一版本 —— 第 1 道（锁）与第 3 道（唯一索引）
# ══════════════════════════════════════════════════════════════════════


async def _validated_scenario(api, *, policy: dict | None = None) -> tuple[uuid.UUID, int, str]:
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id, policy=policy)
    validated = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/1/validate",
        headers=_if_match(row["checksum"]),
    )
    assert validated.status_code == 200, validated.text
    return scenario_id, 1, row["checksum"]


async def test_p3_concurrent_publish_same_version_is_serialized(api, monkeypatch):
    """T4-P3：同 validated、同 `If-Match` 并发 W5 → 两个 200，`changed` 一 true 一 false。

    ★ 断言「两个都 200」是在断言**走的是第 1 道**（场景行锁把两次发布串行化，
      后到者看到 `published` → 幂等返回）。若只断言「最终只有一个 published」，
      那么「第 1 道失效、第 3 道兜底」也会过 —— 而那时客户端会收到 409，
      重试语义完全不同。
    """
    _install_overlap(monkeypatch)
    scenario_id, version, checksum = await _validated_scenario(api)

    async def _one():
        return await api.client.post(
            f"/api/scenarios/{scenario_id}/versions/{version}/publish",
            headers=_if_match(checksum),
        )

    first, second = await asyncio.gather(_one(), _one())

    assert {first.status_code, second.status_code} == {200}, (
        f"并发发布同一版本不是两个 200：{first.status_code}/{second.status_code} "
        f"{first.text} {second.text}"
    )
    changed = sorted([first.json()["changed"], second.json()["changed"]])
    assert changed == [False, True], f"changed 序列是 {changed}：锁没有把两次发布串行化"
    assert await _published_count(scenario_id) == 1


async def test_p5_without_the_lock_the_third_gate_still_holds(api, monkeypatch):
    """T4-P5：把**两把行锁**的 `FOR UPDATE` 都删掉后重跑 P3 → 仍只有 1 个 published。

    ★ 这条是唯一「故意破坏实现」的用例，价值在于把「锁失效时会不会静默出两个
      published」从**推理**变成**观测**。没有它，§B.5.3 的第 3 道就只是一句注释。
    ★ 期望形态：一个 200、一个 409（`STATE_CONFLICT` 或 `PUBLISH_CONFLICT`）。
      前者来自 `UPDATE ... AND state='validated'` 的 rowcount=0（第 2.5 道），
      后者来自 `uq_scenario_versions_one_published`（第 3 道）——
      两条都算「应用层锁不存在时，库仍然不让两个 published 并存」。
    ★ **为什么不只删场景锁**（实测结论，不是推测）：
      只删 `_SQL_LOCK_SCENARIO` 时，两次请求仍然是两个 200 `changed` 一 true 一 false ——
      原因是 `_SQL_LOCK_VERSION` 的 `FOR UPDATE` 把后到者挡在版本行上，等前一个提交后
      它读到的是 `state='published'`，于是走**幂等分支**返回 200。
      也就是说：只删一把锁，这个用例会**通过但对第 3 道毫无信息**（假绿）。
      要观测到库层兜底，必须让两次请求都真的读到自己那份 `validated`。
    """
    from app.services.scenario import write_service

    scenario_id, version, checksum = await _validated_scenario(api)
    # ★ 先插重叠、再换掉 SQL：只换 SQL 不插桩，两个请求不会重叠，用例会假绿
    _install_overlap(monkeypatch)

    for name in ("_SQL_LOCK_SCENARIO", "_SQL_LOCK_VERSION"):
        unlocked = getattr(write_service, name).replace("FOR UPDATE", "")
        assert "FOR UPDATE" not in unlocked.upper(), name
        monkeypatch.setattr(write_service, name, unlocked)

    async def _one():
        return await api.client.post(
            f"/api/scenarios/{scenario_id}/versions/{version}/publish",
            headers=_if_match(checksum),
        )

    first, second = await asyncio.gather(_one(), _one())

    assert await _published_count(scenario_id) == 1, "锁失效后出现了两个 published"
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409], (
        f"去掉行锁后并发发布的状态码是 {statuses}（两个都成功说明第 3 道不存在）"
    )
    failed = first if first.status_code == 409 else second
    assert _code(failed) in {"SCENARIO_STATE_CONFLICT", "SCENARIO_PUBLISH_CONFLICT"}


# ══════════════════════════════════════════════════════════════════════
# T4-P4：并发发布两个不同版本 —— 恰好一个 published，另一个被自动归档
# ══════════════════════════════════════════════════════════════════════


async def test_p4_concurrent_publish_two_versions_leaves_exactly_one_published(api, monkeypatch):
    """T4-P4：两个不同 validated 版本并发 W5 → 两个 200、恰好 1 published、

    另一个 `archived` 且 `archived_at IS NOT NULL`。

    ★ 「另一个被自动归档」而不是「报错」：这两个发布请求都是合法意图
      （运维分别点了两版），系统语义是「最后发布的那版生效，前一个退位」。
      如果这里返回 409，那么「同时点了两版」就变成了必须人工排查的故障。
    """
    scenario_id = await _mk_scenario(api)
    v1 = await _mk_version(api, scenario_id)
    v2 = await _mk_version(api, scenario_id, policy={"recall_top_k": 20})
    for version, row in ((1, v1), (2, v2)):
        response = await api.client.post(
            f"/api/scenarios/{scenario_id}/versions/{version}/validate",
            headers=_if_match(row["checksum"]),
        )
        assert response.status_code == 200, response.text

    _install_overlap(monkeypatch)

    async def _publish(version: int, checksum: str):
        return await api.client.post(
            f"/api/scenarios/{scenario_id}/versions/{version}/publish",
            headers=_if_match(checksum),
        )

    first, second = await asyncio.gather(
        _publish(1, v1["checksum"]), _publish(2, v2["checksum"])
    )

    assert {first.status_code, second.status_code} == {200}, (
        f"并发发布两个版本不是两个 200：{first.status_code}/{second.status_code} "
        f"{first.text} {second.text}"
    )
    rows = {row["version"]: row for row in await _db_versions(scenario_id)}
    assert await _published_count(scenario_id) == 1, (
        f"出现了不止一个 published：{ {v: r['state'] for v, r in rows.items()} }"
    )
    states = sorted(row["state"] for row in rows.values())
    assert states == ["archived", "published"], f"最终状态是 {states}"
    archived = next(row for row in rows.values() if row["state"] == "archived")
    assert archived["archived_at"] is not None, "被自动归档的行没有 archived_at（撞了 CHECK）"
