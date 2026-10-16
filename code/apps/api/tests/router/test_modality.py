"""形态判定单测（WU-M-02 / INV-M-01）。

四组用例：扩展名 / 请求体字段 / 源类型 / 未知输入报错，
外加指标落点校验（`rag.router.decision`）。
"""
from __future__ import annotations

import pytest

from app.infra import metrics
from app.services.router import (
    Modality,
    UnknownModalityError,
    detect,
    dispatch,
)


@pytest.fixture(autouse=True)
def _clean_metrics():
    """每个用例前后清空指标，避免互相污染。"""
    metrics.reset()
    yield
    metrics.reset()


# ── 扩展名组 ──────────────────────────────────────────


@pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"])
def test_image_ext_maps_to_image(ext: str):
    assert detect(ext=ext) is Modality.IMAGE


@pytest.mark.parametrize(
    "ext", ["pdf", "md", "txt", "docx", "xlsx", "xls", "html", "htm"]
)
def test_text_ext_maps_to_text(ext: str):
    assert detect(ext=ext) is Modality.TEXT


def test_ext_is_case_insensitive_and_tolerates_leading_dot():
    assert detect(ext="PNG") is Modality.IMAGE
    assert detect(ext=".PDF") is Modality.TEXT


# ── 请求体字段组（聊天带图）──────────────────────────


def test_has_images_maps_to_image():
    assert detect(has_images=True) is Modality.IMAGE


def test_has_images_false_without_ext_is_unknown():
    with pytest.raises(UnknownModalityError):
        detect(has_images=False)


# ── 源类型组 ──────────────────────────────────────────


def test_web_source_maps_to_web():
    assert detect(source_type="web") is Modality.WEB


def test_web_source_takes_precedence_over_ext():
    # 网页源即使带了图片扩展名，整体仍是 WEB（页面按 html 清洗）
    assert detect(source_type="web", ext="png") is Modality.WEB


def test_non_web_source_falls_back_to_ext():
    assert detect(source_type="upload", ext="pdf") is Modality.TEXT
    assert detect(source_type="sync_git", ext="docx") is Modality.TEXT


# ── 未知输入报错 ──────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"ext": "exe"},
        {"ext": None},
        {"source_type": "ftp"},
        {"ext": "exe", "source_type": "sync_local"},
    ],
)
def test_unknown_input_raises(kwargs: dict):
    with pytest.raises(UnknownModalityError):
        detect(**kwargs)


def test_dispatch_rejects_unregistered_modality():
    with pytest.raises(UnknownModalityError):
        dispatch("sar")  # type: ignore[arg-type]


# ── 分派映射 ──────────────────────────────────────────


def test_dispatch_returns_chain_key():
    assert dispatch(Modality.TEXT) == "text"
    assert dispatch(Modality.IMAGE) == "image"
    assert dispatch(Modality.WEB) == "html"


# ── 指标落点 ──────────────────────────────────────────


def test_decision_metric_records_modality():
    detect(ext="png")
    detect(ext="png")
    detect(ext="pdf")
    assert metrics.get("rag.router.decision", modality="image") == 2
    assert metrics.get("rag.router.decision", modality="text") == 1


def test_decision_metric_records_unknown():
    with pytest.raises(UnknownModalityError):
        detect(ext="exe")
    assert metrics.get("rag.router.decision", modality="unknown") == 1
