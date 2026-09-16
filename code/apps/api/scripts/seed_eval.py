"""评估集种子 —— 手册 §6 M2 任务 10 + §7.3。

30 条 eval_cases：20 可答 + 10 不可答。
★ 用户上传真实文档后，需要根据文档内容调整 question / expected_doc_ids。
  本脚本提供通用模板，跑前先校准。
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import EvalCase, KnowledgeBase, Tenant

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


async def seed_eval() -> None:
    settings = get_settings()
    async with SessionLocal() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.code == settings.tenant_code)
        )
        if tenant is None:
            raise RuntimeError("租户未初始化，先跑 scripts/seed.py")

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

        await session.commit()
        print(f"eval_cases 插入 {inserted} 条（可答 {len(ANSWERABLE_QUESTIONS)} + 不可答 {len(UNANSWERABLE_QUESTIONS)}）")


if __name__ == "__main__":
    asyncio.run(seed_eval())
