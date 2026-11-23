"""S6-7：备份脚本必须覆盖 N 库，并落盘备份窗口与 RPO 政策值。

两层结构：

  (a) **契约层（必跑）**：静态检查脚本结构 —— 动态枚举库（pg_database）、
      逐库 `pg_dump -Fc`、任一库失败即中止、清单含窗口与 RPO 政策值、
      MinIO 未丢、不再只备份单个 `$POSTGRES_DB`。
  (b) **行为层（有可用 bash 时跑）**：用假 docker 替换外部命令真跑脚本，
      断言控制库 + 场景库各有一份 dump、维护库被排除、manifest 字段齐全、
      某库导出失败时脚本非零退出且不产出清单。

★ 行为层在本机通过 WSL 的 bash 执行（脚本本身是部署侧 Linux bash）。
  没有可用 bash 时明确 skip，不伪造通过。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# tests/scenario/test_step6_backup_script.py -> code/
CODE_DIR = Path(__file__).resolve().parents[4]
BACKUP_SH = CODE_DIR / "scripts" / "backup.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
# 假 docker：只实现 backup.sh 用到的三种调用（psql 枚举 / pg_dump / tar 打包）。
set -u
args="$*"

if [[ "$args" == *" psql "* ]]; then
  printf 'kagent\\nkagent_finance\\npostgres\\n'
  exit 0
fi

if [[ "$args" == *" pg_dump "* ]]; then
  db="${@: -1}"
  if [ -n "${FAKE_PG_DUMP_FAIL_DB:-}" ] && [ "$db" = "$FAKE_PG_DUMP_FAIL_DB" ]; then
    echo "simulated pg_dump failure: $db" >&2
    exit 1
  fi
  printf 'PGDUMP:%s\\n' "$db"
  exit 0
fi

if [[ "$args" == *" tar czf "* ]]; then
  for ((i = 1; i <= $#; i++)); do
    if [ "${!i}" = "-v" ]; then
      j=$((i + 1))
      spec="${!j}"
      if [[ "$spec" == *:/backup ]]; then
        host="${spec%:/backup}"
        printf 'MINIO\\n' > "$host/minio-data.tar.gz"
      fi
    fi
  done
  exit 0
fi

exit 0
"""


def _bash_path(win_path: str) -> str:
    """把 Windows 路径转成 bash 能用的路径；Linux 上原样返回。"""
    if sys.platform != "win32":
        return win_path
    # Windows 的 C:\Windows\System32\bash.exe 不会把 -c 之后的位置参数转发给
    # 真正的 bash，必须经 wsl -e 启动才能正确传参。
    out = subprocess.run(
        ["wsl", "-e", "bash", "-c", 'wslpath -a -u "$1"', "sh", win_path],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.fixture(scope="module")
def bash_cmd() -> list[str]:
    """返回可用的 bash 启动命令；不可用则 skip（不伪造通过）。"""
    if sys.platform == "win32":
        if not shutil.which("wsl"):
            pytest.skip("本机没有 wsl，跳过备份脚本行为层")
        cmd = ["wsl", "-e", "bash"]
    else:
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("本机没有 bash，跳过备份脚本行为层")
        cmd = [bash]
    probe = subprocess.run([*cmd, "-c", "true"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("bash 不可执行（WSL/子系统未就绪），跳过行为层")
    if sys.platform == "win32":
        try:
            _bash_path(str(BACKUP_SH))
        except subprocess.CalledProcessError:
            pytest.skip("bash 中无法访问工作区（wslpath 失败），跳过行为层")
    return cmd


# ══════════════════════════════════════════════════════════════════════
# A. 契约层（必跑）
# ══════════════════════════════════════════════════════════════════════


def test_script_enumerates_all_business_databases():
    """库清单来自 pg_database，而不是单个 $POSTGRES_DB。"""
    text = BACKUP_SH.read_text(encoding="utf-8")

    assert "pg_database" in text
    assert "datistemplate = false" in text
    assert "datallowconn = true" in text
    # 维护库在宿主侧排除，避免把 postgres 库当成业务库备份
    assert "grep -vx 'postgres'" in text
    # 不允许再出现"只 dump 一个库"的旧写法
    assert '-d "$POSTGRES_DB"' not in text


def test_each_database_gets_its_own_custom_format_dump():
    """逐库 custom 格式导出，文件名与库名一一对应。"""
    text = BACKUP_SH.read_text(encoding="utf-8")

    assert 'pg_dump -U "$POSTGRES_USER" -Fc -d "$1"' in text
    assert '"${OUT_ABS}/db_${db}.dump"' in text
    assert "db_names+=(\"$db\")" in text


def test_any_database_failure_aborts_the_backup():
    """任一库导出失败必须非零退出，不能带着缺库的备份继续。"""
    text = BACKUP_SH.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert "if ! $COMPOSE exec" in text
    assert "导出失败" in text


def test_manifest_records_window_and_rpo_policy():
    """清单必须含窗口与 RPO 政策值，便于 U13 回填。"""
    text = BACKUP_SH.read_text(encoding="utf-8")

    for key in (
        "started_at=",
        "finished_at=",
        "window_seconds=",
        "database_count=",
        "rpo_policy=",
        "minio_seconds=",
    ):
        assert key in text, key
    assert "每日一次全库 dump" in text
    assert "manifest.txt" in text


def test_minio_backup_is_kept():
    """PostgreSQL 扩展不能挤掉 MinIO 原始文档备份。"""
    text = BACKUP_SH.read_text(encoding="utf-8")

    assert "minio-data.tar.gz" in text
    assert "-v kagent_minio_data:/data:ro" in text


# ══════════════════════════════════════════════════════════════════════
# B. 行为层（真跑脚本，假 docker 替换外部命令）
# ══════════════════════════════════════════════════════════════════════


def _forward_to_wsl(env: dict[str, str], names: list[str]) -> None:
    """Windows 下自定义变量默认不会进入 WSL，必须登记到 WSLENV。"""
    if sys.platform != "win32":
        return
    existing = [item for item in env.get("WSLENV", "").split(":") if item]
    env["WSLENV"] = ":".join(dict.fromkeys([*existing, *names]))


def _run_backup(bash_cmd: list[str], tmp: Path, extra_env: dict[str, str] | None = None):
    """在临时目录里跑一次备份脚本，返回 (完成结果, 本次输出目录)。"""
    fake = tmp / "fake-docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
    backup_root = tmp / "backups"
    env = os.environ.copy()
    env["BACKUP_DOCKER"] = _bash_path(str(fake))
    env["BACKUP_DIR"] = _bash_path(str(backup_root))
    # 清掉可能从外部带进来的失败注入，避免用例互相污染
    env.pop("FAKE_PG_DUMP_FAIL_DB", None)
    if extra_env:
        env.update(extra_env)
    _forward_to_wsl(env, ["BACKUP_DIR", "BACKUP_DOCKER", "FAKE_PG_DUMP_FAIL_DB"])

    proc = subprocess.run(
        [*bash_cmd, _bash_path(str(BACKUP_SH))],
        cwd=str(BACKUP_SH.parent.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, backup_root


def test_backup_covers_control_and_scene_databases(bash_cmd: list[str]):
    """真跑：控制库与场景库都有 dump，维护库 postgres 被排除。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        proc, backup_root = _run_backup(bash_cmd, tmp)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        runs = list(backup_root.iterdir())
        assert len(runs) == 1
        out_dir = runs[0]

        control = out_dir / "db_kagent.dump"
        scene = out_dir / "db_kagent_finance.dump"
        assert control.read_text(encoding="utf-8").strip() == "PGDUMP:kagent"
        assert scene.read_text(encoding="utf-8").strip() == "PGDUMP:kagent_finance"
        assert not (out_dir / "db_postgres.dump").exists()
        assert (out_dir / "minio-data.tar.gz").exists()

        manifest = (out_dir / "manifest.txt").read_text(encoding="utf-8")
        assert "database_count=2" in manifest
        assert "rpo_policy=每日一次全库 dump" in manifest
        for field in ("started_at=", "finished_at=", "window_seconds="):
            assert field in manifest
        assert "database\tkagent\t" in manifest
        assert "database\tkagent_finance\t" in manifest
        assert "minio_seconds=" in manifest


def test_backup_fails_fast_when_one_database_dump_fails(bash_cmd: list[str]):
    """任一库导出失败：非零退出、不产出清单（避免把它当成有效备份）。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        proc, backup_root = _run_backup(
            bash_cmd, tmp, {"FAKE_PG_DUMP_FAIL_DB": "kagent_finance"}
        )

        assert proc.returncode != 0
        assert "kagent_finance" in proc.stderr
        manifests = list(backup_root.rglob("manifest.txt"))
        assert manifests == []


def test_backup_reports_no_database(bash_cmd: list[str]):
    """枚举为空时必须明确失败，而不是静默产出空备份。"""
    text = BACKUP_SH.read_text(encoding="utf-8")
    assert "未枚举到任何业务数据库" in text

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        fake = tmp / "fake-docker-empty"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                args="$*"
                if [[ "$args" == *" psql "* ]]; then
                  printf 'postgres\\n'
                fi
                exit 0
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        env = os.environ.copy()
        env["BACKUP_DOCKER"] = _bash_path(str(fake))
        env["BACKUP_DIR"] = _bash_path(str(tmp / "backups"))
        _forward_to_wsl(env, ["BACKUP_DIR", "BACKUP_DOCKER"])
        proc = subprocess.run(
            [*bash_cmd, _bash_path(str(BACKUP_SH))],
            cwd=str(BACKUP_SH.parent.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0
        assert "未枚举到任何业务数据库" in proc.stderr
