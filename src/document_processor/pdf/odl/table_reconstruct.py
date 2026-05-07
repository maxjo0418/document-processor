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

from dataclasses import dataclass
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
            vertical_rule_segments = [
                p
                for p in primitives
                if "vertical_line_segment" in set(p.candidate_roles)
            ]
            if not dotted_h and not dotted_v and not vertical_rule_segments:
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
                table_vertical_rule_segments = _rules_outside(
                    vertical_rule_segments,
                    nested_bboxes,
                )
                _apply_dotted_splits(
                    table,
                    table_dotted_h,
                    table_dotted_v,
                    vertical_rule_segments=table_vertical_rule_segments,
                )
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
    *,
    vertical_rule_segments: list[PdfPreviewVisualPrimitive] | None = None,
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
    provisional_ys, provisional_xs = _provisional_grid_boundaries(
        existing_ys=existing_ys,
        existing_xs=existing_xs,
        cell_x_splits=cell_x_splits,
        sub_rect_y_splits=sub_rect_y_splits,
    )
    endpoint_splits = _vertical_endpoint_splits(
        table_bbox=table_bbox,
        raw_cells=raw_cells,
        provisional_ys=provisional_ys,
        provisional_xs=provisional_xs,
        cell_x_bands=cell_x_bands,
        sub_rect_y_splits=sub_rect_y_splits,
        vertical_rule_segments=vertical_rule_segments or [],
    )
    if endpoint_splits:
        _merge_endpoint_splits(
            raw_cells=raw_cells,
            cell_x_bands=cell_x_bands,
            sub_rect_y_splits=sub_rect_y_splits,
            endpoint_splits=endpoint_splits,
        )

    all_x_splits = _collect_new_boundaries(cell_x_splits, existing=existing_xs)
    all_y_splits = _collect_new_y_boundaries(sub_rect_y_splits, existing=existing_ys)
    has_local_y_splits = any(bool(ys) for ys in sub_rect_y_splits.values())
    if not has_local_y_splits and not all_x_splits:
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


def _provisional_grid_boundaries(
    *,
    existing_ys: list[float],
    existing_xs: list[float],
    cell_x_splits: dict[int, list[float]],
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
) -> tuple[list[float], list[float]]:
    """Return the grid after ODL plus dotted splits, before endpoint inference."""

    provisional_xs = _dedupe_close(
        sorted(existing_xs + _collect_new_boundaries(cell_x_splits, existing=existing_xs)),
        _AXIS_MERGE_TOL_PT,
    )
    provisional_ys = _dedupe_close(
        sorted(existing_ys + _collect_new_y_boundaries(sub_rect_y_splits, existing=existing_ys)),
        _AXIS_MERGE_TOL_PT,
    )
    return provisional_ys, provisional_xs


def _vertical_endpoint_y_splits(
    *,
    table_bbox: PdfBoundingBox,
    raw_cells: list[dict[str, Any]],
    provisional_ys: list[float],
    provisional_xs: list[float],
    vertical_rule_segments: list[PdfPreviewVisualPrimitive],
) -> list[float]:
    if not vertical_rule_segments or len(provisional_xs) < 2:
        return []

    text_bboxes = _collect_text_bboxes(raw_cells)
    virtual_rows = _virtual_rows_from_endpoint_points(
        _vertical_endpoint_points(
            table_bbox=table_bbox,
            vertical_rule_segments=vertical_rule_segments,
            text_bboxes=text_bboxes,
        )
    )
    candidate_ys = [
        row.y
        for row in virtual_rows
        if not _virtual_row_overlaps_provisional_grid(row, provisional_ys=provisional_ys)
    ]
    candidate_ys = _dedupe_close(sorted(candidate_ys), _AXIS_MERGE_TOL_PT)
    candidate_ys = _filter_too_thin_row_splits(candidate_ys, provisional_ys)
    if not candidate_ys:
        return []

    merged_ys = _dedupe_close(sorted(provisional_ys + candidate_ys), _AXIS_MERGE_TOL_PT)
    if not _row_bands_have_enough_text(merged_ys, text_bboxes, table_bbox):
        return []
    return candidate_ys


@dataclass(frozen=True)
class _VirtualEndpointRow:
    y: float
    xs: list[float]

    @property
    def x_left(self) -> float:
        return min(self.xs)

    @property
    def x_right(self) -> float:
        return max(self.xs)


@dataclass(frozen=True)
class _EndpointSplit:
    y: float
    x_left: float
    x_right: float


def _vertical_endpoint_splits(
    *,
    table_bbox: PdfBoundingBox,
    raw_cells: list[dict[str, Any]],
    provisional_ys: list[float],
    provisional_xs: list[float],
    cell_x_bands: dict[int, list[tuple[float, float]]],
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
    vertical_rule_segments: list[PdfPreviewVisualPrimitive],
) -> list[_EndpointSplit]:
    if not vertical_rule_segments or len(provisional_xs) < 2:
        return []

    text_bboxes = _collect_text_bboxes(raw_cells)
    virtual_rows = _virtual_rows_from_endpoint_points(
        _vertical_endpoint_points(
            table_bbox=table_bbox,
            vertical_rule_segments=vertical_rule_segments,
            text_bboxes=text_bboxes,
        )
    )
    if not virtual_rows:
        return []

    coverage_by_y = _horizontal_boundary_coverage(
        raw_cells=raw_cells,
        cell_x_bands=cell_x_bands,
        sub_rect_y_splits=sub_rect_y_splits,
    )
    endpoint_splits: list[_EndpointSplit] = []
    for row in virtual_rows:
        row_interval = (
            max(table_bbox.left_pt, row.x_left),
            min(table_bbox.right_pt, row.x_right),
        )
        if row_interval[1] - row_interval[0] < _AXIS_MERGE_TOL_PT:
            continue
        covered = _coverage_at_y(row.y, coverage_by_y)
        uncovered_ranges = _subtract_covered_ranges(row_interval, covered)
        for x_left, x_right in uncovered_ranges:
            if x_right - x_left < _AXIS_MERGE_TOL_PT:
                continue
            endpoint_splits.append(_EndpointSplit(y=row.y, x_left=x_left, x_right=x_right))

    endpoint_splits = _dedupe_endpoint_splits(endpoint_splits)
    if not endpoint_splits:
        return []

    new_candidate_ys = _dedupe_close(
        sorted(
            split.y
            for split in endpoint_splits
            if not any(abs(split.y - existing) <= _INTERIOR_PAD_PT for existing in provisional_ys)
        ),
        _AXIS_MERGE_TOL_PT,
    )
    new_candidate_ys = _filter_too_thin_row_splits(new_candidate_ys, provisional_ys)
    endpoint_splits = [
        split
        for split in endpoint_splits
        if any(abs(split.y - existing) <= _INTERIOR_PAD_PT for existing in provisional_ys)
        or any(abs(split.y - kept) <= _AXIS_MERGE_TOL_PT for kept in new_candidate_ys)
    ]
    if not endpoint_splits:
        return []

    merged_ys = _dedupe_close(
        sorted(provisional_ys + [split.y for split in endpoint_splits]),
        _AXIS_MERGE_TOL_PT,
    )
    if not _row_bands_have_enough_text(merged_ys, text_bboxes, table_bbox):
        return []
    return endpoint_splits


def _virtual_rows_from_endpoint_points(
    points: list[tuple[float, float]],
) -> list[_VirtualEndpointRow]:
    xs_by_y: dict[float, list[float]] = {}
    for x, y in points:
        bucket_y = _nearby_value(xs_by_y, y) or y
        xs_by_y.setdefault(bucket_y, [])
        if not any(abs(x - existing_x) <= _AXIS_MERGE_TOL_PT for existing_x in xs_by_y[bucket_y]):
            xs_by_y[bucket_y].append(x)
    return [
        _VirtualEndpointRow(y=y, xs=sorted(xs))
        for y, xs in xs_by_y.items()
        if len(xs) >= 2
    ]


def _virtual_row_overlaps_provisional_grid(
    row: _VirtualEndpointRow,
    *,
    provisional_ys: list[float],
) -> bool:
    return any(abs(row.y - y) <= _INTERIOR_PAD_PT for y in provisional_ys)


def _vertical_endpoint_points(
    *,
    table_bbox: PdfBoundingBox,
    vertical_rule_segments: list[PdfPreviewVisualPrimitive],
    text_bboxes: list[PdfBoundingBox],
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for segment in vertical_rule_segments:
        x = _vertical_segment_x_center(segment)
        if x is None or not (
            table_bbox.left_pt - _AXIS_MERGE_TOL_PT
            <= x
            <= table_bbox.right_pt + _AXIS_MERGE_TOL_PT
        ):
            continue
        bbox = segment.bounding_box
        if not _segment_touches_table_y_span(bbox, table_bbox):
            continue

        for y in (bbox.bottom_pt, bbox.top_pt):
            if not _endpoint_y_is_new_split_candidate(
                y,
                table_bbox=table_bbox,
                text_bboxes=text_bboxes,
            ):
                continue
            points.append((x, y))
    return points


def _vertical_segment_x_center(
    segment: PdfPreviewVisualPrimitive,
) -> float | None:
    if "vertical_line_segment" not in set(segment.candidate_roles):
        return None
    bbox = segment.bounding_box
    return (bbox.left_pt + bbox.right_pt) / 2.0


def _segment_touches_table_y_span(
    bbox: PdfBoundingBox,
    table_bbox: PdfBoundingBox,
) -> bool:
    return (
        table_bbox.bottom_pt - _AXIS_MERGE_TOL_PT
        <= bbox.bottom_pt
        <= table_bbox.top_pt + _AXIS_MERGE_TOL_PT
        or table_bbox.bottom_pt - _AXIS_MERGE_TOL_PT
        <= bbox.top_pt
        <= table_bbox.top_pt + _AXIS_MERGE_TOL_PT
    )


def _endpoint_y_is_new_split_candidate(
    y: float,
    *,
    table_bbox: PdfBoundingBox,
    text_bboxes: list[PdfBoundingBox],
) -> bool:
    if not (
        table_bbox.bottom_pt + _INTERIOR_PAD_PT
        < y
        < table_bbox.top_pt - _INTERIOR_PAD_PT
    ):
        return False
    if _candidate_y_crosses_text(y, text_bboxes):
        return False
    return True


def _merge_endpoint_y_splits(
    *,
    raw_cells: list[dict[str, Any]],
    cell_x_bands: dict[int, list[tuple[float, float]]],
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
    endpoint_y_splits: list[float],
) -> None:
    for idx, cell in enumerate(raw_cells):
        bbox = coerce_bbox(cell.get("bounding box"))
        if bbox is None:
            continue
        for band_idx, _band in enumerate(cell_x_bands.get(idx, [])):
            splits = sub_rect_y_splits.setdefault((idx, band_idx), [])
            for y in endpoint_y_splits:
                if bbox.bottom_pt + _INTERIOR_PAD_PT < y < bbox.top_pt - _INTERIOR_PAD_PT:
                    splits.append(y)
            sub_rect_y_splits[(idx, band_idx)] = _dedupe_close(
                sorted(splits),
                _AXIS_MERGE_TOL_PT,
            )


def _merge_endpoint_splits(
    *,
    raw_cells: list[dict[str, Any]],
    cell_x_bands: dict[int, list[tuple[float, float]]],
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
    endpoint_splits: list[_EndpointSplit],
) -> None:
    for idx, cell in enumerate(raw_cells):
        bbox = coerce_bbox(cell.get("bounding box"))
        if bbox is None:
            continue
        for band_idx, (x_left, x_right) in enumerate(cell_x_bands.get(idx, [])):
            splits = sub_rect_y_splits.setdefault((idx, band_idx), [])
            for split in endpoint_splits:
                if not (bbox.bottom_pt + _INTERIOR_PAD_PT < split.y < bbox.top_pt - _INTERIOR_PAD_PT):
                    continue
                overlap = _interval_overlap((x_left, x_right), (split.x_left, split.x_right))
                band_width = max(x_right - x_left, 0.0)
                if overlap < _AXIS_MERGE_TOL_PT or (band_width > 0 and overlap / band_width < 0.5):
                    continue
                splits.append(split.y)
            sub_rect_y_splits[(idx, band_idx)] = _dedupe_close(
                sorted(splits),
                _AXIS_MERGE_TOL_PT,
            )


def _horizontal_boundary_coverage(
    *,
    raw_cells: list[dict[str, Any]],
    cell_x_bands: dict[int, list[tuple[float, float]]],
    sub_rect_y_splits: dict[tuple[int, int], list[float]],
) -> dict[float, list[tuple[float, float]]]:
    coverage: dict[float, list[tuple[float, float]]] = {}
    for idx, cell in enumerate(raw_cells):
        bbox = coerce_bbox(cell.get("bounding box"))
        if bbox is None:
            continue
        for band_idx, (x_left, x_right) in enumerate(cell_x_bands.get(idx, [])):
            for y in (bbox.bottom_pt, bbox.top_pt, *sub_rect_y_splits.get((idx, band_idx), [])):
                bucket_y = _nearby_value(coverage, y) or y
                coverage.setdefault(bucket_y, []).append((x_left, x_right))

    return {
        y: _merge_intervals(intervals)
        for y, intervals in coverage.items()
    }


def _coverage_at_y(
    y: float,
    coverage_by_y: dict[float, list[tuple[float, float]]],
) -> list[tuple[float, float]]:
    for existing_y, intervals in coverage_by_y.items():
        if abs(y - existing_y) <= _INTERIOR_PAD_PT:
            return intervals
    return []


def _subtract_covered_ranges(
    interval: tuple[float, float],
    covered: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    uncovered = [interval]
    for cover_left, cover_right in _merge_intervals(covered):
        next_uncovered: list[tuple[float, float]] = []
        for left, right in uncovered:
            if cover_right <= left + _AXIS_MERGE_TOL_PT or cover_left >= right - _AXIS_MERGE_TOL_PT:
                next_uncovered.append((left, right))
                continue
            if left < cover_left - _AXIS_MERGE_TOL_PT:
                next_uncovered.append((left, min(cover_left, right)))
            if cover_right < right - _AXIS_MERGE_TOL_PT:
                next_uncovered.append((max(cover_right, left), right))
        uncovered = next_uncovered
    return uncovered


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    normalized = sorted(
        (min(left, right), max(left, right))
        for left, right in intervals
        if abs(right - left) > _AXIS_MERGE_TOL_PT
    )
    merged: list[tuple[float, float]] = []
    for left, right in normalized:
        if not merged or left > merged[-1][1] + _AXIS_MERGE_TOL_PT:
            merged.append((left, right))
            continue
        prev_left, prev_right = merged[-1]
        merged[-1] = (prev_left, max(prev_right, right))
    return merged


def _dedupe_endpoint_splits(splits: list[_EndpointSplit]) -> list[_EndpointSplit]:
    deduped: list[_EndpointSplit] = []
    for split in sorted(splits, key=lambda item: (item.y, item.x_left, item.x_right)):
        if any(
            abs(split.y - existing.y) <= _AXIS_MERGE_TOL_PT
            and abs(split.x_left - existing.x_left) <= _AXIS_MERGE_TOL_PT
            and abs(split.x_right - existing.x_right) <= _AXIS_MERGE_TOL_PT
            for existing in deduped
        ):
            continue
        deduped.append(split)
    return deduped


def _interval_overlap(
    left: tuple[float, float],
    right: tuple[float, float],
) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _nearest_boundary_index(value: float, boundaries: list[float]) -> int | None:
    if not boundaries:
        return None
    index = min(range(len(boundaries)), key=lambda i: abs(boundaries[i] - value))
    if abs(boundaries[index] - value) <= _AXIS_MERGE_TOL_PT:
        return index
    return None


def _nearby_value(values: dict[float, Any], value: float) -> float | None:
    for existing in values:
        if abs(existing - value) <= _AXIS_MERGE_TOL_PT:
            return existing
    return None


def _filter_too_thin_row_splits(
    candidate_ys: list[float],
    existing_ys: list[float],
    *,
    min_row_height_pt: float = 8.0,
) -> list[float]:
    kept: list[float] = []
    for candidate in candidate_ys:
        merged = _dedupe_close(sorted(existing_ys + kept + [candidate]), _AXIS_MERGE_TOL_PT)
        if any(
            upper - lower < min_row_height_pt
            for lower, upper in zip(merged, merged[1:])
        ):
            continue
        kept.append(candidate)
    return kept


def _collect_text_bboxes(nodes: list[dict[str, Any]]) -> list[PdfBoundingBox]:
    bboxes: list[PdfBoundingBox] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            child_items: list[Any] = []
            for key in ("kids", "paragraphs", "spans", "runs", "list items"):
                items = node.get(key)
                if isinstance(items, list):
                    child_items.extend(items)
            if child_items:
                for item in child_items:
                    visit(item)
                return

            bbox = coerce_bbox(node.get("bounding box")) or coerce_bbox(node.get("bbox"))
            if bbox is not None and isinstance(node.get("content"), str) and node.get("content"):
                bboxes.append(bbox)
            return
        if isinstance(node, list):
            for item in node:
                visit(item)

    for node in nodes:
        visit(node)
    return bboxes


def _candidate_y_crosses_text(
    y: float,
    text_bboxes: list[PdfBoundingBox],
    *,
    pad_pt: float = 0.5,
) -> bool:
    return any(bbox.bottom_pt + pad_pt < y < bbox.top_pt - pad_pt for bbox in text_bboxes)


def _row_bands_have_enough_text(
    ys: list[float],
    text_bboxes: list[PdfBoundingBox],
    table_bbox: PdfBoundingBox,
    *,
    min_filled_ratio: float = 0.65,
) -> bool:
    if len(ys) < 2:
        return False
    bands = [
        (bottom, top)
        for bottom, top in zip(ys, ys[1:])
        if (
            bottom >= table_bbox.bottom_pt - _AXIS_MERGE_TOL_PT
            and top <= table_bbox.top_pt + _AXIS_MERGE_TOL_PT
        )
    ]
    if not bands:
        return False
    filled = 0
    for bottom, top in bands:
        if any(bottom <= (bbox.bottom_pt + bbox.top_pt) / 2.0 <= top for bbox in text_bboxes):
            filled += 1
    return filled / len(bands) >= min_filled_ratio


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
