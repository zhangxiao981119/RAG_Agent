# AGENT_CONTEXT - 给 Agent 的上下文恢复文件

> 这不是给人看的 README，是**上下文恢复文件**。新接手的 Agent 读完应当立刻知道：这是什么项目、哪些口径不能动、当前链路是怎么走的、接下来该做什么。
> 生成：2026-09-22 ｜ 最近更新：2026-10-26（对齐检索词元化 + 章节补全 + 查询扩展 + 误拒重试；迁移 0023，测试 638）

---

## 1. 项目定位

**企业知识库 RAG 问答 Agent**。FastAPI 后端 + React 18 前端，Docker Compose 10 容器编排，面向"数据不出内网"的私有化交付。

核心链路：

```
上传 → MinIO → worker(arq) 解析 → 分块 → bge-m3 向量化 → pgvector
                                                      ↓
提问 → 打分/改写 → 查询扩展 → 权限下推 → 三路召回（向量 + 词元 tsquery + 词元 ILIKE）
                                                      ↓
              RRF(k=60) → 章节上下文补全 → 重排(默认关) → 0.50 阈值闸门
                                                      ↓
                      注入检测 + 三级记忆 + 预算裁剪 → LLM(SSE) → 引用双向校验 → 拒答判定
                                                      ↓
                              （检索充足却拒答 → 重试一次）→ 外发 / 拒绝
```

技术栈：
- 后端：Python **3.12** + FastAPI + SQLAlchemy + asyncpg + arq
- 前端：React 18.3 + TypeScript + Vite 5 + Ant Design 5
- 数据：PostgreSQL 16(+pgvector) + Redis 7 + MinIO
- 模型：bge-m3（embedding 1024 维）+ bge-reranker-v2-m3（重排，`RERANK_ENABLED` 默认 false）+ LLM 走 OpenAI 兼容接口（当前 `.env` 是 `deepseek-v4-flash`）
- 编排：10 容器 = postgres / redis / minio / embedding / reranker / ocr / migrate / api / worker / web

---

## 2. 硬约束（不能动）

```
1. 所有 middleware 必须 async/await，不用 callback 风格
2. API 响应严格遵循 {"code":0, "data":..., "msg":""}
3. LLM 流式事件序列: meta → stage → citations → delta... → done / refused
4. 解析覆盖 PDF/docx/pptx/xlsx/xls/md/txt/图片；扫描件 PDF 走本地 OCR（两段式，迁移 0019）；
   OCR 不可用时 fail-closed 报错，不静默降级
5. 表格整表一个 chunk；超 CHUNK_MAX_TOKENS 时按行组切，每组重复注入表头
6. PDF 表格由 pdfplumber 识别（pypdf 会把列关系拍平）；同页正文用词坐标排除表格区域
7. Embedding 维度固定 1024
8. Rerank 超时 3s，超时降级为 vector_score
9. LLM 首 token 超时 15s
10. 检索: vector + 词元 tsquery + 词元 ILIKE → RRF(k=60) → 章节补全 → rerank，阈值 0.50
11. 关键词切分入库与查询 MUST 共用 services/keyword_text.py（各写一份 = 静默 0 召回）
12. Grounding 剥离无归因句子，空时降级拒答
13. Parse 失败重试 3 次后进死信队列
14. 对话历史压缩到最近 6 条（3 轮问答）
15. 所有业务 API 需要 JWT Bearer Token
16. 关键口径 MUST 从 config/decisions.py 读，不在业务代码里写常量
```

---

## 3. 架构关键决策（已锁定）

| 决策 | 结论 | 为什么 |
|---|---|---|
| 重排模型 | bge-reranker-v2-m3，mem_limit 4g | base 版召回不足；v2-m3 在 2g 下 OOM。CPU 环境默认 `RERANK_ENABLED=false` |
| 关键词召回 | 词元级（入库 2-gram 预切分 + 查询 3 字窗口 OR） | 整句子串在 206 条评测上命中 0，等于关键词两路空转 |
| 排序融合 | 手写 RRF，k=60，三路等权 | RRF 只看排名不看分数，规避量纲不可比；10 行代码不值得引语言模型框架 |
| 章节补全 | 命中后按 (document_id, heading_path) 补同节片 | 分块为了定位准，补全为了保证条款完整；补全片不进排序与阈值 |
| 查询扩展 | 型号分隔符可选 + kb_terms 别名表 | `XYZ-1000` / `xyz1000` 互不为子串，向量也救不回低语义型号 |
| 前端加密 | jsencrypt 3.3.2（PKCS1-v1_5） | Web Crypto 在 LAN http 下 `crypto.subtle` 为 undefined |
| RSA 密钥持久化 | Redis SETNX 单例 | 多 worker 各自生成密钥会解密失败 |
| Worker 队列 | arq + Redis | parse → chunk → embed → write 四步流水线 |
| 数据库 | PostgreSQL + pgvector | FK SET NULL 软删除，gen_random_uuid |
| PII 脱敏 | generate 出口，`_ensure_line_breaks` 之后 | 只处理 text，保留 citations.snippet 原文 |
| 澄清阈值 | **70**（与改写的 90 独立） | 90 是"要不要改写"，不是"够不够明确"；混用会误杀可检索问题 |
| 误拒重试 | 最多 1 次，条件 ≥3 条且 top1 ≥0.70 | 只按条数重试 57 次仅捞回 1 条；加分数门槛才有正收益 |

---

## 4. 里程碑

| 里程碑 | 时间 | 内容 |
|---|---|---|
| M0–M2 | 2026-09-16 | 编排 + 种子数据 + RAG 核心链路跑通 |
| M3–M4 | 2026-09-17 | 鉴权（JWT/RSA/限流/锁定）+ 知识库成员 + ACL 下推 |
| M5–M6 | 2026-09-18 | 生产 compose + 部署文档 + 配额/敏感词/灰度 |
| 深度审查 | 2026-09-21 | P0~P3 约 49 项修复 |
| M7 | 2026-09 下旬 | 多模态（内嵌图、图片表格、质量门禁）+ 知识治理四维 |
| M8 | 2026-10 上 | 评估与标定体系：eval_runs / 预检链 / 阈值标定 / 两段式 OCR(0019) / 脏页门禁 / 并发压测 |
| M9 | 2026-10 下 | 检索质量攻坚：关键词词元化(0020/0021) / 章节补全(0022) / 查询扩展 + kb_terms(0023) / 澄清阈值分离 / 误拒重试 / 检索分段计时 |

---

## 5. M9 之后的关键改动（读代码前先知道）

| 改动 | 落点 | 事实 |
|---|---|---|
| 关键词词元化 | `services/keyword_text.py`、`retrieve/__init__.py` | `chunks.fts_text` 存中文 2-gram + ASCII 整词，生成列 `fts_vec` + GIN `idx_chunk_fts_vec`；查询侧 3 字窗口 OR。单路命中 149/206、p95 4.8ms |
| ILIKE 词元化 | `retrieve/__init__.py` | 按疑问/功能词切段，≥2 字取最长 3 个，正则 OR；命中 63/206、p95 10.7ms |
| 旧索引删除 | 迁移 0021 | `idx_chunk_fts`（表达式索引）只剩写入成本，已 drop |
| 章节补全 | `retrieve/__init__.py` | 每节 ≤3 片、全局 1200 token、跳过表格片；结果放 `section_recall_chunks` |
| 查询扩展 | `services/lexicon.py`、`scripts/build_kb_terms.py` | 型号归一化 + `kb_terms` jsonb 别名表（迁移 0023） |
| 误拒重试 | `services/generate/__init__.py` | 检索充足却拒 → 重试一次；零外发 UNGROUNDED 也重试；有正文的不重试 |
| 答非所问观测 | 同上 | `rag.answer.low_query_overlap` |
| 澄清阈值 | `decisions.should_clarify` | 70 分，与改写 90 分离 |
| 检索分段计时 | `infra/metrics.py` | embed / vector_sql / keyword_ilike / keyword_fts / section_recall / rerank 各自 p50/p95 |

---

## 6. 踩过的坑（必须记住）

| 坑 | 根因 | 解法 |
|---|---|---|
| **改了源码不生效** | Dockerfile 曾 `pip install .`，site-packages 留了构建快照，运行时优先加载它 | 去掉 `pip install .`，改 `ENV PYTHONPATH=/workspace/apps/api`；改完必须验证 `python -c "import app; print(app.__file__)"` |
| **只重建 api 镜像** | worker 是独立镜像，会静默跑旧解析代码，任务照常返回 indexed | 部署时同 tag 同时 build 两侧 |
| **SSE 长连接打爆连接池** | `get_db` 的 yield 覆盖整条流，每在途请求长期占 1 条、流式中瞬时 2 条 | 池 20+20 可配、timeout 5s、worker 池 5+5 |
| **迁移没跑就重建镜像** | 新 SQL 引用新列（如 `c.fts_text`），列不存在直接 500 | 顺序固定：`alembic upgrade head` → 回填脚本 → 重建容器 |
| **`:param::type` 解析失败** | SQLAlchemy 绑定参数正则带 `(?!:)`，`::` 不被识别，原样发 asyncpg 报语法错 | 用 `CAST(:p AS uuid)`；测试用 `text()` 真实解析断言 `_bindparams` |
| **tsquery 退化通用计划** | planner 估不出 tsquery 选择性，`@@` 变逐行 Filter，5ms → 45ms | 连接级 `plan_cache_mode=force_custom_plan`；`to_tsquery` 内联两遍当常量 |
| **中文整句被当一个 token** | PostgreSQL `simple` 配置不做中文分词 | 入库预切 2-gram；查询侧同规则生成 tsquery |
| **两侧切分逻辑不一致** | 全角/半角、断点差一个字符 | 共用 `keyword_text.py`，单测锁同一输入同一输出 |
| antd TableRowSelection 导入失败 | antd/es/table 不导出该类型 | 直接传对象，onChange 显式标 `React.Key[]` |
| fetch 不支持上传进度 | 无 upload.onprogress | 必须用 XMLHttpRequest |
| SSE 断网追加 refused 气泡 | 没区分 abort 与网络错误 | 非 abort 时保留 fullText + networkError 标记 |
| reranker 2g OOM | v2-m3 峰值 4GB+ | mem_limit 提到 4g |
| 表格「表头+一行」超 MAX 死循环 | `_accumulate` 边界没兜住 | 见硬约束 5，已加单测 |
| arq 3 次失败后指数退避到 8.7 小时 | 退避策略 | 清 Redis retry record 后手动 parse |

---

## 7. 启动与验证

```bash
cd code
docker compose up -d --build          # 首次 10~30 分钟（模型下载）
docker compose ps                     # 10 容器 healthy
curl http://localhost:8000/api/health # 各项 ok
# 前端 http://localhost:5173   种子账号 admin / ChangeMe123!

# 改完代码后的正确顺序
docker exec code-api-1 alembic upgrade head          # 有迁移时
docker exec code-api-1 python scripts/backfill_fts_text.py   # 改过关键词切分时
docker compose up -d --build api worker               # 两侧一起
```

跑测试（容器内需要先装 dev 依赖）：

```bash
docker exec code-api-1 pip install -q "pytest>=9,<10" "pytest-asyncio>=1.0" "pytest-cov"
docker exec -w /workspace/apps/api code-api-1 python -m pytest -o addopts=--strict-markers -q
# 638 passed；要覆盖率加 --cov=app
```

---

## 8. 目录结构速查

```
code/
├── apps/
│   ├── api/                    FastAPI 后端
│   │   ├── app/
│   │   │   ├── api/            路由 21 个模块 / 80 接口
│   │   │   ├── services/       parse · chunk · retrieve · generate · grounding
│   │   │   │                   keyword_text(关键词切分) · lexicon(查询扩展)
│   │   │   │                   multimodal · governance · guard · memory · acl
│   │   │   ├── workers/        arq（parse_job 两段式 + sync + governance）
│   │   │   ├── models/         SQLAlchemy ORM
│   │   │   ├── schemas/        Pydantic
│   │   │   ├── config/         settings.py · decisions.py（口径单一真源）· prompts.py
│   │   │   └── infra/          metrics · trace · redis/arq 连接池
│   │   ├── alembic/versions/   23 版迁移（0001~0023）
│   │   ├── scripts/            eval 链 · calibrate · backfill_fts_text · build_kb_terms · loadtest
│   │   └── tests/              pytest（45 文件 / 638 用例，行覆盖 65%）
│   ├── ocr/                    GOT-OCR2.0（扫描件专用，单路串行）
│   └── web/                    React 18 前端（5 业务页 + 12 管理面板）
├── docker-compose.yml          开发 10 服务
├── docker-compose.prod.yml     生产 12 服务
├── docker-compose.loadtest.yml 压测 overlay（隔离上游 + 抬限流）
└── ops/                        Caddy · Prometheus · Grafana
```

---

## 9. 给接手 Agent 的指令

1. **先读 `README.md`**（项目全貌，含设计取舍与已知边界），再读本文件。两者冲突时以代码为准，并顺手修正文档。
2. **不要凭记忆改口径**：阈值、预算、配额一律回 `app/config/decisions.py`，那里有 `self_check()` 会在启动时拒绝坏值。
3. **改检索链路前先跑评测**：`scripts/run_eval.py`，217 条用例，看 `eval_runs` 最新基线（当前拒答 100% / 漏答 0% / 命中 100%）。任何"优化"若让这三个数变差，就要说明理由。
4. **改关键词切分要两侧同时生效**：同时改 `keyword_text.py`、跑 `backfill_fts_text.py`、重建 api 与 worker。
5. **提交前跑**：`ruff check` + 容器内 `pytest`（638 passed 是当前基线）。
6. 仓库已托管：Gitee `origin`、GitHub `github`（GitHub 需走本机 Clash 代理 `127.0.0.1:7897`）。

---

## 10. 当前已知缺口（不要当成已完成）

| 缺口 | 现状 |
|---|---|
| `workers/parse_job.py` 覆盖率 | 14%，是全仓最低处，优先补 |
| 重排默认关闭 | CPU 单次 50 条 66s，远超 3s 超时；GPU 化后才可开 |
| 质量判决未落库 | `pass/enhance/reject_to_manual` 只写 worker 日志，界面无法筛选 |
| 生产未编排 OCR | prod compose 12 服务不含 ocr，扫描件入库需自行加 |
| 单 uvicorn worker | QPS 平台约 4.5，瓶颈在这里；需先改 prometheus multiprocess 再谈扩容 |
| 关键词是 2-gram 非分词 | 同义词/词形变化靠 `kb_terms` 与向量兜，不是语言学分词 |
