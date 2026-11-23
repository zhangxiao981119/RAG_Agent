"""Step 4B-1 真库用例：T-4B-1 … T-4B-7（设计 §D.2 的八条，逐条落地）。

★ 每条用例的 docstring 都写了「**把哪一行改坏它必须变红**」。没有这一列的用例在
  本步不被接受 —— 它是「有牙」的唯一判据（设计 §D.2 的表头）。

★ 反假绿的三条纪律（设计 §D.4，本文件逐条遵守）：
  1. **期望值来自构造，不来自被测对象**：`universe == 9` / `hard_stale == 3` 这些
     数字由「我插了几行、每行是什么口径」决定，写字面量；`collect()` 只被**比对**，
     不被引用。判据：把 `collect()` 改成 `return ReindexReport(universe=0, ...)`，
     下面每一条都会红。
  2. **第二条独立实现**：凡涉及"库里到底有多少条"的地方，另用一条**直接查 chunks 表**
     的 SQL 复核（不经 `fn_chunk_reindex_candidates`）—— 函数与脚本同源，
     但"函数实现错了"只有独立 SQL 抓得住。
  3. **负向一律走 HTTP 层**（`status_code` + `detail.code`），不写裸 `pytest.raises`：
     `test_0028_migration.py` 的守卫要求跨约束用例只允许**一处** `pytest.raises`。

★ 造数据全部走**独立连接 + 显式提交**（`pg_helpers` 的 committed 族）：
  handler 内部会 `commit()`，外层事务回滚不掉它（`pg_helpers` 模块 docstring 的
  第 2 条纪律）。代价是数据留在库里，所以容器用完即弃。

★ `chunks` 的清理（`_reset_chunks()`）只在**断言部署级计数**的用例里做：
  部署级没有租户过滤，任何残留都会让数字不可解释。租户级用例一律靠"每个用例
  一个新租户"隔离，不清全表 —— 清全表会影响同目录其它文件的既有前提。
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from app.config.index_profile import current_index_profile
from app.models import IntegrationApplication
from app.services.scenario import needs_reindex, resolver
from scripts import reindex_embeddings
from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    committed_kb,
    committed_rows,
    committed_scalar,
    committed_tenant,
    committed_user,
    migrate,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not SCENARIO_TEST_DB_URL,
        reason="needs SCENARIO_TEST_DB_URL (real PG with 0029 applied)",
    ),
]


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def schema_0029(schema) -> str:
    """在 `prepare_schema()` 的 0028 基线上再推 0029。

    ★ 为什么不在 `pg_helpers.prepare_schema` 里直接改成 0029：那个函数是 Step 1–2
      与 Step 4A **全部**真库用例的前置，改它等于让 4A 的 18+63 条用例跟着换基线；
      而 4B 的函数/视图对它们毫无影响（不建表、不改列）。分开推，各自可回滚。
    ★ 每个用到 `api` 的用例都必须**显式**把它列入参数：`api` 依赖的是 `schema`
      （只到 0028），不列它就可能先跑在 0028 上，于是 `/admin/needs-reindex`
      以「函数不存在」的形式 500 —— 那个红与用例要测的东西毫无关系。
    """
    migrate("0029")
    return schema


@pytest_asyncio.fixture()
async def dbfactory(schema_0029):
    """测试库的 session 工厂（`NullPool`：一次连接一条，互不占用）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(SCENARIO_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


# ══════════════════════════════════════════════════════════════════════
# 构造工具（本文件私有）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么这些构造器不放进 `pg_helpers`：那个模块的定位是「Step 1–4A 共用的真库
#   口径」，其中每个函数都被两个以上文件使用。这里的 chunk/documents 构造器
#   只有本文件需要（4B-1 不新增任何表、不新增任何约束），放进去等于让公共口径
#   承担一份私有形状。**等 4B-2 真的开工**、第二个文件也需要造 chunk 时再提取 ——
#   那时才有「两份口径分叉」的真实风险（`pg_helpers` docstring 的原始理由）。

_VEC_DIM = 1024
"""`chunks.embedding` 是 `vector(1024)`；给不足/超出的值会被 PG 直接拒。"""


def _vec(fill: float = 0.01) -> str:
    """构造一个 1024 维向量字面量。

    以**文本**形式传参再 `::text::vector` 转型：asyncpg 对自定义类型（`vector`）
    没有内置编码器，直接把 Python 值绑到 `$n::vector` 会去查 pgvector 的类型 OID；
    走 text 通道则完全不依赖驱动是否认识向量类型。这个坑在写本文件时踩过一次
    （`DataError: no binary format encoder`），留在这里作为警示。
    """
    return "[" + ",".join([str(fill)] * _VEC_DIM) + "]"


async def _reset_chunks() -> None:
    """清空 `chunks`（仅供**断言部署级计数**的用例使用）。

    ★ `citations` 是 `ON DELETE CASCADE`（`entities.py:403`），因此直接 DELETE 不会
      撞外键。全库唯一一条引用 `chunks.id` 的外键就是它。
    """
    await committed_scalar("DELETE FROM chunks")


async def _new_document(
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    *,
    status: str = "indexed",
    filename: str = "caliber-test.txt",
) -> uuid.UUID:
    """插一篇文档（`indexed` 是 `documents_indexed` 计数的分母，`deleted_at` 留空）。"""
    document_id = uuid.uuid4()
    await committed_scalar(
        "INSERT INTO documents "
        "(id, tenant_id, kb_id, doc_group_id, version, is_latest, filename, ext, "
        " size_bytes, storage_key, status, level, level_rank) "
        "VALUES ($1,$2,$3,$4,1,true,$5,'txt',10,$6,$7,1,20)",
        document_id,
        tenant_id,
        kb_id,
        uuid.uuid4(),
        filename,
        f"test/{document_id}.txt",
        status,
    )
    return document_id


async def _new_chunk(
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    chunk_index: int,
    *,
    embedding_model: str,
    embedding_text_version: str,
    chunker_version: str,
    with_embedding: bool = True,
    is_latest: bool = True,
) -> uuid.UUID:
    """插一条 chunk —— **给全列**，不依赖 `server_default`。

    ★ `acl_tags` 必须非空：`ck_chunks_acl_tags_not_empty` 要求
      `cardinality(acl_tags) > 0`。漏给会让 INSERT 以「约束违规」的形式失败，
      而那种失败在有 CHECK 的表上极容易被误读成"目标约束拦住了坏值"。
    ★ `embedding=None` 是**一个独立的桶**（`embedding_missing`），
      不是"某种过期" —— 所以这里用开关表达，而不是靠忘传参数。
    """
    chunk_id = uuid.uuid4()
    await committed_scalar(
        "INSERT INTO chunks "
        "(id, tenant_id, kb_id, document_id, chunk_index, content, token_count, "
        " level_rank, acl_tags, deny_subjects, is_latest, embedding, "
        " embedding_model, embedding_text_version, chunker_version, governance_weight) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,1,$8::text[],$9::text[],$10,$11::text::vector,"
        "        $12,$13,$14,0)",
        chunk_id,
        tenant_id,
        kb_id,
        document_id,
        chunk_index,
        f"口径用例正文 {chunk_index}",
        12,
        ["g:public"],
        [],
        is_latest,
        _vec() if with_embedding else None,
        embedding_model,
        embedding_text_version,
        chunker_version,
    )
    return chunk_id


async def _new_tenant_kb() -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = await committed_tenant()
    return tenant_id, await committed_kb(tenant_id)


async def _collect(dbfactory, tenant_id: uuid.UUID | None = None):
    async with dbfactory() as session:
        return await needs_reindex.collect(session, tenant_id=tenant_id)


async def _independent_hard_stale(tenant_id: uuid.UUID | None = None) -> int:
    """**独立实现**的硬过期计数（直接查 `chunks`，不经 0029 的函数）。

    ★ 它存在的唯一理由是抓「函数实现错了」：脚本与视图/端点同源（I-4B-2），
      所以三者会一起错。只有另写一遍谓词才能发现。
    """
    profile = current_index_profile()
    sql = (
        "SELECT count(*) FROM chunks WHERE is_latest = true "
        "AND (embedding_model <> $1 OR embedding_text_version <> $2)"
    )
    args: list[object] = [profile.embedding_model, profile.embedding_text_version]
    if tenant_id is not None:
        sql += " AND tenant_id = $3"
        args.append(tenant_id)
    return int(await committed_scalar(sql, *args))


# ══════════════════════════════════════════════════════════════════════
# T-4B-1：脚本 / 端点 / 视图同源
# ══════════════════════════════════════════════════════════════════════


async def test_t4b1_script_endpoint_and_view_agree(dbfactory, monkeypatch):
    """三类 chunk（干净 2 / 硬过期 3 / 只 chunker 过期 4）→ 三个消费方给出同一组数。

    破坏方式（必须变红）：
      ① 删掉 `0029` 函数里 `OR c.embedding_text_version <> p_embedding_text_version`
         这条比较（只比 model）→ 第 3 条硬过期行是「**仅** text_version 不同」，
         `_load_stale()` 的长度从 3 变 2 → 红（已实测）；
      ② 把 `_load_stale()` 的 `WHERE f.stale_hard` 去掉（脚本退化成"全部 chunk 都重算"）
         → 脚本返回 9 条而构造的过期集合是 3 条 → 红（已实测）。

    ★ 本用例**不**守租户边界，别在这里找那条证据：它在开头就 `_reset_chunks()`，
      库里只有本用例的行，"带不带 `tenant_id` 过滤"结果完全一样。
      租户边界由 `T-4B-7` 守（那里特意造了第二个租户的 4 条）—— 而那个数字
      之所以能守住，是因为它的期望值（3 vs 7）**只能**由租户条件产生。
      把这条分工写在这里，是因为「以为这条也守着」是这类用例最典型的误读。
    """
    await _reset_chunks()
    tenant_id, kb_id = await _new_tenant_kb()
    document_id = await _new_document(tenant_id, kb_id)
    profile = current_index_profile()

    ok_ids = {
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            index,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )
        for index in range(2)
    }
    # 硬过期 3 条：两条换模型（不同模型 = 两个口径分组），一条**仅**换文本公式版本
    hard_ids = {
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            10 + offset,
            embedding_model=f"other-model-{offset}",
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )
        for offset in (0, 1)
    }
    hard_ids.add(
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            12,
            embedding_model=profile.embedding_model,
            embedding_text_version="etext-legacy",
            chunker_version=profile.chunker_version,
        )
    )
    chunker_ids = {
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            20 + offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version="c300-600-40",
        )
        for offset in range(4)
    }

    report = await _collect(dbfactory, tenant_id)
    # 期望值是「我插了什么」的直接后果，不是 `collect()` 的自述
    assert (report.universe, report.ok, report.hard_stale, report.chunker_only) == (9, 2, 3, 4)
    assert report.embedding_missing == 0
    assert report.reconciles, "四桶之和 != universe（漏判或重判）"

    # ① 脚本：与端点同源（同一个函数），但**取数的列由脚本自己决定**
    monkeypatch.setattr(reindex_embeddings, "SessionLocal", dbfactory)
    stale_ids = {row[0] for row in await reindex_embeddings._load_stale()}
    assert stale_ids == hard_ids, (
        f"脚本选出的过期集合与构造不一致：多 {stale_ids - hard_ids}，少 {hard_ids - stale_ids}"
    )
    assert len(stale_ids) == 3
    assert ok_ids.isdisjoint(stale_ids) and chunker_ids.isdisjoint(stale_ids)

    # ② 独立 SQL（不经函数）：抓「函数自己写错了」
    assert await _independent_hard_stale(tenant_id) == 3

    # ③ 巡检视图：不接口径参数，回答「库里到底有几套口径」
    groups = await committed_rows(
        "SELECT chunk_count FROM v_chunk_caliber_distribution WHERE tenant_id = $1",
        tenant_id,
    )
    counts = sorted(int(row["chunk_count"]) for row in groups)
    assert sum(counts) == 9, "视图的分组丢行（它必须覆盖全部 is_latest 行）"
    assert counts == [1, 1, 1, 2, 4], (
        f"口径分组与构造不一致：{counts}（干净 2 / 换模型各 1 / 仅换公式 1 / 只换分块 4）"
    )


async def test_t4b1b_check_exit_code_tracks_stale_rows(dbfactory, monkeypatch):
    """`--check` 的退出码 = 「有没有硬过期」，这是 CI 门禁的唯一信号。

    破坏方式（必须变红）：把 `main()` 里的 `return 1 if total else 0` 改成 `return 0`
    → 第一格从 1 变 0 → 红。这条拦的是「CI 门禁被静默关掉」——它不会有任何症状，
    只会让所有过期悄悄放行。
    """
    await _reset_chunks()
    tenant_id, kb_id = await _new_tenant_kb()
    document_id = await _new_document(tenant_id, kb_id)
    profile = current_index_profile()

    chunk_id = await _new_chunk(
        tenant_id,
        kb_id,
        document_id,
        0,
        embedding_model="other-model-x",
        embedding_text_version=profile.embedding_text_version,
        chunker_version=profile.chunker_version,
    )
    monkeypatch.setattr(reindex_embeddings, "SessionLocal", dbfactory)
    assert await reindex_embeddings.main(batch_size=8, dry_run=False, check=True) == 1, (
        "库里有硬过期 chunk，--check 必须返回 1"
    )

    # 把那条改成当前口径（等价于「重算完了」）
    await committed_scalar(
        "UPDATE chunks SET embedding_model = $2 WHERE id = $1",
        chunk_id,
        profile.embedding_model,
    )
    assert await reindex_embeddings.main(batch_size=8, dry_run=False, check=True) == 0, (
        "零过期时 --check 必须返回 0（否则门禁永远红 → 运维学会无视）"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-2：软 DNA 不污染硬 DNA（I-4B-3 的可执行形态）
# ══════════════════════════════════════════════════════════════════════


async def test_t4b2_chunker_mismatch_never_becomes_hard(dbfactory, monkeypatch):
    """只造 4 条 `chunker_version` 不同的 chunk → `hard_stale == 0`、`chunker_only == 4`。

    破坏方式（必须变红）：给 `0029` 函数的 `stale_hard` 加上
    `OR c.chunker_version <> p_chunker_version`（即把软 DNA 升硬）→ `hard_stale`
    变 4、`--check` 退出码从 0 变 1 → 两条断言都红。

    ★ 这条就是 DQ4B-3 裁决的可执行形态：将来谁想升硬，它先红，而不是靠人记得
      「分块粒度不影响向量空间」这句话。
    """
    await _reset_chunks()
    tenant_id, kb_id = await _new_tenant_kb()
    document_id = await _new_document(tenant_id, kb_id)
    profile = current_index_profile()

    for offset in range(4):
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version="c300-600-40",
        )

    report = await _collect(dbfactory, tenant_id)
    assert report.universe == 4
    assert report.hard_stale == 0, "软 DNA 被升硬了（chunker 不匹配永不排除检索）"
    assert report.chunker_only == 4
    assert report.ok == 0, "chunker_only 不得进 ok 桶（它仍建议重切）"
    assert report.reconciles
    # 且不得告警：`verdict()` 只看 hard_stale（I-4B-3）
    assert needs_reindex.verdict(report, now=datetime.now(UTC)).level == "ok"

    monkeypatch.setattr(reindex_embeddings, "SessionLocal", dbfactory)
    assert await reindex_embeddings.main(batch_size=8, dry_run=False, check=True) == 0, (
        "只 chunker 过期不该让 CI 门禁变红"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-3：对账恒等式（四桶互斥）
# ══════════════════════════════════════════════════════════════════════


async def test_t4b3_four_buckets_are_mutually_exclusive(dbfactory):
    """四桶各 2 条、且其中一条**既硬过期又 chunker 过期** → 恒等式必须成立。

    破坏方式（必须变红）：把 `chunker_only` 的 FILTER 从
    `stale_chunker AND NOT stale_hard` 改成 `stale_chunker` → 那条「既硬又软」的行
    会同时落进 `hard_stale` 与 `chunker_only` → 右侧多 1 → 恒等式失配 → 红。

    ★ 为什么必须让**四个**桶都非 0：若两个桶恒为 0（例如空库上），
      「互斥」这件事在数学上无法被证伪 —— 那正是本项目抓到过的②号假绿形态。
    """
    await _reset_chunks()
    tenant_id, kb_id = await _new_tenant_kb()
    document_id = await _new_document(tenant_id, kb_id)
    profile = current_index_profile()

    # ① ok 2 条
    for offset in (0, 1):
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )
    # ② hard 2 条：一条换模型（chunker 同时过期 → 优先级必须落在 hard），一条仅换公式
    await _new_chunk(
        tenant_id,
        kb_id,
        document_id,
        10,
        embedding_model="other-model",
        embedding_text_version=profile.embedding_text_version,
        chunker_version="c300-600-40",
    )
    await _new_chunk(
        tenant_id,
        kb_id,
        document_id,
        11,
        embedding_model=profile.embedding_model,
        embedding_text_version="etext-legacy",
        chunker_version=profile.chunker_version,
    )
    # ③ chunker_only 2 条
    for offset in (20, 21):
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version="c300-600-40",
        )
    # ④ embedding_missing 2 条（口径全对，只缺向量）
    for offset in (30, 31):
        await _new_chunk(
            tenant_id,
            kb_id,
            document_id,
            offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
            with_embedding=False,
        )

    report = await _collect(dbfactory, tenant_id)
    assert (
        report.universe,
        report.ok,
        report.hard_stale,
        report.chunker_only,
        report.embedding_missing,
    ) == (8, 2, 2, 2, 2)
    assert report.reconciles, (
        "对账恒等式失配：ok + hard_stale + chunker_only + embedding_missing != universe"
    )
    non_zero = [
        name
        for name, value in (
            ("ok", report.ok),
            ("hard_stale", report.hard_stale),
            ("chunker_only", report.chunker_only),
            ("embedding_missing", report.embedding_missing),
        )
        if value
    ]
    assert len(non_zero) == 4, f"四桶里只有 {non_zero} 非 0，「互斥」无法被证伪"

    # 独立 SQL 复核（同一组数，另一条实现）
    await _assert_independent_buckets(tenant_id)


async def _assert_independent_buckets(tenant_id: uuid.UUID) -> None:
    """用直接查 `chunks` 的 SQL 复核四桶（不经 0029 的函数）。

    ★ 优先级必须在这里**重写一遍**（hard > chunker > missing > ok）：
      若这里引用函数，两边就一起错。
    """
    profile = current_index_profile()
    row = (
        await committed_rows(
            "SELECT "
            " count(*) AS universe,"
            " count(*) FILTER (WHERE NOT hard AND NOT soft AND embedding IS NOT NULL) AS ok,"
            " count(*) FILTER (WHERE hard) AS hard_stale,"
            " count(*) FILTER (WHERE soft AND NOT hard) AS chunker_only,"
            " count(*) FILTER (WHERE embedding IS NULL AND NOT hard AND NOT soft)"
            "   AS embedding_missing "
            "FROM ("
            "  SELECT embedding,"
            "    (embedding_model <> $2 OR embedding_text_version <> $3) AS hard,"
            "    (chunker_version <> $4) AS soft "
            "  FROM chunks WHERE is_latest = true AND tenant_id = $1"
            ") t",
            tenant_id,
            profile.embedding_model,
            profile.embedding_text_version,
            profile.chunker_version,
        )
    )[0]
    assert (int(row["universe"]), int(row["ok"]), int(row["hard_stale"]),
            int(row["chunker_only"]), int(row["embedding_missing"])) == (8, 2, 2, 2, 2), (
        "独立 SQL 与报告给出的四桶不一致 —— 函数或报告有一处算错"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-4：发布期三档判定（I-4B-6 的守卫）—— 走真实 HTTP
# ══════════════════════════════════════════════════════════════════════


async def _mk_scenario(api, key: str = "caliber_bot") -> uuid.UUID:
    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": key, "name": "口径用例场景"}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _mk_version(api, scenario_id: uuid.UUID, *, kb_ids) -> dict:
    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": {},
            "kb_ids": [str(kb) for kb in kb_ids],
            "change_note": "口径用例版本",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _validate(api, scenario_id: uuid.UUID, version: int, checksum: str):
    return await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/{version}/validate",
        headers={"If-Match": f'"{checksum}"'},
    )


async def _publish(api, scenario_id: uuid.UUID, version: int, checksum: str):
    return await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/{version}/publish",
        headers={"If-Match": f'"{checksum}"'},
    )


async def _validated_version(api, scenario_id: uuid.UUID, *, kb_ids) -> dict:
    """建版本 → 校验通过 → 返回该版本行（含最新 checksum）。"""
    version = await _mk_version(api, scenario_id, kb_ids=kb_ids)
    response = await _validate(api, scenario_id, version["version"], version["checksum"])
    assert response.status_code == 200, response.text
    return version


async def _db_version_state(scenario_id: uuid.UUID, version: int) -> str | None:
    rows = await committed_rows(
        "SELECT state FROM scenario_versions WHERE scenario_id = $1 AND version = $2",
        scenario_id,
        version,
    )
    return rows[0]["state"] if rows else None


async def test_t4b4a_blocker_409_and_previous_published_not_archived(api, schema_0029):
    """档①：kb 下有 1 篇 `indexed` 文档却 0 条 chunk → 409，且**旧 published 未被归档**。

    破坏方式（必须变红）：
      ① 把 `write_service.publish()` 里的 `if caliber_verdict.blocker is not None:`
         改成 `if False:` → 响应从 409 变 200 → 红；
      ② 把 `scenario_kb_readability` 的 `kb_ids` 参数写成 `[]` → 判定看成
         「0 chunk 且 0 文档」→ 放行 → 红。

    ★ 第二格（旧 published 未被归档）是本档**唯一**有副作用的断言：快照必须在
      归档之前（设计 §B.4.1）。只断 409 的话，一个"先归档再报错"的实现会全绿，
      而那会让场景进入「没有 published」的中间态。
    """
    scenario_id = await _mk_scenario(api)
    kb_id = api.kb_ids[0]

    first = await _validated_version(api, scenario_id, kb_ids=[kb_id])
    published = await _publish(api, scenario_id, first["version"], first["checksum"])
    assert published.status_code == 200, published.text
    assert await _db_version_state(scenario_id, first["version"]) == "published"

    # 现在让「有已索引文档、0 条 chunk」成立
    await _new_document(api.tenant_id, kb_id, status="indexed")

    second = await _validated_version(api, scenario_id, kb_ids=[kb_id])
    response = await _publish(api, scenario_id, second["version"], second["checksum"])
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "SCENARIO_KB_EMPTY_BUT_INDEXED"
    assert response.json()["detail"]["field"] == "kb_ids"
    assert response.json()["detail"]["context"]["documents_indexed"] == 1
    assert response.json()["detail"]["context"]["chunks_total"] == 0

    # 旧 published 必须还在原位（阻断发生在归档之前）
    assert await _db_version_state(scenario_id, first["version"]) == "published", (
        "阻断时旧 published 已被归档 —— 场景进入了「没有 published」的中间态"
    )
    # 目标版本仍是 validated（没有被非法推进）
    assert await _db_version_state(scenario_id, second["version"]) == "validated"


async def test_t4b4b_hard_stale_only_warns_but_publishes(api, schema_0029):
    """档②：5 条 chunk 全硬过期 → **200**，响应里的 5 必须等于独立 SQL 数出来的 5。

    破坏方式（必须变红）：
      ① 把 `judge_publish_snapshot` 的档②改成拒绝 → 响应变 409 → 红；
      ② 把 `scenario_kb_readability` 的 `WHERE f.kb_id = ANY(:kb_ids)` 去掉 →
         计数混入其它 kb 的 chunk（本文件前面用例留下的行）→ 5 变 N → 红。

    ★ 只断"响应里是 5"是不够的（③号假绿：断言的是被测对象自己的输出）。
      所以这里同时用独立 SQL 数一遍，并断言两者相等。
    """
    scenario_id = await _mk_scenario(api, key="caliber_bot_warn")
    kb_id = api.kb_ids[0]
    document_id = await _new_document(api.tenant_id, kb_id, status="indexed")
    profile = current_index_profile()

    for offset in range(5):
        await _new_chunk(
            api.tenant_id,
            kb_id,
            document_id,
            offset,
            embedding_model="other-model-legacy",
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )

    version = await _validated_version(api, scenario_id, kb_ids=[kb_id])
    response = await _publish(api, scenario_id, version["version"], version["checksum"])
    assert response.status_code == 200, response.text

    snapshot = response.json()["dna_snapshot"]
    assert snapshot["kb_readability"]["chunks_total"] == 5
    assert snapshot["kb_readability"]["chunks_hard_stale"] == 5
    assert snapshot["kb_readability"]["documents_indexed"] == 1
    assert "PARTIAL_UNREADABLE" in snapshot["warnings"]
    assert snapshot["declaration"]["stale_keys"] == [], (
        "版本声明是当前 DNA 写的，不该有漂移（有漂移会让 warnings 多一项，断言失去指向性）"
    )

    # 独立 SQL：同一个数字的另一条实现
    independent = int(
        await committed_scalar(
            "SELECT count(*) FROM chunks "
            "WHERE tenant_id = $1 AND kb_id = $2 AND is_latest = true "
            "AND embedding_model <> $3",
            api.tenant_id,
            kb_id,
            profile.embedding_model,
        )
    )
    assert independent == 5
    assert snapshot["kb_readability"]["chunks_hard_stale"] == independent


async def test_t4b4c_all_aligned_publishes_without_warnings(api, schema_0029):
    """档③：口径全对齐 → 200 且 `warnings == []`。

    破坏方式（必须变红）：把 `judge_publish_snapshot` 里 `declared_stale_keys`
    非空的分支改成无条件 append → `warnings` 多一项 → 红。

    ★ 这一档同时是档②的**正向对照**：没有它，「warnings 非空」可能来自
      「永远非空」而不是「真的有硬过期」。
    """
    scenario_id = await _mk_scenario(api, key="caliber_bot_ok")
    kb_id = api.kb_ids[0]
    document_id = await _new_document(api.tenant_id, kb_id, status="indexed")
    profile = current_index_profile()

    for offset in range(5):
        await _new_chunk(
            api.tenant_id,
            kb_id,
            document_id,
            offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )

    version = await _validated_version(api, scenario_id, kb_ids=[kb_id])
    response = await _publish(api, scenario_id, version["version"], version["checksum"])
    assert response.status_code == 200, response.text
    snapshot = response.json()["dna_snapshot"]
    assert snapshot["kb_readability"]["chunks_total"] == 5
    assert snapshot["kb_readability"]["chunks_hard_stale"] == 0
    assert snapshot["warnings"] == [], f"全对齐时不该有告警：{snapshot['warnings']}"


# ══════════════════════════════════════════════════════════════════════
# T-4B-5：快照是只读旁路（I-4B-5 的守卫）
# ══════════════════════════════════════════════════════════════════════

# 版本行里**允许**因发布而变化的部分是 `state` / `published_at` / `archived_at`；
# 内容四列必须逐字节不变。故 md5 只覆盖这四列 —— 对整行取 md5 是**不可能**成立的断言
# （state 从 validated 变 published 是发布的本义），设计 §D.2 的 T-4B-5 表头那句
# "row_to_json(v) 前后相等" 在这一点上与实现冲突，已在交付报告里登记。
_SQL_CONTENT_MD5 = """
SELECT md5(row_to_json(v)::text) AS h FROM (
    SELECT checksum, policy, kb_ids, dna_declared
    FROM scenario_versions WHERE scenario_id = $1 AND version = $2
) v
"""


async def test_t4b5_publish_snapshot_is_read_only_bypass(api, dbfactory, schema_0029, monkeypatch):
    """发布前后：`checksum` / `policy` / `kb_ids` / `dna_declared` 逐字节不变。

    破坏方式（必须变红）：在快照代码里顺手写一列（例如把 `dna_declared` 重新
    `build_dna_declared()` 后写回版本行）→ 该行的内容 md5 变化 → 红。
    这是最容易滑进去的一步（"既然都算出来了，顺手刷新一下声明吧"）：
    它会让 `If-Match` 的语义被语料状态污染（同一个 checksum 因为换了模型而失效）。
    """
    scenario_id = await _mk_scenario(api, key="caliber_bot_ro")
    kb_id = api.kb_ids[0]
    document_id = await _new_document(api.tenant_id, kb_id, status="indexed")
    profile = current_index_profile()
    for offset in range(3):
        await _new_chunk(
            api.tenant_id,
            kb_id,
            document_id,
            offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )

    version = await _validated_version(api, scenario_id, kb_ids=[kb_id])
    before = await committed_scalar(
        _SQL_CONTENT_MD5, scenario_id, version["version"]
    )
    response = await _publish(api, scenario_id, version["version"], version["checksum"])
    assert response.status_code == 200, response.text
    after = await committed_scalar(_SQL_CONTENT_MD5, scenario_id, version["version"])
    assert after == before, (
        "发布快照改写了版本行的内容列（checksum/policy/kb_ids/dna_declared 之一）"
    )

    # 审计里必须留下这次快照（"半年后能回答当时是不是在降级状态"）
    audit = await committed_rows(
        "SELECT detail FROM audit_logs WHERE object_id = $1 AND action = "
        "'scenario.version.publish' ORDER BY created_at DESC",
        f"{scenario_id}:{version['version']}",
    )
    assert audit, "发布没有写审计"
    assert "dna_snapshot" in (audit[0]["detail"] or ""), "审计里没有口径快照"

    # ── resolver 缓存在提交后必须失效 ────────────────────────────────
    # ★ TTL 拉长到 1 小时：若失效依赖"TTL 到期"，本断言会在 60 秒内恒真而无意义。
    monkeypatch.setattr(resolver, "CACHE_TTL_SECONDS", 3600.0)
    application = IntegrationApplication(
        id=uuid.uuid4(),
        tenant_id=api.tenant_id,
        name="口径用例应用",
        key_hash="0" * 64,
        kb_ids=[str(kb_id)],
        scenario_id=scenario_id,
        rate_limit_per_minute=60,
        daily_token_limit=200_000,
        daily_message_limit=1_000,
        created_by=api.user_id,
    )

    async def _resolved_version() -> int:
        async with dbfactory() as session:
            snapshot = await resolver.resolve_scenario(
                session,
                tenant_id=api.tenant_id,
                application=application,
                requested_kb_ids=[kb_id],
            )
            return snapshot.version

    assert await _resolved_version() == 1, "首次解析应读到已发布的 v1（同时完成缓存预热）"

    second = await _validated_version(api, scenario_id, kb_ids=[kb_id])
    assert second["version"] == 2
    response = await _publish(api, scenario_id, second["version"], second["checksum"])
    assert response.status_code == 200, response.text

    assert await _resolved_version() == 2, (
        "提交后没失效缓存：读到的仍是 v1（TTL 已被拉长到 3600s，不可能是自然过期）"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-6：`reindex` 不再篡改 `chunker_version`（DQ4B-6 的守卫）
# ══════════════════════════════════════════════════════════════════════


class _FakeEmbedder:
    """确定性的假 embedding 服务（**不调云端**，用户明令）。"""

    def __init__(self, dim: int = _VEC_DIM) -> None:
        self._dim = dim
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [[0.02] * self._dim for _ in texts]


async def test_t4b6_reindex_does_not_fake_chunker_version(
    dbfactory, schema_0029, monkeypatch
):
    """重算只写"真的重算过"的三列 —— `chunker_version` 必须保持旧值。

    破坏方式（必须变红）：把 `scripts/reindex_embeddings.py` 的 `.values(...)` 里
    重新加回 `chunker_version=profile.chunker_version` → 该列从 `'c300-600-40'`
    变成当前值 → 红。

    ★ 造数据的形状说明（与设计 §D.2 表头那句"仅 chunker 过期"的差异，已登记）：
      只 chunker 过期的 chunk **不会被** `_load_stale()` 选中（它按 `stale_hard` 过滤），
      于是"模型变成当前值"这半句断言无从成立。所以这里造的是**硬过期 + chunker 也是
      旧值**的行：它会被重算（硬过期），而它**没有被重切**（chunker_version 仍是旧值），
      这正是 DQ4B-6 缺陷的完整形态。
    """
    await _reset_chunks()
    tenant_id, kb_id = await _new_tenant_kb()
    document_id = await _new_document(tenant_id, kb_id)
    profile = current_index_profile()

    chunk_id = await _new_chunk(
        tenant_id,
        kb_id,
        document_id,
        0,
        embedding_model="other-model-legacy",
        embedding_text_version="etext-legacy",
        chunker_version="c300-600-40",
        with_embedding=False,
    )

    monkeypatch.setattr(reindex_embeddings, "SessionLocal", dbfactory)
    fake = _FakeEmbedder()
    monkeypatch.setattr(reindex_embeddings, "get_embedding_service", lambda: fake)

    assert await reindex_embeddings.main(batch_size=8, dry_run=False, check=False) == 0
    assert fake.calls == 1, "重算没有真正调用 embedding（用例会绿得毫无信息）"

    row = (
        await committed_rows(
            "SELECT embedding_model, embedding_text_version, chunker_version, "
            "       (embedding IS NULL) AS embedding_is_null "
            "FROM chunks WHERE id = $1",
            chunk_id,
        )
    )[0]
    assert row["embedding_model"] == profile.embedding_model
    assert row["embedding_text_version"] == profile.embedding_text_version
    assert row["embedding_is_null"] is False, "向量没有被写回"
    assert row["chunker_version"] == "c300-600-40", (
        "重算脚本篡改了 chunker_version —— 它没有重切，写新值就是写假事实"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-7：端点租户边界
# ══════════════════════════════════════════════════════════════════════

_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


async def test_t4b7_endpoint_is_tenant_scoped_and_hides_details(api, schema_0029):
    """`scope=tenant` 只统计本租户；`scope=deployment` 只给总数、不给时间戳与任何 id。

    破坏方式（必须变红）：
      ① 删掉 `_SQL_REPORT_TEMPLATE` 里 `WHERE f.tenant_id = :tenant_id` 那一段 →
         第一格的 universe 变成 7（自己 3 + 别人 4）→ 红；
      ② 把 `scope=deployment` 分支也返回 `newest_ok_at` → 响应体出现 `newest_` →
         第二格红（正则扫的是**响应体原文**，不靠模型定义）。

    ★ 第二格为什么必须扫原文：`response_model_exclude_none=True` 只在值为 None 时
      省键 —— 一旦实现把非 None 的时间戳塞进部署级响应，模型照样通过校验。
    """
    await _reset_chunks()
    profile = current_index_profile()

    # A 租户 = 本用例的登录用户；3 条当前口径
    kb_a = api.kb_ids[0]
    doc_a = await _new_document(api.tenant_id, kb_a, status="indexed")
    for offset in range(3):
        await _new_chunk(
            api.tenant_id,
            kb_a,
            doc_a,
            offset,
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )

    # B 租户：另一个租户的 4 条（用来证明租户过滤真的生效）
    tenant_b, kb_b = await _new_tenant_kb()
    await committed_user(tenant_b)
    doc_b = await _new_document(tenant_b, kb_b, status="indexed")
    for offset in range(4):
        await _new_chunk(
            tenant_b,
            kb_b,
            doc_b,
            offset,
            embedding_model="other-model-b",
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        )

    own = await api.client.get("/api/admin/needs-reindex")
    assert own.status_code == 200, own.text
    body = own.json()
    assert body["scope"] == "tenant"
    assert (body["universe"], body["ok"], body["hard_stale"]) == (3, 3, 0)
    assert body["reconciles"] is True
    assert body["profile"]["embedding_model"] == profile.embedding_model

    everyone = await api.client.get("/api/admin/needs-reindex", params={"scope": "deployment"})
    assert everyone.status_code == 200, everyone.text
    deployment = everyone.json()
    assert deployment["scope"] == "deployment"
    assert deployment["universe"] == 7, "部署级总数必须等于全库 is_latest 行数"
    assert deployment["hard_stale"] == 4, "4 条过期都在 B 租户（跨租户聚合）"
    assert deployment["reconciles"] is True

    # 部署级：只有计数，没有任何时间戳 / uuid 明细
    assert "newest_" not in everyone.text, (
        f"部署级响应泄露了时间戳：{everyone.text[:300]}"
    )
    leaked = _UUID_PATTERN.findall(everyone.text)
    assert not leaked, f"部署级响应泄露了 id：{leaked}"
    assert body["newest_ok_at"] is not None, (
        "租户级必须给 newest_ok_at（否则告警判据无法被复核）"
    )
