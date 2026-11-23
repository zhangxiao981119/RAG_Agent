"""Step 4A 的**静态守卫**（不连库，任何机器上都必须跑）。

★ 为什么这些必须有：Step 4A 里有几处「删掉一行，所有行为用例仍然全绿」的退化 ——
  它们的共同形态是**少一个词就出错，而那个词没有对应的行为观测点**：

  1. `main.py` 少一行 `include_router` → 9 个接口**一个都不可达**。
     import 成功、应用能起、直接测 service 全绿，只有对着文档调接口才发现 404。
  2. 某条写 SQL 少 `tenant_id = :tenant_id` → 跨租户越权。
     行为用例要覆盖它得为**每条语句**各造一个跨租户现场，覆盖不全。
  3. `publish()` 里两条 UPDATE 顺序颠倒 → 撞 `uq_scenario_versions_one_published`
     的部分唯一索引（立即生效、非 DEFERRABLE），合法的并发发布变成 409。
     顺序只在**冲突时**才可观测，平时完全看不出来。
  4. `WriteCode` 新增了成员但忘了加 HTTP 映射 → `_HTTP[code]` 抛 `KeyError` →
     500，而失败码本应给出 4xx。只有走到那一条才炸。

  ★ 反自证：下面每条断言的对象都是**源码文本 / AST / openapi 契约**，
    不是被测函数自己返回的布尔值（设计 §D.7、§D.6 T4-X4）。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WRITE_SERVICE = _API_ROOT / "app" / "services" / "scenario" / "write_service.py"
_WRITE_ERRORS = _API_ROOT / "app" / "services" / "scenario" / "write_errors.py"
_SCENARIO_SCHEMA = _API_ROOT / "app" / "schemas" / "scenario.py"

# 写接口的完整契约（路径 → 方法集合）。少一条就是「文档里有、线上 404」。
_EXPECTED_ROUTES = {
    "/api/scenarios": {"get", "post"},
    "/api/scenarios/{scenario_id}": {"get"},
    "/api/scenarios/{scenario_id}/versions": {"post"},
    "/api/scenarios/{scenario_id}/versions/{version}": {"get", "put"},
    "/api/scenarios/{scenario_id}/versions/{version}/validate": {"post"},
    "/api/scenarios/{scenario_id}/versions/{version}/publish": {"post"},
    "/api/scenarios/{scenario_id}/versions/{version}/archive": {"post"},
    # Step 4B-1（R-4B-2）：发布前预检。**追加一行是加严**，不是放宽 ——
    # 断言仍是「集合精确相等」，漏挂路由 / 多加路由都会红。
    "/api/scenarios/{scenario_id}/versions/{version}/preflight": {"get"},
    "/api/integrations/applications/{application_id}/scenario": {"put"},
}


def test_scenarios_router_is_mounted():
    """W1–W7 + R1–R3 必须真的挂在 app 上（`include_router` 漏一行就全 404）。"""
    from app.main import app

    paths = app.openapi()["paths"]
    # ★ 只取「场景写接口」这一族：`/api/scenarios*` 与集成侧的绑定接口。
    #   早先用「路径含 scenario」过滤，会把 S7-5 的管理端治理面
    #   （`/api/admin/scenario-governance`）也吸进来 —— 那是另一套契约，
    #   它的挂载由 `test_step7_api.py::test_governance_router_is_mounted` 单独守。
    actual = {
        path: set(methods)
        for path, methods in paths.items()
        if path.startswith("/api/scenarios")
        or path == "/api/integrations/applications/{application_id}/scenario"
    }
    assert actual == _EXPECTED_ROUTES, (
        "场景接口的对外契约与设计不一致：\n"
        f"  缺少：{sorted(set(_EXPECTED_ROUTES) - set(actual))}\n"
        f"  多余：{sorted(set(actual) - set(_EXPECTED_ROUTES))}\n"
        f"  方法不符：{ {p: (sorted(_EXPECTED_ROUTES[p]), sorted(actual.get(p, set()))) for p in set(_EXPECTED_ROUTES) & set(actual) if _EXPECTED_ROUTES[p] != actual[p]} }"
    )


# ══════════════════════════════════════════════════════════════════════
# SQL 文本守卫：每条碰 `scenario_id` 的语句都必须带 `tenant_id`
# ══════════════════════════════════════════════════════════════════════


def _string_constants(path: pathlib.Path) -> list[tuple[int, str]]:
    """模块内所有字符串字面量（含函数内联 SQL），带行号便于报错定位。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _is_scenario_statement(text: str) -> bool:
    lowered = text.lower()
    touches_table = "scenarios" in lowered or "scenario_versions" in lowered
    return touches_table and "scenario_id" in lowered


# 租户谓词：`tenant_id = :tenant_id`，允许任意别名前缀（`s.` / `v.` / `f.` / `sc.` / 无别名）。
# `\b` 在 `.` 之后成立，所以 `s.tenant_id` 也能命中；反过来 `p.tenant_id = s.tenant_id`
# 这种**连接条件**不会被误判 —— 它右边不是绑定参数。
_TENANT_PREDICATE = re.compile(r"\btenant_id\s*=\s*:tenant_id\b")

# 负向：把租户谓词整条短路掉的恒真析取。
# 比对前把空白全部抹掉再 lower，所以 `OR  TRUE` / `or 1 = 1` / `OR\tTRUE` 都能抓到。
_FORBIDDEN_DISJUNCTS = ("ortrue", "or1=1", "or'1'='1'", "or1>0", "or2>1", "or(1=1)", "or2=2")


def _flatten_sql(text: str) -> str:
    """抹掉所有空白后小写化 —— 负向匹配的唯一规范化形态。"""
    return re.sub(r"\s+", "", text).lower()


def test_every_scenario_sql_is_tenant_scoped():
    """T4-X4：凡是按 `scenario_id` 定位 `scenarios` / `scenario_versions` 的语句，

    都必须同时带 `tenant_id = :tenant_id` 谓词，且不得带恒真析取。

    ★ 为什么**不能**用「`tenant_id` 子串计数」（4A 的原始实现，4B 前修掉）：
      子串分不清 `=`、`!=` 与 `OR true` —— 把 `WHERE s.tenant_id = :tenant_id`
      改成 `WHERE (s.tenant_id = :tenant_id OR true)` 之后，子串还在、计数还是 1，
      守卫绿着，而列表会返回**所有租户**的场景、详情能读到别人的
      `policy` / `kb_ids` / `dna_declared` / `checksum`。独立验证实测：
      这样改完 287 条产品用例全绿（含本条守卫）。
      所以本条现在是**两条互补断言**：正向要谓词存在、负向要没有短路析取。
    ★ 为什么是文本守卫而不是行为用例：这类 SQL 有 12 条，每条都要造一个
      「别人的场景真实存在」的现场才能观测到越权；而少写一个词的后果是**静默越权**，
      不是报错。文本守卫是唯一能**全量**覆盖的方式（设计 §D.6 T4-X4）。
      行为层的观测在 `tests/scenario/test_step4_tenant_isolation.py`（真库 + HTTP）。
    ★ 断言的是 SQL 文本，不是 `write_service` 自己的返回值 —— 后者会自证。
    """
    unscoped: list[int] = []
    shorted: list[tuple[int, str]] = []
    for lineno, text in _string_constants(_WRITE_SERVICE):
        if not _is_scenario_statement(text):
            continue
        if not _TENANT_PREDICATE.search(text):
            unscoped.append(lineno)
        flat = _flatten_sql(text)
        hit = next((bad for bad in _FORBIDDEN_DISJUNCTS if bad in flat), None)
        if hit is not None:
            shorted.append((lineno, hit))

    assert not unscoped, (
        f"write_service.py 第 {unscoped} 行的 SQL 按 scenario_id 定位却没有 "
        "`tenant_id = :tenant_id` 谓词（不是「有 tenant_id 字样」）—— 跨租户外键"
    )
    assert not shorted, (
        f"write_service.py 第 {shorted} 行的 SQL 含恒真析取（`OR true` / `OR 1=1` 一类）—— "
        "它会把整条租户谓词短路掉，等于没有 WHERE。"
        "命中详情：" + ", ".join(f"L{n}<-{bad}" for n, bad in shorted)
    )


def test_tenant_guard_covers_all_scenario_statements():
    """守卫自身的**覆盖率前置**：抽到的语句条数不能塌到 0。

    ★ 没有它，`test_every_scenario_sql_is_tenant_scoped` 会在
      「有人把 SQL 改成 f-string / 挪进别的模块」时变成空转（`missing == []` 恒成立），
      绿得毫无信息 —— 与 Step 1–2 那次「种子行数 0 == 0」是同一类假绿。
    """
    statements = [
        text for _, text in _string_constants(_WRITE_SERVICE)
        if _is_scenario_statement(text) and ("select" in text.lower() or "update" in text.lower())
    ]
    assert len(statements) >= 10, (
        f"只抽到 {len(statements)} 条碰 scenario_id 的 SQL —— 守卫已空转，"
        "先修 `_string_constants` 再谈通过"
    )


# ══════════════════════════════════════════════════════════════════════
# 锁纪律：`FOR UPDATE` 存在 + 顺序（scenarios 先于 scenario_versions）
# ══════════════════════════════════════════════════════════════════════


def _module_constants(path: pathlib.Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    values[target.id] = node.value.value
    return values


def test_both_lock_statements_use_for_update():
    """两把行锁都必须 `FOR UPDATE`，且都带 `tenant_id`（锁别人的行 = 跨租户阻塞）。"""
    constants = _module_constants(_WRITE_SERVICE)

    for name in ("_SQL_LOCK_SCENARIO", "_SQL_LOCK_VERSION"):
        text = constants[name]
        assert "FOR UPDATE" in text.upper(), f"{name} 缺 FOR UPDATE —— 并发写不再串行化"
        assert "tenant_id" in text, f"{name} 缺 tenant_id —— 会锁住别的租户的行"

    assert "scenarios" in constants["_SQL_LOCK_SCENARIO"]
    assert "scenario_versions" in constants["_SQL_LOCK_VERSION"]


def _call_order_sync(func_name: str) -> list[str]:
    """返回函数内 `_lock_scenario` / `_lock_version` 的**出现顺序**（去重保序）。

    AST 解析本身是同步的（`write_service` 只是读文本），所以这里不包 async ——
    `test_lock_order_is_global_and_unique` 也用同步函数：一个 `async def` 测试里
    只为了调一个同步函数而 `await`，会让「忘了加 `pytest.mark.asyncio`」变成静默 skip。
    """
    tree = ast.parse(_WRITE_SERVICE.read_text(encoding="utf-8"))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name
    )
    # ★ 必须按 `lineno` 排序：`ast.walk` 是**广度优先**，嵌套在 `if` / `try` 里的
    #   调用会先于更早写的顶层调用被访问 —— 直接取 walk 的顺序会得出与源码相反的结论。
    calls = sorted(
        (
            node for node in ast.walk(target)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in {
                "_lock_scenario", "_lock_version"
            }
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    seen: list[str] = []
    for node in calls:
        if node.func.id not in seen:
            seen.append(node.func.id)
    return seen


@pytest.mark.parametrize(
    "func_name",
    ["replace_content", "validate_version", "publish", "archive"],
)
def test_lock_order_is_global_and_unique(func_name):
    """碰**两张表**的写用例都必须**先**锁场景行、**再**锁版本行（全局唯一顺序）。

    ★ 两个写用例（发布 v3 / 归档 v2）会同时碰这两张表；顺序不一致就是死锁的
      教科书条件。而死锁只在**并发**下出现，单请求行为用例永远看不到它。
    ★ `create_version` 不在参数里：它只锁场景行，因为**版本行还不存在**
      （版本号就在这把锁下分配 —— 这正是它不需要第二把锁的原因）。
      把它写死成「必须两把锁」会逼实现去锁一条不存在的行。
    """
    order = _call_order_sync(func_name)
    assert order[:2] == ["_lock_scenario", "_lock_version"], (
        f"{func_name} 的加锁顺序是 {order}；MUST 是 [场景行, 版本行]（否则死锁）"
    )


def test_publish_archives_before_publishing():
    """`publish()` 里必须先执行「归档旧 published」再执行「发布目标」。

    ★ `uq_scenario_versions_one_published` 是**立即生效**的部分唯一索引
      （非 DEFERRABLE）：先发布会在这条语句执行的瞬间报 UniqueViolation，
      与「并发发布」这个合法场景混成同一个错误。顺序反了平时看不出，
      只有在「已有 published 的场景发新版本」时才炸（而那是唯一有用的场景）。
    """
    tree = ast.parse(_WRITE_SERVICE.read_text(encoding="utf-8"))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "publish"
    )
    # ★ 按 `lineno` 排序：`ast.walk` 是广度优先，`try` 块里的调用会先于更早写的
    #   顶层调用被访问 —— 用它自带的顺序会把「先归档后发布」判成相反。
    used = [
        node.args[0].id
        for node in sorted(
            (
                node for node in ast.walk(target)
                if isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "text"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in {"_SQL_ARCHIVE_PUBLISHED", "_SQL_PUBLISH"}
            ),
            key=lambda node: (node.lineno, node.col_offset),
        )
    ]
    assert used[:2] == ["_SQL_ARCHIVE_PUBLISHED", "_SQL_PUBLISH"], (
        f"publish() 的 UPDATE 顺序是 {used}；MUST 是先归档后发布"
    )


# ══════════════════════════════════════════════════════════════════════
# 错误码表完整性
# ══════════════════════════════════════════════════════════════════════


def test_every_write_code_has_status_and_message():
    """`WriteCode` 的每个成员都必须有 HTTP 状态与默认文案（否则 `_HTTP[code]` KeyError）。

    ★ `KeyError` 会落到 `main.py` 的全局兜底 → 500。于是一个本该是 409 的
      「可自助修」的失败变成 500，客户端按 4xx 写的重试逻辑全部失效。
    """
    from app.services.scenario import write_errors

    codes = set(write_errors.WriteCode)
    assert set(write_errors._HTTP) == codes, (
        f"缺 HTTP 映射：{sorted(code.value for code in codes - set(write_errors._HTTP))}"
    )
    assert set(write_errors._MSG) == codes, (
        f"缺默认文案：{sorted(code.value for code in codes - set(write_errors._MSG))}"
    )
    # 「缺头」与「条件不满足」必须给出不同状态（428 vs 409）
    assert write_errors.http_status(write_errors.WriteCode.PRECONDITION_REQUIRED) == 428
    assert write_errors.http_status(write_errors.WriteCode.CHECKSUM_MISMATCH) == 409


def test_write_error_codes_are_stable_machine_keys():
    """`code` 是对外稳定机器键：必须是 `SCENARIO_*` 前缀的大写下划线形式。"""
    from app.services.scenario.write_errors import WriteCode

    for code in WriteCode:
        assert code.value.startswith("SCENARIO_"), code.value
        assert code.value.isupper(), code.value
        assert code.value == code.name or code.value == f"SCENARIO_{code.name}", code


# ══════════════════════════════════════════════════════════════════════
# 契约里「故意没有的字段」
# ══════════════════════════════════════════════════════════════════════


def test_version_content_has_no_binding_mode_nor_dna():
    """请求体 MUST NOT 提供 `binding_mode` / `dna_declared`（不留入口 > 加校验）。

    ★ 若客户端能设 `binding_mode=unrestricted`，写侧就要为它写校验；
      而「不给入口」让 `ck_..._binding_consistent` 恒成立。
      `dna_declared` 同理：由场景指定 DNA 等于让场景改平台口径。
    ★ `extra="forbid"` 是这条的**执行机制**：没有它，pydantic 会静默丢弃多余字段，
      于是「我传了 binding_mode」既没生效也没报错 —— 客户端以为设成功了。
    """
    from app.schemas.scenario import ScenarioCreate, VersionContent

    fields = set(VersionContent.model_fields)
    assert "binding_mode" not in fields
    assert "dna_declared" not in fields
    assert VersionContent.model_config.get("extra") == "forbid"
    assert ScenarioCreate.model_config.get("extra") == "forbid"

    # 真传一个多余字段必须 422（而不是被静默忽略）
    with pytest.raises(Exception) as exc_info:
        VersionContent(
            policy={}, kb_ids=["aaaaaaaa-0000-0000-0000-000000000001"],
            change_note="x", binding_mode="unrestricted",
        )
    assert "binding_mode" in str(exc_info.value)


def test_write_service_does_not_import_resolver_lookup():
    """写侧 MUST NOT 复用 resolver 的读查询（「写接口把读路径搞坏」在结构上不可能）。

    ★ 允许的唯一交集是 `resolver.invalidate`（缓存失效必须由提交后的写侧调用）。
      若哪天有人为了「读一下当前 published」而 import `LOOKUP_SQL`，
      那条 SQL 就会同时承担读与写的口径 —— 一次改动两个后果。
    """
    source = _WRITE_SERVICE.read_text(encoding="utf-8")
    assert "LOOKUP_SQL" not in source
    assert "resolver.invalidate" in source, "缓存失效调用消失了 —— 发布后读路径会读到旧行"
