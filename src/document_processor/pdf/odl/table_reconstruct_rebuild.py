"""ODL 점선 table 재구성 중 row/cell을 다시 만드는 helper."""

from __future__ import annotations

from typing import Any

from ..meta import PdfBoundingBox, coerce_bbox
from .table_reconstruct_geometry import (
    _AXIS_MERGE_TOL_PT,
    _dedupe_close,
    _find_boundary_index,
)
from .table_reconstruct_text import _distribute_children

_EXPLICIT_BORDER_TO_BOOL = {
    "border top": "has top border",
    "border bottom": "has bottom border",
    "border left": "has left border",
    "border right": "has right border",
}


def _rebuild_rows(
    original_cells: list[dict[str, Any]],
    *,
    cell_x_bands: dict[int, list[tuple[float, float]]],
    cell_x_split_borders: dict[int, dict[float, str]],
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
    sub_rect_y_split_borders: dict[tuple[int, int], dict[float, str]],
    ys: list[float],
    xs: list[float],
) -> list[dict[str, Any]]:
    """sub-column마다 계산된 horizontal split을 사용해 ``rows``를 다시 만든다.

    원본 cell은 ``cell_x_bands`` 기준으로 하나 이상의 sub-column으로 나뉜다.
    각 sub-column은 ``sub_rect_y_splits``에 기록된 자기 y split으로만 잘린다.
    split이 없는 sub-column은 하나의 sub-cell로 남고, 포함하는 merged row만큼
    rowspan을 가진다.
    """
    ny = len(ys) - 1
    if ny <= 0 or len(xs) < 2:
        return []

    flat_cells: list[dict[str, Any]] = []

    for idx, cell in enumerate(original_cells):
        bbox = coerce_bbox(cell.get("bounding box"))
        if bbox is None:
            continue
        bands = cell_x_bands.get(idx, [(bbox.left_pt, bbox.right_pt)])
        for band_idx, (x_lo, x_hi) in enumerate(bands):
            y_splits = sub_rect_y_splits.get((idx, band_idx), [])
            y_cuts = sorted({bbox.bottom_pt, bbox.top_pt, *y_splits})
            y_cuts = _dedupe_close(y_cuts, _AXIS_MERGE_TOL_PT)

            lo_col = _find_boundary_index(x_lo, xs)
            hi_col = _find_boundary_index(x_hi, xs)
            if lo_col is None or hi_col is None or hi_col <= lo_col:
                continue
            colspan = hi_col - lo_col
            col_number = lo_col + 1

            for yi in range(len(y_cuts) - 1):
                y_lo, y_hi = y_cuts[yi], y_cuts[yi + 1]
                if y_hi - y_lo < _AXIS_MERGE_TOL_PT:
                    continue
                lo_band = _find_boundary_index(y_lo, ys)
                hi_band = _find_boundary_index(y_hi, ys)
                if lo_band is None or hi_band is None or hi_band <= lo_band:
                    continue
                rowspan = hi_band - lo_band
                row_number = ny - (hi_band - 1)

                sub_bbox = PdfBoundingBox(
                    left_pt=x_lo, bottom_pt=y_lo, right_pt=x_hi, top_pt=y_hi
                )
                border_overrides = _split_border_overrides(
                    sub_bbox,
                    x_split_borders=cell_x_split_borders.get(idx, {}),
                    y_split_borders=sub_rect_y_split_borders.get((idx, band_idx), {}),
                )
                flat_cells.append(
                    _build_sub_cell(
                        source=cell,
                        sub_bbox=sub_bbox,
                        row_number=row_number,
                        col_number=col_number,
                        rowspan=rowspan,
                        colspan=colspan,
                        border_overrides=border_overrides,
                    )
                )

    if not flat_cells:
        return []

    rows_by_number: dict[int, list[dict[str, Any]]] = {}
    for sub_cell in flat_cells:
        rows_by_number.setdefault(sub_cell["row number"], []).append(sub_cell)

    new_rows: list[dict[str, Any]] = []
    for row_number in sorted(rows_by_number):
        row_cells = sorted(rows_by_number[row_number], key=lambda c: c["column number"])
        new_rows.append(
            {"type": "table row", "row number": row_number, "cells": row_cells}
        )
    return new_rows


def _build_sub_cell(
    source: dict[str, Any],
    sub_bbox: PdfBoundingBox,
    *,
    row_number: int,
    col_number: int,
    rowspan: int,
    colspan: int,
    border_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    sub_bbox_list = [
        sub_bbox.left_pt,
        sub_bbox.bottom_pt,
        sub_bbox.right_pt,
        sub_bbox.top_pt,
    ]
    cell = dict(source)
    cell["row number"] = row_number
    cell["column number"] = col_number
    cell["row span"] = rowspan
    cell["column span"] = colspan
    cell["bounding box"] = sub_bbox_list
    cell["kids"] = _distribute_children(source.get("kids"), sub_bbox)
    if "paragraphs" in source:
        cell["paragraphs"] = _distribute_children(source.get("paragraphs"), sub_bbox)
    if border_overrides:
        for border_key, border_css in border_overrides.items():
            cell[border_key] = border_css
            bool_key = _EXPLICIT_BORDER_TO_BOOL.get(border_key)
            if bool_key is not None:
                cell[bool_key] = True
    return cell


def _split_border_overrides(
    sub_bbox: PdfBoundingBox,
    *,
    x_split_borders: dict[float, str],
    y_split_borders: dict[float, str],
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value, border_css in x_split_borders.items():
        if abs(sub_bbox.left_pt - value) <= _AXIS_MERGE_TOL_PT:
            overrides["border left"] = border_css
        if abs(sub_bbox.right_pt - value) <= _AXIS_MERGE_TOL_PT:
            overrides["border right"] = border_css
    for value, border_css in y_split_borders.items():
        if abs(sub_bbox.bottom_pt - value) <= _AXIS_MERGE_TOL_PT:
            overrides["border bottom"] = border_css
        if abs(sub_bbox.top_pt - value) <= _AXIS_MERGE_TOL_PT:
            overrides["border top"] = border_css
    return overrides
