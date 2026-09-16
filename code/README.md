# 可运行骨架 · `code/`

《知识库问答 Agent · AI Coding 开发手册》的配套代码骨架。
上层文档：`../知识库问答Agent-AI-Coding开发手册.md`

## 这是什么 / 不是什么

**是**：手册 §3.5 的常量表 + §7.2 十条权限用例的**可运行实现**。
判定逻辑已经写完并跑通，`pytest` 全绿。

**不是**：完整应用。没有数据库、没有 HTTP、没有前端。
数据访问是内存结构（`DeptNode` / `Chunk` / `UserRecord`）。

## 怎么跑

```bash
cd code
python -m pytest tests/acl -v                 # 19 passed
python -c "from app.config.decisions import self_check; self_check()"
python app/config/decisions.py                # 打印 self_check OK
```

依赖只有 `pytest`（可选 `ruff` / `mypy`）。

## 目录

```
code/
├── app/
│   ├── config/decisions.py          # ★ 手册 §3.5 逐字落盘，含 self_check()
│   └── services/acl/
│       ├── subjects.py              # ★ acl_tags 的唯一生成入口 + 主体解析
│       ├── visibility.py            # ★ 四闸门判定 + 下推 SQL 真源
│       └── errors.py
├── tests/acl/
│   ├── conftest.py                  # 种子数据：3 层组织树 + 2 个知识库 + 7 个用户
│   └── test_visibility.py           # ★ 手册 §7.2 十条用例（+9 条补充）
└── conftest.py / pytest.ini
```

## 三条接续要求（给 AI Coding）

1. **判定式 MUST 逐字保留。** 上数据库时，把 `is_visible()` 的六个条件
   搬进 SQL（用 `PUSHDOWN_WHERE_SQL`），**MUST NOT** 简化、合并或改成 OR。
2. **`compute_doc_acl_tags()` 是 `acl_tags` 的唯一生成入口。**
   新写入路径（同步、导入、迁移脚本）MUST 调它，MUST NOT 自己拼标签。
3. **`tests/acl/` 的用例 MUST NOT 删。** 换实现时它们必须继续绿。
   用例 3b / 5 / 9 / 10 是"口径对、实现错、测试全绿"型的泄漏面。

## 这些测试有牙吗

把 `decisions.py` 里的口径常量改错，测试会红、`self_check()` 会拦住启动。
实测记录（改完即还原）：

| 故意改错 | 结果 |
|---|---|
| `DOC_TAG_ANCESTOR_EXPANSION = True` | `test_10` / `test_10b` / `test_15` 红；`self_check` 拒绝启动 |
| `ACL_PUBLIC_MUTEX = False` | `test_3b_write` / `test_15` 红；`self_check` 拒绝启动 |
| `ACL_LEVEL_TAGS_ALLOWED = True` | `test_05` / `test_15` 红；`self_check` 拒绝启动 |

## 十条用例对照表

| 用例 | 测试函数 | 守住什么 |
|---|---|---|
| 1 | `test_01_dept_internal_hit_visible` | G4 基本路径 |
| 2 | `test_02_unauthorized_kb_invisible` | G2 是必要条件 |
| 3 | `test_03_insufficient_clearance_invisible` | G1（含 `<=` 边界） |
| 3b | `test_3b_public_kb_mixed_tag_invisible` | 进公开库 ≠ 全可见 |
| 3b-写入 | `test_3b_write_mixed_public_tag_rejected` | H2 · `public` 互斥 |
| 4 | `test_04_deny_overrides_allow` | G3 优先级最高 |
| 5 | `test_05_level_tag_rejected` | H3 · 防 G4 绕过 G1 |
| 6 | `test_06_new_account_public_only` | Q9 新账号口径 |
| 7 | `test_07_concurrent_post_union` | Q4 兼岗并集 |
| 8 | `test_08_ancestor_sees_descendant` | A1 祖先展开 |
| 9 | `test_09_sibling_invisible` | 兄弟隔离 |
| 10 | `test_10_no_company_wide_leak` | H1 · 防全公司互通 |
| 10b | `test_10b_ancestor_expansion_would_leak` | 反面证明（证明上面那条有牙） |
| — | `test_11` ~ `test_16` | 空标签规范化 / 租户隔离 / 软删 / D-12 敏感部门 / 自检 / 下推 SQL |
