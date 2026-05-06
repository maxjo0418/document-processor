"""Builder utilities for structural document IR."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import (
    DocIR,
    ParagraphIR,
    RunIR,
    TableCellIR,
    TableIR,
    _anchored_node_id,
    _make_native_anchor,
    _node_anchor_path,
)

if TYPE_CHECKING:
    from .style_types import ParaStyleInfo, StyleMap


_STRUCTURAL_NUM_RE = re.compile(r"\d+")
_PARAGRAPH_KEY_RE = re.compile(r"^(s\d+\.p\d+)")


def _structural_path_sort_key(structural_path: str) -> tuple[tuple[int, ...], str]:
    nums = tuple(int(v) for v in _STRUCTURAL_NUM_RE.findall(structural_path))
    return nums, structural_path


def _new_paragraph(path: str, *, para_style=None) -> ParagraphIR:
    return ParagraphIR(
        node_id=_anchored_node_id("paragraph", path),
        para_style=para_style,
        native_anchor=_make_native_anchor("paragraph", path),
    )


def _new_run(path: str, *, text: str, run_style=None) -> RunIR:
    return RunIR(
        node_id=_anchored_node_id("run", path),
        text=text,
        run_style=run_style,
        native_anchor=_make_native_anchor("run", path, text=text),
    )


def _new_table(path: str, *, table_style=None) -> TableIR:
    return TableIR(
        node_id=_anchored_node_id("table", path),
        row_count=table_style.row_count if table_style else 0,
        col_count=table_style.col_count if table_style else 0,
        table_style=table_style,
        native_anchor=_make_native_anchor("table", path),
    )


def _new_cell(path: str, *, row_index: int, col_index: int, cell_style=None) -> TableCellIR:
    return TableCellIR(
        node_id=_anchored_node_id("cell", path),
        cell_style=cell_style,
        native_anchor=_make_native_anchor("cell", path),
    )


def _safe_para_for_id(
    paragraph_map: dict[str, ParagraphIR],
    paragraph_id: str,
    *,
    style_map: "StyleMap | None",
) -> ParagraphIR:
    paragraph = paragraph_map.get(paragraph_id)
    if paragraph is not None:
        return paragraph

    para_style = style_map.paragraphs.get(paragraph_id) if style_map else None
    paragraph = _new_paragraph(paragraph_id, para_style=para_style)
    paragraph_map[paragraph_id] = paragraph
    return paragraph


def _merge_para_style(
    existing_style: "ParaStyleInfo | None",
    incoming_style: "ParaStyleInfo | None",
) -> "ParaStyleInfo | None":
    if incoming_style is None:
        return existing_style
    if existing_style is None:
        return incoming_style

    merged_style = incoming_style.model_copy(deep=True)
    if merged_style.column_layout is None and existing_style.column_layout is not None:
        merged_style.column_layout = existing_style.column_layout.model_copy(deep=True)
    if merged_style.list_info is None and existing_style.list_info is not None:
        merged_style.list_info = existing_style.list_info.model_copy(deep=True)
    return merged_style


def _is_token(token: str, prefix: str) -> bool:
    return token.startswith(prefix) and token[len(prefix):].isdigit()


def _get_or_create_table(
    parent,
    table_id: str,
    *,
    style_map: "StyleMap | None",
    table_map: dict[str, TableIR],
) -> TableIR:
    table = table_map.get(table_id)
    if table is not None:
        return table

    table_style = style_map.tables.get(table_id) if style_map else None
    table = _new_table(table_id, table_style=table_style)
    table_map[table_id] = table
    parent.append_content(table)
    return table


def _get_or_create_cell(
    table: TableIR,
    table_id: str,
    row_index: int,
    col_index: int,
    *,
    style_map: "StyleMap | None",
    cell_map: dict[tuple[str, int, int], TableCellIR],
) -> TableCellIR:
    cell_bucket_key = (table_id, row_index, col_index)
    cell = cell_map.get(cell_bucket_key)
    if cell is not None:
        return cell

    cell_id = f"{table_id}.tr{row_index}.tc{col_index}"
    cell_style = style_map.cells.get(cell_id) if style_map else None
    cell = _new_cell(cell_id, row_index=row_index, col_index=col_index, cell_style=cell_style)
    cell_map[cell_bucket_key] = cell
    table.append_cell(cell, row_index=row_index, col_index=col_index)
    return cell


def _get_or_create_cell_paragraph(
    cell: TableCellIR,
    table_id: str,
    row_index: int,
    col_index: int,
    paragraph_index: int,
    *,
    style_map: "StyleMap | None",
    cell_paragraph_map: dict[tuple[str, int, int, int], ParagraphIR],
) -> ParagraphIR:
    cell_paragraph_bucket_key = (table_id, row_index, col_index, paragraph_index)
    cell_paragraph = cell_paragraph_map.get(cell_paragraph_bucket_key)
    if cell_paragraph is not None:
        return cell_paragraph

    cell_paragraph_id = f"{table_id}.tr{row_index}.tc{col_index}.p{paragraph_index}"
    para_style = style_map.paragraphs.get(cell_paragraph_id) if style_map else None
    cell_paragraph = _new_paragraph(cell_paragraph_id, para_style=para_style)
    cell_paragraph_map[cell_paragraph_bucket_key] = cell_paragraph
    cell.paragraphs.append(cell_paragraph)
    return cell_paragraph


def _attach_run(
    container,
    container_id: str,
    run_token: str,
    text: str,
    *,
    style_map: "StyleMap | None",
) -> None:
    run_id = f"{container_id}.{run_token}"
    run_style = style_map.runs.get(run_id) if style_map else None
    container.append_content(_new_run(run_id, text=text, run_style=run_style))


def _ingest_table_tokens(
    table: TableIR,
    table_id: str,
    tokens: list[str],
    text: str,
    *,
    style_map: "StyleMap | None",
    table_map: dict[str, TableIR],
    cell_map: dict[tuple[str, int, int], TableCellIR],
    cell_paragraph_map: dict[tuple[str, int, int, int], ParagraphIR],
) -> None:
    if len(tokens) < 3 or not _is_token(tokens[0], "tr") or not _is_token(tokens[1], "tc") or not _is_token(tokens[2], "p"):
        return

    row_index = int(tokens[0][2:])
    col_index = int(tokens[1][2:])
    paragraph_index = int(tokens[2][1:])

    cell = _get_or_create_cell(
        table,
        table_id,
        row_index,
        col_index,
        style_map=style_map,
        cell_map=cell_map,
    )
    cell_paragraph = _get_or_create_cell_paragraph(
        cell,
        table_id,
        row_index,
        col_index,
        paragraph_index,
        style_map=style_map,
        cell_paragraph_map=cell_paragraph_map,
    )
    _ingest_paragraph_like_tokens(
        cell_paragraph,
        _node_anchor_path(cell_paragraph),
        tokens[3:],
        text,
        style_map=style_map,
        table_map=table_map,
        cell_map=cell_map,
        cell_paragraph_map=cell_paragraph_map,
        allow_run_anchored_table=False,
    )


def _ingest_paragraph_like_tokens(
    container,
    container_id: str,
    tokens: list[str],
    text: str,
    *,
    style_map: "StyleMap | None",
    table_map: dict[str, TableIR],
    cell_map: dict[tuple[str, int, int], TableCellIR],
    cell_paragraph_map: dict[tuple[str, int, int, int], ParagraphIR],
    allow_run_anchored_table: bool,
) -> None:
    if not tokens:
        return

    token = tokens[0]
    if _is_token(token, "r"):
        if len(tokens) == 1:
            _attach_run(
                container,
                container_id,
                token,
                text,
                style_map=style_map,
            )
            return

        if allow_run_anchored_table and len(tokens) >= 2 and _is_token(tokens[1], "tbl"):
            table_id = f"{container_id}.{token}.{tokens[1]}"
            table = _get_or_create_table(
                container,
                table_id,
                style_map=style_map,
                table_map=table_map,
            )
            _ingest_table_tokens(
                table,
                table_id,
                tokens[2:],
                text,
                style_map=style_map,
                table_map=table_map,
                cell_map=cell_map,
                cell_paragraph_map=cell_paragraph_map,
            )
            return

    if _is_token(token, "tbl"):
        table_id = f"{container_id}.{token}"
        table = _get_or_create_table(
            container,
            table_id,
            style_map=style_map,
            table_map=table_map,
        )
        _ingest_table_tokens(
            table,
            table_id,
            tokens[1:],
            text,
            style_map=style_map,
            table_map=table_map,
            cell_map=cell_map,
            cell_paragraph_map=cell_paragraph_map,
        )


def _finalize_table(
    table: TableIR,
) -> None:
    max_row = 0
    max_col = 0
    for row_index, col_index, cell in table.iter_cell_positions():
        max_row = max(max_row, row_index)
        max_col = max(max_col, col_index)

        cell.paragraphs.sort(key=lambda cp: _structural_path_sort_key(_node_anchor_path(cp)))
        for cell_paragraph in cell.paragraphs:
            cell_paragraph.sort_content(key=lambda node: _structural_path_sort_key(_node_anchor_path(node)))
            for nested_table in cell_paragraph.tables:
                _finalize_table(nested_table)
            cell_paragraph.recompute_text()

        cell.recompute_text()

    if table.row_count <= 0:
        table.row_count = max_row
    if table.col_count <= 0:
        table.col_count = max_col


def _infer_source_doc_type(source_path: str | Path | None) -> str | None:
    if source_path is None:
        return None
    suffix = Path(source_path).suffix.lower()
    if suffix.startswith("."):
        suffix = suffix[1:]
    return suffix or None


def apply_style_map_to_doc_ir(doc_ir: "DocIR", style_map: "StyleMap | None") -> "DocIR":
    """Attach styles to an existing structural document IR."""
    if style_map is None:
        return doc_ir

    def _apply_table_styles(table: TableIR) -> None:
        table_path = _node_anchor_path(table)
        if table_path in style_map.tables:
            table_style = style_map.tables[table_path]
            table.table_style = table_style
            if table.row_count <= 0:
                table.row_count = table_style.row_count
            if table.col_count <= 0:
                table.col_count = table_style.col_count

        for cell in table.iter_cells():
            cell_path = _node_anchor_path(cell)
            if cell_path in style_map.cells:
                cell.cell_style = style_map.cells[cell_path]
            for paragraph in cell.paragraphs:
                paragraph_path = _node_anchor_path(paragraph)
                if paragraph_path in style_map.paragraphs:
                    paragraph.para_style = _merge_para_style(
                        paragraph.para_style,
                        style_map.paragraphs[paragraph_path],
                    )
                for run in paragraph.runs:
                    run_path = _node_anchor_path(run)
                    if run_path in style_map.runs:
                        run.run_style = style_map.runs[run_path]
                for nested_table in paragraph.tables:
                    _apply_table_styles(nested_table)

    for paragraph in doc_ir.paragraphs:
        paragraph_path = _node_anchor_path(paragraph)
        if paragraph_path in style_map.paragraphs:
            paragraph.para_style = _merge_para_style(
                paragraph.para_style,
                style_map.paragraphs[paragraph_path],
            )
        for run in paragraph.runs:
            run_path = _node_anchor_path(run)
            if run_path in style_map.runs:
                run.run_style = style_map.runs[run_path]
        for table in paragraph.tables:
            _apply_table_styles(table)

    return doc_ir


def build_doc_ir_from_mapping(
    mapping: dict[str, str],
    *,
    style_map: "StyleMap | None" = None,
    source_path: str | Path | None = None,
    source_doc_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
    doc_cls: type["DocIR"] | None = None,
    **doc_kwargs: Any,
) -> "DocIR":
    """Build document IR from a run-level structural mapping."""

    paragraph_map: dict[str, ParagraphIR] = {}
    table_map: dict[str, TableIR] = {}
    cell_map: dict[tuple[str, int, int], TableCellIR] = {}
    cell_paragraph_map: dict[tuple[str, int, int, int], ParagraphIR] = {}

    sorted_items = sorted(mapping.items(), key=lambda kv: _structural_path_sort_key(kv[0]))

    for structural_path, text in sorted_items:
        paragraph_match = _PARAGRAPH_KEY_RE.match(structural_path)
        if not paragraph_match:
            continue

        paragraph_id = paragraph_match.group(1)
        paragraph = _safe_para_for_id(paragraph_map, paragraph_id, style_map=style_map)

        _ingest_paragraph_like_tokens(
            paragraph,
            paragraph_id,
            structural_path.split(".")[2:],
            text,
            style_map=style_map,
            table_map=table_map,
            cell_map=cell_map,
            cell_paragraph_map=cell_paragraph_map,
            allow_run_anchored_table=True,
        )

    paragraphs = sorted(paragraph_map.values(), key=lambda p: _structural_path_sort_key(_node_anchor_path(p)))

    for paragraph in paragraphs:
        paragraph.sort_content(key=lambda node: _structural_path_sort_key(_node_anchor_path(node)))

        for table in paragraph.tables:
            _finalize_table(table)

        paragraph.recompute_text()

    resolved_source_path = str(source_path) if source_path is not None else None
    resolved_doc_type = source_doc_type or _infer_source_doc_type(source_path)
    resolved_doc_id = doc_id
    if resolved_doc_id is None and source_path is not None:
        resolved_doc_id = Path(source_path).stem

    resolved_doc_cls = doc_cls or DocIR
    doc_ir = resolved_doc_cls(
        doc_id=resolved_doc_id,
        source_path=resolved_source_path,
        source_doc_type=resolved_doc_type,
        metadata=metadata or {},
        paragraphs=paragraphs,
        **doc_kwargs,
    )
    return apply_style_map_to_doc_ir(doc_ir, style_map)

__all__ = ["apply_style_map_to_doc_ir", "build_doc_ir_from_mapping"]
