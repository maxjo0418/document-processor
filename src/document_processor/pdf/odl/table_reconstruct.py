"""ODL 원본 table에 점선 기반 cell split을 반영하는 전처리기.

ODL은 일반 실선 grid는 잘 읽지만 table 내부의 점선을 놓치는
경우가 있다. 이 모듈은 pdfium visual primitive에서
`segmented_horizontal_rule` / `segmented_vertical_rule`을 찾아 ODL 원본
table의 `rows`/`cells`/`grid boundaries`에 추가 split을 반영한다.

점선은 cell 단위로 판단한다. 어떤 점선이 cell 내부를 실제로 가로지를 때만
split으로 취급한다. merged cell에서는 이 차이가 중요하다. 예를 들어 오른쪽
상세 column에만 걸친 가로 점선은 상세 row만 쪼개고, 왼쪽 category cell은
큰 rowspan을 유지해야 한다.

이 파일은 orchestration만 담당한다. geometry 계산, row 재구성, text tree
pruning은 sibling module에 둔다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..meta import PdfBoundingBox, coerce_bbox, coerce_int
from ..preview.analyze import extract_pdfium_table_rule_primitives
from ..preview.models import PdfPreviewVisualPrimitive
from .table_reconstruct_geometry import (
    _AXIS_MERGE_TOL_PT,
    _INTERIOR_PAD_PT,
    _bbox_encloses,
    _boundary_values,
    _cell_split_boundaries,
    _collect_new_boundaries,
    _compute_cell_x_bands,
    _dedupe_close,
    _reconstruct_boundaries_from_cells,
    _rules_outside,
    _snap_split_border_map,
    _snap_values,
)
from .table_reconstruct_rebuild import _rebuild_rows
from .table_reconstruct_text import _learn_unit_separators, _presplit_merged_leaves


def preprocess_dotted_rule_splits(
    raw_document: dict[str, Any],
    *,
    pdf_path: str | Path,
    page_numbers: Iterable[int] | None = None,
) -> None:
    """`raw_document`를 제자리 수정해 table에 점선 split을 포함시킨다."""
    resolved_pdf_path = Path(pdf_path).expanduser()
    if not resolved_pdf_path.exists():
        return

    tables_by_page = _collect_table_nodes_by_page(raw_document)
    if page_numbers is not None:
        wanted = {int(p) for p in page_numbers}
        tables_by_page = {p: t for p, t in tables_by_page.items() if p in wanted}
    if not tables_by_page:
        return

    try:
        import pypdfium2 as pdfium
    except Exception:
        return
    try:
        document = pdfium.PdfDocument(str(resolved_pdf_path))
    except Exception:
        return

    try:
        page_count = _document_page_count(document)
        for page_number, tables in tables_by_page.items():
            if page_number <= 0 or page_number > page_count:
                continue

            primitives = extract_pdfium_table_rule_primitives(
                document[page_number - 1],
                page_number=page_number,
            )
            dotted_h = [
                p for p in primitives if p.object_type == "segmented_horizontal_rule"
            ]
            dotted_v = [
                p for p in primitives if p.object_type == "segmented_vertical_rule"
            ]
            if not dotted_h and not dotted_v:
                continue

            table_bboxes = [(t, coerce_bbox(t.get("bounding box"))) for t in tables]
            ordered_tables = sorted(
                tables,
                key=lambda t: -_enclosing_depth(t, table_bboxes),
            )

            for table in ordered_tables:
                outer_bbox = coerce_bbox(table.get("bounding box"))
                nested_bboxes = [
                    other_bbox
                    for other, other_bbox in table_bboxes
                    if other is not table
                    and other_bbox is not None
                    and outer_bbox is not None
                    and _bbox_encloses(outer_bbox, other_bbox)
                ]
                table_dotted_h = _rules_outside(dotted_h, nested_bboxes)
                table_dotted_v = _rules_outside(dotted_v, nested_bboxes)
                _apply_dotted_splits(table, table_dotted_h, table_dotted_v)
    finally:
        document.close()


def _collect_table_nodes_by_page(root: Any) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "table":
                page_number = coerce_int(node.get("page number"))
                if page_number is not None:
                    grouped.setdefault(page_number, []).append(node)
            for value in node.values():
                visit(value)
            return
        if isinstance(node, list):
            for item in node:
                visit(item)

    visit(root)
    return grouped


def _document_page_count(document: Any) -> int:
    page_count = getattr(document, "page_count", None)
    if isinstance(page_count, int) and page_count > 0:
        return page_count
    try:
        return len(document)
    except TypeError:
        return 0


def _enclosing_depth(
    table: dict[str, Any],
    table_bboxes: list[tuple[dict[str, Any], PdfBoundingBox | None]],
) -> int:
    self_bbox = coerce_bbox(table.get("bounding box"))
    if self_bbox is None:
        return 0
    return sum(
        1
        for other, other_bbox in table_bboxes
        if other is not table
        and other_bbox is not None
        and _bbox_encloses(other_bbox, self_bbox)
    )


def _apply_dotted_splits(
    table: dict[str, Any],
    dotted_h: list[PdfPreviewVisualPrimitive],
    dotted_v: list[PdfPreviewVisualPrimitive],
) -> None:
    table_bbox = coerce_bbox(table.get("bounding box"))
    if table_bbox is None:
        return

    raw_cells = _collect_table_cells(table)
    if not raw_cells:
        return

    existing_ys = _boundary_values(table.get("grid row boundaries"))
    existing_xs = _boundary_values(table.get("grid column boundaries"))
    if len(existing_ys) < 2 or len(existing_xs) < 2:
        existing_ys, existing_xs = _reconstruct_boundaries_from_cells(raw_cells)
    if len(existing_ys) < 2 or len(existing_xs) < 2:
        return

    # 전체 흐름:
    # 1. 세로 점선으로 각 cell 내부의 x split과 sub-column band를 만든다.
    # 2. 각 sub-column마다 가로 점선 y split을 따로 계산한다.
    # 3. split을 전역 grid boundary에 합치고, 가까운 값은 같은 축으로 snap한다.
    # 4. 기존 cell을 sub-cell로 재구성하면서 paragraph와 border style을 배분한다.
    cell_x_splits, cell_x_split_borders = _vertical_split_plan(raw_cells, dotted_v)
    cell_x_bands = _compute_cell_x_bands(raw_cells, cell_x_splits)
    sub_rect_y_splits, sub_rect_y_split_borders = _horizontal_split_plan(
        raw_cells,
        dotted_h,
        cell_x_bands,
    )

    all_x_splits = _collect_new_boundaries(cell_x_splits, existing=existing_xs)
    all_y_splits = _collect_new_y_boundaries(sub_rect_y_splits, existing=existing_ys)
    if not all_y_splits and not all_x_splits:
        return

    merged_ys = _dedupe_close(sorted(existing_ys + all_y_splits), _AXIS_MERGE_TOL_PT)
    merged_xs = _dedupe_close(sorted(existing_xs + all_x_splits), _AXIS_MERGE_TOL_PT)

    # Snap 이후에는 같은 선으로 판정된 값들이 하나로 접힌다.
    # split 위치에 매달린 border map도 같은 기준으로 보정한다.
    cell_x_splits = {
        idx: _snap_values(splits, merged_xs) for idx, splits in cell_x_splits.items()
    }
    cell_x_split_borders = {
        idx: _snap_split_border_map(split_borders, merged_xs)
        for idx, split_borders in cell_x_split_borders.items()
    }
    cell_x_bands = _compute_cell_x_bands(raw_cells, cell_x_splits)
    sub_rect_y_splits = {
        key: _snap_values(ys, merged_ys) for key, ys in sub_rect_y_splits.items()
    }
    sub_rect_y_split_borders = {
        key: _snap_split_border_map(split_borders, merged_ys)
        for key, split_borders in sub_rect_y_split_borders.items()
    }

    _presplit_cells_with_merged_leaves(raw_cells, sub_rect_y_splits)

    new_rows = _rebuild_rows(
        raw_cells,
        cell_x_bands=cell_x_bands,
        cell_x_split_borders=cell_x_split_borders,
        sub_rect_y_splits=sub_rect_y_splits,
        sub_rect_y_split_borders=sub_rect_y_split_borders,
        ys=merged_ys,
        xs=merged_xs,
    )
    if not new_rows:
        return

    table["rows"] = new_rows
    # ODL's convention for `grid row boundaries` is descending (top-down).
    table["grid row boundaries"] = list(reversed(merged_ys))
    table["grid column boundaries"] = list(merged_xs)
    table["number of rows"] = max(len(merged_ys) - 1, 0)
    table["number of columns"] = max(len(merged_xs) - 1, 0)


def _collect_table_cells(table: dict[str, Any]) -> list[dict[str, Any]]:
    raw_cells: list[dict[str, Any]] = []
    for row in table.get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        for cell in row.get("cells", []) or []:
            if isinstance(cell, dict) and coerce_bbox(cell.get("bounding box")) is not None:
                raw_cells.append(cell)
    return raw_cells


def _vertical_split_plan(
    raw_cells: list[dict[str, Any]],
    dotted_v: list[PdfPreviewVisualPrimitive],
) -> tuple[dict[int, list[float]], dict[int, dict[float, str]]]:
    cell_x_splits: dict[int, list[float]] = {}
    cell_x_split_borders: dict[int, dict[float, str]] = {}
    for idx, cell in enumerate(raw_cells):
        bbox = coerce_bbox(cell["bounding box"])
        if bbox is None:
            continue
        splits = _cell_split_boundaries(bbox, dotted_v, axis="x")
        cell_x_splits[idx] = [split.value for split in splits]
        cell_x_split_borders[idx] = {
            split.value: split.border_css for split in splits
        }
    return cell_x_splits, cell_x_split_borders


def _horizontal_split_plan(
    raw_cells: list[dict[str, Any]],
    dotted_h: list[PdfPreviewVisualPrimitive],
    cell_x_bands: dict[int, list[tuple[float, float]]],
) -> tuple[dict[tuple[int, int], list[float]], dict[tuple[int, int], dict[float, str]]]:
    sub_rect_y_splits: dict[tuple[int, int], list[float]] = {}
    sub_rect_y_split_borders: dict[tuple[int, int], dict[float, str]] = {}

    for idx, cell in enumerate(raw_cells):
        bbox = coerce_bbox(cell["bounding box"])
        if bbox is None:
            continue
        for band_idx, (x_lo, x_hi) in enumerate(cell_x_bands.get(idx, [])):
            sub_bbox = PdfBoundingBox(
                left_pt=x_lo,
                bottom_pt=bbox.bottom_pt,
                right_pt=x_hi,
                top_pt=bbox.top_pt,
            )
            splits = _cell_split_boundaries(sub_bbox, dotted_h, axis="y")
            sub_rect_y_splits[(idx, band_idx)] = [split.value for split in splits]
            sub_rect_y_split_borders[(idx, band_idx)] = {
                split.value: split.border_css for split in splits
            }

    return sub_rect_y_splits, sub_rect_y_split_borders


def _collect_new_y_boundaries(
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
    *,
    existing: list[float],
) -> list[float]:
    aggregated_ys: list[float] = []
    for ys in sub_rect_y_splits.values():
        aggregated_ys.extend(ys)
    aggregated_ys = _dedupe_close(sorted(aggregated_ys), _AXIS_MERGE_TOL_PT)
    return [
        y
        for y in aggregated_ys
        if not any(abs(y - e) <= _INTERIOR_PAD_PT for e in existing)
    ]


def _presplit_cells_with_merged_leaves(
    raw_cells: list[dict[str, Any]],
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
) -> None:
    # ODL이 여러 행의 텍스트를 하나의 leaf로 합친 경우가 있다.
    # 먼저 leaf를 논리 unit으로 나눠두면 이후 bbox 기반 분배가 안정적이다.
    unit_separators = _learn_unit_separators(raw_cells)
    for idx, cell in enumerate(raw_cells):
        cell_y_splits_union = sorted(
            {
                y
                for (cell_idx, _band_idx), ys in sub_rect_y_splits.items()
                if cell_idx == idx
                for y in ys
            }
        )
        if not cell_y_splits_union:
            continue
        _presplit_merged_leaves(cell, cell_y_splits_union, unit_separators)


__all__ = ["preprocess_dotted_rule_splits"]
