"""打印当前运行时配置口径（脱敏）—— 方案三：运行时配置版本。

用法：
  python scripts/snapshot_config.py

输出 JSON：{ "config_fingerprint": "...", "runtime_config": { ...脱敏... } }

用途：
  ① 交付留档 —— 记下「这台机器跑的是什么配置」；
  ② 环境比对 —— 两套环境（如预发 vs 生产）配置指纹相同即配置一致，不同则逐项查差异。

★ 输出已脱敏（URL 去掉账号密码、密钥不回显），可安全贴进工单 / 发布记录。
"""
from __future__ import annotations

import json

from app.config.config_profile import config_fingerprint, runtime_config_snapshot


def main() -> int:
    snapshot = runtime_config_snapshot()
    print(
        json.dumps(
            {
                "config_fingerprint": config_fingerprint(snapshot),
                "runtime_config": snapshot,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())