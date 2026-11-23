"""S6-5：存量搬迁（COPY 行过滤 + 逐表摘要校验 + 回滚回拷）。

两层结构（与 `tests/scenario/conftest.py` 一致）：

  (a) **NO-DB 层（必跑）**：
      ① 搬迁顺序与 `scene_db_schema.MIGRATION_TABLES` 一一对应且父表在前；
      ② 每张表的过滤键显式声明（kb_id / document_id / job_id），只用 $1 参数；
      ③ 校验 SQL 含行数 + id 摘要，chunks 另加正文长度与正文摘要；
      ④ 列发现 SQL 排除生成列（fts_vec / name_vec 不可显式写入）；
      ⑤ 探测 SQL 只用两个参数，不做字符串拼接。
  (b) **Real-DB 层（`SCENARIO_TEST_DB_URL` 缺失即 skip）**：
      两个临时库（控制库 + 场景库）→ 手术前拒绝 → 空 kb 拒绝 →
      正向搬迁 + 探测等价 → 幂等重跑 → 别家 kb 拒绝 →
      回滚增量回拷（只增不删、可重跑）。

★ 真库用例必须真跑，权限不足时 `pytest.skip` 并写明原因，不得伪造通过。
"""
from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command

from app.infra.scene_db_migrate import (
    COLUMN_SQL,
    MIGRATE_SOURCE_EMPTY,
    MIGRATE_TARGET_HAS_FOREIGN_KB,
    MIGRATE_TARGET_NOT_PREPARED,
    MIGRATION_ORDER,
    PROBE_SQL,
    MigrateError,
    _database_of,
    _metrics_sql,
    migrate_forward,
    migrate_reverse,
)
from app.infra.scene_db_schema import MIGRATION_TABLES, apply_surgery
from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    alembic_config,
    dsn,
    requires_db,
)


# ══════════════════════════════════════════════════════════════════════
# A. NO-DB 层
# ══════════════════════════════════════════════════════════════════════


def test_migration_order_matches_table_set_and_is_parent_first():
    """顺序集合必须与 MIGRATION_TABLES 相等；父表必须排在子表之前。"""
    names = [spec.name for spec in MIGRATION_ORDER]

    assert set(names) == MIGRATION_TABLES
    assert len(names) == len(MIGRATION_TABLES)

    index = {name: position for position, name in enumerate(names)}
    assert index["documents"] < index["chunks"]
    assert index["documents"] < index["parse_jobs"]
    assert index["documents"] < index["governance_jobs"]
    assert index["documents"] < index["wiki_pages"]
    assert index["documents"] < index["graph_entity_mentions"]
    assert index["documents"] < index["graph_relations"]
    assert index["chunks"] < index["graph_entity_mentions"]
    assert index["graph_entities"] < index["graph_entity_mentions"]
    assert index["graph_entities"] < index["graph_relations"]
    assert index["parse_jobs"] < index["parse_segments"]


def test_every_predicate_is_explicit_and_parameterized():
    """过滤键逐表声明：不推导、不拼接 kb 值，且只用 $1 一个参数。"""
    specs = {spec.name: spec.predicate for spec in MIGRATION_ORDER}

    assert specs["documents"] == '"kb_id" = $1'
    for table in (
        "chunks",
        "kb_terms",
        "graph_entities",
        "graph_entity_mentions",
        "graph_relations",
        "wiki_pages",
    ):
        assert specs[table] == '"kb_id" = $1', table

    for table in ("parse_jobs", "governance_jobs"):
        predicate = specs[table]
        assert '"document_id" IN' in predicate
        assert 'FROM "documents" WHERE "kb_id" = $1' in predicate

    segments = specs["parse_segments"]
    assert '"job_id" IN' in segments
    assert 'FROM "parse_jobs"' in segments
    assert 'JOIN "documents"' in segments

    for predicate in specs.values():
        assert "$1" in predicate
        assert "$2" not in predicate
        assert "%s" not in predicate


def test_metrics_sql_uses_count_and_md5_and_chunks_extra_content():
    """校验 SQL：行数 + id 摘要；chunks 必须多出正文长度与正文摘要。"""
    specs = {spec.name: spec for spec in MIGRATION_ORDER}

    documents_sql = _metrics_sql(specs["documents"]).lower()
    assert "count(*)" in documents_sql
    assert "md5(string_agg" in documents_sql
    assert "order by \"id\"" in documents_sql
    assert "content_len" not in documents_sql

    chunks_sql = _metrics_sql(specs["chunks"]).lower()
    assert "content_len" in chunks_sql
    assert "content_md5" in chunks_sql
    assert "sum(length(\"content\"))" in chunks_sql


def test_column_sql_excludes_generated_columns():
    """生成列必须排除：fts_vec / name_vec 显式写入会直接 COPY 失败。"""
    sql = COLUMN_SQL.lower()

    assert "information_schema.columns" in sql
    assert "is_generated = 'never'" in sql
    assert "ordinal_position" in sql
    assert "$1" in sql


def test_probe_sql_has_two_parameters():
    """探测 SQL：kb_id 与词都走参数绑定，不拼字符串。"""
    sql = PROBE_SQL.lower()

    assert "$1" in sql and "$2" in sql
    assert "%s" not in sql
    assert "ilike" in sql


def test_same_database_detection_uses_host_and_dbname():
    """同一个 host+dbname 必须被判为同库（防止把控制库当场景库 TRUNCATE）。"""
    assert _database_of("postgresql://u:p@h:5432/kagent") == ("h", "kagent")
    assert _database_of("postgresql://u:p@h:5432/kagent") == _database_of(
        "postgresql://x:y@h:5432/kagent"
    )
    assert _database_of("postgresql://u:p@h:5432/kagent") != _database_of(
        "postgresql://u:p@h:5432/kagent_finance"
    )


# ══════════════════════════════════════════════════════════════════════
# B. Real-DB 层
# ══════════════════════════════════════════════════════════════════════


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _seed_control(connection: asyncpg.Connection) -> dict[str, uuid.UUID]:
    """在控制库造两个 kb 的数据面行（kb2 用于验证过滤，不搬进场景库）。"""
    ids = {
        "tenant": uuid.uuid4(),
        "kb1": uuid.uuid4(),
        "kb2": uuid.uuid4(),
        "user": uuid.uuid4(),
        "doc1": uuid.uuid4(),
        "doc2": uuid.uuid4(),
        "chunk1": uuid.uuid4(),
        "chunk2": uuid.uuid4(),
        "term": uuid.uuid4(),
        "entity1": uuid.uuid4(),
        "entity2": uuid.uuid4(),
        "mention": uuid.uuid4(),
        "relation": uuid.uuid4(),
        "wiki": uuid.uuid4(),
        "job": uuid.uuid4(),
        "segment": uuid.uuid4(),
        "governance": uuid.uuid4(),
    }

    await connection.execute(
        "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
        ids["tenant"],
        "搬迁测试租户",
        f"migrate-{ids['tenant'].hex[:8]}",
    )
    await connection.execute(
        "INSERT INTO users (id, tenant_id, username, password_hash, display_name)"
        " VALUES ($1, $2, $3, $4, $5)",
        ids["user"],
        ids["tenant"],
        "migrate-user",
        "x",
        "搬迁测试用户",
    )
    for key, name in (("kb1", "搬迁库"), ("kb2", "对照库")):
        await connection.execute(
            "INSERT INTO knowledge_bases (id, tenant_id, name) VALUES ($1, $2, $3)",
            ids[key],
            ids["tenant"],
            f"{name}-{ids[key].hex[:8]}",
        )

    for doc_key, kb_key, filename in (
        ("doc1", "kb1", "alpha.txt"),
        ("doc2", "kb2", "beta.txt"),
    ):
        await connection.execute(
            "INSERT INTO documents"
            " (id, tenant_id, kb_id, doc_group_id, filename, ext, size_bytes,"
            "  storage_key, uploaded_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            ids[doc_key],
            ids["tenant"],
            ids[kb_key],
            uuid.uuid4(),
            filename,
            "txt",
            12,
            f"kb/{filename}",
            ids["user"],
        )

    for chunk_key, doc_key, kb_key, index, content, fts in (
        ("chunk1", "doc1", "kb1", 0, "alpha 关键词命中的正文", "alpha"),
        ("chunk2", "doc2", "kb2", 0, "beta 对照正文", "beta"),
    ):
        await connection.execute(
            "INSERT INTO chunks"
            " (id, tenant_id, kb_id, document_id, chunk_index, content, fts_text,"
            "  token_count, heading_path, level_rank, acl_tags, deny_subjects,"
            "  embedding_model, embedding_text_version, chunker_version)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
            ids[chunk_key],
            ids["tenant"],
            ids[kb_key],
            ids[doc_key],
            index,
            content,
            fts,
            8,
            f"章节-{index}",
            20,
            ["public"],
            [],
            "test-model",
            "v1",
            "v1",
        )

    await connection.execute(
        "INSERT INTO kb_terms (id, tenant_id, kb_id, term, kind) VALUES ($1, $2, $3, $4, $5)",
        ids["term"],
        ids["tenant"],
        ids["kb1"],
        "alpha",
        "term",
    )
    for key, name in (("entity1", "Alpha"), ("entity2", "Beta")):
        await connection.execute(
            "INSERT INTO graph_entities"
            " (id, tenant_id, kb_id, name, name_key, name_fts, entity_type)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)",
            ids[key],
            ids["tenant"],
            ids["kb1"],
            name,
            name.lower(),
            name.lower(),
            "TERM",
        )
    await connection.execute(
        "INSERT INTO graph_entity_mentions"
        " (id, tenant_id, kb_id, document_id, chunk_id, entity_id, surface,"
        "  extractor_model, prompt_version)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        ids["mention"],
        ids["tenant"],
        ids["kb1"],
        ids["doc1"],
        ids["chunk1"],
        ids["entity1"],
        "Alpha",
        "test-model",
        "v1",
    )
    await connection.execute(
        "INSERT INTO graph_relations"
        " (id, tenant_id, kb_id, src_entity_id, dst_entity_id, relation,"
        "  document_id, extractor_model, prompt_version)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        ids["relation"],
        ids["tenant"],
        ids["kb1"],
        ids["entity1"],
        ids["entity2"],
        "related_to",
        ids["doc1"],
        "test-model",
        "v1",
    )
    await connection.execute(
        "INSERT INTO wiki_pages"
        " (id, tenant_id, kb_id, document_id, title, summary, generator_model,"
        "  prompt_version, content_hash)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        ids["wiki"],
        ids["tenant"],
        ids["kb1"],
        ids["doc1"],
        "Alpha 导航页",
        "摘要",
        "test-model",
        "v1",
        "hash-1",
    )
    await connection.execute(
        "INSERT INTO parse_jobs (id, tenant_id, document_id) VALUES ($1, $2, $3)",
        ids["job"],
        ids["tenant"],
        ids["doc1"],
    )
    await connection.execute(
        "INSERT INTO parse_segments (id, job_id, segment_index, page_start, page_end)"
        " VALUES ($1, $2, $3, $4, $5)",
        ids["segment"],
        ids["job"],
        0,
        1,
        1,
    )
    await connection.execute(
        "INSERT INTO governance_jobs (id, tenant_id, document_id) VALUES ($1, $2, $3)",
        ids["governance"],
        ids["tenant"],
        ids["doc1"],
    )
    return ids


async def _insert_extra_document(
    connection: asyncpg.Connection,
    ids: dict[str, uuid.UUID],
    kb_id: uuid.UUID,
    filename: str,
) -> uuid.UUID:
    """在场景库新增一篇文档（模拟切绑后的增量写入）。"""
    doc_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO documents"
        " (id, tenant_id, kb_id, doc_group_id, filename, ext, size_bytes,"
        "  storage_key, uploaded_by)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        doc_id,
        ids["tenant"],
        kb_id,
        uuid.uuid4(),
        filename,
        "txt",
        3,
        f"kb/{filename}",
        ids["user"],
    )
    return doc_id


async def _run_roundtrip() -> None:
    base_url = SCENARIO_TEST_DB_URL or ""
    suffix = uuid.uuid4().hex[:12]
    control_name = f"mig_ctrl_{suffix}"
    scene_name = f"mig_scene_{suffix}"
    admin_dsn = _with_database(dsn(base_url), "postgres")
    control_url = _with_database(base_url, control_name)
    scene_url = _with_database(base_url, scene_name)
    control_dsn = _with_database(dsn(base_url), control_name)
    scene_dsn = _with_database(dsn(base_url), scene_name)

    admin = await asyncpg.connect(admin_dsn)
    created: list[str] = []
    try:
        for name in (control_name, scene_name):
            try:
                await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
            except asyncpg.exceptions.InsufficientPrivilegeError as exc:
                pytest.skip(f"当前账号无 CREATE DATABASE 权限（{exc}）")
            created.append(name)

        for url in (control_url, scene_url):
            await asyncio.to_thread(command.upgrade, alembic_config(url), "head")

        control = await asyncpg.connect(control_dsn)
        try:
            ids = await _seed_control(control)
        finally:
            await control.close()

        # 1) 场景库未做手术：必须拒绝（跨库外键会让 COPY 失败）
        with pytest.raises(MigrateError) as unprepared:
            await migrate_forward(control_dsn, scene_dsn, str(ids["kb1"]))
        assert unprepared.value.code == MIGRATE_TARGET_NOT_PREPARED

        await apply_surgery(scene_dsn)

        # 2) 源库该 kb 没有文档：必须拒绝（常见于 --kb-id 写错）
        with pytest.raises(MigrateError) as empty:
            await migrate_forward(control_dsn, scene_dsn, str(uuid.uuid4()))
        assert empty.value.code == MIGRATE_SOURCE_EMPTY

        # 3) 正向搬迁 + 检索等价比对
        report = await migrate_forward(
            control_dsn, scene_dsn, str(ids["kb1"]), probe_term="alpha"
        )
        assert report["ok"] is True
        assert report["equivalence"]["ran"] is True
        assert report["equivalence"]["ok"] is True
        assert report["equivalence"]["control"]["row_count"] >= 1
        assert all(item["ok"] for item in report["tables"])

        scene = await asyncpg.connect(scene_dsn)
        try:
            assert await scene.fetchval(
                "SELECT count(*) FROM documents"
            ) == 1
            assert await scene.fetchval(
                "SELECT count(*) FROM documents WHERE kb_id <> $1", ids["kb1"]
            ) == 0
            assert await scene.fetchval(
                "SELECT count(*) FROM chunks"
            ) == 1
            # 生成列必须由目标库重算（COPY 不写 fts_vec）
            assert await scene.fetchval(
                "SELECT fts_vec IS NOT NULL FROM chunks"
            ) is True
            assert await scene.fetchval(
                "SELECT count(*) FROM parse_segments"
            ) == 1
        finally:
            await scene.close()

        # 4) 幂等：同一 kb 重跑，行数与摘要不变
        rerun = await migrate_forward(control_dsn, scene_dsn, str(ids["kb1"]))
        assert rerun["ok"] is True
        assert {
            item["table"]: item["target"] for item in rerun["tables"]
        } == {item["table"]: item["source"] for item in rerun["tables"]}

        # 5) 目标库已存在别家 kb 的数据：必须拒绝（不许静默 TRUNCATE 掉）
        scene = await asyncpg.connect(scene_dsn)
        try:
            foreign_doc = await _insert_extra_document(
                scene, ids, ids["kb2"], "foreign.txt"
            )
        finally:
            await scene.close()
        with pytest.raises(MigrateError) as foreign:
            await migrate_forward(control_dsn, scene_dsn, str(ids["kb1"]))
        assert foreign.value.code == MIGRATE_TARGET_HAS_FOREIGN_KB
        scene = await asyncpg.connect(scene_dsn)
        try:
            await scene.execute("DELETE FROM documents WHERE id = $1", foreign_doc)
        finally:
            await scene.close()

        # 6) 回滚回拷：场景库新增行增量回控制库，只增不删、可重跑
        scene = await asyncpg.connect(scene_dsn)
        try:
            new_doc = await _insert_extra_document(scene, ids, ids["kb1"], "new.txt")
            new_chunk = uuid.uuid4()
            await scene.execute(
                "INSERT INTO chunks"
                " (id, tenant_id, kb_id, document_id, chunk_index, content,"
                "  token_count, level_rank, acl_tags, deny_subjects,"
                "  embedding_model, embedding_text_version, chunker_version)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
                new_chunk,
                ids["tenant"],
                ids["kb1"],
                new_doc,
                0,
                "新增正文",
                4,
                20,
                ["public"],
                [],
                "test-model",
                "v1",
                "v1",
            )
        finally:
            await scene.close()

        reverse = await migrate_reverse(scene_dsn, control_dsn, str(ids["kb1"]))
        by_table = {item["table"]: item for item in reverse["tables"]}
        assert by_table["documents"]["inserted"] == 1
        assert by_table["chunks"]["inserted"] == 1
        assert by_table["documents"]["existing"] == 1

        control = await asyncpg.connect(control_dsn)
        try:
            assert await control.fetchval(
                "SELECT count(*) FROM documents WHERE id = $1", new_doc
            ) == 1
        finally:
            await control.close()

        repeat = await migrate_reverse(scene_dsn, control_dsn, str(ids["kb1"]))
        assert all(item["inserted"] == 0 for item in repeat["tables"])
    finally:
        for name in created:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


@requires_db
def test_real_db_migration_roundtrip():
    """临时双库上的完整往返：拒绝 → 正向校验 → 幂等 → 别家 kb → 回滚回拷。"""
    asyncio.run(_run_roundtrip())
