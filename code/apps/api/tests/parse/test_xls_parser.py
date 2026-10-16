"""xls_parser 测试 —— 本模块此前零覆盖，随合并单元格修复补齐。

xlrd 只能读不能写，无法程序化生成 .xls 固件；合并展开逻辑用
monkeypatch 伪 workbook 钉住（含 formatting_info=True 调用契约），
真实 BIFF 解码由 xlrd 本身保证。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.parse import xls_parser
from app.services.parse.xls_parser import parse_xls


def _fake_sheet(name: str, rows: list[list[object]], merged: list[tuple]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        nrows=len(rows),
        ncols=max(len(r) for r in rows),
        row_values=lambda r, _rows=rows: list(_rows[r]),
        merged_cells=merged,
    )


def _patch_workbook(monkeypatch: pytest.MonkeyPatch, sheets: list[SimpleNamespace]) -> dict:
    """替换 xlrd.open_workbook；返回记录调用参数的容器。"""
    calls: dict = {}

    def _fake_open(file_contents, formatting_info=False):
        calls["formatting_info"] = formatting_info
        calls["bytes_len"] = len(file_contents)
        return SimpleNamespace(sheets=lambda: sheets)

    monkeypatch.setattr(xls_parser.xlrd, "open_workbook", _fake_open)
    return calls


def test_merged_cells_fill_down(monkeypatch: pytest.MonkeyPatch):
    """合并矩形 (rlo, rhi, clo, chi) 半开区间：左上值填满区域（半开边界）。"""
    sheet = _fake_sheet(
        "S",
        [["部门", "金额"], ["研发部", 100], [None, 200]],
        merged=[(1, 3, 0, 1)],  # A2:A3 合并（0 基，rhi/chi exclusive）
    )
    calls = _patch_workbook(monkeypatch, [sheet])

    blocks = parse_xls(b"fake-biff-bytes")
    assert calls["formatting_info"] is True, "merged_cells 只在 formatting_info=True 下被解析"
    lines = blocks[0].text.split("\n")
    assert lines[0] == "部门\t金额"
    assert lines[1] == "研发部\t100"
    assert lines[2] == "研发部\t200", "合并延续行 MUST 沿用左上值"


def test_no_merged_cells_unchanged(monkeypatch: pytest.MonkeyPatch):
    sheet = _fake_sheet("S", [["a", "b"], ["c", "d"]], merged=[])
    _patch_workbook(monkeypatch, [sheet])

    blocks = parse_xls(b"fake")
    assert blocks[0].text == "a\tb\nc\td"
    assert blocks[0].is_table is True
    assert blocks[0].table_header == ["a", "b"]
    assert blocks[0].heading_path == "S"


def test_cell_tab_newline_sanitized(monkeypatch: pytest.MonkeyPatch):
    sheet = _fake_sheet("S", [["名称", "值"], ["伪\t列", "多\n行"]], merged=[])
    _patch_workbook(monkeypatch, [sheet])

    lines = parse_xls(b"fake")[0].text.split("\n")
    assert len(lines) == 2, "换行消毒后不产生额外行"
    assert lines[1].split("\t") == ["伪 列", "多 行"]


def test_empty_sheet_skipped(monkeypatch: pytest.MonkeyPatch):
    empty = SimpleNamespace(name="空", nrows=0, ncols=0, merged_cells=[])
    filled = _fake_sheet("有", [["a", 1]], merged=[])
    _patch_workbook(monkeypatch, [empty, filled])

    blocks = parse_xls(b"fake")
    assert [b.heading_path for b in blocks] == ["有"]


def test_open_failure_raises_parse_error(monkeypatch: pytest.MonkeyPatch):
    def _boom(file_contents, formatting_info=False):
        raise ValueError("unsupported format")

    monkeypatch.setattr(xls_parser.xlrd, "open_workbook", _boom)
    with pytest.raises(Exception, match="XLS 打开失败"):
        parse_xls(b"bad")
