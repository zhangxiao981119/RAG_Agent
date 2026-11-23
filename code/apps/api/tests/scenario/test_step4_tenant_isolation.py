"""T4-ISO：跨租户隔离的**行为层**证据（真库 + ASGI 直连，走 HTTP）。

★ 为什么需要这个文件（与 `test_step4_guards.py` 的分工）：
  - `test_step4_guards.py` 守的是**源码文本**（每条 SQL 必须带
    `tenant_id = :tenant_id` 谓词、且不得带恒真析取）。它能**全量**覆盖 12 条语句，
    但它证明不了「整条 HTTP 链路上没有别的口子」—— 例如某条 SELECT 守住了，
    而路由层把 `tenant_id` 传成了 `None`。
  - 本文件守的是**观测到的行为**：B 租户拿着**真实存在**的 A 租户场景 id 逐端点打，
    每个端点都必须 404。这是「SQL 写对了」与「调用方传对了」两件事的交集。

★ 反自证：断言对象是 HTTP `status_code` 与 `detail.code`，不是 service 的返回值，
  也不是写侧自己算出来的布尔值。

★ 为什么用两个**真租户**而不是把 `tenant_id` 参数 monkeypatch 成别人：
  直接改参数会绕开「`user.tenant_id` 从 JWT 来」这一环 ——
  而「路由层用了 `user.tenant_id` 而不是路径里的 id」正是本文件要证明的事。

★ 全部用例共用同一条硬约束（每条用例的 docstring 里重复了一次，因为它是
  「这条用例到底有没有用」的唯一判据）：
  把 `app/services/scenario/write_service.py:861`（`_SQL_LIST_SCENARIOS` 的 WHERE）或
  `:878`（`_SQL_GET_VERSION` 的 WHERE）改成 `(x.tenant_id = :tenant_id OR true)`
  → 对应用例**必须变红**。4B 之前这里是子串守卫，那样改完 287 条用例全绿。
  写端点（5 条）的判据是两把锁：`:51`（`_SQL_LOCK_SCENARIO`）与
  `:65`（`_SQL_LOCK_VERSION` 的 `v.tenant_id = :tenant_id`）。

★ 实测的**纵深**结论（写在这里，因为它决定了写端点用例该怎么读）：
  只短路 `_SQL_LOCK_SCENARIO` 时，5 条写用例里**只有 `create_version` 变红**
  （201：B 在 A 的场景下建出了 draft）。其余 4 条仍被第二把锁
  `_SQL_LOCK_VERSION` 挡成 404 —— 这是刻意的纵深，不是冗余。
  把两把锁**同时**短路，5 条全红（201 / 409 / 409 / 428 / 428）。
  所以：`create_version` 守第一把锁，其余 4 条守「两把锁都在」。
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    committed_kb,
    committed_tenant,
    committed_user,
)

requires_db = pytest.mark.skipif(
    not SCENARIO_TEST_DB_URL,
    reason="needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)",
)


# ══════════════════════════════════════════════════════════════════════
# 夹具：A 租户有一个 published 场景；B 租户是攻击者
# ══════════════════════════════════════════════════════════════════════


class TwoTenants:
    """A 的资源坐标 + B 的登录态容器。"""

    def __init__(self, api, *, scenario_id, version, checksum, kb_ids, tenant_b, user_b):
        self.api = api
        self.scenario_id = scenario_id
        self.version = version
        self.checksum = checksum
        self.tenant_b = tenant_b
        self.user_b = user_b
        self.kb_ids_b = list(kb_ids)

    def paths(self) -> dict[str, str]:
        """7 个端点的实际路径（以 `app/api/scenarios.py` 的 `@router` 为准）。

        ★ 与设计表格的差异：内容替换的路径是
          `PUT /scenarios/{id}/versions/{version}`（**没有** `/content` 后缀）——
          照表格猜路径会让这条用例永远 404（自己制造的假绿）。
        """
        base = f"/api/scenarios/{self.scenario_id}"
        version = f"{base}/versions/{self.version}"
        return {
            "detail": base,
            "get_version": version,
            "create_version": f"{base}/versions",
            "replace_content": version,
            "validate": f"{version}/validate",
            "publish": f"{version}/publish",
            "archive": f"{version}/archive",
        }


async def _make_published_scenario(api) -> tuple[uuid.UUID, int, str]:
    """W1 → W2 → W4 → W5，产出一个 published v1（A 租户）。"""
    created = await api.client.post(
        "/api/scenarios", json={"scenario_key": "iso_tenant", "name": "隔离用例"}
    )
    assert created.status_code == 201, created.text
    scenario_id = uuid.UUID(created.json()["id"])

    version = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": {"recall_top_k": 11},
            "kb_ids": [str(kb) for kb in api.kb_ids],
            "change_note": "隔离用例",
        },
    )
    assert version.status_code == 201, version.text
    row = version.json()
    for action in ("validate", "publish"):
        acted = await api.client.post(
            f"/api/scenarios/{scenario_id}/versions/{row['version']}/{action}",
            headers={"If-Match": f'"{row["checksum"]}"'},
        )
        assert acted.status_code == 200, acted.text
    return scenario_id, row["version"], row["checksum"]


@pytest_asyncio.fixture()
async def two_tenants(api) -> TwoTenants:
    """造 A 的 published 场景 + B 的租户/用户/知识库，然后**把当前登录用户切成 B**。

    ★ 为什么 B 也要有一个真实知识库：`POST /versions` 与 `PUT /versions/{v}` 的请求体
      要求 `kb_ids` 非空，且 service 会校验知识库属于**调用方租户**。若 B 用 A 的 kb，
      那两条用例会因为 400 `SCENARIO_WRITE_INVALID` 提前失败，于是 404 这条
      「最该验的断言」压根没跑到 —— 用一个假绿换另一个假绿。
    """
    scenario_id, version, checksum = await _make_published_scenario(api)

    tenant_b = await committed_tenant()
    user_b = await committed_user(tenant_b)
    kb_b = await committed_kb(tenant_b, owner_id=user_b)

    env = TwoTenants(
        api,
        scenario_id=scenario_id,
        version=version,
        checksum=checksum,
        kb_ids=[kb_b],
        tenant_b=tenant_b,
        user_b=user_b,
    )
    api.as_user(tenant_b, user_b)
    return env


def _assert_not_found(response, env: TwoTenants) -> None:
    """404 + `code == SCENARIO_NOT_FOUND` + 响应体不含 A 的任何内容标识。"""
    assert response.status_code == 404, (
        f"跨租户请求返回 {response.status_code}（期望 404）。"
        f"路径：{response.request.url}；响应体：{response.text}"
    )
    detail = response.json()["detail"]
    assert detail["code"] == "SCENARIO_NOT_FOUND", detail
    raw = response.text
    for leaked in (str(env.scenario_id), "iso_tenant"):
        assert leaked not in raw, (
            f"响应体泄露了 A 租户的标识 {leaked!r} —— 404 的语义是「不存在」，"
            f"回显 id/key 等于确认「存在但不给你」。实际：{raw}"
        )


# ══════════════════════════════════════════════════════════════════════
# 读端点
# ══════════════════════════════════════════════════════════════════════


@requires_db
@pytest.mark.asyncio
async def test_cross_tenant_scenario_detail_is_404(two_tenants):
    """`GET /api/scenarios/{a_id}` —— B 读 A 的场景详情必须 404。

    ★ 反自证判据：把 `write_service.py:861`（`_SQL_LIST_SCENARIOS` 的 WHERE）改成
      `(s.tenant_id = :tenant_id OR true)` → 本用例必须变红（实测返回 200 并带回
      A 的 `scenario_key`）。
      该函数（`get_scenario_detail`）的实现是「列出本租户全部场景再线性查找」，
      所以它唯一的数据来源就是那条 SQL —— 一旦短路，A 的行会出现在 B 的列表里，
      于是详情 200 且带回 A 的 `scenario_key`。
    """
    response = await two_tenants.api.client.get(two_tenants.paths()["detail"])
    _assert_not_found(response, two_tenants)


@requires_db
@pytest.mark.asyncio
async def test_cross_tenant_version_read_does_not_leak_content(two_tenants):
    """`GET /api/scenarios/{a_id}/versions/{v}` —— 必须 404，且不泄露 policy/kb/checksum。

    ★ 反自证判据：把 `write_service.py:878`（`_SQL_GET_VERSION` 的 WHERE）改成
      `(tenant_id = :tenant_id OR true)` → 本用例必须变红（实测返回 200 并带回
      A 的 `policy` / `checksum`）。
      这条端点是最危险的一个：它直接返回 `policy` / `kb_ids` / `dna_declared` /
      `checksum`，短路后 B 能读到 A 的完整口径（连 `ETag` 都会带着 A 的 checksum 回来）。
    """
    response = await two_tenants.api.client.get(two_tenants.paths()["get_version"])
    _assert_not_found(response, two_tenants)
    # checksum 是内容指纹：它出现在响应里就说明内容已经被读出去了
    assert two_tenants.checksum not in response.text
    assert "ETag" not in response.headers or two_tenants.checksum not in response.headers["ETag"]


@requires_db
@pytest.mark.asyncio
async def test_list_scenarios_does_not_include_other_tenant(two_tenants):
    """`GET /api/scenarios` —— B 的列表里不得出现 A 的场景。

    ★ 断言的是**字符串包含**而不是「条数」：条数相等也可能整批换成了别人的行
      （B 本来 0 条，短路后变成 1 条 A 的 —— 若有人把断言写成 `len(rows) == 0`
      而 fixture 里给 B 也建了场景，就会两两抵消）。
    ★ 反自证判据：把 `write_service.py:861` 改成
      `(s.tenant_id = :tenant_id OR true)` → 本用例必须变红（列表里出现 `iso_tenant`）。
    """
    response = await two_tenants.api.client.get("/api/scenarios")
    assert response.status_code == 200, response.text
    raw = response.text
    assert str(two_tenants.scenario_id) not in raw, (
        f"B 的场景列表里出现了 A 的场景 id —— 列表查询没有按租户过滤。实际：{raw}"
    )
    assert "iso_tenant" not in raw, (
        f"B 的场景列表里出现了 A 的场景 key —— 同上。实际：{raw}"
    )


# ══════════════════════════════════════════════════════════════════════
# 写端点（5 个）
# ══════════════════════════════════════════════════════════════════════


@requires_db
@pytest.mark.asyncio
async def test_cross_tenant_create_version_is_404(two_tenants):
    """`POST /api/scenarios/{a_id}/versions` —— 不得给 A 的场景建版本。

    ★ 这一条若失败，后果不是「读到数据」而是「往别人租户写数据」：
      B 能在 A 的场景下建 draft，A 下次发布就可能把 B 写的内容发出去。
    ★ 反自证判据：把 `write_service.py:51`（`_SQL_LOCK_SCENARIO` 的 WHERE，
      `WHERE id = :scenario_id AND tenant_id = :tenant_id`）改成
      `WHERE id = :scenario_id AND (tenant_id = :tenant_id OR true)` → 本用例必须变红
      （**实测返回 201**：B 在 A 的场景下建出了一个 draft，`version=1`）。
    ★ 五条写用例里**只有本条**会被「单点短路第一把锁」打穿 —— 因为
      `create_version` 是唯一「版本行还不存在、只有一把锁」的写路径
      （其余 4 条还有 `_SQL_LOCK_VERSION` 兜着，见模块 docstring 的纵深说明）。
      这正是把本条单独留下的理由：它守的是**第一把锁本身**，不是纵深。
    ★ 判据**不是** `:861` / `:878` —— 那两条是读路径的唯一 SQL，
      分别由 `test_cross_tenant_scenario_detail_is_404` 与
      `test_cross_tenant_version_read_does_not_leak_content` 守。
    """
    response = await two_tenants.api.client.post(
        two_tenants.paths()["create_version"],
        json={
            "policy": {},
            "kb_ids": [str(kb) for kb in two_tenants.kb_ids_b],
            "change_note": "越权尝试",
        },
    )
    _assert_not_found(response, two_tenants)


@requires_db
@pytest.mark.asyncio
async def test_cross_tenant_replace_content_is_404(two_tenants):
    """`PUT /api/scenarios/{a_id}/versions/{v}` —— 不得改 A 的内容。

    ★ 请求**故意不带** `If-Match`：这样本条同时证明了「存在性判定先于前置条件判定」。
      若实现先判头，会返回 428（缺头）而不是 404 —— 而 428 等于告诉攻击者
      「这个版本存在，你只是缺个头」。设计 §C.2.3 把这条顺序写成硬要求。
    ★ 反自证判据：**两把锁同时**短路才红 —— 把 `write_service.py:51`
      （`_SQL_LOCK_SCENARIO`）与 `:65`（`_SQL_LOCK_VERSION` 的 `v.tenant_id = :tenant_id`）
      的租户谓词都改成 `... OR true` → 本用例必须变红（实测 409，
      `SCENARIO_STATE_CONFLICT`：A 的 v1 已是 published）。
      只短路第一把锁时本条**仍绿**，因为第二把锁还会把它判成 404 ——
      这是纵深，别误以为本条没用（见模块 docstring）。
    """
    response = await two_tenants.api.client.put(
        two_tenants.paths()["replace_content"],
        json={
            "policy": {"recall_top_k": 3},
            "kb_ids": [str(kb) for kb in two_tenants.kb_ids_b],
            "change_note": "越权尝试",
        },
    )
    _assert_not_found(response, two_tenants)


@requires_db
@pytest.mark.asyncio
async def test_cross_tenant_validate_is_404(two_tenants):
    """`POST /api/scenarios/{a_id}/versions/{v}/validate` —— 不得把 A 的版本推成 validated。

    ★ 同样故意不带 `If-Match`（见上一条：428 也是一种泄露）。
    ★ 反自证判据：**两把锁同时**短路才红（`write_service.py:51` + `:65` 都改成
      `... OR true`）→ 本用例必须变红（实测 409 `SCENARIO_STATE_CONFLICT`）。
      只短路第一把锁时仍绿：`_SQL_LOCK_VERSION` 是第二道防线。
    ★ 若 B 能 validate，A 的发布前置（`state == 'validated'`）就被别人控制了 ——
      这是「跨租户写入」里最隐蔽的一种：库里只多了一个 `validated_at`。
    """
    response = await two_tenants.api.client.post(two_tenants.paths()["validate"])
    _assert_not_found(response, two_tenants)


@requires_db
@pytest.mark.asyncio
async def test_cross_tenant_publish_is_404(two_tenants):
    """`POST /api/scenarios/{a_id}/versions/{v}/publish` —— 不得发布 A 的版本。

    ★ 发布是**改生产口径**的动作：若能跨租户发布，攻击者可以把 A 的某个 draft
      发成 published，A 的全部集成应用立刻换口径，而 A 侧只会看到
      「version 变了」。
    ★ 反自证判据：**两把锁同时**短路才红（`write_service.py:51` + `:65` 都改成
      `... OR true`）→ 本用例必须变红（实测 **428**：`_require_if_match` 先喊缺头 ——
      这也说明「两把锁都坏了」时客户端看到的第一个信号是 428 而不是越权成功）。
      只短路第一把锁时仍绿：`_SQL_LOCK_VERSION` 是第二道防线。
    """
    response = await two_tenants.api.client.post(two_tenants.paths()["publish"])
    _assert_not_found(response, two_tenants)


@requires_db
@pytest.mark.asyncio
async def test_cross_tenant_archive_is_404(two_tenants):
    """`POST /api/scenarios/{a_id}/versions/{v}/archive` —— 不得归档 A 的已发布版本。

    ★ 归档一个正在被应用引用的 published 版本 = 让 A 的全部集成请求开始 500
      （`T4-R4` 实测过这条链路的终点）。跨租户可达就等于「一个租户能打掉另一个租户」。
    ★ 反自证判据：**两把锁同时**短路才红（`write_service.py:51` + `:65` 都改成
      `... OR true`）→ 本用例必须变红（实测 **428**，同 publish）。
      只短路第一把锁时仍绿：`_SQL_LOCK_VERSION` 是第二道防线。
    """
    response = await two_tenants.api.client.post(two_tenants.paths()["archive"])
    _assert_not_found(response, two_tenants)


# ══════════════════════════════════════════════════════════════════════
# 反向对照：A 自己打同一个端点必须成功
# ══════════════════════════════════════════════════════════════════════


@requires_db
@pytest.mark.asyncio
async def test_owner_tenant_can_still_read_its_own_scenario(two_tenants):
    """反向对照：A 本人读同一个场景必须 200 —— 否则上面的 404 可能只是「全都 404」。

    ★ 为什么必须有这条：若 `get_scenario_detail` 因为任何与租户无关的原因
      （比如 `list_scenarios` 抛错被吞、或路径写错）对**所有人**都 404，
      上面 8 条会全绿而毫无信息 —— 这是本项目已经出现过两次的假绿形态
      （「两套阈值都等于 0.50」、「种子行数 0 == 0」）。
    ★ 切回 A 之后再断言，顺带证明 `as_user` 这一层真的在起作用。
    """
    api = two_tenants.api
    api.as_user(api.tenant_id, api.user_id)

    detail = await api.client.get(two_tenants.paths()["detail"])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["scenario_key"] == "iso_tenant"
    assert body["published"]["version"] == two_tenants.version
    # 详情能读到版本摘要，说明 `_SQL_GET_VERSIONS` 也按租户放行了
    assert [item["version"] for item in body["versions"]] == [two_tenants.version]

    version = await api.client.get(two_tenants.paths()["get_version"])
    assert version.status_code == 200, version.text
    assert version.json()["policy"] == {"recall_top_k": 11}
    assert version.headers["ETag"] == f'"{two_tenants.checksum}"'
