"""评估集种子 —— 手册 §6 M2 任务 10 + §7.3。

共 63 条 eval_cases：43 可答 + 20 不可答。
  · 旧 30 条（20 可答 + 10 不可答）：通用模板，expected_doc_ids 留空（不进命中率分母），
    保留以与历史基线可比。
  · 2026-10-07 新增 33 条（23 可答 + 10 不可答）：逐条经检索层预检（阈值置 0）筛入，
    可答题标注 expected_doc_ids，命中率可判定；刻意混入英文术语/编号类问题
    （JVM / HashMap / XYZ-1000 / M-1002 / AccessToken 等），覆盖 tsquery 互补场景。
★ 脚本幂等：按 (tenant_id, question) 去重，重复执行只补不删。
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import Document, EvalCase, KnowledgeBase, Tenant

# 20 条可答问题（通用模板，需根据实际文档校准）
ANSWERABLE_QUESTIONS = [
    "公司的报销流程是什么？",
    "员工手册里考勤制度是怎么规定的？",
    "新员工入职流程有哪些步骤？",
    "公司组织架构是什么样的？",
    "代码规范对语言选择有什么要求？",
    "绩效考核采用什么体系？",
    "财务审批流程分为几层？",
    "公司薪酬结构是怎样的？",
    "API 设计规范有哪些要求？",
    "安全操作规程包含哪些内容？",
    "故障处理的标准流程是什么？",
    "劳动合同模板包含哪些条款？",
    "年度预算报告的核心数据是什么？",
    "公司实行什么样的工时制？",
    "员工报销需要附什么材料？",
    "代码提交规范是什么？",
    "绩效考核结果分几档？",
    "差旅费报销需要什么凭证？",
    "公司法定节假日怎么执行？",
    "新员工多久内要签员工手册？",
]

# 10 条不可答问题（知识库里没有的，MUST 拒答）
UNANSWERABLE_QUESTIONS = [
    "今天天气怎么样？",
    "推荐一个好吃的菜谱？",
    "最近哪只股票值得买？",
    "世界杯哪支球队赢了？",
    "某明星的八卦是什么？",
    "北京到上海的机票多少钱？",
    "怎么做红烧肉？",
    "最新电影票房是多少？",
    "某城市的房价走势如何？",
    "某游戏通关攻略是什么？",
]

# 2026-10-07 新增 23 条可答：(问题, 期望命中文档文件名)
# 预检口径：阈值置 0 跑纯检索层，top1 均命中期望文档（top1 区间 0.539~0.790）。
# 其中 19 条含英文术语/编号词元（JVM/HashMap/XYZ-1000/M-1002/Docling/AccessToken 等），
# 用于覆盖 tsquery 英文/数字词元精确匹配的互补召回场景。
NEW_ANSWERABLE_QUESTIONS: list[tuple[str, str]] = [
    # ── JAVA 面试 PDF（英文术语密集）──
    ("JVM 运行时内存区域分为哪几部分？", "JAVA面试核心知识点整理.pdf"),
    ("HashMap 的底层数据结构是怎样的？", "JAVA面试核心知识点整理.pdf"),
    ("Java 为什么能够跨平台？", "JAVA面试核心知识点整理.pdf"),
    ("volatile 关键字有什么作用？", "JAVA面试核心知识点整理.pdf"),
    ("synchronized 和 ReentrantLock 有什么区别？", "JAVA面试核心知识点整理.pdf"),
    # ── 产品规格说明.txt（型号编号 XYZ-1000）──
    ("XYZ-1000 智能终端的处理器是什么配置？", "产品规格说明.txt"),
    ("XYZ-1000 整机保修几年？", "产品规格说明.txt"),
    ("XYZ-1000 配备了哪些接口？", "产品规格说明.txt"),
    ("XYZ-1000 待机和满载功耗分别是多少？", "产品规格说明.txt"),
    # ── 销售数据.xlsx / 库存清单.xls（精确编号）──
    ("XYZ-1000 的销售单价是多少？", "销售数据.xlsx"),
    ("XYZ-2000 的销售单价是多少？", "销售数据.xlsx"),
    ("M-1001 主板组件在上海仓有多少库存？", "库存清单.xls"),
    ("M-1002 物料是什么、库存状态如何？", "库存清单.xls"),
    ("深圳仓哪些物料处于待补货状态？", "库存清单.xls"),
    # ── 员工手册.docx（2026 版）──
    ("研发岗试用期多长、转正怎么考核？", "员工手册.docx"),
    ("司龄 3 年以上年假有多少天？", "员工手册.docx"),
    ("试用期工资不低于转正工资的多少？", "员工手册.docx"),
    # ── 项目实战-企业知识库.pdf（RAG / Docling / HTML 英文术语）──
    ("Docling 在 RAG 系统里用来做什么？", "项目实战-企业知识库.pdf"),
    ("RAG 冠军方案为什么把表格序列化从 Markdown 改成 HTML？", "项目实战-企业知识库.pdf"),
    # ── 畅韵科技 md（AccessToken / RefreshToken）──
    ("AccessToken 和 RefreshToken 的有效期分别是多久？",
     "畅韵科技-跨境数据中台全栈岗-面试终极背诵手册.md"),
    # ── Vue3 / 前端文档（Composition API / Gzip 英文术语）──
    ("Vue3 的 Composition API 要解决什么问题？", "Vue3 Composition API 完整解析（面试版）.pdf"),
    ("Vue2 的响应式原理是怎么实现的？", "前端高频核心知识点完整版文档.docx"),
    ("开启 Gzip 压缩通常能减少多少资源体积？",
     "前端性能优化终极手册（React+Next.js+Webpack 完整版）.pdf"),
]

# 2026-10-07 新增 10 条不可答（库内无对应内容）
# 其中 2 条预检 top1 越过 0.50（Kubernetes 0.547 / 食堂菜单 0.512），
# 刻意保留用于检验 grounding 第二道闸；其余 8 条 top1 均 <0.50 由 L1 拦截。
NEW_UNANSWERABLE_QUESTIONS = [
    "Python 的 GIL 全局解释器锁是什么？",
    "Kubernetes 的 Pod 调度策略有哪些？",
    "2026 年诺贝尔物理学奖得主是谁？",
    "比特币当前价格是多少美元？",
    "帮我写一首关于秋天的七言绝句",
    "F09 会议室今天下午有空吗？",
    "公司食堂本周菜单有什么菜？",
    "上海到纽约的直飞飞行时间多长？",
    "怎么腌腊八蒜？",
    "最新一期双色球开奖号码是什么？",
]

# 2026-10-25 新增 8 条可答：型号别名形态（P-B 型号/标识符归一化）
# 文档里固定写 `XYZ-1000` / `M-1002`，提问改成无连字符、空格分隔、大小写混排三种形态，
# 覆盖「同一型号多种写法」的长尾召回。入库前必须过 precheck_cases.py（阈值 0）：
# 只有 top1 命中期望文档的用例才允许进集。
MODEL_ALIAS_ANSWERABLE_QUESTIONS: list[tuple[str, str]] = [
    ("xyz1000 智能终端的处理器是什么配置？", "产品规格说明.txt"),
    ("XYZ 1000 整机保修几年？", "产品规格说明.txt"),
    ("Xyz1000 配备了哪些接口？", "产品规格说明.txt"),
    ("xyz1000 待机和满载功耗分别是多少？", "产品规格说明.txt"),
    ("xyz1000 的销售单价是多少？", "销售数据.xlsx"),
    ("xyz2000 的销售单价是多少？", "销售数据.xlsx"),
    ("m1001 主板组件在上海仓有多少库存？", "库存清单.xls"),
    ("M 1002 物料是什么、库存状态如何？", "库存清单.xls"),
]

# 2026-10-25 新增 3 条不可答：型号本身不存在，用于验证归一化没有把「相似型号」误连
MODEL_ALIAS_UNANSWERABLE_QUESTIONS = [
    "XYZ-9000 的销售单价是多少？",
    "M-9999 物料在深圳仓有多少库存？",
    "abc9999 的保修政策是什么？",
]


async def seed_eval() -> None:
    settings = get_settings()
    async with SessionLocal() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.code == settings.tenant_code)
        )
        if tenant is None:
            raise RuntimeError("租户未初始化，先跑 scripts/seed.py")

        # ★ 表非空即整体跳过，而不是逐条去重补录。
        #   entrypoint 每次启动都会跑本脚本，逐条补录的后果是：只要有人
        #   清理过下面的内置用例（评测集换成经预检筛过的版本时必然发生），
        #   下一次容器重启就会把它们全部插回去，评测集构成被悄悄改变，
        #   上一轮基线直接失去可比性。
        #   现在的口径：本脚本只负责「空库初始化」，评测集的增删改一律走
        #   scripts/import_eval_cases.py（带备份 + 预检）。
        existing_total = await session.scalar(
            select(func.count()).select_from(EvalCase).where(EvalCase.tenant_id == tenant.id)
        )
        if existing_total:
            print(f"eval_cases 已有 {existing_total} 条，跳过种子（评测集以已导入版本为准）")
            return

        # 取第一个知识库作为默认 kb_id（用户可后续调整）
        kb = await session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id).limit(1)
        )
        kb_id = kb.id if kb else None

        inserted = 0
        for question in ANSWERABLE_QUESTIONS:
            existing = await session.scalar(
                select(EvalCase).where(
                    EvalCase.tenant_id == tenant.id, EvalCase.question == question
                )
            )
            if existing is None:
                session.add(
                    EvalCase(
                        tenant_id=tenant.id,
                        kb_id=kb_id,
                        question=question,
                        expected_answerable=True,
                        expected_doc_ids=[],
                        note="可答（需校准 expected_doc_ids）",
                    )
                )
                inserted += 1

        for question in UNANSWERABLE_QUESTIONS:
            existing = await session.scalar(
                select(EvalCase).where(
                    EvalCase.tenant_id == tenant.id, EvalCase.question == question
                )
            )
            if existing is None:
                session.add(
                    EvalCase(
                        tenant_id=tenant.id,
                        kb_id=kb_id,
                        question=question,
                        expected_answerable=False,
                        expected_doc_ids=[],
                        note="不可答（MUST 拒答）",
                    )
                )
                inserted += 1

        # ── 2026-10-07 新增可答：按文件名反查全部 indexed 文档 id 作为期望命中集合。
        #   库内存在同名重复文档（如企业知识库.md 两份），任一命中即算命中。
        filename_to_doc_ids: dict[str, list[uuid.UUID]] = {}
        for _question, expected_filename in (
            NEW_ANSWERABLE_QUESTIONS + MODEL_ALIAS_ANSWERABLE_QUESTIONS
        ):
            if expected_filename in filename_to_doc_ids:
                continue
            docs = (
                await session.execute(
                    select(Document).where(
                        Document.tenant_id == tenant.id,
                        Document.filename == expected_filename,
                        Document.status == "indexed",
                    )
                )
            ).scalars().all()
            filename_to_doc_ids[expected_filename] = [d.id for d in docs]
            if not docs:
                print(f"[!] 未找到 indexed 文档：{expected_filename}，该用例 expected_doc_ids 为空")

        for question, expected_filename in NEW_ANSWERABLE_QUESTIONS:
            existing = await session.scalar(
                select(EvalCase).where(
                    EvalCase.tenant_id == tenant.id, EvalCase.question == question
                )
            )
            if existing is None:
                session.add(
                    EvalCase(
                        tenant_id=tenant.id,
                        kb_id=kb_id,
                        question=question,
                        expected_answerable=True,
                        expected_doc_ids=filename_to_doc_ids[expected_filename],
                        note=f"可答（2026-10-07 检索层预检，期望 {expected_filename}）",
                    )
                )
                inserted += 1

        # ── 2026-10-07 新增不可答 ──
        for question in NEW_UNANSWERABLE_QUESTIONS:
            existing = await session.scalar(
                select(EvalCase).where(
                    EvalCase.tenant_id == tenant.id, EvalCase.question == question
                )
            )
            if existing is None:
                session.add(
                    EvalCase(
                        tenant_id=tenant.id,
                        kb_id=kb_id,
                        question=question,
                        expected_answerable=False,
                        expected_doc_ids=[],
                        note="不可答（MUST 拒答，2026-10-07 扩样）",
                    )
                )
                inserted += 1

        # ── 2026-10-25 新增：型号别名形态（P-B 型号归一化）──
        for question, expected_filename in MODEL_ALIAS_ANSWERABLE_QUESTIONS:
            existing = await session.scalar(
                select(EvalCase).where(
                    EvalCase.tenant_id == tenant.id, EvalCase.question == question
                )
            )
            if existing is None:
                session.add(
                    EvalCase(
                        tenant_id=tenant.id,
                        kb_id=kb_id,
                        question=question,
                        expected_answerable=True,
                        expected_doc_ids=filename_to_doc_ids[expected_filename],
                        note=f"可答（2026-10-25 型号别名，期望 {expected_filename}）",
                    )
                )
                inserted += 1

        for question in MODEL_ALIAS_UNANSWERABLE_QUESTIONS:
            existing = await session.scalar(
                select(EvalCase).where(
                    EvalCase.tenant_id == tenant.id, EvalCase.question == question
                )
            )
            if existing is None:
                session.add(
                    EvalCase(
                        tenant_id=tenant.id,
                        kb_id=kb_id,
                        question=question,
                        expected_answerable=False,
                        expected_doc_ids=[],
                        note="不可答（2026-10-25 型号不存在，MUST 拒答）",
                    )
                )
                inserted += 1

        await session.commit()
        total_answerable = (
            len(ANSWERABLE_QUESTIONS)
            + len(NEW_ANSWERABLE_QUESTIONS)
            + len(MODEL_ALIAS_ANSWERABLE_QUESTIONS)
        )
        total_unanswerable = (
            len(UNANSWERABLE_QUESTIONS)
            + len(NEW_UNANSWERABLE_QUESTIONS)
            + len(MODEL_ALIAS_UNANSWERABLE_QUESTIONS)
        )
        print(
            f"eval_cases 插入 {inserted} 条；当前用例集合计 "
            f"{total_answerable + total_unanswerable} 条"
            f"（可答 {total_answerable} + 不可答 {total_unanswerable}）"
        )


if __name__ == "__main__":
    asyncio.run(seed_eval())
