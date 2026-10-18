"""脏页真实入库验证（P0-2）—— 走 HTTP 上传，验证门禁在完整链路里真的生效。

用法（容器内）：
    python scripts/verify_dirty_pages.py --dir /tmp/imgsamples --kb <kb_id>

做四件事：
  1. RSA-OAEP 加密密码登录，拿 access token（与前端同路径）
  2. 逐张 POST /api/kbs/{kb}/documents（真实上传，不做 docker cp 绕过）
  3. 轮询 documents.status 直到 indexed / failed，记录 parse_jobs.last_error
  4. 对 indexed 的，直接查 chunks 判断水印行是否真的进了库

★ 这是「端到端」与「直调 analyze()」的分界线：只有走完这条链路，
  才能说「门禁在真实入库里生效」，否则只是单元级验证。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import text

from app.database import engine

API = "http://localhost:8000/api"
ADMIN, PASSWORD = "admin", "ChangeMe123!"

# 水印判定特征：chunk 里出现这些短句，说明文本层清理没兜住
WM_MARKERS = ("内部资料", "严禁外传", "禁止外传", "机密", "BXYC")


def _encrypt(pub_b64: str, plain: str) -> str:
    pub = serialization.load_der_public_key(base64.b64decode(pub_b64))
    ct = pub.encrypt(
        plain.encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ct).decode()


async def login(c: httpx.AsyncClient) -> str:
    pk = (await c.get(f"{API}/auth/public-key")).json()["public_key"]
    r = await c.post(
        f"{API}/auth/login",
        json={"username": ADMIN, "password": _encrypt(pk, PASSWORD)},
    )
    r.raise_for_status()
    return r.json()["token"]


async def upload(c: httpx.AsyncClient, kb: str, path: Path) -> dict:
    r = await c.post(
        f"{API}/kbs/{kb}/documents",
        files={"file": (path.name, path.read_bytes(), "image/png")},
    )
    return {"file": path.name, "code": r.status_code, "body": r.json()}


async def list_docs(c: httpx.AsyncClient, kb: str) -> list[dict]:
    r = await c.get(f"{API}/kbs/{kb}/documents", params={"page_size": 100})
    body = r.json()
    # 该端点在部分版本返回裸 list，另一部分包 {items: [...]} —— 两种都吃
    return body.get("items", []) if isinstance(body, dict) else body


async def wait_done(c: httpx.AsyncClient, kb: str, names: set[str], timeout: float):
    """轮询到全部终态（indexed / failed）或超时。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        docs = await list_docs(c, kb)
        todo = {
            d["filename"]
            for d in docs
            if d["filename"] in names and d["status"] in ("pending", "parsing")
        }
        if not todo:
            return docs
        print(f"  …解析中 {len(todo)} 个（已等 {time.time()-t0:.0f}s）", flush=True)
        await asyncio.sleep(10)
    return await list_docs(c, kb)


async def chunk_report(doc_ids: list[str]) -> dict[str, dict]:
    """按文档统计 chunk 数与水印命中 —— 判定文本层清理是否真的生效。"""
    out: dict[str, dict] = {}
    async with engine.connect() as conn:
        for did in doc_ids:
            rows = (
                await conn.execute(
                    text(
                        "select c.content from chunks c join documents d on d.id = c.document_id "
                        "where d.id = :did"
                    ),
                    {"did": did},
                )
            ).fetchall()
            joined = "\n".join(r[0] or "" for r in rows)
            hits = {m: joined.count(m) for m in WM_MARKERS if m in joined}
            out[did] = {"chunks": len(rows), "chars": len(joined), "wm_hits": hits}
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/imgsamples")
    ap.add_argument("--kb", required=True)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument(
        "--skip-upload",
        action="store_true",
        help="不重新上传，只轮询并出报告（避免重复上传产生新版本）",
    )
    args = ap.parse_args()

    files = sorted(Path(args.dir).glob("*.png"))
    if not files:
        print(f"目录无 png：{args.dir}")
        return 2

    async with httpx.AsyncClient(timeout=120) as c:
        token = await login(c)
        c.headers["Authorization"] = f"Bearer {token}"
        names = {f.name for f in files}

        if args.skip_upload:
            print(f"跳过上传，直接轮询 {len(names)} 个已有文档\n")
        else:
            print(f"登录成功，开始上传 {len(files)} 张 → KB {args.kb}\n")
            uploaded = []
            for f in files:
                res = await upload(c, args.kb, f)
                mark = "OK " if res["code"] == 201 else f"{res['code']}"
                print(f"[{mark}] {f.name}  {str(res['body'])[:110]}")
                uploaded.append(res)

            names = {r["file"] for r in uploaded if r["code"] == 201}
            if not names:
                print("\n没有任何文件上传成功 —— 先确认 ALLOWED_EXT 与 OCR_ENABLED")
                return 1

        print(f"\n等待 {len(names)} 个文档解析完成（上限 {args.timeout:.0f}s）…")
        docs = await wait_done(c, args.kb, names, args.timeout)

    rows = [d for d in docs if d["filename"] in names]
    print("\n=== 解析结果 ===")
    for d in sorted(rows, key=lambda x: x["filename"]):
        print(f"{d['filename']:22s} {d['status']:9s} id={d['id']}")

    ok = [d for d in rows if d["status"] == "indexed"]
    if ok:
        rep = await chunk_report([d["id"] for d in ok])
        print("\n=== chunk 与水印命中 ===")
        for d in ok:
            r = rep[d["id"]]
            flag = "⚠ 水印残留" if r["wm_hits"] else "✓ 无残留"
            print(
                f"{d['filename']:22s} chunks={r['chunks']:3d} chars={r['chars']:5d} "
                f"{flag} {r['wm_hits'] or ''}"
            )

    failed = [d for d in rows if d["status"] != "indexed"]
    if failed:
        print("\n=== 非 indexed 文档的错误 ===")
        async with engine.connect() as conn:
            for d in failed:
                err = (
                    await conn.execute(
                        text(
                            "select last_error from parse_jobs where document_id = :d "
                            "order by created_at desc limit 1"
                        ),
                        {"d": d["id"]},
                    )
                ).scalar()
                print(f"{d['filename']:22s} {d['status']}: {str(err)[:200]}")

    print(f"\n小结：{len(ok)}/{len(rows)} 入库成功，{len(failed)} 未成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
