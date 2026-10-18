"""OCR 字符错误率（CER）实测 —— 回答「去噪/去污到底提升了多少」。

口径（三条，别混）：
  raw      = 原图直接送 OCR
  enhanced = 过门禁增强后送 OCR（verdict=pass 时与 raw 同字节）
  indexed  = 增强 + 水印行清理，即最终进 chunk 的文本

CER = (替换 + 删除 + 插入) / GT 字符数，中文按字符计，先去全部空白
（版面空行/缩进不是识别错误，混进去会让 CER 虚高）。

★ 合成页的优势：GT 就是生成时的 BODY 文本，精确可比，不需要人工标注。
★ 局限：合成脏页分布窄于真实扫描件，绝对值只能看方向与量级，不能当线上 SLA。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.services.ocr import ocr_image
from app.services.parse.image_quality import analyze, prepare_for_ocr, strip_watermark_text

# 与 gen_dirty_pages.py 的 BODY 严格一致（合成页的 ground truth）
GT_LINES = [
    "员工报销管理办法（2026 年修订版）",
    "第一条 为规范公司费用报销流程，提高审批效率，制定本办法。",
    "第二条 报销范围包括差旅费、交通费、住宿费、业务招待费。",
    "第三条 员工应在费用发生后三十日内提交报销申请，逾期需说明原因。",
    "第四条 报销凭证须为发票原件或电子发票打印件，复印件不予受理。",
    "第五条 单笔金额超过五千元的，需部门负责人与财务负责人共同签批。",
    "第六条 住宿费标准按职级执行，一线城市上限为每晚六百元。",
    "第七条 差旅期间产生的市内交通费，凭票据实报实销，单日不超过一百元。",
    "第八条 业务招待费须事前申请，注明客户名称、人数与事由。",
    "第九条 财务部应在收到完整单据后五个工作日内完成审核并付款。",
    "第十条 本办法自发布之日起施行，由财务部负责解释。",
]
GT = "".join(GT_LINES)


def _norm(s: str) -> str:
    """去空白 + 去 OCR 结构标记残留，只留可比字符。"""
    return "".join(s.split())


def _lev(a: str, b: str) -> int:
    """编辑距离（替换/删除/插入各计 1）。短文本，O(n·m) 足够。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(hyp: str, ref: str = GT) -> float:
    h, r = _norm(hyp), _norm(ref)
    return _lev(h, r) / len(r) if r else 0.0


async def ocr_once(data: bytes) -> str:
    block = await ocr_image(data, "image/png")
    return block.text


async def run_one(path: Path) -> dict:
    data = path.read_bytes()
    report = analyze(data)

    raw_text = await ocr_once(data)

    enhanced, _ = prepare_for_ocr(data, source="image")
    same = enhanced is data or enhanced == data
    enh_text = raw_text if same else await ocr_once(enhanced)

    cleaned, cleanup = strip_watermark_text(
        enh_text, watermark_detected=bool(report.watermark_detected)
    )

    return {
        "file": path.name,
        "verdict": report.verdict,
        "enhanced_bytes": not same,
        "wm_detected": report.watermark_detected,
        "wm_removed_lines": cleanup.removed_lines,
        "raw_cer": cer(raw_text),
        "enh_cer": cer(enh_text),
        "idx_cer": cer(cleaned),
        "raw_chars": len(_norm(raw_text)),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/imgsamples")
    ap.add_argument(
        "--only",
        default="",
        help="只跑指定文件，逗号分隔（如 01_clean.png,05_stain.png）；留空 = 全部",
    )
    args = ap.parse_args()

    files = sorted(Path(args.dir).glob("*.png"))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        files = [f for f in files if f.name in wanted]
    if not files:
        print(f"没有匹配的文件：dir={args.dir} only={args.only!r}")
        return 2

    rows = []
    for f in files:
        r = await run_one(f)
        rows.append(r)
        print(
            f"{r['file']:20s} {r['verdict']:6s} "
            f"enh={'Y' if r['enhanced_bytes'] else 'N'} wm={'Y' if r['wm_detected'] else 'N'} "
            f"raw={r['raw_cer']:6.2%} enh={r['enh_cer']:6.2%} idx={r['idx_cer']:6.2%}",
            flush=True,
        )

    print("\n=== 汇总（CER 越低越好，GT 共 %d 字）===" % len(GT))
    print(f"{'文件':20s} {'raw':>8s} {'enh':>8s} {'indexed':>8s} {'变化':>9s}")
    for r in rows:
        delta = r["enh_cer"] - r["raw_cer"]
        print(
            f"{r['file']:20s} {r['raw_cer']:8.2%} {r['enh_cer']:8.2%} "
            f"{r['idx_cer']:8.2%} {delta:+9.2%}"
        )

    improved = [r for r in rows if r["enh_cer"] < r["raw_cer"]]
    worsened = [r for r in rows if r["enh_cer"] > r["raw_cer"]]
    print(
        f"\n增强改善 {len(improved)} 张，恶化 {len(worsened)} 张，"
        f"无变化 {len(rows) - len(improved) - len(worsened)} 张"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
