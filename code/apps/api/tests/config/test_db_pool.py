"""DB 连接池口径的回归测试 —— 钉住 P1-5 压测暴露的那次雪崩。

事故现场（2026-10-18 压测，桩上游、20 并发）：
    76 个请求失败 36 个（47%），P50 总耗时 60s，日志里
    `QueuePool limit of size 10 overflow 10 reached, connection timed out, timeout 30.00`

根因不是"池太小"这么简单：
  · `get_db` 的 `yield` 覆盖整条 SSE 流，**每个在途问答请求长期占 1 条连接**；
  · 流式过程中还会瞬时再开 1 条（读历史 / 检索 / 落库 / 审计各用一次 SessionLocal）；
  · 池上限 20 正好卡在「c=10 正常、c=20 雪崩」的临界点上。
    c=20 时某个请求等池 30s，等待期间仍然占着自己那条依赖连接 → 越等越堵。

所以这里钉的是三条口径，而不是具体数字：
  1. engine 的池参数必须真的来自 settings（防「改了 settings 忘了改 database.py」，
     这种漏配在单测里看不出来，只在高并发时才炸）
  2. `pool_timeout` 必须短 —— 过载要快速 503，不能挂满 30 秒再 500
  3. 池容量 × 副本数要留在 PG 的安全区里（max_connections=100 不能全给业务）
"""
from __future__ import annotations

from app import database
from app.config.settings import get_settings


def test_engine_pool_wired_from_settings():
    """database.py 的池参数 MUST 取自 settings，不能各写一份字面量。"""
    s = get_settings()
    pool = database.engine.pool
    assert pool.size() == s.db_pool_size, "pool_size 与 settings 不一致"
    assert pool._max_overflow == s.db_max_overflow, "max_overflow 与 settings 不一致"
    assert pool._timeout == s.db_pool_timeout_s, "pool_timeout 与 settings 不一致"


def test_pool_timeout_fails_fast():
    """过载时 MUST 快速失败。

    30s 的默认值会让请求"看起来没挂、其实在等池"，压测里表现为 P50 总耗时 60s
    却几乎没有报错 —— 这比直接 503 更难排查。
    """
    assert 0 < get_settings().db_pool_timeout_s <= 10


def test_pool_capacity_covers_two_conns_per_inflight_request():
    """容量口径：按「每在途请求 2 条连接」算，池至少覆盖 10 并发底噪。"""
    s = get_settings()
    assert s.db_pool_size + s.db_max_overflow >= 20


def test_pool_capacity_leaves_room_for_postgres():
    """单副本池容量 MUST 有上限，不能"越大越好"地往上加。

    PG `max_connections=100`，且 api 与 worker 各自一份池。
    按「每在途请求 2 条」的现口径，40 条池 ≈ 20 并发 —— 这就是单副本的天花板。
    再往上不要加池：加池只会让**更多请求排队等一条连不上的连接**，
    正确做法是先消掉「每个在途请求长期占 2 条」这个前提（把流式期间不需要的
    session 提前释放），否则扩副本时会直接撞穿 PG 的连接上限。
    """
    s = get_settings()
    assert s.db_pool_size + s.db_max_overflow <= 45
