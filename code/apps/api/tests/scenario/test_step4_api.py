"""Step 4A 写接口：**应用层**状态机 / 幂等 / 前置条件 / 审计 / 跨租户（真库 + ASGI 直连）。

★ 每条用例最后都用**独立连接回读库**（`_db_version`），不只断响应体：
  响应体是 handler 自己算的，拿它当证据是循环论证（设计 §D.3 的推导）。
★ 状态一律写**字面量** `"published"` / `"validated"` / `"archived"`，
  绝不写 `SomeEnum.PUBLISHED` —— 用被测代码的常量做期望就是自证：枚举值改错时
  用例会跟着一起绿（设计 §D.7.3）。
★ 库层面的 CHECK / 部分唯一索引另有 `test_step4_constraints.py`。
  两层拦的不是同一件事：这里是「正常路径走不到那里」，那里是「直写 SQL 也拦得住」。
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.fake_settings import fake_settings
from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    committed_application,
    committed_rows,
    committed_scalar,
    committed_scenario,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not SCENARIO_TEST_DB_URL,
        reason="needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)",
    ),
]


# ══════════════════════════════════════════════════════════════════════
# 构造 / 回读工具
# ══════════════════════════════════════════════════════════════════════


async def _mk_scenario(api, key: str = "cs_bot") -> uuid.UUID:
    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": key, "name": "客服机器人"}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _mk_version(
    api, scenario_id: uuid.UUID, *, policy: dict | None = None, kb_ids=None, note: str = "初稿"
) -> dict:
    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": policy or {},
            "kb_ids": [str(kb) for kb in (kb_ids or api.kb_ids)],
            "change_note": note,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _if_match(checksum: str) -> dict[str, str]:
    # 客户端可以带引号也可以不带（`_require_if_match` 两边都剥），这里带引号 ——
    # 那是 RFC 7232 的规范形式，也是最容易被中间层原样转发的那种。
    return {"If-Match": f'"{checksum}"'}


async def _action(
    api, scenario_id: uuid.UUID, version: int, action: str, checksum: str | None
):
    headers = _if_match(checksum) if checksum else {}
    return await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/{version}/{action}", headers=headers
    )


async def _db_version(scenario_id: uuid.UUID, version: int) -> dict | None:
    rows = await committed_rows(
        "SELECT version, state, checksum, policy, kb_ids, binding_mode, "
        "       validated_at, published_at, archived_at "
        "FROM scenario_versions WHERE scenario_id = $1 AND version = $2",
        scenario_id,
        version,
    )
    if not rows:
        return None
    row = rows[0]
    # ★ asyncpg 不解码 json/jsonb（原样返回文本），所以这里显式 loads。
    #   不在 SQL 里加 `::text` 再 loads：那会把「列是不是 jsonb」这件事藏起来，
    #   而列类型本身是约束的一部分（`policy` 必须是 jsonb 才能用 `?` 运算符）。
    row["policy"] = json.loads(row["policy"])
    row["kb_ids"] = json.loads(row["kb_ids"])
    return row


async def _db_states(scenario_id: uuid.UUID) -> dict[int, str]:
    rows = await committed_rows(
        "SELECT version, state FROM scenario_versions "
        "WHERE scenario_id = $1 ORDER BY version",
        scenario_id,
    )
    return {int(row["version"]): row["state"] for row in rows}


async def _published_count(scenario_id: uuid.UUID) -> int:
    value = await committed_scalar(
        "SELECT count(*) FROM scenario_versions "
        "WHERE scenario_id = $1 AND state = 'published'",
        scenario_id,
    )
    return int(value)


def _code(response) -> str:
    return response.json()["detail"]["code"]


# ══════════════════════════════════════════════════════════════════════
# D.3 状态跃迁：S1–S17（每条一个，含非法跃迁）
# ══════════════════════════════════════════════════════════════════════


async def test_s1_first_version_is_draft(api):
    """T4-S1：无版本 → W2 → 201、version=1、state=draft（库里也是 draft）。"""
    scenario_id = await _mk_scenario(api)

    row = await _mk_version(api, scenario_id)

    assert row["version"] == 1
    assert row["state"] == "draft"
    stored = await _db_version(scenario_id, 1)
    assert stored["state"] == "draft"
    assert stored["validated_at"] is None and stored["published_at"] is None


async def test_s2_second_version_increments(api):
    """T4-S2：再 W2 → version=2，两条都是 draft（版本号由库里 MAX+1 分配）。"""
    scenario_id = await _mk_scenario(api)
    await _mk_version(api, scenario_id)
    second = await _mk_version(api, scenario_id, note="第二稿")

    assert second["version"] == 2
    assert await _db_states(scenario_id) == {1: "draft", 2: "draft"}


async def test_s3_validate_moves_draft_to_validated(api):
    """T4-S3：draft → W4 → changed=true，库里 validated 且 `validated_at` 非空。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)

    response = await _action(api, scenario_id, 1, "validate", row["checksum"])

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is True
    stored = await _db_version(scenario_id, 1)
    assert stored["state"] == "validated"
    assert stored["validated_at"] is not None


async def test_s4_publish_draft_directly_is_rejected(api):
    """T4-S4：draft 直接 W5 → 409 STATE_CONFLICT（校验与发布之间不许有后门）。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)

    response = await _action(api, scenario_id, 1, "publish", row["checksum"])

    assert response.status_code == 409, response.text
    assert _code(response) == "SCENARIO_STATE_CONFLICT"
    assert (await _db_version(scenario_id, 1))["state"] == "draft"


async def test_s5_publish_archives_previous_published(api):
    """T4-S5：发布 v2 → 200，`archived_previous_version=1`；库里 v1=archived、v2=published。"""
    scenario_id = await _mk_scenario(api)
    v1 = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", v1["checksum"])
    await _action(api, scenario_id, 1, "publish", v1["checksum"])

    v2 = await _mk_version(api, scenario_id, policy={"recall_top_k": 30}, note="改召回")
    await _action(api, scenario_id, 2, "validate", v2["checksum"])
    response = await _action(api, scenario_id, 2, "publish", v2["checksum"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True
    assert body["archived_previous_version"] == 1
    assert await _db_states(scenario_id) == {1: "archived", 2: "published"}
    assert (await _db_version(scenario_id, 1))["archived_at"] is not None


async def test_s6_republish_published_is_idempotent(api):
    """T4-S6：published 再 W5（同 If-Match）→ 200 changed=false，库不变。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])

    response = await _action(api, scenario_id, 1, "publish", row["checksum"])

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert await _db_states(scenario_id) == {1: "published"}


async def test_s7_archive_published(api):
    """T4-S7：published → W6 → changed=true，库里 archived 且 `archived_at` 非空。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])

    response = await _action(api, scenario_id, 1, "archive", row["checksum"])

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is True
    stored = await _db_version(scenario_id, 1)
    assert stored["state"] == "archived"
    assert stored["archived_at"] is not None


async def test_s8_replace_content_on_published_is_rejected(api):
    """T4-S8：published 上 W3 → 409（已发布出去的内容不可原地改）。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])

    response = await api.client.put(
        f"/api/scenarios/{scenario_id}/versions/1",
        json={"policy": {"recall_top_k": 7}, "kb_ids": [str(kb) for kb in api.kb_ids],
              "change_note": "偷改"},
        headers=_if_match(row["checksum"]),
    )

    assert response.status_code == 409, response.text
    assert _code(response) == "SCENARIO_STATE_CONFLICT"
    assert (await _db_version(scenario_id, 1))["policy"] == {}


async def test_s9_replace_content_on_validated_is_rejected(api):
    """T4-S9：validated 上 W3 → 409（「校验过的内容」与「发布出去的内容」之间无中间窗口）。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])

    response = await api.client.put(
        f"/api/scenarios/{scenario_id}/versions/1",
        json={"policy": {"recall_top_k": 7}, "kb_ids": [str(kb) for kb in api.kb_ids],
              "change_note": "校验后偷改"},
        headers=_if_match(row["checksum"]),
    )

    assert response.status_code == 409, response.text
    assert (await _db_version(scenario_id, 1))["policy"] == {}


async def test_s10_archive_replay_is_idempotent(api):
    """T4-S10：archived 再 W6（同 If-Match）→ 200 changed=false。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])
    await _action(api, scenario_id, 1, "archive", row["checksum"])

    response = await _action(api, scenario_id, 1, "archive", row["checksum"])

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert (await _db_version(scenario_id, 1))["state"] == "archived"


async def test_s11_publish_archived_is_rejected(api):
    """T4-S11：archived 上 W5 → 409（终态无回退；要恢复必须新建版本重新校验）。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])
    await _action(api, scenario_id, 1, "archive", row["checksum"])

    response = await _action(api, scenario_id, 1, "publish", row["checksum"])

    assert response.status_code == 409, response.text
    assert (await _db_version(scenario_id, 1))["state"] == "archived"


async def test_s12_archive_draft_is_rejected(api):
    """T4-S12：draft 上 W6 → 409（草稿不提供归档：它没有对外生效过）。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)

    response = await _action(api, scenario_id, 1, "archive", row["checksum"])

    assert response.status_code == 409, response.text
    assert (await _db_version(scenario_id, 1))["state"] == "draft"


async def test_s13_create_version_on_builtin_is_rejected(api):
    """T4-S13：内置场景 W2 → 409 BUILTIN_IMMUTABLE，且**库里没有新增行**。"""
    builtin_id = await committed_scenario(api.tenant_id, is_builtin=True, key="default")

    response = await api.client.post(
        f"/api/scenarios/{builtin_id}/versions",
        json={"policy": {}, "kb_ids": [str(api.kb_ids[0])], "change_note": "改内置"},
    )

    assert response.status_code == 409, response.text
    assert _code(response) == "SCENARIO_BUILTIN_IMMUTABLE"
    count = await committed_scalar(
        "SELECT count(*) FROM scenario_versions WHERE scenario_id = $1", builtin_id
    )
    assert int(count) == 0


async def test_s14_archive_builtin_is_rejected(api):
    """T4-S14：内置场景的已发布版本 W6 → 409 BUILTIN_IMMUTABLE，库里仍 published。

    ★ 为什么要拦：归档内置行会让该租户全部走 `builtin_fallback`，并一直打
      `scenario.builtin_row_missing` 这个「该补种」的告警 —— 把真告警淹掉。
    """
    builtin_id = await committed_scenario(api.tenant_id, is_builtin=True, key="default")
    await committed_rows(
        "INSERT INTO scenario_versions "
        "(tenant_id, scenario_id, version, state, policy, kb_ids, binding_mode, "
        " dna_declared, checksum, published_at) "
        "VALUES ($1,$2,1,'published','{}'::jsonb,'[]'::jsonb,'unrestricted',"
        "        $3::jsonb, $4, now()) RETURNING id",
        api.tenant_id,
        builtin_id,
        _seed_dna_json(),
        "0123456789abcdef",
    )

    response = await _action(api, builtin_id, 1, "archive", "0123456789abcdef")

    assert response.status_code == 409, response.text
    assert _code(response) == "SCENARIO_BUILTIN_IMMUTABLE"
    assert (await _db_version(builtin_id, 1))["state"] == "published"


async def test_s15_bind_scenario_without_published_is_rejected(api):
    """T4-S15：无 published 的场景 W7 绑定 → 400 NOT_PUBLISHABLE，application 未变。

    ★ 只建了一个 draft 版本 → 「能绑但不能用」的组合会让绑定请求在进链路前就 500。
      所以绑定期就拒，而不是等第一个 `/ask` 才炸。
    """
    scenario_id = await _mk_scenario(api)
    await _mk_version(api, scenario_id)  # 只有 draft
    application_id = await committed_application(
        api.tenant_id, created_by=api.user_id
    )

    response = await api.client.put(
        f"/api/integrations/applications/{application_id}/scenario",
        json={"scenario_id": str(scenario_id)},
    )

    assert response.status_code == 400, response.text
    assert _code(response) == "SCENARIO_NOT_PUBLISHABLE"
    bound = await committed_scalar(
        "SELECT scenario_id FROM integration_applications WHERE id = $1", application_id
    )
    assert bound is None


async def test_s16_archive_in_use_is_rejected(api):
    """T4-S16：有 1 个应用绑定时 W6 → 409 IN_USE，`context.application_count=1`。

    ★ 归档是 UPDATE，`fk_integration_app_scenario` 的 RESTRICT 完全不参与：
      不前置查引用，这批应用的下一次 `/ask` 会 500（不是 404、也不是旧值）。
    """
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])
    application_id = await committed_application(
        api.tenant_id, created_by=api.user_id, scenario_id=scenario_id
    )

    response = await _action(api, scenario_id, 1, "archive", row["checksum"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "SCENARIO_IN_USE"
    assert detail["context"]["application_count"] == 1
    assert (await _db_version(scenario_id, 1))["state"] == "published"
    # 前置：确实是「有引用」才被拒，不是别的原因
    still_bound = await committed_scalar(
        "SELECT scenario_id FROM integration_applications WHERE id = $1", application_id
    )
    assert still_bound == scenario_id


async def test_s17_unbind_then_archive_succeeds(api):
    """T4-S17：先 W7 解绑（null）再 W6 → 两个 200，库里 archived。

    ★ 与 S16 成对：只用 S16 无法区分「IN_USE 判定生效」与「W6 永远 409」。
    """
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])
    application_id = await committed_application(
        api.tenant_id, created_by=api.user_id, scenario_id=scenario_id
    )

    unbind = await api.client.put(
        f"/api/integrations/applications/{application_id}/scenario",
        json={"scenario_id": None},
    )
    assert unbind.status_code == 200, unbind.text
    assert unbind.json()["scenario_id"] is None

    response = await _action(api, scenario_id, 1, "archive", row["checksum"])

    assert response.status_code == 200, response.text
    assert (await _db_version(scenario_id, 1))["state"] == "archived"


# ══════════════════════════════════════════════════════════════════════
# D.2 应用层约束：C1 / C2（库层约束见 test_step4_constraints.py）
# ══════════════════════════════════════════════════════════════════════


async def test_c1_invalid_scenario_key_rejected_before_db(api):
    """T4-C1：`cs-bot`（带连字符）→ 422，**且库里没有新增行**。

    ★ 为什么必须再断言「库里没有新增行」：pydantic 422 与「入库了但响应 422」
      在只看状态码时是一样的。这条区分的是「入口拒绝」与「拒绝得太晚」。
    """
    before = int(await committed_scalar("SELECT count(*) FROM scenarios"))

    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": "cs-bot", "name": "客服机器人"}
    )

    assert response.status_code == 422, response.text
    after = int(await committed_scalar("SELECT count(*) FROM scenarios"))
    assert after == before


async def test_c2_default_key_is_reserved_but_default2_is_not(api):
    """T4-C2：`default` → 409 KEY_RESERVED；`default2` → 201（正向对照）。

    ★ `default` 是按租户插的种子行；若某租户的内置行缺失（`builtin_fallback` 路径存在），
      W1 用 `default` 会插成功 —— 于是出现一个「叫 default 但不是内置」的场景，
      `uq_scenarios_tenant_key` 挡不住。所以这条判等必须在应用层显式写。
    """
    reserved = await api.client.post(
        "/api/scenarios", json={"scenario_key": "default", "name": "冒充内置"}
    )
    assert reserved.status_code == 409, reserved.text
    assert _code(reserved) == "SCENARIO_KEY_RESERVED"

    ok = await api.client.post(
        "/api/scenarios", json={"scenario_key": "default2", "name": "不是内置"}
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["scenario_key"] == "default2"


async def test_c2b_duplicate_key_is_conflict_not_upsert(api):
    """T4-C2 续：同租户同 key 再建 → 409 KEY_EXISTS（不做 upsert）。

    ★ upsert 会把「两次点提交」与「两人抢同一个 key」压成同一个结果，
      掩盖真实冲突；而响应 201 会让客户端以为「我这一版生效了」。
    """
    await _mk_scenario(api, key="cs_bot")

    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": "cs_bot", "name": "重复"}
    )

    assert response.status_code == 409, response.text
    assert _code(response) == "SCENARIO_KEY_EXISTS"
    count = await committed_scalar(
        "SELECT count(*) FROM scenarios WHERE tenant_id = $1 AND scenario_key = 'cs_bot'",
        api.tenant_id,
    )
    assert int(count) == 1


# ══════════════════════════════════════════════════════════════════════
# If-Match：缺头 428 / 不匹配 409（两种处置必须可区分）
# ══════════════════════════════════════════════════════════════════════


async def test_missing_if_match_is_428_not_409(api):
    """缺 `If-Match` → **428**。

    ★ 「你没给条件」≠「条件不满足」。混成 409 后客户端分不清「我拿的是旧值」
      与「我漏发头」，重试策略完全不同（前者要重读，后者要补头）。
    """
    scenario_id = await _mk_scenario(api)
    await _mk_version(api, scenario_id)

    response = await api.client.get(f"/api/scenarios/{scenario_id}")
    version = response.json()["versions"][0]["version"]
    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/{version}/validate"
    )

    assert response.status_code == 428, response.text
    assert _code(response) == "SCENARIO_PRECONDITION_REQUIRED"


async def test_stale_if_match_is_409_and_does_not_leak_full_checksum(api):
    """过期 `If-Match` → 409 CHECKSUM_MISMATCH，`context` 只回显前 6 位。"""
    scenario_id = await _mk_scenario(api)
    await _mk_version(api, scenario_id)

    response = await _action(api, scenario_id, 1, "validate", "deadbeefdeadbeef")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "SCENARIO_CHECKSUM_MISMATCH"
    assert len(detail["context"]["expected_prefix"]) == 6
    assert (await _db_version(scenario_id, 1))["state"] == "draft"


async def test_missing_if_match_on_nonexistent_version_is_404(api):
    """存在性判定必须**先于** If-Match 判定：不存在的版本回 404，不是 428。

    ★ 顺序反了会让 428 变成存在性探针：`428` = 「版本存在且你需要补头」，
      `404` = 「版本不存在」—— 攻击者据此可枚举版本号。
    """
    scenario_id = await _mk_scenario(api)

    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/9/validate"
    )

    assert response.status_code == 404, response.text
    assert _code(response) == "SCENARIO_NOT_FOUND"


# ══════════════════════════════════════════════════════════════════════
# T4：发布期第二道闸（W4 不跑、W5 必跑 —— 两者都必须是可观测的）
# ══════════════════════════════════════════════════════════════════════


async def test_t4_runs_only_at_publish_not_at_validate(api, monkeypatch):
    """T4：`recall_top_k` 小于平台最终取用条数 → **校验通过、发布拒绝**。

    ★ 为什么必须成对断言「W4 通过」+「W5 拒绝」：
      - 只断言 W5 拒绝 → 无法区分「T4 在拦」与「`validate_policy` 顺手拦了」
        （那会让 T4 变成一个没有职责的空壳，删掉它仍然全绿）；
      - 只断言 W4 通过 → 无法证明 T4 真的存在。
      W4 **故意**不跑 T4（§B.5）：校验期只做内容自洽，平台侧的外部条件
      （重排是否开启、最终取用条数）不该被冻结在「校验那一刻」——
      否则「今天校验、明天平台改配置、后天发布」会发布一个与校验结论相反的东西。
    ★ 期望值一律写字面量：注入 `TOP_K_RERANK=5`，用 `recall_top_k=3`（3 < 5）。
      不写 `decisions.TOP_K_RERANK - 1`：那是拿被测代码的常量当期望（自证）。
    """
    from app.config import decisions
    from app.services.scenario import self_check

    monkeypatch.setattr(decisions, "TOP_K_RERANK", 5)
    monkeypatch.setattr(self_check.decisions, "TOP_K_RERANK", 5)

    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id, policy={"recall_top_k": 3})

    validated = await _action(api, scenario_id, 1, "validate", row["checksum"])
    assert validated.status_code == 200, (
        f"W4 不该跑 T4，却拒了：{validated.text}"
    )
    assert (await _db_version(scenario_id, 1))["state"] == "validated"

    published = await _action(api, scenario_id, 1, "publish", row["checksum"])

    assert published.status_code == 400, published.text
    detail = published.json()["detail"]
    assert detail["code"] == "SCENARIO_NOT_PUBLISHABLE"
    assert detail["field"] == "recall_top_k"
    # 失败后状态不变（T4 在归档之前 —— 否则旧 published 已被归档，进入「无 published」中间态）
    assert (await _db_version(scenario_id, 1))["state"] == "validated"


async def test_t4_rejects_rerank_on_when_platform_off(api, monkeypatch):
    """T4：平台未启用重排时，策略里 `rerank_enabled=true` → 发布拒绝。

    ★ 这条是「三处拒因同一个码、靠 `field` 区分」的第二处（另一处是 `recall_top_k`）。
      两条都在，`SCENARIO_NOT_PUBLISHABLE` 才不是「一个笼统的 400」。
    ★ 平台开关用 monkeypatch 注入而不是读 `.env`：否则用例的期望会随部署环境变，
      在「本地重排开着、CI 重排关着」两种机器上给出相反结论。
    """
    from app.services.scenario import write_service

    monkeypatch.setattr(
        write_service, "get_settings", lambda: fake_settings(rerank_enabled=False)
    )

    scenario_id = await _mk_scenario(api, key="ct_rerank")
    row = await _mk_version(api, scenario_id, policy={"rerank_enabled": True})
    validated = await _action(api, scenario_id, 1, "validate", row["checksum"])
    assert validated.status_code == 200, validated.text

    published = await _action(api, scenario_id, 1, "publish", row["checksum"])

    assert published.status_code == 400, published.text
    detail = published.json()["detail"]
    assert detail["code"] == "SCENARIO_NOT_PUBLISHABLE"
    assert detail["field"] == "rerank_enabled"


async def test_t4_accepts_the_same_policy_when_platform_rerank_on(api, monkeypatch):
    """T4 的正向对照：同一个 `rerank_enabled=true` 在平台开启重排时可以发布。

    ★ 没有这条，「T4 拒绝」与「这个策略永远不可发布」不可区分 ——
      后者会让所有开启重排的场景都无法发布，而用例仍全绿。
    """
    from app.services.scenario import write_service

    monkeypatch.setattr(
        write_service, "get_settings", lambda: fake_settings(rerank_enabled=True)
    )

    scenario_id = await _mk_scenario(api, key="ct_rerank_on")
    row = await _mk_version(api, scenario_id, policy={"rerank_enabled": True})
    await _action(api, scenario_id, 1, "validate", row["checksum"])

    published = await _action(api, scenario_id, 1, "publish", row["checksum"])

    assert published.status_code == 200, published.text
    assert (await _db_version(scenario_id, 1))["state"] == "published"


# ══════════════════════════════════════════════════════════════════════
# D.6 审计：A1 / A2 / A3
# ══════════════════════════════════════════════════════════════════════


async def _audit_actions(api) -> list[str]:
    rows = await committed_rows(
        "SELECT action FROM audit_logs WHERE tenant_id = $1 ORDER BY created_at",
        api.tenant_id,
    )
    return [row["action"] for row in rows]


async def test_a1_every_write_action_is_audited(api):
    """T4-A1：W1/W2/W4/W5/W6/W7 各自的 `action` 都留痕（同事务，不丢）。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])
    application_id = await committed_application(api.tenant_id, created_by=api.user_id)
    bind = await api.client.put(
        f"/api/integrations/applications/{application_id}/scenario",
        json={"scenario_id": str(scenario_id)},
    )
    assert bind.status_code == 200, bind.text
    await api.client.put(
        f"/api/integrations/applications/{application_id}/scenario",
        json={"scenario_id": None},
    )
    await _action(api, scenario_id, 1, "archive", row["checksum"])

    actions = await _audit_actions(api)

    for expected in (
        "scenario.create",
        "scenario.version.create",
        "scenario.version.validate",
        "scenario.version.publish",
        "scenario.version.archive",
        "integration.application.bind_scenario",
    ):
        assert expected in actions, f"缺少审计动作 {expected}；实际 {actions}"
    # `trace_id` 非空：没有它，审计行无法与请求日志对齐（§B.7）
    trace_ids = await committed_rows(
        "SELECT trace_id FROM audit_logs WHERE tenant_id = $1", api.tenant_id
    )
    assert all(row["trace_id"] for row in trace_ids), "有审计行缺 trace_id"


async def test_a2_replace_content_audits_symmetric_difference(api):
    """T4-A2：W3 的 `detail.changed_keys` 是**对称差**（增删都算），不是「新键集合」。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id, policy={"recall_top_k": 30})

    response = await api.client.put(
        f"/api/scenarios/{scenario_id}/versions/1",
        json={
            "policy": {"relevance_threshold_vector": 0.62},
            "kb_ids": [str(kb) for kb in api.kb_ids],
            "change_note": "换阈值",
        },
        headers=_if_match(row["checksum"]),
    )
    assert response.status_code == 200, response.text

    rows = await committed_rows(
        "SELECT detail FROM audit_logs WHERE tenant_id = $1 "
        "AND action = 'scenario.version.update'",
        api.tenant_id,
    )
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"])["changed_keys"] == [
        "recall_top_k",
        "relevance_threshold_vector",
    ]
    # 整体替换：旧键必须消失（否则「回到跟随平台」无法表达）
    assert (await _db_version(scenario_id, 1))["policy"] == {
        "relevance_threshold_vector": 0.62
    }


async def test_a3_audit_failure_rolls_back_the_whole_change(api, monkeypatch):
    """T4-A3：审计写入失败 → 主变更**随之回滚**（证明同事务，不是「审计在旁边写」）。

    ★ 这条只能靠 monkeypatch 制造：正常路径永远不失败。
      它证明的是 `record_with_session(session, ...)` 这条**共用 session** 的选择，
      而不是 `audit.record()`（自开 `SessionLocal` 并吞异常）—— 后者会造成
      「主事务回滚但审计已提交」的幽灵记录。
    """
    from app.services import audit

    scenario_id = await _mk_scenario(api)

    async def _boom(*args, **kwargs):
        raise RuntimeError("审计写入失败（注入）")

    monkeypatch.setattr(audit, "record_with_session", _boom)

    with pytest.raises(RuntimeError):
        await api.client.post(
            f"/api/scenarios/{scenario_id}/versions",
            json={"policy": {}, "kb_ids": [str(api.kb_ids[0])], "change_note": "会回滚"},
        )

    count = await committed_scalar(
        "SELECT count(*) FROM scenario_versions WHERE scenario_id = $1", scenario_id
    )
    assert int(count) == 0, "审计失败后主变更仍留在库里 —— 两者不在同一事务"


# ══════════════════════════════════════════════════════════════════════
# D.6 跨租户：X1 / X2 / X3
# ══════════════════════════════════════════════════════════════════════


async def _published_scenario(api) -> tuple[uuid.UUID, int, str]:
    """造一个「已发布」的场景，返回 (id, version, checksum)。"""
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, 1, "validate", row["checksum"])
    await _action(api, scenario_id, 1, "publish", row["checksum"])
    return scenario_id, 1, row["checksum"]


async def test_x1_other_tenant_publish_is_404_and_changes_nothing(api):
    """T4-X1：A 租户对 B 租户**真实存在**的场景 W5 → 404，B 的版本状态未变。"""
    from tests.scenario.pg_helpers import (
        committed_tenant as _ct,
        committed_user as _cu,
    )

    scenario_id, _, _ = await _published_scenario(api)  # 属于 A
    other_tenant = await _ct()
    other_user = await _cu(other_tenant)
    api.as_user(other_tenant, other_user)

    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/1/publish",
        headers=_if_match("0123456789abcdef"),
    )

    assert response.status_code == 404, response.text
    assert _code(response) == "SCENARIO_NOT_FOUND"
    assert (await _db_version(scenario_id, 1))["state"] == "published"


async def test_x2_random_uuid_is_byte_identical_to_x1(api):
    """T4-X2：随机 UUID 的响应体必须与 X1 **逐字节相同**（不可枚举租户资源）。"""
    from tests.scenario.pg_helpers import (
        committed_tenant as _ct,
        committed_user as _cu,
    )

    scenario_id, _, _ = await _published_scenario(api)
    other_tenant = await _ct()
    other_user = await _cu(other_tenant)

    api.as_user(other_tenant, other_user)
    real = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/1/publish",
        headers=_if_match("0123456789abcdef"),
    )
    random_id = uuid.uuid4()
    fake = await api.client.post(
        f"/api/scenarios/{random_id}/versions/1/publish",
        headers=_if_match("0123456789abcdef"),
    )

    assert real.status_code == fake.status_code == 404
    assert real.text == fake.text, (
        "「别人的真实场景」与「不存在的场景」响应体不同 —— 等于给出存在性探针"
    )


async def test_x3_bind_other_tenant_scenario_is_404(api):
    """T4-X3：A 租户 W7 绑定 B 租户的场景 → 404（按 A 的视角它就是不存在）。"""
    from tests.scenario.pg_helpers import (
        committed_application as _ca,
        committed_tenant as _ct,
        committed_user as _cu,
    )

    scenario_id, _, _ = await _published_scenario(api)
    other_tenant = await _ct()
    other_user = await _cu(other_tenant)
    my_application = await _ca(api.tenant_id, created_by=api.user_id)
    api.as_user(other_tenant, other_user)

    response = await api.client.put(
        f"/api/integrations/applications/{my_application}/scenario",
        json={"scenario_id": str(scenario_id)},
    )

    # 先撞「应用不属于本租户」—— 也是 404，且不会透露场景是否存在
    assert response.status_code == 404, response.text
    bound = await committed_scalar(
        "SELECT scenario_id FROM integration_applications WHERE id = $1", my_application
    )
    assert bound is None


# ══════════════════════════════════════════════════════════════════════
# 读接口（R1–R3）：`If-Match` 的输入来源
# ══════════════════════════════════════════════════════════════════════


async def test_r1_list_scenarios_shows_published_and_draft(api):
    """R1：列表里同时给出 `published` 摘要与 `draft_version`（客户端据此决定下一步）。"""
    scenario_id, _, checksum = await _published_scenario(api)
    await _mk_version(api, scenario_id, policy={"recall_top_k": 20}, note="下一稿")

    response = await api.client.get("/api/scenarios")

    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["id"] == str(scenario_id))
    assert row["published"]["version"] == 1
    assert row["published"]["checksum"] == checksum
    assert row["draft_version"] == 2
    assert row["is_builtin"] is False


async def test_r2_detail_lists_versions_newest_first(api):
    """R2：详情里的版本摘要按 version **倒序**（最新一版在最上面）。"""
    scenario_id = await _mk_scenario(api)
    await _mk_version(api, scenario_id)
    await _mk_version(api, scenario_id, note="第二稿")

    response = await api.client.get(f"/api/scenarios/{scenario_id}")

    assert response.status_code == 200, response.text
    versions = [item["version"] for item in response.json()["versions"]]
    assert versions == [2, 1]


async def test_r3_get_version_returns_etag_equal_to_checksum(api):
    """R3：`ETag` == `"checksum"` —— 客户端可直接把它当 `If-Match` 回送。

    ★ 语义边界：`checksum` **不含 version / state**，因此两个不同版本可以同 checksum。
      `If-Match` 必须与 path 里的 version 联合才有定位能力（§U4-4）。
    """
    scenario_id = await _mk_scenario(api)
    row = await _mk_version(api, scenario_id)

    response = await api.client.get(f"/api/scenarios/{scenario_id}/versions/1")

    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == f'"{row["checksum"]}"'
    assert response.json()["checksum"] == row["checksum"]


async def test_r3b_two_versions_can_share_checksum(api):
    """R3 的语义边界：**同内容不同版本 ⇒ 同 checksum**（所以 ETag 不是版本标识）。

    ★ 这条是反面证据：若有人把 `checksum` 改成含 version，它会红 ——
      而那是**有害**的改动：`checksum` 的用途是「内容是否被我看到的同一份」，
      含 version 后「因为版本号变了所以不能提交」会挡住合法的并发编辑。
    """
    scenario_id = await _mk_scenario(api)
    first = await _mk_version(api, scenario_id)
    second = await _mk_version(api, scenario_id, note="内容一样")

    assert first["checksum"] == second["checksum"]


def _seed_dna_json() -> str:
    """种子行的 `dna_declared`（与迁移 `_SEED_DNA` 逐字同构）。"""
    from tests.scenario.conftest import load_migration

    import json as _json

    return _json.dumps(load_migration()._SEED_DNA)
