"""容量看板（Step 7 S7-6）：M_A 全景下界、库观测与阈值现状的**只读**快照。

★ 本模块的职责只有一个：把「我们**知道**什么」和「我们**不知道**什么」如实分开。
  所以每个常量都带 `calibrated=false`，每条阈值都带 `evaluable` 与 `over`，
  无法评估时 `over` 是 `None` 而不是 `False` —— `False` 会被读成「已评估且没超」，
  那是最危险的假信号（看起来一切正常，实际上从没测过）。

★ 为什么所有算术只有一个入口（`compute_m_a`）：
  `total_lower_mb` 与页面上的公式必须同源。各写一份算术，改了一处另一处就会
  悄悄给出旧数字，而两处都不会报错。

★ `M_A` 是**下界**不是实测占用：
  - `M_shared` 只含 embedding + reranker 两个模型服务的 mem_limit 声明值，
    其余常驻服务（postgres/redis/minio/ocr/api/mcp/worker/web）没有内存上限，
    它们的实际占用**无法从上界推出来**；
  - `M_control_pg` 只含控制库的 `shared_buffers`，不含连接、扩展、缓存、WAL 等；
  - `M_scene_pg` 是框架示例里的假设，**没有任何实测数据**。
  因此 `lower_bound_only=True`，任何「容量还够」的结论都不能只靠这张看板。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import placeholders
from app.infra import db_router
from app.infra import metrics

M_SHARED_DECLARED_MB: Final[int] = 8192
"""两个模型服务的**声明**内存之和：embedding 4g + reranker 4g。

★ 来源：`docker-compose.prod.yml` 里 embedding 与 reranker 各自的 `mem_limit: 4g`。
  这是编排**声明值**，不是实测驻留（稳态实测分别约 1.4GB / 2.4GB，见编排注释）。
★ 口径提醒：`docker-compose.yml`（dev）把 4g 设在 reranker 与 ocr 上、embedding 未设限。
  两套编排的「哪两个服务有上限」不同，但**总量**仍是两处 4g；`UNBOUNDED_SERVICES`
  按 prod 编排列名（见下方常量注释），看板数字不区分环境，读数时需知道自己部署的是哪套。
★ 它**不含**其余常驻服务 —— 那 8 个服务没有设上限（见 `UNBOUNDED_SERVICES`）。
"""

M_CONTROL_PG_LOWER_MB: Final[int] = 128
"""控制库 PostgreSQL 侧的下界：仅 `shared_buffers`。

★ 128 是 PG 的默认值，同时是 `compute_m_a` 的默认入参与登记镜像值。
★ 运行期的真实值一律以 `current_setting('shared_buffers')` 为准；解析失败直接报错，
  **没有**「读不到就用 128」的兜底 —— 那会让看板展示一个不存在的值。
★ 未标定：本项目从未在目标机器上测过 shared_buffers 是否需要调大。
"""

M_SCENE_PG_MB: Final[int] = 300
"""每个热库（hot）在 PostgreSQL 侧预留的内存。

★ 来源：框架示例里的假设值，**没有任何实测依据**。
★ 未标定：不同数据量 / 索引规模下差异会很大，标定前不要用它做采购决策。
"""

UNBOUNDED_SERVICES: Final[tuple[str, ...]] = (
    "postgres",
    "redis",
    "minio",
    "ocr",
    "api",
    "mcp",
    "worker",
    "web",
)
"""编排里**没有** `mem_limit` 的常驻服务（`migrate` 是一次性任务，不算常驻）。

★ 按 `docker-compose.prod.yml` 列名：prod 给 embedding / reranker 设了 4g，其余 8 个
  常驻服务未设限；dev 编排给 reranker / ocr 设了 4g，因此 dev 下 `embedding` 也未设限。
  这里选 prod 口径与 `M_SHARED_DECLARED_MB` 的来源保持一致，避免两处各说一套。
★ 为什么要在看板上列出它们：M_A 只加得进「已声明」的内存，未设上限的服务
  可以吃掉宿主机剩余内存 —— 不列出来，读的人会以为 M_A 就是总量。
"""

_MIB: Final[int] = 1024 * 1024
_PG_BLOCK_BYTES: Final[int] = 8192
"""PG 无单位参数的换算单位：一个块 8kB（`shared_buffers` 无单位时按块数计）。"""

_UNIT_FACTORS_MB: Final[dict[str, float]] = {
    "b": 1.0 / _MIB,
    "kb": 1.0 / 1024.0,
    "mb": 1.0,
    "gb": 1024.0,
    "tb": 1024.0 * 1024.0,
}


def compute_m_a(n_hot_effective: int, *, control_pg_mb: int = M_CONTROL_PG_LOWER_MB) -> int:
    """M_A 全景下界（MB）：`M_shared + M_control_pg + N_hot_effective × M_scene_pg`。

    ★ 控制库内存作为关键字参数：运行时 `shared_buffers` 可能不等于 128，
      看板必须用读到的真实值算，而不是把 128 再写死一遍。
    """
    return M_SHARED_DECLARED_MB + control_pg_mb + n_hot_effective * M_SCENE_PG_MB


def _parse_shared_buffers_mb(raw: str) -> int:
    """把 `current_setting('shared_buffers')` 的原始字符串解析成 MB。

    支持 `"128MB"` / `"1GB"` / `"16384kB"` / `"8192"`（无单位按 8kB 块数换算），
    单位大小写不敏感。

    ★ 解析失败或结果 <= 0 **直接抛 ValueError**，不做任何兜底：
      把读不懂的配置当成 128 会让看板显示一个不存在的值，而没有任何信号。
    """
    value = raw.strip().lower()
    if not value:
        raise ValueError(f"无法解析 shared_buffers：{raw!r}")

    unit = ""
    for candidate in ("tb", "gb", "mb", "kb", "b"):
        if value.endswith(candidate):
            unit = candidate
            value = value[: -len(candidate)].strip()
            break

    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"无法解析 shared_buffers：{raw!r}") from exc

    if unit:
        megabytes = number * _UNIT_FACTORS_MB[unit]
    else:
        megabytes = number * _PG_BLOCK_BYTES / _MIB

    result = int(megabytes)
    if result <= 0:
        raise ValueError(f"shared_buffers 解析结果必须为正：{raw!r} -> {result}MB")
    return result


async def _collect_tier_counts(session: AsyncSession) -> tuple[dict[str, int], int]:
    """读场景分级计数：返回 `(tier_counts, n_hot_declared)`，缺失档位补 0。"""
    rows = (
        await session.execute(text("SELECT tier, count(*) AS n FROM scenarios GROUP BY tier"))
    ).all()
    counts = {"cold": 0, "warm": 0, "hot": 0}
    for row in rows:
        tier = row[0]
        if tier in counts:
            counts[tier] = int(row[1])
    return counts, counts["hot"]


async def _collect_databases(session: AsyncSession) -> list[dict]:
    """按**注册键顺序**输出各库位的观测（大小与缓存命中率）。

    ★ 库名一律经 `db_router.database_name_for` 从 DSN 解析，查询用
      `bindparam(expanding=True)` 绑定，不手拼 SQL（库名可能含 `-` 等字符）。
    ★ 某注册库在 `pg_stat_database` 无行（比如 DSN 指向别的集群）时，
      数值字段一律 `None` 并附 `note` —— 填 0 会被读成「库是空的、命中率是 0」。
    """
    keys = db_router.registered_db_keys()
    names = [db_router.database_name_for(key) for key in keys]

    stmt = text(
        """
        SELECT datname, pg_database_size(datname) AS size_bytes, blks_hit, blks_read
        FROM pg_stat_database
        WHERE datname IN :names
        """
    ).bindparams(bindparam("names", expanding=True))
    rows = (await session.execute(stmt, {"names": names})).all()
    by_name = {row[0]: row for row in rows}

    databases: list[dict] = []
    for db_key, database in zip(keys, names, strict=True):
        row = by_name.get(database)
        if row is None:
            databases.append(
                {
                    "db_key": db_key,
                    "database": database,
                    "size_bytes": None,
                    "size_mb": None,
                    "blks_hit": None,
                    "blks_read": None,
                    "hit_rate": None,
                    "note": "pg_stat_database 无该库记录",
                }
            )
            continue
        size_bytes = int(row[1])
        blks_hit = int(row[2])
        blks_read = int(row[3])
        denominator = blks_hit + blks_read
        databases.append(
            {
                "db_key": db_key,
                "database": database,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / _MIB, 3),
                "blks_hit": blks_hit,
                "blks_read": blks_read,
                # 分母为 0 ⇒ None（不是 0）：0 会被读成「命中率是 0%」
                "hit_rate": (blks_hit / denominator) if denominator > 0 else None,
            }
        )
    return databases


def _build_thresholds(databases: list[dict]) -> list[dict]:
    """四条阈值现状：每条都声明 `calibrated=False`，不可评估时 `over=None`。"""
    sampled = [item for item in databases if item["hit_rate"] is not None]
    if sampled:
        lowest = min(sampled, key=lambda item: item["hit_rate"])
        observed = lowest["hit_rate"]
        database = lowest["database"]
    else:
        observed = None
        database = None
    evaluable = observed is not None

    return [
        {
            "name": "db_cache_hit_rate",
            "threshold": 0.95,
            "unit": "ratio",
            "calibrated": False,
            "evaluable": evaluable,
            "over": (observed < 0.95) if evaluable else None,
            "observed": observed,
            "database": database,
            "reason": "有读写样本即可评估；阈值 0.95 本身待标定",
        },
        {
            "name": "db_size_vs_instance_memory",
            "threshold": 0.30,
            "unit": "ratio",
            "calibrated": False,
            "evaluable": False,
            "over": None,
            "reason": "实例内存未知，无法确定 30% 的分母",
        },
        {
            "name": "retrieval_p95_vs_baseline",
            "threshold": 1.5,
            "unit": "x_baseline",
            "calibrated": False,
            "evaluable": False,
            "over": None,
            "reason": "无基线存储，P95 基线从未标定",
        },
        {
            "name": "independent_rate_limit",
            "threshold": None,
            "unit": None,
            "calibrated": False,
            "evaluable": False,
            "over": None,
            "reason": "独立限流未实现，本期只有连接预算闸门",
        },
    ]


async def capacity_snapshot(session: AsyncSession) -> dict:
    """容量看板快照（只读 SELECT，无副作用）。

    数据来源：
      1. `scenarios` 的 tier 计数（`N_hot_declared`）；
      2. `current_setting('shared_buffers')`（控制库 PG 侧下界的实测值）；
      3. `pg_stat_database`（各注册库的大小与块读写计数）；
      4. 进程内 HTTP 直方图 `http.request.duration`（全进程 P95）。

    ★ `n_hot_effective` 固定 0：hot 档本期未实现，**不把 hot 偷算成 warm**。
      声明了 hot 但还没生效时，公式按 0 算并在 `hot_unsupported` 里明示。
    """
    tier_counts, n_hot_declared = await _collect_tier_counts(session)
    n_hot_effective = 0

    raw_shared_buffers = await session.scalar(text("SELECT current_setting('shared_buffers')"))
    raw = str(raw_shared_buffers)
    control_pg_mb = _parse_shared_buffers_mb(raw)

    databases = await _collect_databases(session)
    thresholds = _build_thresholds(databases)

    p95 = metrics.quantile_ms("http.request.duration", 0.95)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "formula": "M_A(N) = M_shared + M_control_pg + N_hot_effective × M_scene_pg",
        "constants": {
            "M_SHARED_DECLARED_MB": {
                "value": M_SHARED_DECLARED_MB,
                "calibrated": False,
                "source": (
                    "docker-compose.prod.yml 中 embedding 与 reranker 各 mem_limit 4g "
                    "的声明值之和（4g+4g），非实测驻留"
                ),
            },
            "M_CONTROL_PG_LOWER_MB": {
                "value": M_CONTROL_PG_LOWER_MB,
                "calibrated": False,
                "source": (
                    "PG 默认 shared_buffers 的下界；运行时以 "
                    "current_setting('shared_buffers') 覆盖"
                ),
            },
            "M_SCENE_PG_MB": {
                "value": M_SCENE_PG_MB,
                "calibrated": False,
                "source": "框架示例假设每个热库 300MB，无实测依据",
            },
        },
        "unbounded_services": list(UNBOUNDED_SERVICES),
        "inputs": {
            "m_shared_declared_mb": M_SHARED_DECLARED_MB,
            "m_control_pg_lower_mb": control_pg_mb,
            "m_control_pg_shared_buffers_raw": raw,
            "m_scene_pg_mb": M_SCENE_PG_MB,
            "n_hot_declared": n_hot_declared,
            "n_hot_effective": n_hot_effective,
            "tier_counts": tier_counts,
        },
        "result": {
            # 数据库进程侧下界：控制库 shared_buffers + 生效热库
            "postgres_side_lower_mb": control_pg_mb + n_hot_effective * M_SCENE_PG_MB,
            # M_A 全景下界：再加上两个模型服务的声明内存
            "total_lower_mb": compute_m_a(n_hot_effective, control_pg_mb=control_pg_mb),
            "calibrated": False,
            "lower_bound_only": True,
            "note": "只含 shared_buffers 与已声明内存；不含连接、扩展、未设上限服务开销",
        },
        "hot_unsupported": True,
        "databases": databases,
        "latency": {
            "p95_ms": p95,
            "metric": "http.request.duration",
            "scope": "process-wide",
            "note": "全进程所有 HTTP 请求的 P95，不是按库或按检索链路的延迟",
        },
        "thresholds": thresholds,
    }


# ── 站位值一致性断言（import 期，fail-fast；与 policy_spec.py 同款）──────
# 登记表里的 value 是**镜像**，不是真源：只查名字不够，改常量忘改镜像时
# 横幅会继续显示旧值且永不报错 —— 所以这里同时比对值。
_CAPACITY_PLACEHOLDERS: Final[dict[str, int]] = {
    "CAPACITY_M_SHARED_DECLARED_MB": M_SHARED_DECLARED_MB,
    "CAPACITY_M_CONTROL_PG_LOWER_MB": M_CONTROL_PG_LOWER_MB,
    "CAPACITY_M_SCENE_PG_MB": M_SCENE_PG_MB,
}

_capacity_registered_names = placeholders.names_for("capacity")
for _placeholder_name, _placeholder_value in _CAPACITY_PLACEHOLDERS.items():
    if _placeholder_name not in _capacity_registered_names:
        raise RuntimeError(
            f"{_placeholder_name} 是站位值，但未登记在 app/config/placeholders.py —— "
            "未登记的站位值不会出现在启动横幅"
        )
    _mirror_value = placeholders.value_of(_placeholder_name)
    if _mirror_value != _placeholder_value:
        raise RuntimeError(
            f"app/config/placeholders.py 里 {_placeholder_name} 的镜像值 "
            f"{_mirror_value!r} 与本模块的 {_placeholder_value!r} 不一致 —— "
            "启动横幅会显示一个不存在的值"
        )
