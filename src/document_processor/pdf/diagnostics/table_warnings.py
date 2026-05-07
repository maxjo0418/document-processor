"""Conservative diagnostics for PDF table extraction edge cases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_COORD_TOLERANCE = 1.0
_CLUSTER_GAP_PT = 2.0
_MIN_DENSE_LINEART_COUNT = 100
_MIN_DENSE_UNIQUE_X = 6
_MIN_DENSE_UNIQUE_Y = 16
_MIN_DENSE_CLUSTER_WIDTH_PT = 250.0
_MIN_DENSE_CLUSTER_HEIGHT_PT = 250.0
_MIN_DENSE_TEXT_INSIDE = 80
_MAX_ADJACENT_TABLE_GAP_PT = 8.0
_MIN_ADJACENT_X_OVERLAP_RATIO = 0.95
_MAX_ADJACENT_EDGE_DELTA_PT = 3.0
_MIN_OUTER_BORDER_MISSING_RATIO = 0.8
_MAX_OPEN_BORDER_TABLE_ROWS = 2
_MIN_OPEN_BORDER_TABLE_COLUMNS = 4
_MIN_OPEN_BORDER_TABLE_WIDTH_PT = 250.0
_MIN_OPEN_BORDER_TABLE_HEIGHT_PT = 35.0
_MAX_OPEN_BORDER_PAGE_LINEART_COUNT = 12
_MIN_OPEN_BORDER_CONTAINER_WIDTH_PT = 250.0
_MIN_OPEN_BORDER_CONTAINER_HEIGHT_PT = 80.0
_MIN_SPARSE_OPEN_BORDER_LINEART_COUNT = 5
_MAX_SPARSE_OPEN_BORDER_LINEART_COUNT = 30
_MIN_SPARSE_OPEN_BORDER_TEXT_INSIDE = 20
_MIN_SPARSE_OPEN_BORDER_TOP_LINEARTS = 3
_OPEN_BORDER_TOP_EDGE_TOLERANCE_PT = 3.0
_OPEN_BORDER_HEADER_DEPTH_PT = 35.0
_PDFIUM_OUTER_RULE_X_TOLERANCE_PT = 3.0
_PDFIUM_OUTER_RULE_MIN_Y_OVERLAP_PT = 10.0
_PDFIUM_OUTER_RULE_MIN_Y_OVERLAP_RATIO = 0.4

_TEXT_NODE_TYPES = {"paragraph", "heading", "caption", "list item", "text chunk"}

_MESSAGES = {
    "dense_lineart_table_missing": (
        "possible_table_mismatch - dense line-art grid was not parsed as a table"
    ),
    "adjacent_table_split": (
        "possible_table_mismatch - adjacent tables may be one split table"
    ),
    "open_border_table_risk": (
        "possible_table_mismatch - open-border table may have inaccurate cells"
    ),
}


@dataclass(frozen=True, slots=True)
class PdfTableWarning:
    page_number: int
    message: str
    bbox: list[float] | None
    source_path: str | None


@dataclass(frozen=True, slots=True)
class _RawNode:
    path: str
    node: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Cluster:
    linearts: list[_RawNode]
    bbox: list[float]


class _PdfRuleInspector:
    def __init__(self, source_path: str | None) -> None:
        self._path = Path(source_path).expanduser() if source_path else None
        self._document: Any | None = None
        self._primitives_by_page: dict[int, list[Any] | None] = {}

    def outer_vertical_rule_presence(self, page_number: int, bbox: list[float]) -> tuple[bool, bool] | None:
        primitives = self._page_primitives(page_number)
        if primitives is None:
            return None
        vertical_primitives = [primitive for primitive in primitives if _is_vertical_rule_primitive(primitive)]
        return (
            _has_outer_vertical_rule(vertical_primitives, bbox, side="left"),
            _has_outer_vertical_rule(vertical_primitives, bbox, side="right"),
        )

    def close(self) -> None:
        document = self._document
        self._document = None
        if document is not None and hasattr(document, "close"):
            try:
                document.close()
            except Exception:
                pass

    def _page_primitives(self, page_number: int) -> list[Any] | None:
        if page_number in self._primitives_by_page:
            return self._primitives_by_page[page_number]
        primitives = self._load_page_primitives(page_number)
        self._primitives_by_page[page_number] = primitives
        return primitives

    def _load_page_primitives(self, page_number: int) -> list[Any] | None:
        if self._path is None or not self._path.exists() or page_number <= 0:
            return None
        try:
            import pypdfium2 as pdfium

            from ..preview.analyze import extract_pdfium_table_rule_primitives
        except Exception:
            return None
        try:
            if self._document is None:
                self._document = pdfium.PdfDocument(str(self._path))
            return extract_pdfium_table_rule_primitives(
                self._document[page_number - 1],
                page_number=page_number,
            )
        except Exception:
            return None


def detect_pdf_table_warnings(
    raw_document: dict[str, Any],
    *,
    source_path: str | Path | None = None,
) -> list[PdfTableWarning]:
    """Detect high-confidence PDF table extraction warnings from ODL raw JSON."""
    resolved_source_path = str(source_path) if source_path is not None else _string_or_none(raw_document.get("file name"))
    nodes = list(_walk_raw_nodes(raw_document))
    tables = [raw for raw in nodes if raw.node.get("type") == "table"]
    linearts = _lineart_nodes(raw_document)
    pages = _page_numbers(nodes, linearts)
    rule_inspector = _PdfRuleInspector(resolved_source_path)

    warnings: list[PdfTableWarning] = []
    try:
        for page_number in sorted(pages):
            page_tables = [table for table in tables if _page_number(table.node) == page_number]
            page_linearts = [lineart for lineart in linearts if _page_number(lineart.node) == page_number]
            page_nodes = [node for node in nodes if _page_number(node.node) == page_number]

            warnings.extend(_detect_adjacent_split_tables(page_number, page_tables, page_nodes, resolved_source_path))
            warnings.extend(
                _detect_open_border_table_risks(
                    page_number,
                    page_tables,
                    page_linearts,
                    page_nodes,
                    resolved_source_path,
                    rule_inspector,
                )
            )
            warnings.extend(
                _detect_dense_lineart_without_table(
                    page_number,
                    page_tables,
                    page_linearts,
                    page_nodes,
                    resolved_source_path,
                )
            )
    finally:
        rule_inspector.close()
    return warnings


def log_pdf_table_warnings(warnings: list[PdfTableWarning], logger: Any) -> None:
    for warning in warnings:
        logger.warning(
            "PDF table warning: %s source=%s page=%s bbox=%s",
            warning.message,
            warning.source_path,
            warning.page_number,
            warning.bbox,
        )


def _detect_dense_lineart_without_table(
    page_number: int,
    page_tables: list[_RawNode],
    page_linearts: list[_RawNode],
    page_nodes: list[_RawNode],
    source_path: str | None,
) -> list[PdfTableWarning]:
    if page_tables or not page_linearts or _line_chunk_count(page_nodes, page_linearts) > 0:
        return []

    clusters = _cluster_linearts(page_linearts)
    warnings: list[PdfTableWarning] = []
    for cluster in clusters:
        if len(cluster.linearts) < _MIN_DENSE_LINEART_COUNT:
            continue
        unique_x, unique_y = _unique_boundary_counts([lineart.node for lineart in cluster.linearts])
        width = cluster.bbox[2] - cluster.bbox[0]
        height = cluster.bbox[3] - cluster.bbox[1]
        text_inside = _text_nodes_inside(page_nodes, cluster.bbox)
        if (
            unique_x < _MIN_DENSE_UNIQUE_X
            or unique_y < _MIN_DENSE_UNIQUE_Y
            or width < _MIN_DENSE_CLUSTER_WIDTH_PT
            or height < _MIN_DENSE_CLUSTER_HEIGHT_PT
            or text_inside < _MIN_DENSE_TEXT_INSIDE
        ):
            continue
        warnings.append(
            PdfTableWarning(
                page_number=page_number,
                message=_MESSAGES["dense_lineart_table_missing"],
                bbox=_round_bbox(cluster.bbox),
                source_path=source_path,
            )
        )
        break
    return warnings


def _detect_adjacent_split_tables(
    page_number: int,
    page_tables: list[_RawNode],
    page_nodes: list[_RawNode],
    source_path: str | None,
) -> list[PdfTableWarning]:
    if len(page_tables) < 2:
        return []

    sorted_tables = sorted(
        [table for table in page_tables if _bbox(table.node) is not None],
        key=lambda table: _bbox(table.node)[3],  # type: ignore[index]
        reverse=True,
    )
    for upper_index, upper in enumerate(sorted_tables):
        upper_box = _bbox(upper.node)
        if upper_box is None:
            continue
        for lower in sorted_tables[upper_index + 1 :]:
            lower_box = _bbox(lower.node)
            if lower_box is None:
                continue
            vertical_gap = upper_box[1] - lower_box[3]
            if vertical_gap < -_COORD_TOLERANCE or vertical_gap > _MAX_ADJACENT_TABLE_GAP_PT:
                continue
            if abs(upper_box[0] - lower_box[0]) > _MAX_ADJACENT_EDGE_DELTA_PT:
                continue
            if abs(upper_box[2] - lower_box[2]) > _MAX_ADJACENT_EDGE_DELTA_PT:
                continue
            x_overlap_ratio = _x_overlap_ratio(upper_box, lower_box)
            if x_overlap_ratio < _MIN_ADJACENT_X_OVERLAP_RATIO:
                continue
            if _has_meaningful_text_between(page_nodes, upper.path, lower.path, upper_box, lower_box):
                continue
            warning_bbox = _union_bbox([upper_box, lower_box])
            return [
                PdfTableWarning(
                    page_number=page_number,
                    message=_MESSAGES["adjacent_table_split"],
                    bbox=_round_bbox(warning_bbox),
                    source_path=source_path,
                )
            ]
    return []


def _detect_open_border_table_risks(
    page_number: int,
    page_tables: list[_RawNode],
    page_linearts: list[_RawNode],
    page_nodes: list[_RawNode],
    source_path: str | None,
    rule_inspector: _PdfRuleInspector,
) -> list[PdfTableWarning]:
    if not page_tables:
        return _detect_sparse_open_border_candidate(page_number, page_linearts, page_nodes, source_path, rule_inspector)

    visual_containers = _open_border_visual_containers(page_linearts)
    warnings: list[PdfTableWarning] = []
    for table in page_tables:
        table_box = _bbox(table.node)
        if table_box is None:
            continue
        rows, cols = _table_shape(table.node)
        if not _is_shallow_open_border_shape(table_box, rows, cols):
            continue
        cells = _table_cells(table.node)
        left_missing_ratio, right_missing_ratio = _outer_border_missing_ratios(cells)
        raw_open_border_signal = (
            left_missing_ratio >= _MIN_OUTER_BORDER_MISSING_RATIO
            and right_missing_ratio >= _MIN_OUTER_BORDER_MISSING_RATIO
        )
        container = _overlapping_large_container(table_box, visual_containers)
        container_signal = container is not None and len(page_linearts) <= _MAX_OPEN_BORDER_PAGE_LINEART_COUNT
        if not raw_open_border_signal and not container_signal:
            continue
        outer_rules = rule_inspector.outer_vertical_rule_presence(page_number, table_box)
        if outer_rules == (True, True):
            continue
        warnings.append(
            PdfTableWarning(
                page_number=page_number,
                message=_MESSAGES["open_border_table_risk"],
                bbox=_round_bbox(table_box),
                source_path=source_path,
            )
        )
    return warnings


def _detect_sparse_open_border_candidate(
    page_number: int,
    page_linearts: list[_RawNode],
    page_nodes: list[_RawNode],
    source_path: str | None,
    rule_inspector: _PdfRuleInspector,
) -> list[PdfTableWarning]:
    if _line_chunk_count(page_nodes, page_linearts) > 0:
        return []
    if not (
        _MIN_SPARSE_OPEN_BORDER_LINEART_COUNT
        <= len(page_linearts)
        <= _MAX_SPARSE_OPEN_BORDER_LINEART_COUNT
    ):
        return []

    candidates = sorted(
        [lineart for lineart in page_linearts if _bbox(lineart.node) is not None],
        key=lambda lineart: _area(_bbox(lineart.node)),  # type: ignore[arg-type]
        reverse=True,
    )
    for candidate in candidates:
        candidate_box = _bbox(candidate.node)
        if candidate_box is None:
            continue
        width = candidate_box[2] - candidate_box[0]
        height = candidate_box[3] - candidate_box[1]
        if width < _MIN_OPEN_BORDER_CONTAINER_WIDTH_PT or height < _MIN_OPEN_BORDER_CONTAINER_HEIGHT_PT:
            continue
        top_linearts = _top_aligned_linearts(candidate, page_linearts)
        if len(top_linearts) < _MIN_SPARSE_OPEN_BORDER_TOP_LINEARTS:
            continue
        text_inside = _text_nodes_inside(page_nodes, candidate_box)
        if text_inside < _MIN_SPARSE_OPEN_BORDER_TEXT_INSIDE:
            continue
        outer_rules = rule_inspector.outer_vertical_rule_presence(page_number, candidate_box)
        if outer_rules == (True, True):
            continue
        return [
            PdfTableWarning(
                page_number=page_number,
                message=_MESSAGES["open_border_table_risk"],
                bbox=_round_bbox(candidate_box),
                source_path=source_path,
            )
        ]
    return []


def _walk_raw_nodes(value: Any, path: str = "root") -> list[_RawNode]:
    result: list[_RawNode] = []
    if isinstance(value, dict):
        result.append(_RawNode(path=path, node=value))
        for key in ("kids", "spans", "rows", "cells", "list items"):
            child_value = value.get(key)
            if isinstance(child_value, list):
                for index, child in enumerate(child_value):
                    result.extend(_walk_raw_nodes(child, f"{path}.{key}[{index}]"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_walk_raw_nodes(child, f"{path}[{index}]"))
    return result


def _lineart_nodes(raw_document: dict[str, Any]) -> list[_RawNode]:
    raw_linearts = raw_document.get("line arts")
    if not isinstance(raw_linearts, list):
        return []
    return [
        _RawNode(path=f"root.line arts[{index}]", node=lineart)
        for index, lineart in enumerate(raw_linearts)
        if isinstance(lineart, dict) and _bbox(lineart) is not None
    ]


def _page_numbers(nodes: list[_RawNode], linearts: list[_RawNode]) -> set[int]:
    pages = {
        page
        for page in [_page_number(raw.node) for raw in [*nodes, *linearts]]
        if page is not None
    }
    return pages


def _page_number(node: dict[str, Any]) -> int | None:
    value = node.get("page number")
    return value if isinstance(value, int) else None


def _bbox(node: dict[str, Any]) -> list[float] | None:
    value = node.get("bounding box")
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        left, bottom, right, top = [float(component) for component in value]
    except (TypeError, ValueError):
        return None
    if right <= left or top <= bottom:
        return None
    return [left, bottom, right, top]


def _table_shape(table: dict[str, Any]) -> tuple[int, int]:
    rows = _int_or_zero(table.get("number of rows"))
    cols = _int_or_zero(table.get("number of columns"))
    return rows, cols


def _int_or_zero(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _table_cells(table: dict[str, Any]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    rows = table.get("rows")
    if not isinstance(rows, list):
        return cells
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_cells = row.get("cells")
        if not isinstance(raw_cells, list):
            continue
        cells.extend(cell for cell in raw_cells if isinstance(cell, dict))
    return cells


def _outer_border_missing_ratios(cells: list[dict[str, Any]]) -> tuple[float, float]:
    columns = [_int_or_zero(cell.get("column number")) for cell in cells]
    columns = [column for column in columns if column > 0]
    if not columns:
        return 0.0, 0.0
    min_column = min(columns)
    max_column = max(columns)
    left_cells = [cell for cell in cells if _int_or_zero(cell.get("column number")) == min_column]
    right_cells = [cell for cell in cells if _int_or_zero(cell.get("column number")) == max_column]
    return _missing_ratio(left_cells, "has left border"), _missing_ratio(right_cells, "has right border")


def _missing_ratio(cells: list[dict[str, Any]], key: str) -> float:
    if not cells:
        return 0.0
    return sum(1 for cell in cells if cell.get(key) is False) / len(cells)


def _line_chunk_count(page_nodes: list[_RawNode], page_linearts: list[_RawNode]) -> int:
    explicit_lines = sum(1 for raw in page_nodes if raw.node.get("type") == "line")
    lineart_lines = 0
    for lineart in page_linearts:
        chunks = lineart.node.get("line chunks")
        if isinstance(chunks, list):
            lineart_lines += len(chunks)
    return explicit_lines + lineart_lines


def _cluster_linearts(linearts: list[_RawNode]) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    for lineart in linearts:
        lineart_box = _bbox(lineart.node)
        if lineart_box is None:
            continue
        matching_index = None
        for index, cluster in enumerate(clusters):
            if _boxes_touch(cluster.bbox, lineart_box, gap=_CLUSTER_GAP_PT):
                matching_index = index
                break
        if matching_index is None:
            clusters.append(_Cluster(linearts=[lineart], bbox=lineart_box))
        else:
            cluster = clusters[matching_index]
            clusters[matching_index] = _Cluster(
                linearts=[*cluster.linearts, lineart],
                bbox=_union_bbox([cluster.bbox, lineart_box]),
            )
    return _merge_clusters(clusters)


def _merge_clusters(clusters: list[_Cluster]) -> list[_Cluster]:
    merged = True
    while merged:
        merged = False
        for i, first in enumerate(clusters):
            for j in range(i + 1, len(clusters)):
                second = clusters[j]
                if not _boxes_touch(first.bbox, second.bbox, gap=_CLUSTER_GAP_PT):
                    continue
                clusters[i] = _Cluster(
                    linearts=[*first.linearts, *second.linearts],
                    bbox=_union_bbox([first.bbox, second.bbox]),
                )
                clusters.pop(j)
                merged = True
                break
            if merged:
                break
    return clusters


def _unique_boundary_counts(nodes: list[dict[str, Any]]) -> tuple[int, int]:
    xs: list[float] = []
    ys: list[float] = []
    for node in nodes:
        box = _bbox(node)
        if box is None:
            continue
        xs.extend([box[0], box[2]])
        ys.extend([box[1], box[3]])
    return len(_dedupe_coordinates(xs)), len(_dedupe_coordinates(ys))


def _dedupe_coordinates(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > _COORD_TOLERANCE:
            result.append(value)
    return result


def _text_nodes_inside(page_nodes: list[_RawNode], bbox: list[float]) -> int:
    count = 0
    for raw in page_nodes:
        if raw.node.get("type") not in _TEXT_NODE_TYPES:
            continue
        node_box = _bbox(raw.node)
        if node_box is None:
            continue
        if _intersection_area(node_box, bbox) > 0:
            count += 1
    return count


def _has_meaningful_text_between(
    page_nodes: list[_RawNode],
    upper_path: str,
    lower_path: str,
    upper_box: list[float],
    lower_box: list[float],
) -> bool:
    gap_bottom = lower_box[3]
    gap_top = upper_box[1]
    if gap_top <= gap_bottom:
        return False
    combined_x = [min(upper_box[0], lower_box[0]), gap_bottom, max(upper_box[2], lower_box[2]), gap_top]
    for raw in page_nodes:
        if raw.path.startswith(upper_path) or raw.path.startswith(lower_path):
            continue
        if raw.node.get("type") not in _TEXT_NODE_TYPES:
            continue
        if not _node_text(raw.node).strip():
            continue
        node_box = _bbox(raw.node)
        if node_box is not None and _intersection_area(node_box, combined_x) > 0:
            return True
    return False


def _node_text(node: dict[str, Any]) -> str:
    value = node.get("content")
    return value if isinstance(value, str) else ""


def _is_shallow_open_border_shape(table_box: list[float], rows: int, cols: int) -> bool:
    return (
        rows <= _MAX_OPEN_BORDER_TABLE_ROWS
        and cols >= _MIN_OPEN_BORDER_TABLE_COLUMNS
        and table_box[2] - table_box[0] >= _MIN_OPEN_BORDER_TABLE_WIDTH_PT
        and table_box[3] - table_box[1] >= _MIN_OPEN_BORDER_TABLE_HEIGHT_PT
    )


def _open_border_visual_containers(page_linearts: list[_RawNode]) -> list[_RawNode]:
    result: list[_RawNode] = []
    for lineart in page_linearts:
        box = _bbox(lineart.node)
        if box is None:
            continue
        if (
            box[2] - box[0] >= _MIN_OPEN_BORDER_CONTAINER_WIDTH_PT
            and box[3] - box[1] >= _MIN_OPEN_BORDER_CONTAINER_HEIGHT_PT
        ):
            result.append(lineart)
    return result


def _top_aligned_linearts(candidate: _RawNode, page_linearts: list[_RawNode]) -> list[_RawNode]:
    candidate_box = _bbox(candidate.node)
    if candidate_box is None:
        return []
    result: list[_RawNode] = []
    for lineart in page_linearts:
        if lineart.path == candidate.path:
            continue
        lineart_box = _bbox(lineart.node)
        if lineart_box is None:
            continue
        if _x_overlap_ratio(candidate_box, lineart_box) <= 0:
            continue
        top_delta = abs(candidate_box[3] - lineart_box[3])
        bottom_depth = candidate_box[3] - lineart_box[1]
        if top_delta <= _OPEN_BORDER_TOP_EDGE_TOLERANCE_PT and 0.0 <= bottom_depth <= _OPEN_BORDER_HEADER_DEPTH_PT:
            result.append(lineart)
    return result


def _is_vertical_rule_primitive(primitive: Any) -> bool:
    roles = set(getattr(primitive, "candidate_roles", []) or [])
    return (
        getattr(primitive, "object_type", None) == "segmented_vertical_rule"
        or "vertical_line_segment" in roles
        or "vertical_rule" in roles
    )


def _has_outer_vertical_rule(primitives: list[Any], bbox: list[float], *, side: Literal["left", "right"]) -> bool:
    side_x = bbox[0] if side == "left" else bbox[2]
    table_height = bbox[3] - bbox[1]
    min_y_overlap = max(_PDFIUM_OUTER_RULE_MIN_Y_OVERLAP_PT, table_height * _PDFIUM_OUTER_RULE_MIN_Y_OVERLAP_RATIO)
    for primitive in primitives:
        primitive_box = _primitive_bbox(primitive)
        if primitive_box is None:
            continue
        center_x = (primitive_box[0] + primitive_box[2]) / 2
        if abs(center_x - side_x) > _PDFIUM_OUTER_RULE_X_TOLERANCE_PT:
            continue
        y_overlap = max(0.0, min(bbox[3], primitive_box[3]) - max(bbox[1], primitive_box[1]))
        if y_overlap >= min_y_overlap:
            return True
    return False


def _primitive_bbox(primitive: Any) -> list[float] | None:
    box = getattr(primitive, "bounding_box", None)
    if box is None:
        return None
    try:
        return [float(box.left_pt), float(box.bottom_pt), float(box.right_pt), float(box.top_pt)]
    except (TypeError, ValueError, AttributeError):
        return None


def _overlapping_large_container(table_box: list[float], containers: list[_RawNode]) -> _RawNode | None:
    for container in containers:
        container_box = _bbox(container.node)
        if container_box is None:
            continue
        overlap_ratio = _intersection_area(table_box, container_box) / max(_area(table_box), 1.0)
        if overlap_ratio >= 0.85:
            return container
    return None


def _boxes_touch(first: list[float], second: list[float], *, gap: float) -> bool:
    return (
        first[2] >= second[0] - gap
        and second[2] >= first[0] - gap
        and first[3] >= second[1] - gap
        and second[3] >= first[1] - gap
    )


def _x_overlap_ratio(first: list[float], second: list[float]) -> float:
    overlap = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    return overlap / max(min(first[2] - first[0], second[2] - second[0]), 1.0)


def _intersection_area(first: list[float] | None, second: list[float] | None) -> float:
    if first is None or second is None:
        return 0.0
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _area(box: list[float]) -> float:
    return max(box[2] - box[0], 0.0) * max(box[3] - box[1], 0.0)


def _union_bbox(boxes: list[list[float]]) -> list[float]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _round_bbox(box: list[float] | None) -> list[float] | None:
    if box is None:
        return None
    return [round(component, 3) for component in box]


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "PdfTableWarning",
    "detect_pdf_table_warnings",
    "log_pdf_table_warnings",
]
