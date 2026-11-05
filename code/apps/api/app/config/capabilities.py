"""能力快照与启动横幅 —— 「这台机器现在开着哪些能力、按什么口径、数据准备到哪一步」。

三处共用同一份实现（避免三份文案各自漂移）：
  ① 启动横幅（api 的 lifespan / worker 的 on_startup）：人可读块 + 一条结构化 JSON；
  ② `/api/system/capabilities`（管理端「系统能力」页数据源，第 2 步接端点）；
  ③ caliber 快照（每条答案的审计留痕）。

★ 为什么横幅要「人可读 + JSON」两份：
  人可读块给 `docker logs` 第 1 屏（部署时肉眼确认）；JSON 给 ELK / Loki
  （按 event=capability.banner 检索，且带 config_fingerprint 供两侧比对）。
  ⚠ 只打人可读块时，排障要靠翻屏幕；只打 JSON 时，部署评审没人看得懂。

★ 为什么横幅必须包含 config_fingerprint（§7.4 的可发现性机制）：
  抽取在 worker、召回在 api，两者运行期不通信。只重建一侧容器 / .env 没同步时，
  唯一能发现不一致的地方就是「比对两侧启动横幅的指纹」—— 这是刻意的运维约束，
  不是日志装饰。

★ 关闭态不查库：能力关闭时不产生任何数据库访问（连数据就绪度都不查），
  与「未引入本能力」逐字节等价。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.config_profile import config_fingerprint, runtime_config_snapshot
from app.config.graph_profile import current_graph_profile
from app.config.settings import get_settings
from app.config.wiki_profile import current_wiki_profile

logger = logging.getLogger(__name__)

_SEP: Final[str] = "=" * 80
_SUB: Final[str] = "-" * 80

# 能力名称在横幅里对齐用（中文按显示宽度补齐，CJK 字符占 2 列）
_GRAPH_LABEL: Final[str] = " 实体图召回（GraphRAG） : "
_WIKI_LABEL: Final[str] = " LLM wiki 导航         : "


def detect_role() -> str:
    """当前进程角色：'worker' / 'api'。

    ★ 为什么从 sys.argv[0] 推：api 是 uvicorn、worker 是 arq，两者没有共同的环境
      标识；而 role 只用于日志字段（让人一眼看出这条横幅来自哪一侧），
      推错的后果仅是可读性，不是语义。判据取 argv[0] 的 basename，两者都不含对方名字。
    """
    name = (sys.argv[0] or "").lower()
    return "worker" if "arq" in name else "api"


async def collect_graph_data_stats(session: AsyncSession) -> dict[str, int]:
    """图数据就绪度与规模（仅在 GRAPH_ENABLED=true 时调用）。

    ★ 「已完成」MUST 按**当前口径**统计，而不是只看 graph_status='done'：
      换抽取模型 / 改 prompt 后，旧口径的 done 会被检索直接排除（fail-closed），
      若横幅仍把它算作「已完成」，就会显示 100% 覆盖而图路实际全空 ——
      这正是「配了却不生效」的静默降级。所以口径不匹配的 done 计入「待补建」。

    ★ 分母 MUST 与**补建脚本的选取条件**同一批文档（`status='indexed'`）：
      图抽取的输入是 chunks，没有 chunks 的文档永远抽不出东西，脚本也永远不选它。
      曾用「全部未删除文档」当分母，结果一篇解析失败的 `e2e_spec.png`
      （status='failed'，0 分片）让就绪度永远停在 20/21 —— 闸门永远红，
      运维很快就学会无视它，这道门就白设了。
      未索引的文档单独计数（`documents_not_indexed`）：那是解析链路的问题，
      不该记在图抽取的账上，两者的修法也不同。
    """
    profile = current_graph_profile()
    params = {
        "m": profile.extractor_model,
        "pv": profile.prompt_version,
    }
    # 只统计「最新版本、未软删、且已解析完成」的文档：历史版本与已删除文档不该进
    # 就绪度分母（它们本来就不会被检索到，也不会被补建脚本选中）。
    rows = (
        await session.execute(
            text(
                "SELECT "
                "  count(*) AS total, "
                "  count(*) FILTER (WHERE graph_status = 'done' "
                "        AND graph_extractor_model = :m "
                "        AND graph_prompt_version = :pv) AS done, "
                "  count(*) FILTER (WHERE graph_status = 'failed') AS failed, "
                "  count(*) FILTER (WHERE graph_status = 'skipped') AS skipped "
                "FROM documents "
                "WHERE is_latest = true AND deleted_at IS NULL "
                "  AND status = 'indexed'"
            ),
            params,
        )
    ).one()
    total = int(rows.total or 0)
    done = int(rows.done or 0)
    failed = int(rows.failed or 0)
    skipped = int(rows.skipped or 0)
    # 未索引 = 解析还没成功（pending / parsing / failed）。单独取，不进上面的分母。
    not_indexed = int(
        (
            await session.scalar(
                text(
                    "SELECT count(*) FROM documents "
                    "WHERE is_latest = true AND deleted_at IS NULL "
                    "  AND status <> 'indexed'"
                )
            )
        )
        or 0
    )
    entities = int(
        (await session.scalar(text("SELECT count(*) FROM graph_entities"))) or 0
    )
    relations = int(
        (
            await session.scalar(
                text(
                    "SELECT count(*) FROM graph_relations "
                    "WHERE extractor_model = :m AND prompt_version = :pv"
                ),
                params,
            )
        )
        or 0
    )
    return {
        "documents_total": total,
        "documents_done": done,
        # 待补建 = 总数 − 已完成 − 失败 − 跳过：running 与 pending 都算「待补建」
        # （running 可能是崩溃残留，补建脚本会按阈值改选）。
        "documents_pending": max(0, total - done - failed - skipped),
        "documents_failed": failed,
        "documents_skipped": skipped,
        "documents_not_indexed": not_indexed,
        "entities": entities,
        "relations": relations,
    }


async def collect_wiki_data_stats(session: AsyncSession) -> dict[str, int]:
    """wiki 页统计（仅在 WIKI_ENABLED=true 时调用）。

    「已完成」按**当前口径**统计（generator_model + prompt_version 双匹配）：
    换生成模型 / bump prompt 后旧页会被 rebuild_wiki --recaliber 判为待重建，
    若仍把它们算作就绪，管理端会显示虚高的覆盖率 —— 与图就绪度的口径同理。
    """
    profile = current_wiki_profile()
    params = {"m": profile.generator_model, "pv": profile.prompt_version}
    row = (
        await session.execute(
            text(
                "SELECT "
                "  count(*) AS pages_total, "
                "  count(*) FILTER (WHERE generator_model = :m "
                "        AND prompt_version = :pv AND status = 'ready') AS ready, "
                "  count(*) FILTER (WHERE generator_model = :m "
                "        AND prompt_version = :pv AND status = 'stale') AS stale, "
                "  count(*) FILTER (WHERE status = 'failed') AS failed, "
                "  count(*) FILTER (WHERE NOT (generator_model = :m "
                "        AND prompt_version = :pv)) AS caliber_mismatch "
                "FROM wiki_pages"
            ),
            params,
        )
    ).one()
    return {
        "pages_total": int(row.pages_total or 0),
        "pages_ready": int(row.ready or 0),
        "pages_stale": int(row.stale or 0),
        "pages_failed": int(row.failed or 0),
        "pages_caliber_mismatch": int(row.caliber_mismatch or 0),
    }


def capability_snapshot(
    fingerprint: str,
    stats: dict[str, int] | None = None,
    wiki_stats: dict[str, int] | None = None,
) -> dict:
    """能力快照（§9.6 的结构，数据部分由 stats 注入）。

    stats 为 None（能力关闭）时数据字段全部为 None —— MUST NOT 用 0 冒充
    「没有数据」：0 会被读成「数据已就绪但没有内容」，None 才是「未启用 / 未统计」。
    """
    settings = get_settings()
    graph_profile = current_graph_profile()
    wiki_profile = current_wiki_profile()
    graph_enabled = settings.graph_enabled
    wiki_enabled = settings.wiki_enabled

    def _pick(key: str) -> int | None:
        return None if stats is None else stats.get(key)

    return {
        "config_fingerprint": fingerprint,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": [
            {
                "key": "graph",
                "label": "实体图召回（GraphRAG）",
                "enabled": graph_enabled,
                "deployment_fixed": True,
                "backend": settings.graph_store_backend if graph_enabled else None,
                "caliber": {
                    "extractor_model": graph_profile.extractor_model,
                    "prompt_version": graph_profile.prompt_version,
                },
                "runtime": {
                    "timeout_ms": settings.graph_retrieve_timeout_ms,
                    "top_k": decisions.GRAPH_RETRIEVE_TOP_K,
                    "max_hops": decisions.GRAPH_MAX_HOPS,
                    "max_recall_chunks": decisions.GRAPH_RECALL_MAX_CHUNKS,
                    # 离线抽取口径：换过这两个值，已抽的图可能不完整/不成立，
                    # 管理端「系统能力」页要把它们显示出来才解释得清覆盖率为啥不是 100%
                    "extract_timeout_seconds": settings.graph_extract_timeout_seconds,
                    "extract_max_output_tokens": settings.graph_extract_max_output_tokens,
                },
                "data": {
                    "documents_total": _pick("documents_total"),
                    "documents_done": _pick("documents_done"),
                    "documents_pending": _pick("documents_pending"),
                    "documents_failed": _pick("documents_failed"),
                    # 解析未完成的文档不参与图抽取（见 collect_graph_data_stats），
                    # 前端要能把它们和「待补建」区分开，否则运维会去跑补建脚本修解析问题
                    "documents_not_indexed": _pick("documents_not_indexed"),
                    "entities": _pick("entities"),
                    "relations": _pick("relations"),
                },
                "rebuild": {
                    "on_toggle": "补建实体抽取：python -m scripts.backfill_graph --all",
                    "not_needed": "无需重跑解析、分块、嵌入",
                },
            },
            {
                "key": "wiki",
                "label": "LLM wiki 导航",
                "enabled": wiki_enabled,
                "deployment_fixed": True,
                "scope": settings.wiki_scope if wiki_enabled else None,
                "caliber": {
                    "generator_model": wiki_profile.generator_model,
                    "prompt_version": wiki_profile.prompt_version,
                },
                "runtime": {"input_max_chars": settings.wiki_input_max_chars},
                # 页面统计（§9.6 ①）：None = 未启用 / 未统计（MUST NOT 用 0 冒充）
                "data": {
                    "pages_total": None if wiki_stats is None else wiki_stats.get("pages_total"),
                    "pages_ready": None if wiki_stats is None else wiki_stats.get("pages_ready"),
                    "pages_stale": None if wiki_stats is None else wiki_stats.get("pages_stale"),
                    "pages_failed": None if wiki_stats is None else wiki_stats.get("pages_failed"),
                    "pages_caliber_mismatch": (
                        None if wiki_stats is None else wiki_stats.get("pages_caliber_mismatch")
                    ),
                },
                "rebuild": {
                    "on_toggle": "重建 wiki 页：python -m scripts.rebuild_wiki --all",
                    "not_needed": "无需重跑解析、分块、嵌入、图抽取",
                },
            },
        ],
    }


def _render_text(
    *, role: str, app_env: str, fingerprint: str,
    graph_enabled: bool, wiki_enabled: bool, stats: dict[str, int] | None,
) -> str:
    """人可读横幅（文案照设计文档 §12.1，逐字；只替换数值占位）。"""
    settings = get_settings()
    graph_profile = current_graph_profile()
    wiki_profile = current_wiki_profile()
    lines: list[str] = [
        _SEP,
        f" kagent 能力快照 · app_env={app_env} · config_fingerprint={fingerprint}",
        _SUB,
    ]

    if graph_enabled:
        lines.append(f"{_GRAPH_LABEL}已启用   backend={settings.graph_store_backend}")
        lines.append(
            f"                          口径 = {graph_profile.extractor_model}"
            f" | {graph_profile.prompt_version}"
        )
        lines.append(
            f"                          在线 = timeout {settings.graph_retrieve_timeout_ms}ms"
            f" · top_k {decisions.GRAPH_RETRIEVE_TOP_K}"
            f" · max_hops {decisions.GRAPH_MAX_HOPS}"
            f" · 入上下文 ≤{decisions.GRAPH_RECALL_MAX_CHUNKS} 片"
        )
        # ★ 离线抽取的两个上限也必须露出来：截断类故障的唯一有效线索就是
        #   「输出 ≤N tokens」这个数。不显示时，抓到的错误只有「LLM 输出非法 JSON」，
        #   运维会去改 prompt / 换模型，而真正要动的是这个环境变量。
        lines.append(
            f"                          离线 = timeout"
            f" {settings.graph_extract_timeout_seconds:g}s"
            f" · 输出 ≤{settings.graph_extract_max_output_tokens} tokens"
            f" · {decisions.GRAPH_EXTRACT_BATCH_CHUNKS} 片/请求"
        )
    else:
        lines.append(f"{_GRAPH_LABEL}未启用（GRAPH_ENABLED=false）")

    if wiki_enabled:
        lines.append(f"{_WIKI_LABEL}已启用   scope={settings.wiki_scope}")
        lines.append(
            f"                          口径 = {wiki_profile.generator_model}"
            f" | {wiki_profile.prompt_version}"
        )
    else:
        lines.append(f"{_WIKI_LABEL}未启用（WIKI_ENABLED=false）")

    if not graph_enabled and not wiki_enabled:
        # 能力全关（默认态）：明写既有链路不变，避免运维以为「开了什么」
        lines.append(
            " 知识检索              : 向量 + 词元 tsquery + 词元 ILIKE 三路 + RRF（既有链路不变）"
        )
    elif stats is not None:
        total = stats["documents_total"]
        done = stats["documents_done"]
        skipped = stats["documents_skipped"]
        # 覆盖率分母 = 已索引 − 跳过：skipped 是「明确不该抽」（零分片 / 超分片上限），
        # 算作**已处理**。把它留在分母里会让就绪度永远差那几个百分点，
        # 于是没人再去看这个数字。
        effective = total - skipped
        coverage = f"{(done * 100.0 / effective):.1f}% 覆盖" if effective > 0 else "0% 覆盖"
        lines.append(
            f" 图数据就绪度           : {done}/{total} 篇已抽取"
            f" · 待补建 {stats['documents_pending']}"
            f" · 失败 {stats['documents_failed']}（{coverage}）"
        )
        if stats.get("documents_not_indexed"):
            # 单独一行，且写明「不参与」：这是解析链路的问题，跑补建脚本修不了它。
            lines.append(
                f"                          另 {stats['documents_not_indexed']} 篇未索引，"
                "不参与图抽取（解析未完成，先修解析链路）"
            )
        lines.append(
            f" 图规模                : 实体 {stats['entities']}"
            f" · 关系 {stats['relations']}"
        )

    lines.append(_SUB)
    if graph_enabled or wiki_enabled:
        lines.extend(
            [
                " [部署期固定] 图与 wiki 只由环境变量决定：没有运行时开关、没有请求级开关、",
                "              不按用户/部门放量。改了 MUST 重启 api 与 worker 两个容器。",
                " [改了要重做什么]",
                "   · 开/关图           → 补建实体抽取（scripts/backfill_graph.py）",
                "                          不用重跑：解析、分块、嵌入",
                "   · 换抽取模型/prompt → 重抽（旧口径会被检索直接排除）",
                "                          不用重跑：解析、分块、嵌入",
                "   · 开/关 wiki        → 重建 wiki 页（scripts/rebuild_wiki.py）",
                "                          不用重跑：解析、分块、嵌入、图抽取",
                "   · 换分块器          → 全链重跑：解析 → 分块 → 嵌入 → 抽取 → wiki",
                "   · 换嵌入口径        → 只需重算向量（chunk 行不变，不用重抽图与 wiki）",
                " [改之前请先读] code/docs/DEPLOY.md §3.5「能力开关与重跑口径」",
                f" [一致性] 另一侧（{'worker' if role == 'api' else 'api'}）的启动横幅"
                "必须打印同一个 config_fingerprint",
            ]
        )
    else:
        lines.extend(
            [
                " [部署期固定] 这两项能力只由环境变量决定，运行期不可切换、不按用户放量。",
                "              如需启用：按 code/docs/DEPLOY.md §3.5 设置环境变量，",
                "              并**同时重建 api 与 worker 两个镜像**（只重建一侧会静默跑旧配置）。",
                "              启用后需要补建/重建数据，不是「改个变量就自动生效」。",
            ]
        )
    lines.append(_SEP)
    return "\n".join(lines)


async def log_capability_banner(role: str | None = None) -> dict:
    """打印启动能力横幅（人可读 + 结构化 JSON），返回能力快照。

    ★ 关闭态不查库：只有 graph_enabled 为真时才去统计就绪度 —— 关闭能力不应因为
      「迁移还没跑 / 库连不上」而让启动失败或变慢。
    """
    settings = get_settings()
    role = role or detect_role()
    snapshot = runtime_config_snapshot()
    fingerprint = config_fingerprint(snapshot)

    stats: dict[str, int] | None = None
    if settings.graph_enabled:
        # 延迟导入：关闭能力时不引入数据库模块（保持关闭态零副作用）
        from app.database import SessionLocal

        try:
            async with SessionLocal() as session:
                stats = await collect_graph_data_stats(session)
        except Exception:
            # 就绪度统计失败不该阻断启动（表缺失已由 check_capabilities 拦在前面，
            # 能走到这里说明是别的原因：库抖动 / 权限）。横幅照打，数据字段留空。
            logger.warning("图数据就绪度统计失败（横幅仍输出）", exc_info=True)

    wiki_stats: dict[str, int] | None = None
    if settings.wiki_enabled:
        # 延迟导入同上：关闭态不引入数据库模块
        from app.database import SessionLocal

        try:
            async with SessionLocal() as session:
                wiki_stats = await collect_wiki_data_stats(session)
        except Exception:
            logger.warning("wiki 页统计失败（横幅仍输出）", exc_info=True)

    text_block = _render_text(
        role=role,
        app_env=settings.app_env,
        fingerprint=fingerprint,
        graph_enabled=settings.graph_enabled,
        wiki_enabled=settings.wiki_enabled,
        stats=stats,
    )
    # print 而非 logger：横幅要出现在 `docker logs` 第一屏的原始 stdout 里，
    # 且不被日志级别过滤（LOG_LEVEL=WARNING 时更不能把「能力已开启」这条吃掉）。
    print(text_block, flush=True)

    graph_profile = current_graph_profile()
    wiki_profile = current_wiki_profile()
    logger.info(
        "启动能力快照",
        extra={
            "event": "capability.banner",
            "role": role,
            "app_env": settings.app_env,
            "config_fingerprint": fingerprint,
            "graph": {
                "enabled": settings.graph_enabled,
                "backend": settings.graph_store_backend if settings.graph_enabled else None,
                "extractor_model": graph_profile.extractor_model,
                "prompt_version": graph_profile.prompt_version,
                "timeout_ms": settings.graph_retrieve_timeout_ms,
                "top_k": decisions.GRAPH_RETRIEVE_TOP_K,
                "max_hops": decisions.GRAPH_MAX_HOPS,
                # 离线抽取的两个上限：出现「抽取失败但文档可检索」时，
                # 先看这两个数对不对得上语料规模（4 片/请求 × 8K 是默认口径）
                "extract_timeout_seconds": settings.graph_extract_timeout_seconds,
                "extract_max_output_tokens": settings.graph_extract_max_output_tokens,
                "docs_total": None if stats is None else stats["documents_total"],
                "docs_done": None if stats is None else stats["documents_done"],
                "docs_pending": None if stats is None else stats["documents_pending"],
                "docs_failed": None if stats is None else stats["documents_failed"],
                "docs_not_indexed": (
                    None if stats is None else stats["documents_not_indexed"]
                ),
            },
            "wiki": {
                "enabled": settings.wiki_enabled,
                "scope": settings.wiki_scope if settings.wiki_enabled else None,
                "generator_model": wiki_profile.generator_model,
                "prompt_version": wiki_profile.prompt_version,
                "pages_total": (
                    None if wiki_stats is None else wiki_stats.get("pages_total")
                ),
                "pages_ready": (
                    None if wiki_stats is None else wiki_stats.get("pages_ready")
                ),
                "pages_caliber_mismatch": (
                    None if wiki_stats is None else wiki_stats.get("pages_caliber_mismatch")
                ),
            },
        },
    )
    return capability_snapshot(fingerprint, stats, wiki_stats)
