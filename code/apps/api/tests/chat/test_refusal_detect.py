"""拒答识别单测 —— generate.is_no_answer_output。

用例取自 2026-10-18 全量评测（206 条）里 8 条真实的模型输出变体。
这批文本此前被严格等值比较漏判成"已回答"，导致：
  · 产品层：不发 refused 事件，前端展示「没找到 + 8 条不相关引用」
  · 度量层：拒答正确率被低估到 90.62%（真实应为 100%）
"""
from __future__ import annotations

import pytest

from app.services.generate import (
    is_no_answer_output,
    is_offtopic_fallback,
    is_unattributed_output,
)

# 8 条真实模型输出（节选自 /tmp/eval.log），全部**应当**判为拒答
REFUSAL_VARIANTS = [
    # 1. 尾部补括号说明
    "知识库中未找到相关内容（现有资料仅包含 Java 版快速排序的原理与代码片段，未提供 Python 实现）。",
    # 2. 插入加粗主语
    "知识库中未找到 **XYZ-2000 整机保修期**的相关内容。",
    # 3. 补一整句解释
    "知识库中未找到 **2027 版员工手册** 的任何内容；现有资料中只收录了 **员工手册（2026 版）**。",
    # 4. 整句加粗 + 补充其他仓库信息
    "**知识库中未找到杭州仓的库存清单。** 知识库中唯一的库存类文档是库存清单.xls。",
    # 5. 加粗主语 + 给替代答案
    "知识库中未找到 **XYZ-1000 售后维修网点电话**的相关内容。技术支持热线 400-888-1000。",
    # 6. 否定动词被加粗切断
    "知识库中**未找到**关于公司员工班车的任何内容，包括班车线路、班车时刻或班车乘坐规定等。",
    # 7. 换成"没有直接规定"的说法
    '知识库中没有直接规定"试用期员工能否请年假"的条款，但可以查询到以下相关依据。',
    # 8. 句中嵌标准句 + 冒号补充
    "知识库中未找到相关内容：现有资料未说明餐补是按天计算还是按月计算。",
    # 9. 否定词后接一个完整定语再落到"说明"（2026-10-18 第二轮评测新增）
    "知识库中没有关于“策略模式适用场景”的正文说明，**策略模式**仅在目录中被列为一条条目，位于第 201 页 [4]。",
    # 10. 同类："没有…的任何内容"
    "知识库中没有关于公司年会安排的任何内容。",
    # 11. 标题遮挡（2026-10-18 第五轮评测暴露的真实缺陷）：
    #     模型先写一个 markdown 标题，正文第一句才是契约句。旧实现归一化后
    #     字符串以标题文字开头，前缀匹配失效，3 条已明确拒答的回答被当成正常回答。
    "#### 说明\n\n知识库中**未找到**将 Docker 与传统虚拟机进行直接对比的内容，因此无法给出结论。",
    "#### 结论\n\n知识库中**未找到**直接说明“线程上下文切换会带来哪些开销”的内容 [1][3]。",
    "#### 关于打卡定位范围\n\n知识库中未找到关于考勤打卡**定位范围**的相关规定 [2][3]。",
    # 12. 二级标题 + 空行 + 加粗整句
    "## 回答\n\n**知识库中未找到 2027 版员工手册的任何内容。**",
    # 13. 部分确认型拒答：先说"仅能确认"一点，再声明关键点缺失
    "知识库中仅能确认餐补的归属与发放方式，**具体计算方式与金额标准未找到相关内容**。",
    "知识库中只能提供年假的通用规定，具体到试用期员工的适用性未找到相关内容。",
]

# 正常回答，**不能**被误判成拒答
ANSWER_VARIANTS = [
    "知识库中收录了《员工手册（2026 版）》，其中第 4 节规定实习岗试用期为 6 个月。",
    "根据《员工手册》第 4 节，实习岗试用期 6 个月，转正考核为导师评价 + 述职 [1]。",
    "年假天数按司龄计算：满 1 年 5 天，满 3 年 10 天。",
    "知识库中包含员工手册、库存清单、销售数据等 5 份文档。",
    "库存清单.xls 里有上海仓和深圳仓两个 sheet，杭州仓不在其中。",
    # 放宽否定词后的修饰长度后，这类"没有 + 名词"的正常回答不能被误伤
    "知识库中没有限制使用场景，开发者可以按需调用 [1]。",
    "知识库中有 5 份文档，其中员工手册规定了考勤制度 [2]。",
    # ★ 标题 + 实质答案：剥标题后首句是正常结论，MUST NOT 误判成拒答
    "#### 研发岗试用期与转正考核\n\n研发岗**试用期为 3 个月**，转正考核为项目答辩 + 代码评审 [1]。",
    "## 库存查询结果\n\n上海仓 M-1001 当前库存 420.0，高于安全库存线 [1]。",
    # ★ 正常回答末尾补一句"另有部分未收录"，只判首句才不会误伤
    "年假天数按司龄计算：满 1 年 5 天，满 3 年 10 天 [1]。另有部分特殊岗位细则知识库未收录。",
]


@pytest.mark.parametrize("text", REFUSAL_VARIANTS)
def test_refusal_variants_detected(text: str) -> None:
    assert is_no_answer_output(text) is True


@pytest.mark.parametrize("text", ANSWER_VARIANTS)
def test_normal_answers_not_refused(text: str) -> None:
    assert is_no_answer_output(text) is False


def test_empty_text_is_not_refusal() -> None:
    """L3 校验把句子全剥掉时走的是 UNGROUNDED 分支，不该在这里判成 NO_RELEVANT_CONTENT。"""
    assert is_no_answer_output("") is False
    assert is_no_answer_output("   \n ") is False


# ── 零引用（脱离上下文作答）───────────────────────────────────────
# 真实案例：问「帮我写一段快速排序的 Python 代码」，语料只有 Java 版，
# 模型直接吐了一份 Python 实现，开头是 "#### 结论"，全程无引用。
# 无引用的行在 L3 校验里一律保留，所以必须靠「有没有引用」这道兜底拦住。
HALLUCINATED_CODE = """#### 结论

快速排序的 Python 实现如下：

```python
def quick_sort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    return quick_sort([x for x in arr[1:] if x < pivot]) + [pivot] + quick_sort([x for x in arr[1:] if x >= pivot])
```

时间复杂度 O(n log n)，空间复杂度 O(log n)。
"""


def test_hallucinated_answer_detected() -> None:
    assert is_unattributed_output(HALLUCINATED_CODE, citation_count=8) is True


# ★ 零引用 ≠ 幻觉：模型把上下文概括得很准、只是整篇漏标 [n] 时不能判拒答。
# 真实误伤样本（2026-10-18 第七轮评测）：Netty 的 Channel 和传统 IO 流有什么区别
# → 答案每句都来自 context，一条 [n] 都没有，被旧逻辑直接判成拒答（可答题漏答）。
NETTY_CONTEXT = (
    "NIO 和传统 IO 之间第一个最大的区别是，IO 是面向流的，NIO 是面向缓冲区的。"
    "Java IO 面向流意味着每次从流中读一个或多个字节，直至读取所有字节，"
    "它们没有被缓存在任何地方。此外，它不能前后移动流中的数据。"
    "在 Netty 中，客户端连接建立后创建的 SocketChannel 会注册到 IO 线程池"
    "（sub reactor 线程池）的某个 IO 线程上，由该线程负责 SocketChannel 的读写和编解码工作。"
    "Netty 的所有 IO 操作都是异步非阻塞的。"
)

NETTY_ANSWER_NO_CITE = """#### 可依据上下文的回答

1. 客户端连接建立后创建的 SocketChannel 会注册到 IO 线程池的某个 IO 线程上，由该线程负责读写和编解码工作。

2. Netty 的所有 IO 操作都是异步非阻塞的。

3. 传统 IO 是面向流的，每次从流中读一个或多个字节，直至读取所有字节，它们没有被缓存在任何地方。"""


def test_zero_cite_but_grounded_answer_is_not_refused() -> None:
    """概括自上下文、仅漏标引用 —— 放行，否则可答题被判漏答。"""
    assert (
        is_unattributed_output(NETTY_ANSWER_NO_CITE, citation_count=8, context_text=NETTY_CONTEXT)
        is False
    )


def test_zero_cite_and_off_context_still_refused() -> None:
    """内容不在上下文里（Python 实现 vs Java 语料）—— 仍然拒答。"""
    java_context = (
        "快速排序的基本思想是：通过一趟排序将要排序的数据分割成独立的两部分，"
        "其中一部分的所有数据都比另外一部分的所有数据都要小。"
    )
    assert (
        is_unattributed_output(HALLUCINATED_CODE, citation_count=8, context_text=java_context)
        is True
    )


def test_array_subscript_is_not_a_citation() -> None:
    """`arr[0]` / `arr[1]` 不是引用编号。朴素正则会把它们当成 [0][1]，
    导致"零引用"兜底认定该答案带引用直接放行 —— 恰好放过要拦的那条。"""
    from app.services.generate import _CITE_RE

    assert _CITE_RE.search("pivot = arr[0]") is None
    assert _CITE_RE.search("return quick_sort(arr[1:])") is None
    assert _CITE_RE.search("结论是 6 个月 [1]。") is not None


@pytest.mark.parametrize("text", [
    "M-1002 是**电源模块**，当前库存 96.0，已低于安全库存线 [1]。",
    "研发岗的试用期为 **3 个月**，转正考核方式为**项目答辩 + 代码评审**[1]。",
])
def test_attributed_answer_not_flagged(text: str) -> None:
    assert is_unattributed_output(text, citation_count=8) is False


def test_no_retrieved_chunks_skips_unattributed_check() -> None:
    """检索为空时由 NO_RELEVANT_CONTENT 接管，不在这里重复判拒答。"""
    assert is_unattributed_output(HALLUCINATED_CODE, citation_count=0) is False


# ── 绕道作答（用「知识库中的相关信息」当标题）─────────────────────
# 两条真实样本：都带引用、都"客气"，前面的规则一条都拦不住。
OFFTOPIC_SAMPLES = [
    "#### 知识库中关于上下文切换的说明\n\n上下文切换是指把当前任务的状态保存下来……[3]",
    "#### 知识库中的相关信息\n\n福利补贴包含餐补、交通补贴、通讯补贴 [2]。",
]

NORMAL_HEADINGS = [
    "#### 研发岗试用期与转正考核\n\n1. 研发岗**试用期时长为 3 个月** [1]",
    "#### 知识库中的相关规定\n\n年假天数按司龄计算：满 1 年 5 天 [1]。",
    "**上海仓 M-1001 的库存数量为 420.0** [1]",
]


@pytest.mark.parametrize("text", OFFTOPIC_SAMPLES)
def test_offtopic_fallback_detected(text: str) -> None:
    assert is_offtopic_fallback(text) is True


@pytest.mark.parametrize("text", NORMAL_HEADINGS)
def test_normal_heading_not_flagged(text: str) -> None:
    """带实质结论的小标题不能被误判 —— 误判的代价是可答题被拒（漏答）。"""
    assert is_offtopic_fallback(text) is False
