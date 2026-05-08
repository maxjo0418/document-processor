from __future__ import annotations

import base64
from collections import defaultdict
from html import escape
import re

from pydantic import BaseModel, Field, model_validator

from .models import DocIR, ImageIR, PageInfo, ParagraphIR, RunIR, TableCellIR, TableIR
from .style_types import CellStyleInfo, ColumnLayoutInfo, ParaStyleInfo, RunStyleInfo, normalize_list_marker

_NATIVE_CELL_ALIGNMENT_DOC_TYPES = {"docx", "hwpx", "hwp"}


def _non_negative_pt(value: float | None) -> float | None:
    if value is None:
        return None
    return max(value, 0.0)


def _column_style_key(style: ParaStyleInfo | None) -> tuple[object, ...] | None:
    layout = style.column_layout if style is not None else None
    if layout is None or (layout.count or 1) <= 1:
        return None
    return (
        layout.count,
        round(layout.gap_pt, 3) if layout.gap_pt is not None else None,
        tuple(round(width, 3) for width in layout.widths_pt),
        tuple(round(gap, 3) for gap in layout.gaps_pt),
        layout.equal_width,
    )


def _paragraph_column_style(paragraph: ParagraphIR) -> ColumnLayoutInfo | None:
    return paragraph.para_style.column_layout if _column_style_key(paragraph.para_style) is not None else None


def _column_group_css(style: ColumnLayoutInfo) -> str:
    parts = [
        f"column-count:{max(style.count or 1, 1)}",
        f"-webkit-column-count:{max(style.count or 1, 1)}",
        "column-fill:balance",
        "break-inside:auto",
    ]
    gap_pt = _non_negative_pt(style.gap_pt)
    if gap_pt is not None:
        parts.append(f"column-gap:{gap_pt:.1f}pt")
        parts.append(f"-webkit-column-gap:{gap_pt:.1f}pt")
    return ";".join(parts)


class _AnnotationValidationError(ValueError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class _Annotation(BaseModel):
    target_id: str
    selected_text: str | None = None
    occurrence_index: int | None = Field(default=None, ge=0)
    label: str
    color: str = "#FFFF00"
    note: str = ""

    @model_validator(mode="after")
    def _validate_selection(self) -> "_Annotation":
        if self.selected_text == "":
            raise ValueError("selected_text must not be empty.")
        if self.selected_text is None and self.occurrence_index is not None:
            raise ValueError("occurrence_index requires selected_text.")
        return self


class _ResolvedAnnotation(BaseModel):
    target_id: str
    target_kind: str
    selected_text: str
    occurrence_index: int | None = None
    start: int
    end: int
    label: str
    color: str = "#FFFF00"
    note: str = ""


def _iter_paragraphs(paragraphs: list[ParagraphIR]):
    for paragraph in paragraphs:
        yield paragraph
        for table in paragraph.tables:
            yield from _iter_table_paragraphs(table)


def _iter_table_paragraphs(table: TableIR):
    for cell in table.iter_cells():
        for paragraph in cell.paragraphs:
            yield paragraph
            for nested_table in paragraph.tables:
                yield from _iter_table_paragraphs(nested_table)


def _paragraph_plain_text(paragraph: ParagraphIR) -> str:
    return "".join(run.text for run in paragraph.runs)


def _find_text_occurrences(text: str, selected_text: str) -> list[int]:
    occurrences: list[int] = []
    search_from = 0
    while True:
        index = text.find(selected_text, search_from)
        if index < 0:
            return occurrences
        occurrences.append(index)
        search_from = index + 1


def _resolve_selected_span(
    text: str,
    *,
    selected_text: str | None,
    occurrence_index: int | None,
    target_id: str,
) -> tuple[int, int, str, int | None]:
    if selected_text is None:
        return 0, len(text), text, None

    matches = _find_text_occurrences(text, selected_text)
    if not matches:
        raise _AnnotationValidationError(
            f"Selected text does not occur in {target_id}: {selected_text!r}.",
            code="selected_text_not_found",
        )

    if occurrence_index is None:
        if len(matches) > 1:
            raise _AnnotationValidationError(
                f"Selected text is ambiguous in {target_id}; specify occurrence_index.",
                code="selected_text_ambiguous",
            )
        occurrence_index = 0
    elif occurrence_index >= len(matches):
        raise _AnnotationValidationError(
            f"occurrence_index {occurrence_index} is out of bounds for {target_id}; found {len(matches)} match(es).",
            code="occurrence_index_out_of_bounds",
        )

    start = matches[occurrence_index]
    end = start + len(selected_text)
    return start, end, selected_text, occurrence_index


def _resolve_annotation_target(
    doc: DocIR,
    annotation: _Annotation,
) -> _ResolvedAnnotation:
    doc.ensure_node_identity()
    paragraph_map = {paragraph.node_id: paragraph for paragraph in _iter_paragraphs(doc.paragraphs)}
    paragraph_id_map = {
        paragraph.node_id: paragraph
        for paragraph in paragraph_map.values()
        if paragraph.node_id is not None
    }
    run_map = {
        run.node_id: run
        for paragraph in paragraph_map.values()
        for run in paragraph.runs
    }
    run_id_map = {
        run.node_id: run
        for run in run_map.values()
        if run.node_id is not None
    }

    if annotation.target_id in run_id_map:
        run = run_id_map[annotation.target_id]
        text = run.text
        start, end, resolved_text, resolved_occurrence_index = _resolve_selected_span(
            text,
            selected_text=annotation.selected_text,
            occurrence_index=annotation.occurrence_index,
            target_id=annotation.target_id,
        )
        return _ResolvedAnnotation(
            target_id=annotation.target_id,
            target_kind="run",
            selected_text=resolved_text,
            occurrence_index=resolved_occurrence_index,
            start=start,
            end=end,
            label=annotation.label,
            color=annotation.color,
            note=annotation.note,
        )

    if annotation.target_id in paragraph_id_map:
        paragraph = paragraph_id_map[annotation.target_id]
        if paragraph.tables or paragraph.images:
            raise _AnnotationValidationError(
                f"Paragraph annotations do not support tables/images yet: {annotation.target_id}."
            )
        text = _paragraph_plain_text(paragraph)
        start, end, resolved_text, resolved_occurrence_index = _resolve_selected_span(
            text,
            selected_text=annotation.selected_text,
            occurrence_index=annotation.occurrence_index,
            target_id=annotation.target_id,
        )
        return _ResolvedAnnotation(
            target_id=annotation.target_id,
            target_kind="paragraph",
            selected_text=resolved_text,
            occurrence_index=resolved_occurrence_index,
            start=start,
            end=end,
            label=annotation.label,
            color=annotation.color,
            note=annotation.note,
        )

    raise _AnnotationValidationError(f"Annotation target does not exist in DocIR: {annotation.target_id}")


def _resolve_annotations(
    doc: DocIR,
    annotations: list[_Annotation],
) -> list[_ResolvedAnnotation]:
    return [_resolve_annotation_target(doc, annotation) for annotation in annotations]


def _run_css(style: RunStyleInfo) -> str:
    parts: list[str] = []
    if style.color:
        parts.append(f"color:{style.color}")
    size_pt = _non_negative_pt(style.size_pt)
    if size_pt:
        parts.append(f"font-size:{size_pt:.1f}pt")
    if style.highlight:
        parts.append(f"background-color:{style.highlight}")

    decorations = []
    if style.underline:
        decorations.append("underline")
    if style.strikethrough:
        decorations.append("line-through")
    if decorations:
        parts.append(f"text-decoration:{' '.join(decorations)}")

    return ";".join(parts)


def _style_wrap(html: str, style: RunStyleInfo | None) -> str:
    if style is None:
        return html

    if style.superscript:
        html = f"<sup>{html}</sup>"
    elif style.subscript:
        html = f"<sub>{html}</sub>"
    if style.bold:
        html = f"<b>{html}</b>"
    if style.italic:
        html = f"<i>{html}</i>"

    css = _run_css(style)
    if css:
        html = f'<span style="{css}">{html}</span>'

    return html


def _escape_whitespace(html: str) -> str:
    html = html.replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")
    html = re.sub(r"  +", lambda match: "&nbsp;" * len(match.group(0)), html)
    return html


def _apply_annotations(
    text: str,
    annotations: list[_ResolvedAnnotation],
) -> str:
    if not annotations:
        return escape(text)

    breakpoints = sorted({0, len(text)} | {item.start for item in annotations} | {item.end for item in annotations})
    fragments: list[str] = []

    for index in range(len(breakpoints) - 1):
        start = breakpoints[index]
        end = breakpoints[index + 1]
        segment = text[start:end]
        if not segment:
            continue

        active = [item for item in annotations if item.start <= start and item.end >= end]
        escaped_segment = escape(segment)
        if not active:
            fragments.append(escaped_segment)
            continue

        color = active[0].color
        label = " | ".join(item.label for item in active if item.label)
        note = " | ".join(item.note for item in active if item.note)
        attrs = [f'style="background-color:{escape(color)};padding:1px 2px;border-radius:2px"']
        if label:
            attrs.append(f'data-label="{escape(label)}"')
        if note:
            attrs.append(f'data-note="{escape(note)}"')
        title = " | ".join(part for part in (label, note) if part)
        if title:
            attrs.append(f'title="{escape(title)}"')
        fragments.append(f"<mark {' '.join(attrs)}>{escaped_segment}</mark>")

    return "".join(fragments)


def _wrap_run_with_annotations(run: RunIR, annotations: list[_ResolvedAnnotation]) -> str:
    html = _apply_annotations(run.text, annotations)
    if not html:
        return ""
    html = _escape_whitespace(html)
    html = _style_wrap(html, run.run_style)
    return f'<span data-node-id="{escape(run.node_id or "")}">{html}</span>'


def _render_image(doc_ir: DocIR, image: ImageIR) -> str:
    asset = doc_ir.assets.get(image.image_id)
    if asset is None:
        return ""

    style_parts = ["max-width:100%", "vertical-align:middle"]
    display_width_pt = _non_negative_pt(image.display_width_pt)
    display_height_pt = _non_negative_pt(image.display_height_pt)
    if display_width_pt is not None:
        style_parts.append(f"width:{display_width_pt:.1f}pt")
    if display_height_pt is not None:
        style_parts.append(f"height:{display_height_pt:.1f}pt")
    elif image.display_width_pt is None:
        style_parts.append("height:auto")

    attrs = [
        f'src="{escape(asset.as_data_url(), quote=True)}"',
        f'alt="{escape(image.alt_text or asset.filename or "")}"',
        f'style="{";".join(style_parts)}"',
    ]
    return f"<img {' '.join(attrs)} />"


def _paragraph_indent_values(style: ParaStyleInfo | None) -> tuple[float | None, float | None, float | None]:
    if style is None:
        return None, None, None

    left_indent = _non_negative_pt(style.left_indent_pt)
    right_indent = _non_negative_pt(style.right_indent_pt)
    if style.first_line_indent_pt is None:
        return left_indent, right_indent, None

    left_for_indent = left_indent or 0.0
    effective_first_line_start = max(left_for_indent + style.first_line_indent_pt, 0.0)
    first_line_indent = effective_first_line_start - left_for_indent
    return left_indent, right_indent, first_line_indent


def _paragraph_css(style: ParaStyleInfo | None) -> str:
    parts: list[str] = ["margin:0"]
    if style is not None:
        if style.align:
            parts.append(f"text-align:{style.align}")
        left_indent, right_indent, first_line_indent = _paragraph_indent_values(style)
        if left_indent is not None:
            parts.append(f"padding-left:{left_indent:.1f}pt")
        if right_indent is not None:
            parts.append(f"padding-right:{right_indent:.1f}pt")
        if first_line_indent is not None:
            parts.append(f"text-indent:{first_line_indent:.1f}pt")
    return ";".join(parts)


def _uses_native_cell_alignment_defaults(doc_ir: DocIR) -> bool:
    return (doc_ir.source_doc_type or "").lower() in _NATIVE_CELL_ALIGNMENT_DOC_TYPES


def _paragraph_style_without_align(style: ParaStyleInfo | None) -> ParaStyleInfo | None:
    if style is None or style.align is None:
        return style
    return style.model_copy(update={"align": None})


def _cell_controls_paragraph_alignment(_doc_ir: DocIR, cell: TableCellIR) -> bool:
    return cell.cell_style is not None and cell.cell_style.horizontal_align is not None


def _list_marker_html(style: ParaStyleInfo | None) -> str:
    list_info = style.list_info if style is not None else None
    if list_info is None or not list_info.marker:
        return ""
    level = max(list_info.level, 0)
    min_width = max(12.0, 14.0 + level * 8.0)
    marker = escape(normalize_list_marker(list_info.marker, list_info.marker_type) or "")
    return (
        f'<span class="document-list-marker" '
        f'style="display:inline-block;min-width:{min_width:.1f}pt;margin-right:4.0pt;white-space:nowrap">'
        f"{marker}</span>"
    )


def _flush_paragraph(
    paragraph_id: str,
    fragments: list[str],
    para_style: ParaStyleInfo | None,
) -> str:
    content = "".join(fragments)
    if not content.strip():
        content = "&nbsp;"
    list_marker = _list_marker_html(para_style)
    if list_marker:
        content = f"{list_marker}{content}"
    return (
        f'<p data-node-id="{escape(paragraph_id)}" '
        f'style="{_paragraph_css(para_style)}">'
        f"{content}</p>"
    )


def _parse_border_css(border_css: str | None) -> tuple[int, str, str] | None:
    if not border_css:
        return None
    match = re.fullmatch(r"\s*(\d+)px\s+([a-zA-Z-]+)\s+(#[0-9A-Fa-f]{3,8})\s*", border_css)
    if not match:
        return None
    return int(match.group(1)), match.group(2), match.group(3)


def _svg_dasharray(style_name: str, stroke_width: int) -> str | None:
    if style_name == "dashed":
        return f"{max(stroke_width * 4, 4)} {max(stroke_width * 2, 2)}"
    if style_name == "dotted":
        return f"{max(stroke_width, 1)} {max(stroke_width * 2, 2)}"
    return None


def _svg_diagonal_lines(style: CellStyleInfo) -> str | None:
    diagonals: list[tuple[str, int, str, str]] = []
    for direction, border_css in (
        ("tl_br", style.diagonal_tl_br),
        ("tr_bl", style.diagonal_tr_bl),
    ):
        parsed = _parse_border_css(border_css)
        if parsed is None:
            continue
        stroke_width, style_name, color = parsed
        diagonals.append((direction, stroke_width, style_name, color))

    if not diagonals:
        return None

    svg_parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" preserveAspectRatio="none">']
    for direction, stroke_width, style_name, color in diagonals:
        dasharray = _svg_dasharray(style_name, stroke_width)
        if direction == "tl_br":
            coords = (0, 0, 100, 100)
        else:
            coords = (100, 0, 0, 100)

        attrs = [
            f'x1="{coords[0]}"',
            f'y1="{coords[1]}"',
            f'x2="{coords[2]}"',
            f'y2="{coords[3]}"',
            f'stroke="{color}"',
            f'stroke-width="{stroke_width}"',
        ]
        if dasharray:
            attrs.append(f'stroke-dasharray="{dasharray}"')
        svg_parts.append(f"<line {' '.join(attrs)} />")
    svg_parts.append("</svg>")
    return "".join(svg_parts)


def _cell_diagonal_background(style: CellStyleInfo) -> str | None:
    svg = _svg_diagonal_lines(style)
    if svg is None:
        return None
    svg_base64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{svg_base64}"


def _css_vertical_align(value: str) -> str:
    return "middle" if value == "center" else value


def _cell_padding_css(style: CellStyleInfo | None) -> str:
    if style is None:
        return "padding:0"

    raw_values = (
        style.padding_top_pt,
        style.padding_right_pt,
        style.padding_bottom_pt,
        style.padding_left_pt,
    )
    if all(value is None for value in raw_values):
        return "padding:0"

    top = _non_negative_pt(style.padding_top_pt) or 0.0
    right = _non_negative_pt(style.padding_right_pt) or 0.0
    bottom = _non_negative_pt(style.padding_bottom_pt) or 0.0
    left = _non_negative_pt(style.padding_left_pt) or 0.0
    return f"padding:{top:.1f}pt {right:.1f}pt {bottom:.1f}pt {left:.1f}pt"


def _cell_css(
    style: CellStyleInfo | None,
    *,
    default_horizontal_align: str | None = None,
    default_vertical_align: str | None = None,
) -> str:
    parts: list[str] = ["box-sizing:border-box"]
    vertical_align = style.vertical_align if style is not None else None
    horizontal_align = style.horizontal_align if style is not None else None
    vertical_align = vertical_align or default_vertical_align
    horizontal_align = horizontal_align or default_horizontal_align
    if vertical_align:
        parts.append(f"vertical-align:{_css_vertical_align(vertical_align)}")
    if horizontal_align:
        parts.append(f"text-align:{horizontal_align}")
    if style is not None:
        if style.background:
            parts.append(f"background-color:{style.background}")
        width_pt = _non_negative_pt(style.width_pt)
        height_pt = _non_negative_pt(style.height_pt)
        if width_pt is not None:
            parts.append(f"width:{width_pt:.1f}pt")
        if height_pt is not None:
            parts.append(f"height:{height_pt:.1f}pt")
        parts.append(f"border-top:{style.border_top or 'none'}")
        parts.append(f"border-bottom:{style.border_bottom or 'none'}")
        parts.append(f"border-left:{style.border_left or 'none'}")
        parts.append(f"border-right:{style.border_right or 'none'}")
        diagonal_background = _cell_diagonal_background(style)
        if diagonal_background:
            parts.append(f"background-image:url({diagonal_background})")
            parts.append("background-repeat:no-repeat")
            parts.append("background-size:100% 100%")
    else:
        parts.extend(
            [
                "border-top:none",
                "border-bottom:none",
                "border-left:none",
                "border-right:none",
            ]
        )
    parts.append(_cell_padding_css(style))
    return ";".join(parts)


def _table_css(table: TableIR, para_style: ParaStyleInfo | None) -> str:
    align = para_style.align if para_style is not None else None
    parts = ["border-collapse:collapse", "table-layout:fixed", "margin-top:8px", "margin-bottom:12px"]
    if table.table_style is not None:
        width_pt = _non_negative_pt(table.table_style.width_pt)
        height_pt = _non_negative_pt(table.table_style.height_pt)
        if width_pt is not None:
            parts.append(f"width:{width_pt:.1f}pt")
        if height_pt is not None:
            parts.append(f"height:{height_pt:.1f}pt")
    if align == "center":
        parts.extend(["margin-left:auto", "margin-right:auto"])
    elif align == "right":
        parts.extend(["margin-left:auto", "margin-right:0"])
    else:
        parts.extend(["margin-left:0", "margin-right:auto"])
    return ";".join(parts)


def _table_logical_col_count(table: TableIR) -> int:
    col_count = table.col_count
    if table.table_style is not None:
        col_count = max(col_count, table.table_style.col_count)
    for _row_index, col_index, cell in table.iter_cell_positions():
        colspan = max(cell.cell_style.colspan, 1) if cell.cell_style is not None else 1
        col_count = max(col_count, col_index + colspan - 1)
    return col_count


def _table_column_widths(table: TableIR) -> list[float | None]:
    col_count = _table_logical_col_count(table)
    if col_count <= 0:
        return []

    widths: list[float | None] = [None] * col_count
    spanned_widths: list[tuple[int, int, float]] = []
    for _row_index, col_index, cell in table.iter_cell_positions():
        if cell.cell_style is None:
            continue
        width_pt = _non_negative_pt(cell.cell_style.width_pt)
        if width_pt is None:
            continue
        start = max(col_index - 1, 0)
        if start >= col_count:
            continue
        colspan = min(max(cell.cell_style.colspan, 1), col_count - start)
        if colspan == 1:
            current_width = widths[start]
            widths[start] = width_pt if current_width is None else max(current_width, width_pt)
        else:
            spanned_widths.append((start, colspan, width_pt))

    for start, colspan, total_width in spanned_widths:
        indices = list(range(start, start + colspan))
        known_width = sum(widths[index] or 0.0 for index in indices)
        unknown_indices = [index for index in indices if widths[index] is None]
        if not unknown_indices:
            continue
        remaining_width = max(total_width - known_width, 0.0)
        share = remaining_width / len(unknown_indices) if remaining_width else total_width / colspan
        for index in unknown_indices:
            widths[index] = share

    table_width = _non_negative_pt(table.table_style.width_pt) if table.table_style is not None else None
    if table_width is not None:
        unknown_indices = [index for index, width in enumerate(widths) if width is None]
        known_width = sum(width or 0.0 for width in widths)
        remaining_width = max(table_width - known_width, 0.0)
        if unknown_indices and remaining_width:
            share = remaining_width / len(unknown_indices)
            for index in unknown_indices:
                widths[index] = share

    return widths


def _render_colgroup(table: TableIR) -> list[str]:
    widths = _table_column_widths(table)
    if not any(width is not None for width in widths):
        return []

    lines = ["  <colgroup>"]
    for width in widths:
        if width is None:
            lines.append("    <col />")
        else:
            lines.append(f'    <col style="width:{width:.1f}pt" />')
    lines.append("  </colgroup>")
    return lines


def _run_annotations_for_segment(
    run: RunIR,
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    paragraph_annotations: list[_ResolvedAnnotation],
    paragraph_cursor: int,
) -> list[_ResolvedAnnotation]:
    resolved: list[_ResolvedAnnotation] = []
    for item in run_annotations_by_id.get(run.node_id, []):
        resolved.append(item)

    run_start = paragraph_cursor
    run_end = paragraph_cursor + len(run.text)
    for item in paragraph_annotations:
        if item.end <= run_start or item.start >= run_end:
            continue
        local_start = max(0, item.start - run_start)
        local_end = min(len(run.text), item.end - run_start)
        if local_start >= local_end:
            continue
        resolved.append(
            _ResolvedAnnotation(
                target_id=run.node_id or "",
                target_kind="run_segment",
                selected_text=run.text[local_start:local_end],
                start=local_start,
                end=local_end,
                label=item.label,
                color=item.color,
                note=item.note,
            )
        )
    return resolved


def _render_paragraph_like(
    doc_ir: DocIR,
    paragraph: ParagraphIR,
    paragraph_annotations: list[_ResolvedAnnotation],
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
) -> str:
    parts: list[str] = []
    inline_fragments: list[str] = []
    paragraph_cursor = 0

    for node in paragraph.content:
        if isinstance(node, RunIR):
            annotations = _run_annotations_for_segment(
                node,
                run_annotations_by_id,
                paragraph_annotations,
                paragraph_cursor,
            )
            inline_fragments.append(_wrap_run_with_annotations(node, annotations))
            paragraph_cursor += len(node.text)
            continue

        if isinstance(node, ImageIR):
            image_html = _render_image(doc_ir, node)
            if image_html:
                inline_fragments.append(image_html)
            continue

        if isinstance(node, TableIR):
            if inline_fragments:
                parts.append(
                    _flush_paragraph(
                        paragraph.node_id or "",
                        inline_fragments,
                        paragraph.para_style,
                    )
                )
                inline_fragments = []
            parts.append(
                _render_table(
                    doc_ir,
                    node,
                    run_annotations_by_id,
                    para_style=paragraph.para_style,
                    paragraph_annotations_by_id=paragraph_annotations_by_id,
                )
            )

    if inline_fragments:
        parts.append(
            _flush_paragraph(
                paragraph.node_id or "",
                inline_fragments,
                paragraph.para_style,
            )
        )
    elif not parts:
        parts.append(
            _flush_paragraph(
                paragraph.node_id or "",
                [],
                paragraph.para_style,
            )
        )

    return "\n".join(parts)


def _render_cell_paragraph(
    doc_ir: DocIR,
    cell: TableCellIR,
    paragraph: ParagraphIR,
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
) -> str:
    render_paragraph = paragraph
    if _cell_controls_paragraph_alignment(doc_ir, cell):
        render_paragraph = paragraph.model_copy(
            update={"para_style": _paragraph_style_without_align(paragraph.para_style)}
        )
    return _render_paragraph_like(
        doc_ir,
        render_paragraph,
        paragraph_annotations_by_id.get(paragraph.node_id, []),
        paragraph_annotations_by_id,
        run_annotations_by_id,
    )


def _render_cell(
    doc_ir: DocIR,
    cell: TableCellIR,
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
) -> str:
    use_native_defaults = _uses_native_cell_alignment_defaults(doc_ir)
    cell_css = _cell_css(
        cell.cell_style,
        default_horizontal_align="left" if use_native_defaults else None,
        default_vertical_align="center" if use_native_defaults else None,
    )
    attrs = [
        f'data-node-id="{escape(cell.node_id or "")}"',
        f'style="{cell_css}"',
    ]
    if cell.cell_style is not None:
        if cell.cell_style.colspan > 1:
            attrs.append(f'colspan="{cell.cell_style.colspan}"')
        if cell.cell_style.rowspan > 1:
            attrs.append(f'rowspan="{cell.cell_style.rowspan}"')

    if cell.paragraphs:
        content = "".join(
            _render_cell_paragraph(doc_ir, cell, paragraph, paragraph_annotations_by_id, run_annotations_by_id)
            for paragraph in cell.paragraphs
        )
    else:
        content = "&nbsp;"

    return f"<td {' '.join(attrs)}>{content}</td>"


def _render_table(
    doc_ir: DocIR,
    table: TableIR,
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    *,
    para_style: ParaStyleInfo | None = None,
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]] | None = None,
) -> str:
    paragraph_annotations_by_id = paragraph_annotations_by_id or {}
    if not table.cells:
        return f'<table data-node-id="{escape(table.node_id or "")}" style="{_table_css(table, para_style)}"></table>'

    positioned_cells = list(table.iter_cell_positions())
    covered: set[tuple[int, int]] = set()
    cells_by_pos = {(row_index, col_index): cell for row_index, col_index, cell in positioned_cells}
    max_row = table.row_count
    if table.table_style is not None:
        max_row = max(max_row, table.table_style.row_count)
    max_col = _table_logical_col_count(table)
    for row_index, col_index, cell in positioned_cells:
        rowspan = max(cell.cell_style.rowspan, 1) if cell.cell_style is not None else 1
        colspan = max(cell.cell_style.colspan, 1) if cell.cell_style is not None else 1
        max_row = max(max_row, row_index + rowspan - 1)
        max_col = max(max_col, col_index + colspan - 1)

    lines = [f'<table data-node-id="{escape(table.node_id or "")}" style="{_table_css(table, para_style)}">']
    lines.extend(_render_colgroup(table))
    for row in range(1, max_row + 1):
        lines.append("  <tr>")
        for col in range(1, max_col + 1):
            if (row, col) in covered:
                continue

            cell = cells_by_pos.get((row, col))
            if cell is None:
                use_native_defaults = _uses_native_cell_alignment_defaults(doc_ir)
                cell_css = _cell_css(
                    None,
                    default_horizontal_align="left" if use_native_defaults else None,
                    default_vertical_align="center" if use_native_defaults else None,
                )
                lines.append(f'    <td style="{cell_css}">&nbsp;</td>')
                continue

            rowspan = max(cell.cell_style.rowspan, 1) if cell.cell_style is not None else 1
            colspan = max(cell.cell_style.colspan, 1) if cell.cell_style is not None else 1

            for covered_row in range(row, row + rowspan):
                for covered_col in range(col, col + colspan):
                    if covered_row == row and covered_col == col:
                        continue
                    covered.add((covered_row, covered_col))

            lines.append(
                "    "
                + _render_cell(
                    doc_ir,
                    cell,
                    paragraph_annotations_by_id,
                    run_annotations_by_id,
                )
            )
        lines.append("  </tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _page_style(page: PageInfo) -> str:
    parts = [
        "box-sizing:border-box",
        "background:#fff",
        "border:1px solid #222",
        "box-shadow:0 1px 3px rgba(0,0,0,0.08)",
        "margin:0 auto 24px auto",
    ]
    width_pt = _non_negative_pt(page.width_pt)
    height_pt = _non_negative_pt(page.height_pt)
    if width_pt is not None:
        parts.append(f"width:{width_pt:.1f}pt")
    else:
        parts.append("max-width:900px")
    if height_pt is not None:
        parts.append(f"min-height:{height_pt:.1f}pt")
    return ";".join(parts)


def _page_content_style(page: PageInfo) -> str:
    margin_top = _non_negative_pt(page.margin_top_pt) if page.margin_top_pt is not None else 48.0
    margin_right = _non_negative_pt(page.margin_right_pt) if page.margin_right_pt is not None else 42.0
    margin_bottom = _non_negative_pt(page.margin_bottom_pt) if page.margin_bottom_pt is not None else 48.0
    margin_left = _non_negative_pt(page.margin_left_pt) if page.margin_left_pt is not None else 42.0
    return (
        "box-sizing:border-box;"
        f"padding:{margin_top:.1f}pt {margin_right:.1f}pt {margin_bottom:.1f}pt {margin_left:.1f}pt"
    )


def _render_paragraph(
    doc_ir: DocIR,
    paragraph: ParagraphIR,
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
) -> str:
    return _render_paragraph_like(
        doc_ir,
        paragraph,
        paragraph_annotations_by_id.get(paragraph.node_id, []),
        paragraph_annotations_by_id,
        run_annotations_by_id,
    )


def _render_column_group(
    doc_ir: DocIR,
    paragraphs: list[ParagraphIR],
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
) -> str:
    if not paragraphs or _paragraph_column_style(paragraphs[0]) is None:
        return ""

    column_style = _paragraph_column_style(paragraphs[0])
    if column_style is None:
        return ""
    content_html = "\n\n".join(
        _render_paragraph(doc_ir, paragraph, paragraph_annotations_by_id, run_annotations_by_id)
        for paragraph in paragraphs
    )
    attrs = [
        'class="document-column-group"',
        f'data-column-count="{max(column_style.count or 1, 1)}"',
        f'style="{_column_group_css(column_style)}"',
    ]
    return f"<div {' '.join(attrs)}>{content_html or '&nbsp;'}</div>"


def _render_paragraph_sequence(
    doc_ir: DocIR,
    paragraphs: list[ParagraphIR],
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
) -> str:
    parts: list[str] = []
    column_group: list[ParagraphIR] = []
    current_column_key: tuple[object, ...] | None = None

    def flush_column_group() -> None:
        nonlocal column_group, current_column_key
        if column_group:
            parts.append(
                _render_column_group(
                    doc_ir,
                    column_group,
                    paragraph_annotations_by_id,
                    run_annotations_by_id,
                )
            )
            column_group = []
        current_column_key = None

    for paragraph in paragraphs:
        column_key = _column_style_key(paragraph.para_style)
        if column_key is None:
            flush_column_group()
            parts.append(_render_paragraph(doc_ir, paragraph, paragraph_annotations_by_id, run_annotations_by_id))
            continue

        if current_column_key is not None and column_key != current_column_key:
            flush_column_group()

        current_column_key = column_key
        column_group.append(paragraph)

    flush_column_group()
    return "\n\n".join(parts)


def _render_paged_body(
    doc_ir: DocIR,
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]],
) -> str:
    paragraphs_by_page: dict[int, list[ParagraphIR]] = defaultdict(list)
    unpaged: list[ParagraphIR] = []

    for paragraph in doc_ir.paragraphs:
        if paragraph.page_number is None:
            unpaged.append(paragraph)
            continue
        paragraphs_by_page[paragraph.page_number].append(paragraph)

    parts: list[str] = []
    for page in doc_ir.pages:
        page_paragraphs = paragraphs_by_page.get(page.page_number, [])
        content_html = _render_paragraph_sequence(
            doc_ir,
            page_paragraphs,
            paragraph_annotations_by_id,
            run_annotations_by_id,
        )
        parts.append(
            f'<section class="document-page" data-page-number="{page.page_number}" style="{_page_style(page)}">'
            f'<div class="document-page__content" style="{_page_content_style(page)}">{content_html or "&nbsp;"}</div>'
            "</section>"
        )

    if unpaged:
        parts.append(
            '<section class="document-unpaged">'
            + _render_paragraph_sequence(
                doc_ir,
                unpaged,
                paragraph_annotations_by_id,
                run_annotations_by_id,
            )
            + "</section>"
        )

    return "\n".join(parts)


def _render_annotated_html(
    doc: DocIR,
    annotations: list[_Annotation],
    *,
    title: str | None = None,
) -> str:
    resolved = _resolve_annotations(doc, annotations)
    paragraph_annotations_by_id: dict[str, list[_ResolvedAnnotation]] = defaultdict(list)
    run_annotations_by_id: dict[str, list[_ResolvedAnnotation]] = defaultdict(list)
    for item in resolved:
        if item.target_kind == "paragraph":
            paragraph_annotations_by_id[item.target_id].append(item)
        else:
            run_annotations_by_id[item.target_id].append(item)

    body = (
        _render_paged_body(doc, paragraph_annotations_by_id, run_annotations_by_id)
        if doc.pages
        else _render_paragraph_sequence(
            doc,
            doc.paragraphs,
            paragraph_annotations_by_id,
            run_annotations_by_id,
        )
    )
    resolved_title = title or doc.doc_id or "Document Review"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(resolved_title)}</title>
<style>
  body {{
    max-width: 1100px;
    margin: 2em auto;
    padding: 0 1rem;
    line-height: 1.6;
    color: #1a1a1a;
    font-family: serif;
    background:#f5f5f2;
  }}
  p {{
    margin: 0 0 0.45em 0;
  }}
  table {{
    border-collapse: collapse;
    margin: 8px 0 12px 0;
  }}
  td p {{
    line-height: 1.0;
  }}
  img {{
    max-width: 100%;
    height: auto;
  }}
  mark {{
    cursor: help;
  }}
  .document-unpaged {{
    max-width: 900px;
    margin: 0 auto;
  }}
  .document-column-group {{
    margin: 0 0 0.45em 0;
  }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


__all__: list[str] = []
