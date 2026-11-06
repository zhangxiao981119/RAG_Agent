"""启动期能力自检（C1~C8）—— fail-closed 的完整定义。

放在独立模块（不进 decisions.self_check）的唯一原因：**本自检要查库**。
decisions 里的断言是纯常量校验（无 IO、可单测、可在脚本里跑），混进数据库访问会
让它变成「跑一次要连库」的东西，进而在 CI / 离线脚本里失败。

★ 为什么这里的检查是「拒绝启动」，而既有的索引口径 / schema 版本检查只「告警」：
  两者的判别标准不同（设计文档 §7.2）：
    · 既有检查的对象是「**数据是否过期**」—— 旧 chunk 仍然可用，服务应继续跑；
    · 本自检的对象是「**能力是否真的可用**」—— 开关声称「图召回已启用」而表不存在，
      则每一次提问都会静默走 0 路且没人知道。这正是团队要堵的静默降级。
  所以：依赖缺失（配了却没有）= 启动失败；数据未就绪（表在、服务在，只是还没抽）
  = 允许启动 + 明确告警 + 可补建。这两档 MUST 分开，否则要么脆弱、要么静默。

★ 为什么标量范围校验（超时 / 字符上限）**不看开关**都执行：
  配错的值不会因为「暂时关着」变成对的。关着时不查，等真开启时才发现 → 错误
  发生在本该是「改个环境变量」的部署窗口里，而那时你正以为一切就绪。
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text

from app.config import decisions
from app.config.graph_profile import current_graph_profile
from app.config.settings import get_settings
from app.config.wiki_profile import current_wiki_profile
from app.database import SessionLocal
from app.infra import metrics

logger = logging.getLogger(__name__)

# 本版本**唯一**实现的图存储后端。其它值（neo4j / memory / …）是保留位：
# 它们不在这里就代表「没有实现」，而不是「实现坏了」。
_IMPLEMENTED_BACKENDS: frozenset[str] = frozenset({"postgres"})

# 本版本**唯一**支持的 wiki 生成范围（见设计文档 §7.5 的可见性论证）
_SUPPORTED_WIKI_SCOPES: frozenset[str] = frozenset({"doc"})

_GRAPH_TABLES: tuple[str, ...] = (
    "graph_entities",
    "graph_entity_mentions",
    "graph_relations",
)


def _fail(message: str) -> None:
    """统一的拒绝启动出口：先记 error 日志（容器日志里可 grep），再抛异常。

    记日志 + 抛异常两者都要：
      · 抛异常让进程退出（fail-closed 的实质）；
      · 日志带 [GRAPH_*] / [WIKI_*] 前缀，让「哪一条没通过」在 `docker logs` 里
        一眼可见 —— 异常栈里也能看到，但栈里夹着一堆框架行，运维读不动。
    """
    logger.error("启动自检未通过，拒绝启动", extra={"reason": message})
    raise RuntimeError(message)


def _check_scalar_ranges() -> None:
    """标量配置范围校验（§7.1 的自检规则表，与开关无关）。"""
    settings = get_settings()

    # ── 图召回 / 抽取的数值参数：一组「配错不报错、只会静默变味」的量 ──
    # ★ 为什么这三个放在一起：它们下游都有兜底（`or 全局值` / 立刻超时 / 恒超时走
    #   0 路），兜底在运行期是对的（不该因为一个环境变量把服务打崩），代价是
    #   「配置写错」这件事再也不会浮上来 —— 踩过的坑就是输出上限配 0 后静默回落
    #   全局 4K，抽取被截断，最后报出来的是「LLM 输出非法 JSON」，方向全错。
    #   所以 MUST 在启动时，把兜底能悄悄吃掉的那几种值先拦掉。

    # 下限防「必然超时的假配置」（< 50ms 的图查询在真实库上等于恒超时），
    # 上限防「挂着不动的长尾」（> 5s 会让一次问答的首字节被图路拖住）。
    if not (50 <= settings.graph_retrieve_timeout_ms <= 5000):
        _fail(
            f"[GRAPH_TIMEOUT] GRAPH_RETRIEVE_TIMEOUT_MS={settings.graph_retrieve_timeout_ms} "
            "超出允许区间 [50, 5000]（毫秒）。\n"
            "  50ms 以下在真实库上必然超时（等于永远走 0 路）；5000ms 以上会把首字节拖住。\n"
            "  ★ 拒绝启动 —— 关闭是合法终态：置 GRAPH_ENABLED=false，或改成合理值。"
        )
    if settings.graph_extract_timeout_seconds <= 0:
        _fail(
            f"[GRAPH_TIMEOUT] GRAPH_EXTRACT_TIMEOUT_SECONDS="
            f"{settings.graph_extract_timeout_seconds} MUST > 0。\n"
            "  ★ 拒绝启动 —— 0 或负值会让每次抽取立刻超时，图永远为空。"
        )
    # 0 / 负数会被 extract 侧的 `max_tokens or settings.llm_max_output_tokens`
    # 静默吃掉并回落全局 4K —— 而那正是「输出被硬截断、却报成非法 JSON」的成因。
    if settings.graph_extract_max_output_tokens < 1:
        _fail(
            f"[GRAPH_OUTPUT] GRAPH_EXTRACT_MAX_OUTPUT_TOKENS="
            f"{settings.graph_extract_max_output_tokens} MUST >= 1。\n"
            "  0 或负值会被按调用覆盖的逻辑静默吃成全局 4K（LLM_MAX_OUTPUT_TOKENS），\n"
            "  抽取随即在实体列表中途被截断，症状是「LLM 输出非法 JSON」—— 与配置错无关，\n"
            "  运维只会去改 prompt / 换模型。\n"
            "  ★ 拒绝启动 —— 关闭是合法终态：置 GRAPH_ENABLED=false，或改成 8000。"
        )
    if settings.wiki_generate_timeout_seconds <= 0:
        _fail(
            f"[WIKI_TIMEOUT] WIKI_GENERATE_TIMEOUT_SECONDS="
            f"{settings.wiki_generate_timeout_seconds} MUST > 0。\n"
            "  ★ 拒绝启动 —— 同 GRAPH_EXTRACT_TIMEOUT_SECONDS。"
        )
    if not (2000 <= settings.wiki_input_max_chars <= 60_000):
        _fail(
            f"[WIKI_INPUT] WIKI_INPUT_MAX_CHARS={settings.wiki_input_max_chars} "
            "超出允许区间 [2000, 60000]（字符）。\n"
            "  下限防「摘要没有输入」，上限防「超长文档打出 LLM 超时风暴」\n"
            "  （与 GOVERNANCE_PROMPT_MAX_CHARS 同口径）。\n"
            "  ★ 拒绝启动。"
        )
    # 第 4 步 wiki 导航在线超时（N2）：50ms 是先验值，范围 [10, 200]。
    # ★ 与开关无关地检查（见模块 docstring）：配错的值不会因为 WIKI_ENABLED=false
    #   变成对的；等真开启时才发现，错误就发生在「只改了个环境变量」的部署窗口里。
    #   下限 10ms 防「必然超时、导航恒走 0 路」的假配置；上限 200ms 防把
    #   一条找页 SQL 的墙钟预算拖到与图召回（800ms）同量级。
    if not (10 <= settings.wiki_nav_timeout_ms <= 200):
        _fail(
            f"[WIKI_NAV_TIMEOUT] WIKI_NAV_TIMEOUT_MS={settings.wiki_nav_timeout_ms} "
            "超出允许区间 [10, 200]（毫秒）。\n"
            "  10ms 以下在真实库上必然超时（导航恒降级、扩展恒 0）；"
            "200ms 以上会让一次问答的首字节被这条找页 SQL 拖住。\n"
            "  ★ 拒绝启动 —— 关闭是合法终态：置 WIKI_ENABLED=false，或改成合理值。"
        )

    # 解析后的口径不能为空：空模型名会让写入盖章落空串，检索侧过滤随即失效。
    if settings.graph_enabled and not current_graph_profile().extractor_model.strip():
        _fail(
            "[GRAPH_LLM] GRAPH_ENABLED=true 但抽取模型解析为空：\n"
            "  GRAPH_EXTRACTOR_MODEL 留空时应回落到 LLM_MODEL，而 LLM_MODEL 也是空。\n"
            "  请注入 LLM_MODEL（或显式设置 GRAPH_EXTRACTOR_MODEL）。\n"
            "  ★ 拒绝启动 —— 空模型名会被盖到每行图数据上，口径过滤从此失效。"
        )
    if settings.wiki_enabled and not current_wiki_profile().generator_model.strip():
        _fail(
            "[WIKI_LLM] WIKI_ENABLED=true 但生成模型解析为空：请注入 LLM_MODEL"
            "（或显式设置 WIKI_GENERATOR_MODEL）。\n"
            "  ★ 拒绝启动 —— 同 GRAPH_LLM。"
        )

    # 未登记模型 → 只告警不阻断（与 decisions.check_llm_budget 同口径）：
    # 私有化部署常接自建网关，模型名不可枚举，无法验证不等于配置有错。
    for _name, _model, _enabled in (
        ("GRAPH_EXTRACTOR_MODEL", current_graph_profile().extractor_model, settings.graph_enabled),
        ("WIKI_GENERATOR_MODEL", current_wiki_profile().generator_model, settings.wiki_enabled),
    ):
        if _enabled and _model not in decisions.MODEL_CONTEXT_WINDOWS:
            logger.warning(
                "%s 解析为 %r，未登记在 MODEL_CONTEXT_WINDOWS，跳过上下文窗口校验",
                _name,
                _model,
            )


async def _schema_revision() -> tuple[str, str]:
    """(当前 DB revision, 代码 head)，用于把「表缺失」的提示说得可执行。

    取不到就返回 ("unknown", "unknown") —— 这是提示性信息，不该因为读不到而改变判定。
    """
    db_rev = "unknown"
    code_head = "unknown"
    try:
        from alembic.config import Config as _AlembicConfig
        from alembic.script import ScriptDirectory

        ini = Path(__file__).resolve().parents[2] / "alembic.ini"
        heads = ScriptDirectory.from_config(_AlembicConfig(str(ini))).get_heads()
        code_head = ",".join(sorted(heads)) or "unknown"
        async with SessionLocal() as session:
            value = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
        db_rev = value or "unknown"
    except Exception:  # 提示性信息取不到不影响判定
        logger.warning("读取 schema 版本失败（仅影响提示文案）", exc_info=True)
    return db_rev, code_head


async def _check_graph_schema() -> None:
    """C3：图三表存在（缺一张都拒绝启动）。"""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT to_regclass('graph_entities') IS NOT NULL AS entities, "
                    "       to_regclass('graph_entity_mentions') IS NOT NULL AS mentions, "
                    "       to_regclass('graph_relations') IS NOT NULL AS relations"
                )
            )
        ).one()
    missing = [
        name
        for name, ok in zip(_GRAPH_TABLES, (row.entities, row.mentions, row.relations), strict=True)
        if not ok
    ]
    if missing:
        _fail(
            "[GRAPH_SCHEMA] GRAPH_ENABLED=true 但图数据表不存在"
            f"（{' / '.join(missing)} 缺失）：\n"
            "  请先执行迁移：docker compose -p kagent -f docker-compose.prod.yml "
            "run --rm migrate alembic upgrade head\n"
            "  ★ 拒绝启动 —— 表缺失时每次问答都会静默走 0 路图召回，没有任何人会知道。"
        )


async def _check_graph_health() -> None:
    """C4：图存储可达（health() 内部吞异常只回 bool，见端口契约）。"""
    from app.services.graph.postgres_store import get_graph_store

    ok = False
    try:
        ok = await get_graph_store().health()
    except Exception as exc:  # 端口契约是只回 bool，这里兜底防实现违约
        db_rev, code_head = await _schema_revision()
        _fail(
            f"[GRAPH_HEALTH] GRAPH_ENABLED=true 但图存储健康检查抛异常："
            f"{type(exc).__name__}\n"
            f"  请检查 PostgreSQL 可达性与 migrations 版本"
            f"（当前 DB revision={db_rev}，代码 head={code_head}）。\n"
            "  ★ 拒绝启动 —— 配了却验证不了，等价于没配。"
        )
    if not ok:
        db_rev, code_head = await _schema_revision()
        _fail(
            "[GRAPH_HEALTH] GRAPH_ENABLED=true 但图存储健康检查失败：health() 返回 False\n"
            f"  请检查 PostgreSQL 可达性与 migrations 版本"
            f"（当前 DB revision={db_rev}，代码 head={code_head}）。\n"
            "  ★ 拒绝启动 —— 配了却验证不了，等价于没配。"
        )


async def _check_wiki_dependencies() -> None:
    """C5：wiki 的依赖（LLM 凭据 + wiki_pages 表）。"""
    settings = get_settings()
    if not settings.llm_api_key.strip():
        _fail(
            "[WIKI_LLM] WIKI_ENABLED=true 但 LLM_API_KEY 为空：\n"
            "  生成依赖 LLM，没有凭据时任务会 100% 失败。\n"
            "  请在 .env 注入 LLM_API_KEY（并与 LLM_BASE_URL / LLM_MODEL 一起注入到"
            " **worker 容器**）。\n"
            "  ★ 拒绝启动 —— 允许启动会得到「任务全部 failed、页面永远为空、"
            "而服务看着正常」。"
        )
    async with SessionLocal() as session:
        exists = await session.scalar(
            text("SELECT to_regclass('wiki_pages') IS NOT NULL")
        )
    if not exists:
        _fail(
            "[WIKI_SCHEMA] WIKI_ENABLED=true 但 wiki_pages 表不存在：\n"
            "  请先执行迁移：docker compose -p kagent -f docker-compose.prod.yml "
            "run --rm migrate alembic upgrade head\n"
            "  ★ 拒绝启动 —— 表缺失时导航页接口会每次都 500，而问题看起来像「数据没了」。"
        )


async def _warn_readiness() -> None:
    """C7：图数据就绪度**只告警不阻断**（数据未就绪 ≠ 依赖缺失）。

    ★ 判定口径与补建脚本的选取条件一致：只算 `status='indexed'` 的文档
      （见 collect_graph_data_stats）。否则一篇解析失败的文档会让这条告警
      永远响着 —— 而「永远响的告警」等于没有告警。
    ★ 未索引的文档单独报一条，MUST NOT 混进「图未就绪」：前者是解析链路的问题，
      跑 backfill 修不了，混在一起就会把人引到错误的修法上。
    """
    from app.config.capabilities import collect_graph_data_stats

    try:
        async with SessionLocal() as session:
            stats = await collect_graph_data_stats(session)
    except Exception:
        logger.warning("图就绪度统计失败（不阻断启动）", exc_info=True)
        return

    if stats["documents_not_indexed"] > 0:
        logger.info(
            "%d 篇文档解析未完成（status<>indexed），不参与图抽取："
            "这是解析链路的问题，补建脚本不会选中它们",
            stats["documents_not_indexed"],
            extra={
                "documents_not_indexed": stats["documents_not_indexed"],
                "hint": "先修解析（查看该文档的 parse 任务日志），再重跑 backfill",
            },
        )

    not_ready = stats["documents_pending"] + stats["documents_failed"]
    if not_ready <= 0:
        logger.info(
            "图数据就绪：全部已索引文档已按当前口径抽取",
            extra={
                "documents_total": stats["documents_total"],
                "documents_skipped": stats["documents_skipped"],
            },
        )
        return
    metrics.incr("graph.data.not_ready", not_ready)
    logger.warning(
        "图数据未就绪：%d 篇已索引文档待补建/失败（图召回对它们暂不生效，三路照常）",
        not_ready,
        extra={
            "documents_total": stats["documents_total"],
            "documents_done": stats["documents_done"],
            "documents_pending": stats["documents_pending"],
            "documents_failed": stats["documents_failed"],
            "hint": "python -m scripts.backfill_graph --all",
        },
    )


async def check_capabilities() -> None:
    """C1~C8。任何一条不满足 → RuntimeError（调用方不捕获，进程退出）。

    api 侧在 main.lifespan 调；worker 侧在 WorkerSettings.on_startup 调（同一个函数）。
    ★ 两侧都 MUST 调用：抽取在 worker、召回在 api，只检一侧 = 另一侧的缺失
      只有在「用户提问后什么都没发生」时才被发现。
    """
    settings = get_settings()

    # ── 标量范围（与开关无关，见模块说明）──
    _check_scalar_ranges()

    # ── C8：能力全关 → 不做任何图/wiki 相关检查（连 DB 都不查）──
    if not settings.graph_enabled and not settings.wiki_enabled:
        logger.info("能力自检：图与 wiki 均未启用，跳过依赖检查")
        return

    if settings.graph_enabled:
        # C1：后端必须是已实现的（静默回落是禁止的）
        backend = settings.graph_store_backend.strip().lower()
        if backend not in _IMPLEMENTED_BACKENDS:
            _fail(
                f"[GRAPH_STORE_BACKEND] GRAPH_ENABLED=true 但 "
                f"GRAPH_STORE_BACKEND='{settings.graph_store_backend}'：\n"
                "  本版本未提供 Neo4j 适配实现（端口已留位，实现未写）。\n"
                "  请置 GRAPH_STORE_BACKEND=postgres；若暂时不用图能力，"
                "置 GRAPH_ENABLED=false。\n"
                "  ★ 拒绝启动 —— 静默回落到 postgres 会让你以为在跑 Neo4j，"
                "而数据实际在 PG 里。"
            )
        # C2：LLM 凭据（抽取 100% 依赖它）
        if not settings.llm_api_key.strip():
            _fail(
                "[GRAPH_LLM] GRAPH_ENABLED=true 但 LLM_API_KEY 为空：\n"
                "  实体抽取依赖 LLM，没有凭据时抽取会 100% 失败。\n"
                "  请在 .env 注入 LLM_API_KEY（并与 LLM_BASE_URL / LLM_MODEL 一起"
                "注入到 **worker 容器**）。\n"
                "  ★ 拒绝启动 —— 允许启动会得到「任务全部 failed、图永远为空、"
                "而服务看着正常」。"
            )
        # C3：图三表存在
        await _check_graph_schema()
        # C4：图存储可达
        await _check_graph_health()

    if settings.wiki_enabled:
        # ★ C6 先于 C5（顺序是刻意的，与设计文档 §7.2 的表格编号相反）：
        #   WIKI_SCOPE 是**取值合法性**问题，不依赖任何外部资源就能判定；而 C5 要查
        #   LLM_API_KEY 与 wiki_pages 表。若 C5 在前，运维配了 `WIKI_SCOPE=kb` 时会先
        #   看到「缺密钥 / 缺表」这类泛化错误，去补密钥和跑迁移 —— 方向错了，改完重启
        #   还是失败。先报最具体、最可执行的那一条，是把「拒绝启动」变成「可自助修复」
        #   的前提。C6：生成范围（kb 必须拒绝，理由见设计文档 §7.5）
        scope = settings.wiki_scope.strip().lower()
        if scope == "kb":
            _fail(
                "[WIKI_SCOPE] WIKI_SCOPE='kb'：本版本只支持 doc（一文档一页）。\n"
                "  KB 级页面会把多篇不同密级的文档摘要合成一个对象，无法用文档级四闸门"
                "表达可见性\n"
                "  （要么越权，要么每次请求按用户重建）。该值保留给后续版本。\n"
                "  请置 WIKI_SCOPE=doc，或置 WIKI_ENABLED=false。\n"
                "  ★ 拒绝启动 —— 「悄悄按 doc 处理」会让你以为开的是 KB 级导航。"
            )
        if scope not in _SUPPORTED_WIKI_SCOPES:
            _fail(
                f"[WIKI_SCOPE] WIKI_SCOPE='{settings.wiki_scope}' 不是已知取值"
                "（本版本只支持 doc）。\n"
                "  未知取值 MUST 拒绝启动 —— 否则「配了却按默认处理」会让"
                "运维以为改的配置生效了。\n"
                "  请置 WIKI_SCOPE=doc，或置 WIKI_ENABLED=false。"
            )
        # C5：wiki 依赖（LLM 凭据 + wiki_pages 表）
        await _check_wiki_dependencies()

    # C7：图数据就绪度（只告警）
    if settings.graph_enabled:
        await _warn_readiness()

    logger.info(
        "能力自检通过",
        extra={
            "graph_enabled": settings.graph_enabled,
            "wiki_enabled": settings.wiki_enabled,
        },
    )
