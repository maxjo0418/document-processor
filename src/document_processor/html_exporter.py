"""Render structural document IR as styled HTML."""

from __future__ import annotations

import base64
from html import escape
import re

from .models import DocIR, ImageIR, PageInfo, ParagraphContentNode, ParagraphIR, RunIR, TableCellIR, TableIR, _node_debug_path
from .style_types import CellStyleInfo, ColumnLayoutInfo, ParaStyleInfo, RunStyleInfo

_NATIVE_CELL_ALIGNMENT_DOC_TYPES = {"docx", "hwpx", "hwp"}


def _non_negative_pt(value: float | None) -> float | None:
    if value is None:
        return None
    return max(value, 0.0)


def _pt_label(value: float | None) -> str:
    return "auto" if value is None else f"{value:.1f}pt"


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


def _column_grid_css(layout: ColumnLayoutInfo) -> str:
    parts = [
        "display:grid",
        "grid-template-columns:minmax(0,1fr) minmax(0,1fr)",
        "align-items:start",
        "break-inside:auto",
    ]
    gap_pt = _non_negative_pt(layout.gap_pt)
    if gap_pt is not None:
        parts.append(f"column-gap:{gap_pt:.1f}pt")
        parts.append(f"gap:0 {gap_pt:.1f}pt")
    return ";".join(parts)


def _indexed_columns(paragraphs: list[ParagraphIR]) -> tuple[list[ParagraphIR], list[ParagraphIR]] | None:
    left_paragraphs: list[ParagraphIR] = []
    right_paragraphs: list[ParagraphIR] = []
    for paragraph in paragraphs:
        column_layout = _paragraph_column_style(paragraph)
        column_index = column_layout.column_index if column_layout is not None else None
        if column_index == 0:
            left_paragraphs.append(paragraph)
        elif column_index == 1:
            right_paragraphs.append(paragraph)
        else:
            return None

    if not left_paragraphs and not right_paragraphs:
        return None
    return left_paragraphs, right_paragraphs


def _html_attrs(
    *,
    style: str | None = None,
    extra_attrs: list[str] | None = None,
) -> str:
    attrs: list[str] = []
    if style:
        attrs.append(f'style="{style}"')
    if extra_attrs:
        attrs.extend(extra_attrs)
    return " ".join(attrs)


def _run_css(style: RunStyleInfo) -> str:
    parts: list[str] = []
    if style.font_family:
        parts.append(f"font-family:{style.font_family}")
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
    html = re.sub(r"  +", lambda m: "&nbsp;" * len(m.group(0)), html)
    html = html.replace("\n", "<br>")
    return html


def _wrap_run(run: RunIR) -> str:
    if run.run_style is not None and run.run_style.hidden:
        return ""
    html = escape(run.text)
    if not html:
        return ""
    html = _escape_whitespace(html)
    return _style_wrap(html, run.run_style)


def _render_image(doc_ir: DocIR, image: ImageIR, *, block: bool = False) -> str:
    asset = doc_ir.assets.get(image.image_id)
    if asset is None:
        return ""

    style_parts = ["max-width:100%"]
    if block:
        style_parts.append("display:block")
    else:
        style_parts.append("vertical-align:middle")

    display_width_pt = _non_negative_pt(image.display_width_pt)
    display_height_pt = _non_negative_pt(image.display_height_pt)
    if display_width_pt is not None:
        style_parts.append(f"width:{display_width_pt:.1f}pt")
    if display_height_pt is not None:
        style_parts.append(f"height:{display_height_pt:.1f}pt")
    elif image.display_width_pt is None:
        style_parts.append("height:auto")

    attrs = _html_attrs(
        style=";".join(style_parts),
        extra_attrs=[
            f'src="{escape(asset.as_data_url(), quote=True)}"',
            f'alt="{escape(image.alt_text or asset.filename or "")}"',
        ],
    )
    return f"<img {attrs} />"


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


def _list_marker_html(style: ParaStyleInfo | None) -> str:
    list_info = style.list_info if style is not None else None
    if list_info is None or not list_info.marker:
        return ""
    level = max(list_info.level, 0)
    min_width = max(12.0, 14.0 + level * 8.0)
    marker = escape(list_info.marker)
    return (
        f'<span class="document-list-marker" '
        f'style="display:inline-block;min-width:{min_width:.1f}pt;margin-right:4.0pt;white-space:nowrap">'
        f"{marker}</span>"
    )


def _flush_paragraph(
    run_fragments: list[str],
    para_style: ParaStyleInfo | None,
    *,
    node_id: str | None = None,
    debug_path: str | None = None,
    debug_layout: bool = False,
) -> str:
    content = "".join(run_fragments)
    if not content.strip():
        content = "&nbsp;"

    list_marker = _list_marker_html(para_style)
    if list_marker:
        content = f"{list_marker}{content}"

    tag = para_style.render_tag if para_style is not None and para_style.render_tag else "p"
    attrs = [f'style="{_paragraph_css(para_style)}"']
    if debug_layout and node_id:
        attrs.append(f'data-node-id="{escape(node_id, quote=True)}"')
        attrs.append(f'data-debug-label="{escape(_paragraph_debug_label(debug_path or node_id, para_style), quote=True)}"')
    return f"<{tag} {' '.join(attrs)}>{content}</{tag}>"


def _paragraph_debug_label(debug_path: str, style: ParaStyleInfo | None) -> str:
    left_indent, right_indent, first_line_indent = _paragraph_indent_values(style)
    return (
        f"p {debug_path}: left {_pt_label(left_indent)}, "
        f"first {_pt_label(first_line_indent)}, right {_pt_label(right_indent)}"
    )


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


def _css_border(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"(?<=\s)single(?=\s)", "solid", value.strip(), flags=re.IGNORECASE)


def _uses_native_cell_alignment_defaults(doc_ir: DocIR) -> bool:
    return (doc_ir.source_doc_type or "").lower() in _NATIVE_CELL_ALIGNMENT_DOC_TYPES


def _paragraph_style_without_align(style: ParaStyleInfo | None) -> ParaStyleInfo | None:
    if style is None or style.align is None:
        return style
    return style.model_copy(update={"align": None})


def _cell_controls_paragraph_alignment(_doc_ir: DocIR, cell: TableCellIR) -> bool:
    return cell.cell_style is not None and cell.cell_style.horizontal_align is not None


def _cell_css(
    style: CellStyleInfo | None,
    *,
    render_table_grid: bool = False,
    default_horizontal_align: str | None = None,
    default_vertical_align: str | None = None,
) -> str:
    parts: list[str] = ["box-sizing:border-box"]
    fallback_border = "1px solid #4a4f57" if render_table_grid else "none"

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

        parts.append(f"border-top:{_css_border(style.border_top) or fallback_border}")
        parts.append(f"border-bottom:{_css_border(style.border_bottom) or fallback_border}")
        parts.append(f"border-left:{_css_border(style.border_left) or fallback_border}")
        parts.append(f"border-right:{_css_border(style.border_right) or fallback_border}")

        diagonal_background = _cell_diagonal_background(style)
        if diagonal_background:
            parts.append(f"background-image:url({diagonal_background})")
            parts.append("background-repeat:no-repeat")
            parts.append("background-size:100% 100%")
    else:
        parts.extend(
            [
                f"border-top:{fallback_border}",
                f"border-bottom:{fallback_border}",
                f"border-left:{fallback_border}",
                f"border-right:{fallback_border}",
            ]
        )

    parts.append(_cell_padding_css(style))
    return ";".join(parts)


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

    svg_parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" preserveAspectRatio="none">'
    ]
    for direction, stroke_width, style_name, color in diagonals:
        dasharray = _svg_dasharray(style_name, stroke_width)
        if direction == "tl_br":
            coords = (0, 0, 100, 100)
        else:
            coords = (100, 0, 0, 100)

        if style_name == "double":
            offset = min(max(stroke_width * 1.2, 1.5), 5.0)
            for delta in (-offset, offset):
                if direction == "tl_br":
                    x1, y1, x2, y2 = 0, max(0.0, delta), 100 - max(0.0, delta), 100
                else:
                    x1, y1, x2, y2 = 100, max(0.0, delta), max(0.0, delta), 100
                svg_parts.append(
                    f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{max(stroke_width / 2, 1)}" />'
                )
            continue

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


def _render_paragraph_like(
    doc_ir: DocIR,
    paragraph: ParagraphIR,
    content: list[ParagraphContentNode],
    para_style: ParaStyleInfo | None,
    *,
    debug_layout: bool = False,
) -> str:
    parts: list[str] = []
    inline_fragments: list[str] = []

    for node in content:
        if isinstance(node, RunIR):
            inline_fragments.append(_wrap_run(node))
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
                        inline_fragments,
                        para_style,
                        node_id=paragraph.node_id,
                        debug_path=_node_debug_path(paragraph),
                        debug_layout=debug_layout,
                    )
                )
                inline_fragments = []
            parts.append(_render_table(doc_ir, node, para_style, debug_layout=debug_layout))

    if inline_fragments:
        parts.append(
            _flush_paragraph(
                inline_fragments,
                para_style,
                node_id=paragraph.node_id,
                debug_path=_node_debug_path(paragraph),
                debug_layout=debug_layout,
            )
        )
    elif not parts:
        parts.append(
            _flush_paragraph(
                [],
                para_style,
                node_id=paragraph.node_id,
                debug_path=_node_debug_path(paragraph),
                debug_layout=debug_layout,
            )
        )

    return "\n".join(parts)


def _render_cell_paragraph(
    doc_ir: DocIR,
    cell: TableCellIR,
    paragraph: ParagraphIR,
    *,
    debug_layout: bool = False,
) -> str:
    para_style = paragraph.para_style
    if _cell_controls_paragraph_alignment(doc_ir, cell):
        para_style = _paragraph_style_without_align(para_style)
    return _render_paragraph_like(
        doc_ir,
        paragraph,
        paragraph.content,
        para_style,
        debug_layout=debug_layout,
    )


def _cell_debug_label(cell: TableCellIR) -> str:
    style = cell.cell_style
    width_pt = _non_negative_pt(style.width_pt) if style is not None else None
    height_pt = _non_negative_pt(style.height_pt) if style is not None else None
    return f"cell {_node_debug_path(cell)}: {_pt_label(width_pt)} x {_pt_label(height_pt)}"


def _render_cell(
    doc_ir: DocIR,
    cell: TableCellIR,
    *,
    debug_layout: bool = False,
    render_table_grid: bool = False,
) -> str:
    use_native_defaults = _uses_native_cell_alignment_defaults(doc_ir)
    cell_css = _cell_css(
        cell.cell_style,
        render_table_grid=render_table_grid,
        default_horizontal_align="left" if use_native_defaults else None,
        default_vertical_align="center" if use_native_defaults else None,
    )
    attrs = [f'style="{cell_css}"']
    if debug_layout:
        attrs.append(f'data-node-id="{escape(cell.node_id or "", quote=True)}"')
        attrs.append(f'data-debug-label="{escape(_cell_debug_label(cell), quote=True)}"')
    if cell.cell_style is not None:
        if cell.cell_style.colspan > 1:
            attrs.append(f'colspan="{cell.cell_style.colspan}"')
        if cell.cell_style.rowspan > 1:
            attrs.append(f'rowspan="{cell.cell_style.rowspan}"')

    if cell.paragraphs:
        content = "".join(
            _render_cell_paragraph(doc_ir, cell, paragraph, debug_layout=debug_layout)
            for paragraph in cell.paragraphs
        )
    else:
        content = "&nbsp;"

    return f"<td {' '.join(attrs)}>{content}</td>"


def _table_debug_label(table: TableIR) -> str:
    style = table.table_style
    width_pt = _non_negative_pt(style.width_pt) if style is not None else None
    height_pt = _non_negative_pt(style.height_pt) if style is not None else None
    return f"table {_node_debug_path(table)}: {_pt_label(width_pt)} x {_pt_label(height_pt)}"


def _render_table(
    doc_ir: DocIR,
    table: TableIR,
    para_style: ParaStyleInfo | None = None,
    *,
    debug_layout: bool = False,
) -> str:
    render_table_grid = bool(table.table_style and table.table_style.render_grid)
    attrs = [f'style="{_table_css(table, para_style)}"']
    if debug_layout:
        attrs.append(f'data-node-id="{escape(table.node_id or "", quote=True)}"')
        attrs.append(f'data-debug-label="{escape(_table_debug_label(table), quote=True)}"')
    if not table.cells:
        return f"<table {' '.join(attrs)}></table>"

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

    lines = [f"<table {' '.join(attrs)}>"]
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
                    render_table_grid=render_table_grid,
                    default_horizontal_align="left" if use_native_defaults else None,
                    default_vertical_align="center" if use_native_defaults else None,
                )
                lines.append(
                    f'    <td style="{cell_css}">&nbsp;</td>'
                )
                continue

            if cell.cell_style is not None:
                rowspan = max(cell.cell_style.rowspan, 1)
                colspan = max(cell.cell_style.colspan, 1)
            else:
                rowspan = 1
                colspan = 1

            for covered_row in range(row, row + rowspan):
                for covered_col in range(col, col + colspan):
                    if covered_row == row and covered_col == col:
                        continue
                    covered.add((covered_row, covered_col))

            lines.append(
                f"    {_render_cell(doc_ir, cell, debug_layout=debug_layout, render_table_grid=render_table_grid)}"
            )
        lines.append("  </tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _render_paragraph(doc_ir: DocIR, paragraph: ParagraphIR, *, debug_layout: bool = False) -> str:
    return _render_paragraph_like(
        doc_ir,
        paragraph,
        paragraph.content,
        paragraph.para_style,
        debug_layout=debug_layout,
    )


def _column_group_debug_label(style: ColumnLayoutInfo, paragraphs: list[ParagraphIR]) -> str:
    debug_paths = ", ".join(_node_debug_path(paragraph) for paragraph in paragraphs)
    return f"columns x{style.count or 1}: gap {_pt_label(_non_negative_pt(style.gap_pt))}; {debug_paths}"


def _render_column_group(
    doc_ir: DocIR,
    paragraphs: list[ParagraphIR],
    *,
    debug_layout: bool = False,
) -> str:
    column_style = _paragraph_column_style(paragraphs[0])
    if column_style is None:
        return ""

    indexed_columns = _indexed_columns(paragraphs) if column_style.count == 2 else None
    if indexed_columns is not None:
        left_paragraphs, right_paragraphs = indexed_columns
        attrs = [
            'class="document-column-group document-column-group--indexed"',
            'data-column-mode="indexed"',
            f'data-column-count="{max(column_style.count or 1, 1)}"',
            f'style="{_column_grid_css(column_style)}"',
        ]
        if debug_layout:
            attrs.append(f'data-debug-label="{escape(_column_group_debug_label(column_style, paragraphs), quote=True)}"')

        left_html = "\n\n".join(
            _render_paragraph(doc_ir, paragraph, debug_layout=debug_layout)
            for paragraph in left_paragraphs
        )
        right_html = "\n\n".join(
            _render_paragraph(doc_ir, paragraph, debug_layout=debug_layout)
            for paragraph in right_paragraphs
        )
        return (
            f"<div {' '.join(attrs)}>"
            f'<div class="document-column" data-column-index="1">{left_html or "&nbsp;"}</div>'
            f'<div class="document-column" data-column-index="2">{right_html or "&nbsp;"}</div>'
            "</div>"
        )

    attrs = [
        'class="document-column-group"',
        f'data-column-count="{max(column_style.count or 1, 1)}"',
        f'style="{_column_group_css(column_style)}"',
    ]
    if debug_layout:
        attrs.append(f'data-debug-label="{escape(_column_group_debug_label(column_style, paragraphs), quote=True)}"')

    content_html = "\n\n".join(
        _render_paragraph(doc_ir, paragraph, debug_layout=debug_layout)
        for paragraph in paragraphs
    )
    return f"<div {' '.join(attrs)}>{content_html or '&nbsp;'}</div>"


def _render_paragraph_sequence(
    doc_ir: DocIR,
    paragraphs: list[ParagraphIR],
    *,
    debug_layout: bool = False,
) -> str:
    parts: list[str] = []
    column_group: list[ParagraphIR] = []
    current_column_key: tuple[object, ...] | None = None

    def flush_column_group() -> None:
        nonlocal column_group, current_column_key
        if column_group:
            parts.append(_render_column_group(doc_ir, column_group, debug_layout=debug_layout))
            column_group = []
        current_column_key = None

    for paragraph in paragraphs:
        column_key = _column_style_key(paragraph.para_style)
        if column_key is None:
            flush_column_group()
            parts.append(_render_paragraph(doc_ir, paragraph, debug_layout=debug_layout))
            continue

        if current_column_key is not None and column_key != current_column_key:
            flush_column_group()

        current_column_key = column_key
        column_group.append(paragraph)

    flush_column_group()
    return "\n\n".join(parts)


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


def _page_debug_label(page: PageInfo) -> str:
    return (
        f"page {page.page_number}: {_pt_label(_non_negative_pt(page.width_pt))} x "
        f"{_pt_label(_non_negative_pt(page.height_pt))}; margins "
        f"{_pt_label(_non_negative_pt(page.margin_top_pt))}/"
        f"{_pt_label(_non_negative_pt(page.margin_right_pt))}/"
        f"{_pt_label(_non_negative_pt(page.margin_bottom_pt))}/"
        f"{_pt_label(_non_negative_pt(page.margin_left_pt))}"
    )


def _render_paged_body(doc_ir: DocIR, *, debug_layout: bool = False) -> str:
    paragraphs_by_page: dict[int, list[ParagraphIR]] = {}
    unpaged: list[ParagraphIR] = []

    for paragraph in doc_ir.paragraphs:
        if paragraph.page_number is None:
            unpaged.append(paragraph)
            continue
        paragraphs_by_page.setdefault(paragraph.page_number, []).append(paragraph)

    parts: list[str] = []
    for page in doc_ir.pages:
        page_paragraphs = paragraphs_by_page.get(page.page_number, [])
        content_html = _render_paragraph_sequence(
            doc_ir,
            page_paragraphs,
            debug_layout=debug_layout,
        )
        page_attrs = [
            'class="document-page"',
            f'data-page-number="{page.page_number}"',
            f'style="{_page_style(page)}"',
        ]
        content_attrs = [
            'class="document-page__content"',
            f'style="{_page_content_style(page)}"',
        ]
        if debug_layout:
            page_attrs.append(f'data-debug-label="{escape(_page_debug_label(page), quote=True)}"')
            content_attrs.append('data-debug-label="page content area"')
        parts.append(
            f"<section {' '.join(page_attrs)}>"
            f"<div {' '.join(content_attrs)}>{content_html or '&nbsp;'}</div>"
            "</section>"
        )

    if unpaged:
        parts.append(
            '<section class="document-unpaged">'
            + _render_paragraph_sequence(
                doc_ir,
                unpaged,
                debug_layout=debug_layout,
            )
            + "</section>"
        )

    return "\n".join(parts)


def _debug_layout_css() -> str:
    return """
  body.document-debug-layout [data-debug-label] {
    outline: 1px dashed rgba(220, 20, 60, 0.75);
    outline-offset: -1px;
    position: relative;
  }
  body.document-debug-layout [data-debug-label]::before {
    content: attr(data-debug-label);
    display: block;
    position: absolute;
    z-index: 20;
    top: 0;
    left: 0;
    transform: translateY(-100%);
    min-width: max-content;
    max-width: min(520px, 90vw);
    box-sizing: border-box;
    margin: 0;
    padding: 1px 4px;
    background: rgba(220, 20, 60, 0.88);
    color: #fff;
    font: 10px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    text-indent: 0;
    white-space: normal;
  }
  body.document-debug-layout table[data-debug-label] {
    outline-color: rgba(0, 96, 180, 0.8);
  }
  body.document-debug-layout td[data-debug-label] {
    outline-color: rgba(0, 128, 64, 0.8);
  }
"""


def _debug_layout_script() -> str:
    return """<script>
(() => {
  const PT_PER_PX = 72 / 96;
  for (const el of document.querySelectorAll("[data-debug-label]")) {
    const rect = el.getBoundingClientRect();
    const rendered = `${(rect.width * PT_PER_PX).toFixed(1)}pt x ${(rect.height * PT_PER_PX).toFixed(1)}pt`;
    el.dataset.debugLabel = `${el.dataset.debugLabel} | rendered ${rendered}`;
  }
})();
</script>"""


def _render_html_document_shell(*, title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
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


def render_html_document(doc_ir: DocIR, *, title: str | None = None, debug_layout: bool = False) -> str:
    """Render a document IR tree as a complete HTML document."""
    resolved_title = title or doc_ir.doc_id or "Document"
    body = (
        _render_paged_body(doc_ir, debug_layout=debug_layout)
        if doc_ir.pages
        else _render_paragraph_sequence(
            doc_ir,
            doc_ir.paragraphs,
            debug_layout=debug_layout,
        )
    )
    body_class = ' class="document-debug-layout"' if debug_layout else ""
    debug_css = _debug_layout_css() if debug_layout else ""
    debug_script = "\n" + _debug_layout_script() if debug_layout else ""

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
  .document-unpaged {{
    max-width: 900px;
    margin: 0 auto;
  }}
  .document-column-group {{
    margin: 0 0 0.45em 0;
  }}
{debug_css}
</style>
</head>
<body{body_class}>
{body}
{debug_script}
</body>
</html>
"""


__all__ = ["render_html_document"]
