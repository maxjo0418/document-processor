"""ODL 점선 table 재구성에 쓰는 기하 계산과 split helper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..meta import PdfBoundingBox, coerce_bbox, coerce_float, sanitize_css_color
from ..preview.models import PdfPreviewVisualPrimitive

_AXIS_MERGE_TOL_PT = 2.0
_INTERIOR_PAD_PT = 2.0
_CELL_COVERAGE_RATIO = 0.7


@dataclass(frozen=True)
class _DottedSplit:
    value: float
    border_css: str


def _bbox_encloses(outer: PdfBoundingBox, inner: PdfBoundingBox, pad: float = 1.0) -> bool:
    return (
        inner.left_pt >= outer.left_pt - pad
        and inner.right_pt <= outer.right_pt + pad
        and inner.bottom_pt >= outer.bottom_pt - pad
        and inner.top_pt <= outer.top_pt + pad
    )


def _rules_outside(
    rules: list[PdfPreviewVisualPrimitive],
    exclude_bboxes: list[PdfBoundingBox],
) -> list[PdfPreviewVisualPrimitive]:
    if not exclude_bboxes:
        return rules
    kept: list[PdfPreviewVisualPrimitive] = []
    for rule in rules:
        b = rule.bounding_box
        cx = (b.left_pt + b.right_pt) / 2.0
        cy = (b.bottom_pt + b.top_pt) / 2.0
        if any(
            ex.left_pt <= cx <= ex.right_pt and ex.bottom_pt <= cy <= ex.top_pt
            for ex in exclude_bboxes
        ):
            continue
        kept.append(rule)
    return kept


def _boundary_values(raw: Any) -> list[float]:
    if not isinstance(raw, list):
        return []
    values: list[float] = []
    for item in raw:
        f = coerce_float(item)
        if f is not None:
            values.append(f)
    values.sort()
    return _dedupe_close(values, _AXIS_MERGE_TOL_PT)


def _reconstruct_boundaries_from_cells(
    cells: list[dict[str, Any]],
) -> tuple[list[float], list[float]]:
    ys: list[float] = []
    xs: list[float] = []
    for cell in cells:
        bbox = coerce_bbox(cell.get("bounding box"))
        if bbox is None:
            continue
        ys.extend([bbox.bottom_pt, bbox.top_pt])
        xs.extend([bbox.left_pt, bbox.right_pt])
    return (
        _dedupe_close(sorted(ys), _AXIS_MERGE_TOL_PT),
        _dedupe_close(sorted(xs), _AXIS_MERGE_TOL_PT),
    )


def _cell_split_boundaries(
    bbox: PdfBoundingBox,
    rules: list[PdfPreviewVisualPrimitive],
    *,
    axis: str,
) -> list[_DottedSplit]:
    """cell 내부를 가르는 점선의 축 위치와 border style을 반환한다."""
    if not rules:
        return []
    splits: list[_DottedSplit] = []
    for rule in rules:
        rb = rule.bounding_box
        if axis == "y":
            rule_axis = (rb.top_pt + rb.bottom_pt) / 2.0
            rule_lo, rule_hi = rb.left_pt, rb.right_pt
            cell_axis_lo, cell_axis_hi = bbox.bottom_pt, bbox.top_pt
            cell_span_lo, cell_span_hi = bbox.left_pt, bbox.right_pt
        else:
            rule_axis = (rb.left_pt + rb.right_pt) / 2.0
            rule_lo, rule_hi = rb.bottom_pt, rb.top_pt
            cell_axis_lo, cell_axis_hi = bbox.left_pt, bbox.right_pt
            cell_span_lo, cell_span_hi = bbox.bottom_pt, bbox.top_pt
        if not (
            cell_axis_lo + _INTERIOR_PAD_PT < rule_axis < cell_axis_hi - _INTERIOR_PAD_PT
        ):
            continue
        cell_span = cell_span_hi - cell_span_lo
        if cell_span <= 0:
            continue
        overlap = min(rule_hi, cell_span_hi) - max(rule_lo, cell_span_lo)
        if overlap >= _CELL_COVERAGE_RATIO * cell_span:
            splits.append(
                _DottedSplit(
                    value=rule_axis,
                    border_css=_dotted_rule_border_css(rule),
                )
            )
    return _dedupe_close_splits(
        sorted(splits, key=lambda split: split.value),
        _AXIS_MERGE_TOL_PT,
    )


def _dotted_rule_border_css(rule: PdfPreviewVisualPrimitive) -> str:
    width = coerce_float(rule.stroke_width_pt)
    if width is None or width <= 0.0:
        width = 1.0
    color = _css_color_from_pdfium(rule.stroke_color)
    return f"{_format_css_px(width)} dotted {color}"


def _css_color_from_pdfium(value: Any) -> str:
    color = sanitize_css_color(value)
    if color is None:
        return "#000000"
    if len(color) == 9 and color.startswith("#") and color[7:].lower() == "ff":
        return color[:7]
    return color


def _format_css_px(value: float) -> str:
    rounded = round(value, 3)
    integer = round(rounded)
    if abs(rounded - integer) < 0.0005:
        return f"{integer}px"
    text = f"{rounded:.3f}".rstrip("0").rstrip(".")
    return f"{text}px"


def _collect_new_boundaries(
    cell_splits: dict[int, list[float]],
    *,
    existing: list[float],
) -> list[float]:
    aggregated: list[float] = []
    for splits in cell_splits.values():
        aggregated.extend(splits)
    if not aggregated:
        return []
    aggregated = _dedupe_close(sorted(aggregated), _AXIS_MERGE_TOL_PT)
    return [
        value
        for value in aggregated
        if not any(abs(value - e) <= _INTERIOR_PAD_PT for e in existing)
    ]


def _dedupe_close(sorted_values: list[float], tol: float) -> list[float]:
    if not sorted_values:
        return []
    out = [sorted_values[0]]
    for v in sorted_values[1:]:
        if v - out[-1] > tol:
            out.append(v)
    return out


def _dedupe_close_splits(
    sorted_splits: list[_DottedSplit],
    tol: float,
) -> list[_DottedSplit]:
    if not sorted_splits:
        return []
    out = [sorted_splits[0]]
    for split in sorted_splits[1:]:
        if split.value - out[-1].value > tol:
            out.append(split)
    return out


def _snap_values(values: list[float], boundaries: list[float]) -> list[float]:
    """허용 오차 안에 있는 값을 가장 가까운 boundary 값으로 보정한다."""
    snapped: list[float] = []
    for v in values:
        best = min(boundaries, key=lambda b: abs(b - v))
        if abs(best - v) <= _AXIS_MERGE_TOL_PT:
            snapped.append(best)
    return _dedupe_close(sorted(snapped), _AXIS_MERGE_TOL_PT)


def _snap_split_border_map(
    split_borders: dict[float, str],
    boundaries: list[float],
) -> dict[float, str]:
    snapped: dict[float, str] = {}
    for value, border_css in split_borders.items():
        best = min(boundaries, key=lambda boundary: abs(boundary - value))
        if abs(best - value) <= _AXIS_MERGE_TOL_PT and best not in snapped:
            snapped[best] = border_css
    return snapped


def _find_boundary_index(value: float, boundaries: list[float]) -> int | None:
    for i, b in enumerate(boundaries):
        if abs(value - b) <= _AXIS_MERGE_TOL_PT:
            return i
    return None


def _compute_cell_x_bands(
    raw_cells: list[dict[str, Any]],
    cell_x_splits: dict[int, list[float]],
) -> dict[int, list[tuple[float, float]]]:
    bands_by_cell: dict[int, list[tuple[float, float]]] = {}
    for idx, cell in enumerate(raw_cells):
        bbox = coerce_bbox(cell.get("bounding box"))
        if bbox is None:
            continue
        x_cuts = sorted({bbox.left_pt, bbox.right_pt, *cell_x_splits.get(idx, [])})
        x_cuts = _dedupe_close(x_cuts, _AXIS_MERGE_TOL_PT)
        bands_by_cell[idx] = [
            (x_cuts[i], x_cuts[i + 1]) for i in range(len(x_cuts) - 1)
        ]
    return bands_by_cell
