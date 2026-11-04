# P3 独立验证报告：引用 snippet 按命中位置开窗（`6f9c3f5`）+ 补交测试（`0e04472`）

- 测试执行者：agent-harness-tester（未参与实现，新鲜视角对抗性检查）
- 测试时间：2026-11-04（UTC 05:3x–06:1x）
- 被测对象：
  - `6f9c3f5 feat(chat): 引用 snippet 按命中位置开窗（hit_snippet）`
  - `0e04472 test(eval): 补 §14.6 观测聚合与 caliber 图键的测试文件`
- 环境：真实 PostgreSQL 16.15（`code-postgres-1`，企业知识库.md 4 条 is_latest chunks）；
  全量测试在**全新容器**（`docker compose run --rm --no-deps`，未动运行中的 api）里 `pip install '.[dev]'` 后执行。
- 结论：**主目标达成**——引发投诉的真实 chunk（447 字，「上班时间：9:00-12:00，13:30-18:00」在 230 字处）
  三种问法下窗口都落在证据上；回退逐字节性（含 200/201/202 边界）成立；接线是纯展示字段、SSE 事件序未动；
  3 组变异测试全部变红后还原；定向 464 条测试全绿、ruff 通过。
  发现 **P1 × 1（型号词元缺口：检索用容忍分隔符的正则命中、hit_snippet 用精确 find 探测 → 型号类查询白修）**、
  **P2 × 4**。

---

## 1. 我实际做了什么（不是「已验证」三个字）

| # | 动作 | 手段 |
|---|---|---|
| 1 | 通读 `hit_snippet` 实现与 `chat.py` 接线 diff，核对其依赖（`_bigrams/_dedupe/_CJK_RE/_normalize`） | 代码审阅 + `git show` |
| 2 | 回退逐字节性 + 边界（200/201/202、pos=0、片尾命中、width 扫描 1~300、8 种无词元 query） | 宿主机纯函数探针（PYTHONPATH=apps/api） |
| 3 | 真实投诉场景：查库取出 `企业知识库.md` 全部 4 条 is_latest chunk，逐条调 `hit_snippet(content, 问句)` | 容器内探针（当前源码快照 `/tmp/snapS` + 真库；运行镜像尚未包含 `6f9c3f5`，故用快照，md5 已核对一致 `94a333f6`） |
| 4 | 接线正确性：`question_for_pipeline` 赋值链、snippet 的全部使用点、SSE 顺序钉子测试 | 代码审阅 + 全量测试 |
| 5 | 定向全量：`tests/retrieve tests/graph tests/chat tests/eval tests/services` + `ruff check .` | 全新容器 `docker compose run --rm --no-deps --entrypoint sh api`，`pip install -q '.[dev]'` |
| 6 | 变异测试 × 3：LOOKBACK 30→0 / 去掉边界对齐 / 只试第一个词元，各跑 `test_hit_snippet.py`，验证变红后 `git checkout` 还原 | 宿主机 pytest |
| 7 | `0e04472` 纯度：`git show --name-only` 过滤非 tests 文件；两个文件单独跑并数条数 | git + pytest |
| 8 | 型号词元缺口验证：构造用例 + 真库检索含 AX3000/AX6000 的 chunk 复算 | 宿主机 + 容器内真库 |

---

## 2. 逐条结果（对齐任务书的 6 个对抗性检查点）

### 2.1 回退逐字节性 —— **通过**

```
[✔] len==200 未命中 → 原样
[✔] len==201 未命中 → content[:200] 逐字节
[✔] len==202 未命中 → content[:200] 逐字节
[✔] len==200 有命中 → 原样（短文分支优先，规格如此）
[✔] query='' / '   ' / '？？？？' / '!!' / 'ab' / '…' / '😀😀😀' / 'c++(x)' → 全部逐字节回退 len=200
[✔] 有词元全不命中（量子色动力学重整化）→ 逐字节
```

（第一轮探针曾报 3 个「✘」，复查全部是我**构造错误**——切片到 200 字时把证据切掉了、
或总长不足 200 走了短文分支。修正构造后全过。构造过程记录在此，防止后人复踩。）

### 2.2 真实场景复现（投诉 chunk）—— **通过**

```
447 chunk：len=447，「上班时间」位置 = 230（中段）
旧 content[:200] 含「上班时间」: False（旧片段尾 40 字：'…财务总监（<5000）、CEO（>=50000）。'——是报销审批内容）
q='公司几点上班'（ILIKE 词元全不命中，2-gram 兜底）
  snippet(len=200): '…公司实行标准工时制，每周工作 5 天，每天 8 小时。\n上班时间：9:00-12:00，13:30-18:00。\n迟到超过 30 分钟视为旷工半天。…'
  含「上班时间」True  含「9:00-12:00，13:30-18:00」True  与旧相同 False
q='几点上班' / '上班时间是几点'：同样命中，窗口起点正对「上班时间：」那一行
```

其余 3 条 chunk（569/424/209 字，均不含「上班时间」）行为合理：命中「公司」大词的窗口
挪到组织架构/薪酬段落，未命中的逐字节回退。

### 2.3 接线正确性 —— **通过**（一处口径选择要记录，见 P2-3）

- `snippet` 全仓库只有一个生产使用点：`app/api/chat.py:640`（citations 载荷）；不进生成 prompt、
  不进检索路径；`mask.py` 头注明确「citations.snippet MUST NOT 打码」，无冲突。
- `question_for_pipeline` 的赋值链：263（原始问题）→ 324（带图拼接转录）→ 之后不再变；
  与喂给生成的 `build_memory_prompt(...)`（585）/ `686` 是**同一变量**。✔ 符合验收基准。
- SSE 顺序由既有测试钉住（`test_ask_with_image.py:287-289/314`、`test_sse_retrieval_observation.py:280-282`
  断言 `citations < delta < done`），定向全量 464 条全绿 → 事件顺序未被动过。
- citations 其余字段（n/chunk_id/doc_id/filename/heading_path/page_no/score/raw_url）逐字未改（diff 只有 snippet 一行 + import 一行）。

### 2.4 全量测试 —— **通过**

```
全新容器（code-api-run-904e79…，--no-deps，未动运行中的 api）：
$ pip install -q '.[dev]' && python -m pytest tests/retrieve tests/graph tests/chat tests/eval tests/services
464 passed, 1 warning in 3.11s
$ ruff check .
All checks passed!
```

### 2.5 变异测试（有牙检查）—— **3/3 拦截**

| 变异 | 结果 | 拦截者 |
|---|---|---|
| `_SNIPPET_LOOKBACK: 30 → 0` | `1 failed, 7 passed` | `test_window_aligns_to_sentence_boundary` |
| 边界对齐改为恒 `-1`（去掉对齐） | `1 failed, 7 passed` | 同上 |
| 探测只试第一个词元（`probes[:1]`） | `3 failed, 5 passed` | middle/align/width 三条 |

每次变异后 `git checkout -- app/services/keyword_text.py` 还原，`git status --porcelain` 为空（工作区干净自证）。

### 2.6 `0e04472` 测试文件纯度 —— **通过**

```
$ git show 0e04472 --name-only | grep -E "^code/apps/api/(app|scripts)" → 空（只有 2 个 tests/ 文件）
$ pytest tests/eval/test_run_eval_graph_aggregation.py → 8 passed
$ pytest tests/services/test_caliber_graph_fields.py   → 4 passed（合计 12，全绿）
```

两文件均只 import 被测函数 + httpx/pytest，无夹带实现、无放宽断言。
（任务书说 9+4=13 条，实测 8+4=12 条——差 1 条，是任务书/口头计数偏差，非文件问题。）

---

## 3. 发现的问题

### P1-1 型号词元缺口：检索路命中、开窗探不到（与本任务要修的问题同类）

- 位置：`app/services/keyword_text.py:285-287`（`pos = content.lower().find(term)`）
- 机理：`build_ilike_terms('AX3000')` 产出词元 `['ax-3000']`；检索路（ILIKE）用
  `build_ilike_pattern` 把它展开成**容忍分隔符**的正则 `ax[-_ ]?3000`，能命中 `AX3000`/`AX 3000`/`ax3000`；
  而 `hit_snippet` 用 `find('ax-3000')` **精确子串**探测，对 `AX3000`（无连字符）不命中 →
  ASCII 词元没有 2-gram 兜底 → 整体回退 `content[:200]`。
- 实证（构造 + 真库双确认）：

```
content = '产品概述。'*60 + 'AX3000 路由器支持 Wi-Fi 7。' + '参数。'*30   # len=411，型号在 300
hit_snippet(content, 'AX3000') → 与 content[:200] 逐字节相同（含 AX3000: False）
检索路正则命中: True                                                            ← 白修的实锤
真库：len>250 且含 AX3000/AX6000 的 chunk = 0 条（当前语料型号都落在 <200 字的短 chunk，
      走短文分支原样返回，暂时无感）→ 属**潜伏缺口**
```

- 影响面判断：本产品是路由器知识库，**型号是最高频查询类型**；一旦长 chunk（>200 字）中段
  出现型号，就会出现与用户投诉完全相同的「检索命中了、引用看起来没引用到」。2-gram 兜底
  对 ASCII 无效，堵不上这个洞。
- 建议：探测序列对型号词元追加合体形式（`letters+digits`），或直接用
  `re.search(build_ilike_pattern(terms), content, re.I)` 取 `m.start()`（pattern 全部 re.escape，无注入风险），
  命中语义即与检索路完全同源。

### P2-1 `width=1` 违反「返回串总长 ≤ width」契约

```
hit_snippet(content, '上班时间', width=1) → '……'（len=2 > 1）
```

机理：`body = content[start:end][: width - len(prefix) - len(suffix)]`，当 `width < prefix+suffix`
时切片长度为 0 或**负数**（负数切片从尾部丢字符，长度不受控）。width=2 起正常（实测 2/3/5/50/100/200 全过）。
建议 `width = max(width, 4)` 钳制或在 self_check/函数头断言 `width >= 4`。纯展示参数、默认 200，故 P2。

### P2-2 2-gram 兜底层的「窗口偏到无关位置」退化模式（风险量化，判定：可接受但应写明）

构造：`'我们公司成立于2010年…' + '公司简介。'*60 + '员工上班时间为 9:00-12:00，13:30-18:00。'`
（len=356，证据在 327）→ query「公司几点上班」的 2-gram 先命中开头的「公司」→ 窗口停在片头，
**证据不在窗口内**，snippet 退化为旧 `content[:200]` 的观感。真实投诉 chunk 上「公司」的首次
出现恰好在证据前一句，所以实测是好的——这个兜底层的收益依赖「低区分度词与证据的相对位置」。
判定：**可接受**（纯展示、无正确性风险；比 100% 停在片头好），但建议在 docstring 的第 2 条
语义里补一句这个退化模式，避免后人把它当「保证命中证据」的契约。

### P2-3 snippet 探测用 `question_for_pipeline`（改写前），检索用的是 `retrieve_query`（改写后）

`rewrite_query` 返回改写后的 `retrieve_query` 用于检索（chat.py:360），而 snippet 用
`question_for_pipeline`（640）。改写真正生效时（status="rewritten"），检索靠改写词命中，
snippet 用原始问题探测可能探不到 → 回退 content[:200]。这与验收基准「question_for_pipeline 是
发给生成前的同一变量」一致，**是符合规格的选择**（引用面板服务于用户的问题），但与 P1-1 叠加时
「开窗探不到」的面会变大。记录在此，改 P1-1 时一并权衡（例如探测序列 = 原始问题词元 + 改写词元）。

### P2-4 对齐契约只有单点钉子

变异 1/2 都只被 `test_window_aligns_to_sentence_boundary` 一条测试拦截——删掉这一条，
「向左回看 30 字 + 边界对齐」就完全失去保护。建议补一条「lookback 字数上界」的独立断言
（如命中点 - 窗口起点 ≤ 30 + 对齐修正），把两个参数分别钉住。

（另：commit 信息 `6f9c3f5` 称 `test_hit_snippet.py（8 条）`，实测 8 条 ✔ 无偏差；
`0e04472` 实测 8+4=12 条。）

---

## 4. 纪律自证

- 未修改任何业务代码：三次变异全部 `git checkout --` 还原，最终 `git status --porcelain` 为空。
- 未动运行中的容器：全量测试走 `docker compose run --rm --no-deps`（独立一次性容器）；
  真库探针只读（SELECT），容器内临时文件 `/tmp/probe_db.py`、`/tmp/snapS*` 已删除。
- 运行镜像尚不含 `6f9c3f5`（`import hit_snippet` 报 ImportError 实证），故场景复现用当前源码快照
  `/tmp/snapS`（`keyword_text.py` md5 `94a333f6` 与工作区一致）——已在正文声明，避免「真库实测」被误读为「镜像实测」。
- 纯函数探针（回退/边界/width/2-gram/大小写）在宿主机跑，与容器结果一致。

## 5. 复现命令

```bash
# 变异测试（每次后 git checkout 还原）
cd code/apps/api && sed -i 's/^_SNIPPET_LOOKBACK: int = 30$/_SNIPPET_LOOKBACK: int = 0/' app/services/keyword_text.py
PYTHONPATH=. python -m pytest tests/services/test_hit_snippet.py -p no:cacheprovider   # 期望 1 failed
git checkout -- app/services/keyword_text.py

# 定向全量（全新容器，不动运行中的 api）
cd code && docker compose run --rm --no-deps --entrypoint sh api \
  -c "pip install -q '.[dev]' && python -m pytest tests/retrieve tests/graph tests/chat tests/eval tests/services -q && ruff check ."

# 真实投诉 chunk
docker compose exec -T api python - <<'EOF'
# SELECT c.content FROM chunks c JOIN documents d ON d.id=c.document_id
# WHERE d.filename='企业知识库.md' AND c.is_latest AND length(c.content)=447
# → hit_snippet(content, '公司几点上班') 含 '上班时间：9:00-12:00，13:30-18:00'
EOF
```
