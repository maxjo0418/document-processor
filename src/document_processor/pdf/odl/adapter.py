"""ODL raw JSON to DocIR conversion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.document_ir_parser import _node_kwargs
from ...models import (
    DocIR,
    ImageAsset,
    ImageIR,
    PageInfo,
    ParagraphIR,
    RunIR,
    TableCellIR,
    TableIR,
    _node_debug_path,
)
from ...style_types import CellStyleInfo, ParaStyleInfo, RunStyleInfo, TableStyleInfo


def _pdf_node_kwargs(kind, structural_path: str, *, parent_debug_path: str | None = None, text: str | None = None) -> dict[str, object]:
    """PDF-sourced shorthand around `_node_kwargs(..., source_doc_type='pdf')`."""
    return _node_kwargs(
        kind,
        structural_path,
        source_doc_type="pdf",
        parent_debug_path=parent_debug_path,
        text=text,
    )
from ..meta import (
    PdfBoundingBox,
    coerce_bbox,
    coerce_float,
    coerce_int,
    extract_text_from_odl_children,
    extract_text_from_odl_node,
    node_value,
    normalize_align,
    pixels_to_points,
    sanitize_css_color,
)

_STRIP_CONNECTOR_CHARS = frozenset({"➡", "→", "➜", "➝", "←", "↑", "↓", "↔", "↕", ""})
_STRIP_ROW_TOLERANCE_PT = 18.0
_LINE_CENTER_TOLERANCE_RATIO = 0.55
_LINE_MIN_CENTER_TOLERANCE_PT = 2.0
_LINE_OVERLAP_RATIO = 0.45
_SPACE_WIDTH_FONT_RATIO = 0.5
_MAX_RECONSTRUCTED_SPACES = 80


@dataclass(frozen=True)
class _OdlNodeGeometry:
    page_number: int | None = None
    bounding_box: PdfBoundingBox | None = None


@dataclass(frozen=True)
class _OdlTableContinuationLink:
    paragraph_index: int
    raw_table_id: str
    previous_raw_table_id: str | None = None
    next_raw_table_id: str | None = None


# ---------------------------------------------------------------------------
# Style extraction helpers
# These functions map raw ODL node fields into format-agnostic DocIR style
# models. They are intentionally small and side-effect free so the structural
# conversion helpers below can stay readable.
# ---------------------------------------------------------------------------


def _para_style_from_node(node: dict[str, Any]) -> ParaStyleInfo | None:
    render_tag: str | None = None
    if node.get("type") == "heading":
        level = coerce_int(node.get("heading level"))
        render_tag = f"h{level}" if level is not None and 1 <= level <= 6 else "h2"

    style = ParaStyleInfo(
        align=normalize_align(
            node_value(node, "align", "alignment", "text align", "horizontal align")
        ),
        left_indent_pt=coerce_float(node_value(node, "left indent pt", "left indent")),
        right_indent_pt=coerce_float(node_value(node, "right indent pt", "right indent")),
        first_line_indent_pt=coerce_float(
            node_value(node, "first line indent pt", "first line indent")
        ),
        hanging_indent_pt=coerce_float(
            node_value(node, "hanging indent pt", "hanging indent")
        ),
        render_tag=render_tag,
    )
    return style if style.model_dump(exclude_defaults=True, exclude_none=True) else None


def _run_style_from_node(node: dict[str, Any]) -> RunStyleInfo | None:
    text_format = node_value(node, "text format")
    format_tokens = (
        {token for token in text_format.strip().lower().replace("-", " ").split() if token}
        if isinstance(text_format, str)
        else set()
    )
    font_weight = coerce_float(node_value(node, "font weight"))
    italic_angle = coerce_float(node_value(node, "italic angle"))
    bold = _coerce_bool(node_value(node, "bold"))
    if bold is None:
        bold = (font_weight is not None and font_weight >= 600.0) or ("bold" in format_tokens)
    italic = _coerce_bool(node_value(node, "italic"))
    if italic is None:
        italic = (
            (italic_angle is not None and abs(italic_angle) > 0.01)
            or ("italic" in format_tokens)
            or ("oblique" in format_tokens)
        )
    underline = _coerce_bool(node_value(node, "underline"))
    if underline is None:
        underline = "underline" in format_tokens
    strikethrough = _coerce_bool(node_value(node, "strikethrough"))
    if strikethrough is None:
        strikethrough = "strikethrough" in format_tokens or "strike" in format_tokens

    style = RunStyleInfo(
        font_family=node.get("font") if isinstance(node.get("font"), str) else None,
        bold=bold or False,
        italic=italic or False,
        underline=underline or False,
        strikethrough=strikethrough or False,
        superscript=_coerce_bool(node_value(node, "superscript")) or False,
        subscript=_coerce_bool(node_value(node, "subscript")) or False,
        size_pt=coerce_float(node.get("font size")),
        color=sanitize_css_color(node.get("text color")),
        highlight=sanitize_css_color(node_value(node, "highlight color", "background color")),
        hidden=bool(node.get("hidden text", False)),
    )
    return style if style.model_dump(exclude_defaults=True, exclude_none=True) else None


def _cell_style_from_node(node: dict[str, Any]) -> CellStyleInfo | None:
    width_pt, height_pt = _display_size_from_node(node)
    style = CellStyleInfo(
        background=sanitize_css_color(node.get("background color")),
        vertical_align=_normalize_vertical_align(node_value(node, "vertical align")),
        horizontal_align=normalize_align(node_value(node, "horizontal align", "text align")),
        width_pt=width_pt,
        height_pt=height_pt,
        border_top=_coarse_border_css_from_node(node, "has top border", "border top"),
        border_bottom=_coarse_border_css_from_node(node, "has bottom border", "border bottom"),
        border_left=_coarse_border_css_from_node(node, "has left border", "border left"),
        border_right=_coarse_border_css_from_node(node, "has right border", "border right"),
        rowspan=coerce_int(node.get("row span")) or 1,
        colspan=coerce_int(node.get("column span")) or 1,
    )
    return style if style.model_dump(exclude_defaults=True, exclude_none=True) else None


def _table_style_from_node(node: dict[str, Any]) -> TableStyleInfo | None:
    width_pt, height_pt = _display_size_from_node(node)
    style = TableStyleInfo(
        row_count=coerce_int(node.get("number of rows")) or 0,
        col_count=coerce_int(node.get("number of columns")) or 0,
        width_pt=width_pt,
        height_pt=height_pt,
        render_grid=True,
    )
    return style if style.model_dump(exclude_defaults=True, exclude_none=True) else None


def _display_size_from_node(node: dict[str, Any]) -> tuple[float | None, float | None]:
    width_pt = coerce_float(node_value(node, "display width pt", "width pt"))
    height_pt = coerce_float(node_value(node, "display height pt", "height pt"))
    if width_pt is not None or height_pt is not None:
        return width_pt, height_pt
    dpi = node_value(node, "dpi")
    return (
        pixels_to_points(node_value(node, "width px", "image width"), dpi),
        pixels_to_points(node_value(node, "height px", "image height"), dpi),
    )


def _page_number_from_node(node: dict[str, Any]) -> int | None:
    return coerce_int(node.get("page number"))


def _raw_table_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _node_geometry(node: dict[str, Any]) -> _OdlNodeGeometry | None:
    geometry = _OdlNodeGeometry(
        page_number=_page_number_from_node(node),
        bounding_box=coerce_bbox(node.get("bounding box")),
    )
    return geometry if geometry.page_number is not None or geometry.bounding_box is not None else None


def _compose_node_geometry(
    primary: _OdlNodeGeometry | None,
    fallback: _OdlNodeGeometry | None,
) -> _OdlNodeGeometry | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    geometry = _OdlNodeGeometry(
        page_number=primary.page_number if primary.page_number is not None else fallback.page_number,
        bounding_box=primary.bounding_box if primary.bounding_box is not None else fallback.bounding_box,
    )
    return geometry if geometry.page_number is not None or geometry.bounding_box is not None else None


def _border_css(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _coarse_border_css_from_node(
    node: dict[str, Any],
    bool_key: str,
    legacy_key: str,
) -> str | None:
    explicit = _border_css(node.get(legacy_key))
    if explicit:
        return explicit
    has_border = _coerce_bool(node.get(bool_key))
    if has_border is True:
        return "1px solid"
    if has_border is False:
        return None
    return None


# ---------------------------------------------------------------------------
# Text node conversion
# ODL semantic text nodes become ParagraphIR + RunIR here. This is the main
# place where we preserve run-level styling while still keeping a stable
# paragraph-level `.text` for downstream chunking/RAG paths.
# ---------------------------------------------------------------------------

def _paragraph_from_text_node(
    node: dict[str, Any],
    *,
    unit_id: str,
    paragraph_geometry: _OdlNodeGeometry | None = None,
    run_geometry: _OdlNodeGeometry | None = None,
    style_node: dict[str, Any] | None = None,
    default_page_number: int | None = None,
) -> ParagraphIR | None:
    text = extract_text_from_odl_node(node)
    if not text and node.get("type") not in {"caption", "header", "footer", "formula"}:
        return None
    if not text.strip() and node.get("type") not in {"caption", "header", "footer", "formula"}:
        return None
    style_source = style_node or node
    resolved_geometry = paragraph_geometry if paragraph_geometry is not None else _node_geometry(node)
    resolved_run_geometry = run_geometry if run_geometry is not None else resolved_geometry
    content = _runs_from_text_node(
        node,
        unit_id=unit_id,
        style_node=style_source,
        run_geometry=resolved_run_geometry,
    ) if text else []
    paragraph_text = _reconstructed_text_for_node(text, content) if content else text
    return ParagraphIR(
        **_pdf_node_kwargs("paragraph", unit_id),
        text=paragraph_text,
        page_number=_page_number_from_node(style_source) or _page_number_from_node(node) or (resolved_geometry.page_number if resolved_geometry is not None else None) or default_page_number,
        bbox=resolved_geometry.bounding_box if resolved_geometry is not None else None,
        para_style=_para_style_from_node(style_source),
        content=content,
    )


def _paragraphs_from_container_node(
    node: dict[str, Any],
    *,
    unit_prefix: str,
    assets: dict[str, ImageAsset],
) -> list[ParagraphIR]:
    """Flatten header/footer-like wrapper nodes into paragraph units.

    DocIR stays intentionally flat at the top level, so wrapper containers do
    not survive as dedicated nodes. Their children are converted into regular
    paragraphs/tables/images and appended in reading order.
    """
    paragraphs: list[ParagraphIR] = []
    node_type = node.get("type")
    container_geometry = _node_geometry(node)
    default_page_number = _page_number_from_node(node)

    for child_index, child in enumerate(node.get("kids", []), start=1):
        child_type = child.get("type")
        child_unit_id = f"{unit_prefix}.c{child_index}"
        if child_type == "table":
            child_geometry = _compose_node_geometry(_node_geometry(child), container_geometry)
            paragraphs.append(
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", child_unit_id),
                    text="",
                    page_number=_page_number_from_node(child) or default_page_number,
                    bbox=child_geometry.bounding_box if child_geometry is not None else None,
                    para_style=_para_style_from_node(child),
                    content=[
                        _table_node_to_ir(
                            child,
                            unit_id=f"{child_unit_id}.tbl1",
                            assets=assets,
                                        )
                    ],
                )
            )
            continue
        if child_type == "image":
            paragraph = _image_paragraph(child, unit_id=child_unit_id, assets=assets)
            if paragraph.page_number is None:
                paragraph.page_number = default_page_number
            paragraphs.append(paragraph)
            continue
        if child_type == "list":
            paragraphs.extend(
                _paragraphs_from_list_node(
                    child,
                    unit_prefix=child_unit_id,
                    assets=assets,
                        )
            )
            continue

        paragraph = _paragraph_from_text_node(
            child,
            unit_id=child_unit_id,
            paragraph_geometry=_compose_node_geometry(_node_geometry(child), container_geometry),
            run_geometry=_compose_node_geometry(_node_geometry(child), container_geometry),
            style_node=child,
            default_page_number=default_page_number,
        )
        if paragraph is not None:
            paragraphs.append(paragraph)

    if paragraphs:
        return paragraphs

    paragraph = _paragraph_from_text_node(
        node,
        unit_id=unit_prefix,
        paragraph_geometry=container_geometry,
        default_page_number=default_page_number,
    )
    return [paragraph] if paragraph is not None else []


def _merged_style_node(
    primary: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(fallback)
    merged.update({key: value for key, value in primary.items() if value is not None})
    return merged


def _text_spans_from_node(node: dict[str, Any]) -> list[dict[str, Any]]:
    spans = node.get("spans")
    if not isinstance(spans, list):
        return []
    return [span for span in spans if isinstance(span, dict) and isinstance(span.get("content"), str)]


def _reconstructed_text_for_node(raw_text: str, runs: list[RunIR]) -> str:
    reconstructed = "".join(run.text for run in runs)
    if not raw_text:
        return reconstructed
    if reconstructed == raw_text:
        return raw_text
    if "".join(raw_text.split()) == "".join(reconstructed.split()):
        return reconstructed
    return raw_text


def _runs_from_text_node(
    node: dict[str, Any],
    *,
    unit_id: str,
    style_node: dict[str, Any],
    run_geometry: _OdlNodeGeometry | None,
) -> list[RunIR]:
    """Convert ODL spans into RunIR and merge adjacent identical runs.

    ODL span output can be very fine-grained, including whitespace chunks.
    We preserve the information first, then merge only immediately adjacent
    runs whose effective style/meta signatures are identical.
    """
    text = extract_text_from_odl_node(node)
    spans = _text_spans_from_node(node)
    # Current ODL span output flattens per-line chunks but does not emit explicit
    # newline spans. When node content already contains line breaks, prefer the
    # node-level text so preview fidelity does not regress.
    if text and "\n" in text and not any("\n" in span.get("content", "") for span in spans):
        return [
            RunIR(
                **_pdf_node_kwargs("run", f"{unit_id}.r1"),
                text=text,
                bbox=run_geometry.bounding_box if run_geometry is not None else None,
                run_style=_run_style_from_node(style_node),
            )
        ]

    runs: list[RunIR] = []
    for index, span in enumerate(spans, start=1):
        span_text = span.get("content")
        if not isinstance(span_text, str):
            continue
        span_style_node = _merged_style_node(span, style_node)
        span_geometry = _compose_node_geometry(_node_geometry(span), run_geometry)
        span_bbox = span_geometry.bounding_box if span_geometry is not None else None
        run_style = _run_style_from_node(span_style_node)
        span_text = _expand_wide_space_span_text(span_text, bbox=span_bbox, style=run_style)
        runs.append(
            RunIR(
                **_pdf_node_kwargs("run", f"{unit_id}.r{index}"),
                text=span_text,
                bbox=span_bbox,
                run_style=run_style,
            )
        )
    if runs:
        runs = _reconstruct_visual_run_text(runs, raw_text=text)
        return _merge_adjacent_runs(runs)
    if not text:
        return []
    return [
        RunIR(
            **_pdf_node_kwargs("run", f"{unit_id}.r1"),
            text=text,
            bbox=run_geometry.bounding_box if run_geometry is not None else None,
            run_style=_run_style_from_node(style_node),
        )
    ]


# ---------------------------------------------------------------------------
# Run post-processing helpers
# ---------------------------------------------------------------------------

def _reconstruct_visual_run_text(runs: list[RunIR], *, raw_text: str) -> list[RunIR]:
    if not runs:
        return []
    raw_positions = _raw_text_positions(raw_text, runs)
    reconstructed = [runs[0]]
    for index, source_run in enumerate(runs[1:], start=1):
        run = source_run
        previous = reconstructed[-1]
        if not _runs_on_same_visual_line(previous, run):
            separator = _raw_separator_between_runs(
                raw_text,
                raw_positions[index - 1],
                raw_positions[index],
            )
            if separator and _needs_separator(previous.text, run.text, separator):
                run = run.model_copy(update={"text": f"{separator}{run.text}"})
            reconstructed.append(run)
            continue

        reconstructed.append(run)
    return reconstructed


def _raw_text_positions(raw_text: str, runs: list[RunIR]) -> list[tuple[int, int] | None]:
    positions: list[tuple[int, int] | None] = []
    cursor = 0
    for run in runs:
        if not run.text:
            positions.append((cursor, cursor))
            continue
        start = raw_text.find(run.text, cursor)
        if start < 0:
            positions.append(None)
            continue
        end = start + len(run.text)
        positions.append((start, end))
        cursor = end
    return positions


def _raw_separator_between_runs(
    raw_text: str,
    left_position: tuple[int, int] | None,
    right_position: tuple[int, int] | None,
) -> str:
    if left_position is None or right_position is None:
        return ""
    left_end = left_position[1]
    right_start = right_position[0]
    if right_start < left_end:
        return ""
    return raw_text[left_end:right_start]


def _needs_separator(left_text: str, right_text: str, separator: str) -> bool:
    if separator.isspace():
        return not left_text.endswith((" ", "\t", "\n")) and not right_text.startswith((" ", "\t", "\n"))
    return not right_text.startswith(separator)


def _expand_wide_space_span_text(
    text: str,
    *,
    bbox: PdfBoundingBox | None,
    style: RunStyleInfo | None,
) -> str:
    if not text or not text.isspace() or "\n" in text or "\r" in text:
        return text
    if bbox is None:
        return text

    width_pt = max(bbox.right_pt - bbox.left_pt, 0.0)
    if width_pt <= 0:
        return text

    space_width_pt = _estimated_space_width_from_style_or_bbox(style, bbox)
    space_count = min(max(round(width_pt / space_width_pt), len(text)), _MAX_RECONSTRUCTED_SPACES)
    if space_count <= len(text):
        return text
    return " " * space_count


def _merge_adjacent_runs(runs: list[RunIR]) -> list[RunIR]:
    if not runs:
        return []
    merged_runs: list[RunIR] = [runs[0].model_copy(deep=True)]
    for run in runs[1:]:
        current = merged_runs[-1]
        if _can_merge_runs(current, run):
            current.text += run.text
            current.bbox = _merge_bounding_boxes(current.bbox, run.bbox)
            continue
        merged_runs.append(run.model_copy(deep=True))
    return merged_runs


def _can_merge_runs(left: RunIR, right: RunIR) -> bool:
    if _run_style_signature(left.run_style) != _run_style_signature(right.run_style):
        return False
    if left.text.endswith("\n") or right.text.startswith("\n"):
        return False
    if _is_expanded_space_fill_run(left) != _is_expanded_space_fill_run(right):
        return False
    return _runs_on_same_visual_line(left, right)


def _run_style_signature(style: RunStyleInfo | None) -> dict[str, Any] | None:
    if style is None:
        return None
    return style.model_dump(exclude_defaults=True, exclude_none=True)


def _is_expanded_space_fill_run(run: RunIR) -> bool:
    return len(run.text) > 1 and run.text.isspace() and "\n" not in run.text and "\r" not in run.text


def _runs_on_same_visual_line(left: RunIR, right: RunIR) -> bool:
    left_bbox = left.bbox
    right_bbox = right.bbox
    if left_bbox is None or right_bbox is None:
        return True

    overlap = min(left_bbox.top_pt, right_bbox.top_pt) - max(left_bbox.bottom_pt, right_bbox.bottom_pt)
    left_height = _bbox_height(left_bbox)
    right_height = _bbox_height(right_bbox)
    shorter_height = min(left_height, right_height)
    if shorter_height > 0 and overlap / shorter_height >= _LINE_OVERLAP_RATIO:
        return True

    left_center = (left_bbox.bottom_pt + left_bbox.top_pt) / 2.0
    right_center = (right_bbox.bottom_pt + right_bbox.top_pt) / 2.0
    tolerance = max(shorter_height * _LINE_CENTER_TOLERANCE_RATIO, _LINE_MIN_CENTER_TOLERANCE_PT)
    return abs(left_center - right_center) <= tolerance


def _bbox_height(bbox: PdfBoundingBox) -> float:
    return max(bbox.top_pt - bbox.bottom_pt, 0.0)


def _estimated_space_width_from_style_or_bbox(
    style: RunStyleInfo | None,
    bbox: PdfBoundingBox,
) -> float:
    if style is not None and style.size_pt is not None and style.size_pt > 0:
        return max(style.size_pt * _SPACE_WIDTH_FONT_RATIO, 1.0)
    height = _bbox_height(bbox)
    if height > 0:
        return max(height * _SPACE_WIDTH_FONT_RATIO, 1.0)
    return 4.0


def _merge_bounding_boxes(
    left: PdfBoundingBox | None,
    right: PdfBoundingBox | None,
) -> PdfBoundingBox | None:
    if left is None:
        return right
    if right is None:
        return left
    return PdfBoundingBox(
        left_pt=min(left.left_pt, right.left_pt),
        bottom_pt=min(left.bottom_pt, right.bottom_pt),
        right_pt=max(left.right_pt, right.right_pt),
        top_pt=max(left.top_pt, right.top_pt),
    )


# ---------------------------------------------------------------------------
# Non-text block conversion
# Images, tables, table cells, and list containers become normal DocIR content
# nodes here. The goal is still a flat top-level paragraph stream, with tables
# nested only where DocIR already supports them.
# ---------------------------------------------------------------------------

def _append_image_asset(
    assets: dict[str, ImageAsset],
    *,
    node: dict[str, Any],
    unit_id: str,
) -> None:
    data_uri = node.get("data")
    if not isinstance(data_uri, str):
        return
    mime_type = "application/octet-stream"
    data_base64: str | None = None
    if data_uri.startswith("data:") and ";base64," in data_uri:
        mime_type = data_uri[5:].split(";base64,", 1)[0] or mime_type
        data_base64 = data_uri.split(";base64,", 1)[1]
    if not data_base64:
        return

    image_id = f"odl-img-{unit_id}"
    assets[image_id] = ImageAsset(
        mime_type=mime_type,
        filename=None,
        data_base64=data_base64,
        intrinsic_width_px=coerce_int(node_value(node, "width px", "image width")),
        intrinsic_height_px=coerce_int(node_value(node, "height px", "image height")),
    )


def _image_paragraph(
    node: dict[str, Any],
    *,
    unit_id: str,
    assets: dict[str, ImageAsset],
) -> ParagraphIR:
    _append_image_asset(assets, node=node, unit_id=unit_id)
    display_width_pt, display_height_pt = _display_size_from_node(node)
    image_geometry = _node_geometry(node)
    return ParagraphIR(
        **_pdf_node_kwargs("paragraph", unit_id),
        text="",
        page_number=_page_number_from_node(node),
        bbox=image_geometry.bounding_box if image_geometry is not None else None,
        para_style=_para_style_from_node(node),
        content=[
            ImageIR(
                **_pdf_node_kwargs("image", f"{unit_id}.img1"),
                image_id=f"odl-img-{unit_id}",
                alt_text=node_value(node, "alt text"),
                title=node_value(node, "title", "name"),
                bbox=image_geometry.bounding_box if image_geometry is not None else None,
                display_width_pt=display_width_pt,
                display_height_pt=display_height_pt,
            )
        ],
    )


def _cell_paragraphs(
    children: list[dict[str, Any]],
    *,
    cell_unit_id: str,
    default_page_number: int | None,
    assets: dict[str, ImageAsset],
) -> list[ParagraphIR]:
    """Build the paragraph stream for a table cell.

    Cells can still contain nested tables/images, but from the caller's point
    of view they always become a list of ParagraphIR entries.
    """
    paragraphs: list[ParagraphIR] = []
    child_index = 0
    for child in children:
        child_type = child.get("type")
        unit_id = f"{cell_unit_id}.p{child_index + 1}"
        if child_type == "table":
            child_index += 1
            child_geometry = _node_geometry(child)
            paragraphs.append(
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", unit_id),
                    text="",
                    page_number=_page_number_from_node(child) or default_page_number,
                    bbox=child_geometry.bounding_box if child_geometry is not None else None,
                    para_style=_para_style_from_node(child),
                    content=[
                        _table_node_to_ir(
                            child,
                            unit_id=f"{unit_id}.tbl1",
                            assets=assets,
                                        )
                    ],
                )
            )
            continue
        if child_type == "image":
            child_index += 1
            paragraph = _image_paragraph(child, unit_id=unit_id, assets=assets)
            if paragraph.page_number is None:
                paragraph.page_number = default_page_number
            paragraphs.append(paragraph)
            continue
        paragraph = _paragraph_from_text_node(child, unit_id=unit_id)
        if paragraph is None:
            continue
        child_index += 1
        if paragraph.page_number is None:
            paragraph.page_number = default_page_number
        paragraphs.append(paragraph)
    if not paragraphs:
        paragraphs.append(
            ParagraphIR(
                **_pdf_node_kwargs("paragraph", f"{cell_unit_id}.p1"),
                text="",
                page_number=default_page_number,
                bbox=None,
            )
        )
    return paragraphs


def _iter_raw_table_cells(node: dict[str, Any]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for row in node.get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        for cell in row.get("cells", []) or []:
            if isinstance(cell, dict):
                cells.append(cell)
    return cells


def _effective_bbox_from_descendants(node: Any) -> PdfBoundingBox | None:
    bbox = coerce_bbox(node.get("bounding box")) if isinstance(node, dict) else None
    merged_bbox = bbox

    def visit(value: Any) -> None:
        nonlocal merged_bbox
        if isinstance(value, dict):
            child_bbox = coerce_bbox(value.get("bounding box"))
            if child_bbox is not None:
                merged_bbox = _merge_bounding_boxes(merged_bbox, child_bbox)
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)

    if isinstance(node, dict):
        for child in node.values():
            visit(child)
    return merged_bbox


def _table_node_to_ir(
    node: dict[str, Any],
    *,
    unit_id: str,
    assets: dict[str, ImageAsset],
) -> TableIR:
    """Convert one raw ODL table node into TableIR.

    Dotted-rule cell splits are applied upstream by
    ``preprocess_dotted_rule_splits`` so the raw structure is already complete
    by the time we arrive here.
    """
    table_geometry = _node_geometry(node)
    resolved_cells = _iter_raw_table_cells(node)
    row_count = coerce_int(node.get("number of rows")) or 0
    col_count = coerce_int(node.get("number of columns")) or 0
    table_style = _table_style_from_node(node)
    if table_style is not None:
        table_style.row_count = row_count
        table_style.col_count = col_count

    table = TableIR(
        **_pdf_node_kwargs("table", unit_id),
        row_count=row_count,
        col_count=col_count,
        bbox=table_geometry.bounding_box if table_geometry is not None else None,
        table_style=table_style,
    )
    for cell in resolved_cells:
        row_index = coerce_int(cell.get("row number")) or 1
        col_index = coerce_int(cell.get("column number")) or 1
        cell_geometry = _node_geometry(cell)
        cell_style = _cell_style_from_node(cell)
        if cell_style is not None:
            cell_style.rowspan = max(coerce_int(cell.get("row span")) or 1, 1)
            cell_style.colspan = max(coerce_int(cell.get("column span")) or 1, 1)
        _append_table_cell(
            table,
            row_index=row_index,
            col_index=col_index,
            rowspan=max(coerce_int(cell.get("row span")) or 1, 1),
            colspan=max(coerce_int(cell.get("column span")) or 1, 1),
            cell_bbox=cell_geometry.bounding_box if cell_geometry is not None else None,
            cell_style=cell_style,
            children=cell.get("kids", []),
            unit_id=unit_id,
            assets=assets,
            default_page_number=_page_number_from_node(cell),
        )
    return table


def _append_table_cell(
    table: TableIR,
    *,
    row_index: int,
    col_index: int,
    rowspan: int,
    colspan: int,
    cell_bbox: PdfBoundingBox | None,
    cell_style: CellStyleInfo | None,
    children: list[dict[str, Any]],
    unit_id: str,
    assets: dict[str, ImageAsset],
    default_page_number: int | None,
) -> None:
    cell_unit_id = f"{unit_id}.tr{row_index}.tc{col_index}"
    if cell_style is not None:
        cell_style.rowspan = rowspan
        cell_style.colspan = colspan
    table.cells.append(
        TableCellIR(
            **_pdf_node_kwargs("cell", cell_unit_id),
            row_index=row_index,
            col_index=col_index,
            text=extract_text_from_odl_children(children),
            bbox=cell_bbox,
            cell_style=cell_style,
            paragraphs=_cell_paragraphs(
                children,
                cell_unit_id=cell_unit_id,
                default_page_number=default_page_number,
                assets=assets,
                ),
        )
    )


def _paragraph_is_table_box(paragraph: ParagraphIR) -> bool:
    return (
        paragraph.bbox is not None
        and len(paragraph.content) == 1
        and isinstance(paragraph.content[0], TableIR)
    )


def _paragraph_is_connector(paragraph: ParagraphIR) -> bool:
    if paragraph.bbox is None or not paragraph.content:
        return False
    if not all(isinstance(node, RunIR) for node in paragraph.content):
        return False
    text = "".join(run.text for run in paragraph.content).strip()
    return bool(text) and all(char in _STRIP_CONNECTOR_CHARS for char in text)


def _group_strip_rows(paragraphs: list[ParagraphIR]) -> list[list[ParagraphIR]]:
    rows: list[list[ParagraphIR]] = []
    row_tops: list[float] = []
    for paragraph in paragraphs:
        bbox = paragraph.bbox
        if bbox is None:
            continue
        assigned_row_index: int | None = None
        for row_index, row_top in enumerate(row_tops):
            if abs(bbox.top_pt - row_top) <= _STRIP_ROW_TOLERANCE_PT:
                assigned_row_index = row_index
                break
        if assigned_row_index is None:
            rows.append([paragraph])
            row_tops.append(bbox.top_pt)
            continue
        rows[assigned_row_index].append(paragraph)
        row_tops[assigned_row_index] = sum(
            member.bbox.top_pt for member in rows[assigned_row_index] if member.bbox is not None
        ) / len(rows[assigned_row_index])
    rows.sort(
        key=lambda row: (
            -max(member.bbox.top_pt for member in row if member.bbox is not None),
            min(member.bbox.left_pt for member in row if member.bbox is not None),
        )
    )
    for row in rows:
        row.sort(key=lambda member: member.bbox.left_pt if member.bbox is not None else float("inf"))
    return rows


def _build_strip_table_paragraph(
    paragraphs: list[ParagraphIR],
    *,
    unit_id: str,
) -> ParagraphIR | None:
    if not paragraphs:
        return None

    bboxes = [paragraph.bbox for paragraph in paragraphs if paragraph.bbox is not None]
    if not bboxes:
        return None

    group_bbox = bboxes[0]
    for bbox in bboxes[1:]:
        group_bbox = _merge_bounding_boxes(group_bbox, bbox)
    if group_bbox is None:
        return None

    rows = _group_strip_rows(paragraphs)
    if not rows:
        return None
    col_count = max(len(row) for row in rows)

    cells: list[TableCellIR] = []
    cell_index = 0
    for row_index, row in enumerate(rows, start=1):
        for col_index, paragraph in enumerate(row, start=1):
            bbox = paragraph.bbox
            if bbox is None:
                continue
            cell_index += 1
            cells.append(
                TableCellIR(
                    **_pdf_node_kwargs("cell", f"{unit_id}.cell.{cell_index}"),
                    row_index=row_index,
                    col_index=col_index,
                    text=paragraph.text,
                    bbox=bbox,
                    cell_style=CellStyleInfo(
                        width_pt=max(bbox.right_pt - bbox.left_pt, 0.0),
                        height_pt=max(bbox.top_pt - bbox.bottom_pt, 0.0),
                        horizontal_align="center" if _paragraph_is_connector(paragraph) else None,
                        vertical_align="middle" if _paragraph_is_connector(paragraph) else None,
                    ),
                    paragraphs=[paragraph.model_copy(deep=True)],
                )
            )
    if not cells:
        return None

    table = TableIR(
        **_pdf_node_kwargs("table", f"{unit_id}.tbl1"),
        row_count=max(len(rows), 1),
        col_count=max(col_count, 1),
        bbox=group_bbox,
        table_style=TableStyleInfo(
            row_count=max(len(rows), 1),
            col_count=max(col_count, 1),
            width_pt=max(group_bbox.right_pt - group_bbox.left_pt, 0.0),
            height_pt=max(group_bbox.top_pt - group_bbox.bottom_pt, 0.0),
            render_grid=False,
        ),
        cells=cells,
    )
    paragraph = ParagraphIR(
        **_pdf_node_kwargs("paragraph", unit_id),
        text="",
        page_number=paragraphs[0].page_number,
        bbox=group_bbox,
        content=[table],
    )
    paragraph.recompute_text()
    return paragraph


def _collapse_table_connector_sequences(
    paragraphs: list[ParagraphIR],
    *,
    unit_prefix: str,
) -> list[ParagraphIR]:
    collapsed: list[ParagraphIR] = []
    index = 0
    strip_index = 0
    while index < len(paragraphs):
        kind = (
            "box"
            if _paragraph_is_table_box(paragraphs[index])
            else "connector" if _paragraph_is_connector(paragraphs[index]) else None
        )
        if kind is None:
            collapsed.append(paragraphs[index])
            index += 1
            continue

        end = index
        box_count = 0
        connector_count = 0
        while end < len(paragraphs):
            current = paragraphs[end]
            if _paragraph_is_table_box(current):
                box_count += 1
                end += 1
                continue
            if _paragraph_is_connector(current):
                connector_count += 1
                end += 1
                continue
            break

        block = paragraphs[index:end]
        if (
            len(block) >= 3
            and box_count >= 3
            and connector_count >= 1
            and _paragraph_is_table_box(block[0])
            and _paragraph_is_table_box(block[-1])
        ):
            strip_index += 1
            strip_paragraph = _build_strip_table_paragraph(
                block,
                unit_id=f"{unit_prefix}.strip{strip_index}",
            )
            if strip_paragraph is not None:
                collapsed.append(strip_paragraph)
                index = end
                continue

        collapsed.extend(block)
        index = end

    return collapsed


def _paragraphs_from_list_node(
    node: dict[str, Any],
    *,
    unit_prefix: str,
    assets: dict[str, ImageAsset],
    list_level: int = 1,
) -> list[ParagraphIR]:
    """Flatten list items into normal paragraph units.

    DocIR currently does not keep a dedicated list tree, so list items are
    emitted as ordinary paragraphs in reading order. Nested tables/images are
    still preserved inside paragraph content where supported.
    """
    paragraphs: list[ParagraphIR] = []
    for index, item in enumerate(node.get("list items", []), start=1):
        unit_id = f"{unit_prefix}.li{index}"
        item_paragraphs: list[ParagraphIR] = []
        item_geometry = _node_geometry(item)
        paragraph = _paragraph_from_text_node(
            item,
            unit_id=unit_id,
            paragraph_geometry=item_geometry,
            run_geometry=item_geometry,
        )
        if paragraph is not None:
            item_paragraphs.append(paragraph)
        child_paragraphs: list[ParagraphIR] = []
        for child_index, child in enumerate(item.get("kids", []), start=1):
            child_type = child.get("type")
            child_unit_id = f"{unit_id}.c{child_index}"
            if child_type == "list":
                child_paragraphs.extend(
                    _paragraphs_from_list_node(
                        child,
                        unit_prefix=child_unit_id,
                        assets=assets,
                        list_level=list_level + 1,
                    )
                )
            elif child_type == "table":
                child_geometry = _node_geometry(child)
                child_paragraphs.append(
                    ParagraphIR(
                        **_pdf_node_kwargs("paragraph", child_unit_id),
                        text="",
                        page_number=_page_number_from_node(child),
                        bbox=child_geometry.bounding_box if child_geometry is not None else None,
                        para_style=_para_style_from_node(child),
                        content=[
                            _table_node_to_ir(
                                child,
                                unit_id=f"{child_unit_id}.tbl1",
                                assets=assets,
                                                )
                        ],
                    )
                )
            elif child_type == "image":
                paragraph = _image_paragraph(child, unit_id=child_unit_id, assets=assets)
                child_paragraphs.append(paragraph)
            else:
                nested_paragraph = _paragraph_from_text_node(
                    child,
                    unit_id=child_unit_id,
                    paragraph_geometry=_compose_node_geometry(_node_geometry(child), item_geometry),
                    run_geometry=_compose_node_geometry(_node_geometry(child), item_geometry),
                )
                if nested_paragraph is not None:
                    child_paragraphs.append(nested_paragraph)
        item_paragraphs.extend(
            _collapse_table_connector_sequences(child_paragraphs, unit_prefix=unit_id)
        )
        paragraphs.extend(item_paragraphs)
    return paragraphs


# ---------------------------------------------------------------------------
# Page/document assembly
# Raw ODL output is assembled into one flat DocIR paragraph list here. Page and
# layout provenance stay in metadata; the top-level content model remains flat.
# ---------------------------------------------------------------------------

def _collect_page_numbers(value: Any, page_numbers: set[int]) -> None:
    if isinstance(value, dict):
        page_number = coerce_int(value.get("page number"))
        if page_number is not None:
            page_numbers.add(page_number)
        for child in value.values():
            _collect_page_numbers(child, page_numbers)
        return
    if isinstance(value, list):
        for child in value:
            _collect_page_numbers(child, page_numbers)


def _page_infos_from_odl(raw_document: dict[str, Any]) -> list[PageInfo]:
    page_layouts: dict[int, dict[str, Any]] = {}
    for page in raw_document.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        page_number = coerce_int(page.get("page number"))
        if page_number is None:
            continue
        page_layouts[page_number] = {
            "width_pt": coerce_float(node_value(page, "width pt", "page width pt")),
            "height_pt": coerce_float(node_value(page, "height pt", "page height pt")),
            "margin_left_pt": coerce_float(page.get("margin left pt")),
            "margin_right_pt": coerce_float(page.get("margin right pt")),
            "margin_top_pt": coerce_float(page.get("margin top pt")),
            "margin_bottom_pt": coerce_float(page.get("margin bottom pt")),
        }

    page_numbers: set[int] = set()
    page_count = coerce_int(raw_document.get("number of pages"))
    if page_count is not None and page_count > 0:
        page_numbers.update(range(1, page_count + 1))
    _collect_page_numbers(raw_document.get("kids", []), page_numbers)

    return [
        PageInfo(page_number=page_number, **page_layouts.get(page_number, {}))
        for page_number in sorted(page_numbers)
    ]


def _layout_region_bboxes(raw_document: dict[str, Any]) -> dict[str, list[float]]:
    region_bboxes: dict[str, list[float]] = {}
    for region in raw_document.get("layout regions", []) or []:
        if not isinstance(region, dict):
            continue
        region_id = region.get("region id")
        bbox = region.get("bounding box")
        if isinstance(region_id, str) and isinstance(bbox, list) and len(bbox) == 4:
            region_bboxes[region_id] = bbox
    return region_bboxes


def _fill_missing_bboxes_from_layout_regions(value: Any, region_bboxes: dict[str, list[float]]) -> None:
    if isinstance(value, dict):
        region_id = value.get("layout region id")
        if "bounding box" not in value and isinstance(region_id, str) and region_id in region_bboxes:
            value["bounding box"] = list(region_bboxes[region_id])
        for child in value.values():
            _fill_missing_bboxes_from_layout_regions(child, region_bboxes)
        return
    if isinstance(value, list):
        for child in value:
            _fill_missing_bboxes_from_layout_regions(child, region_bboxes)


def _canonicalize_top_level_paragraphs(paragraphs: list[ParagraphIR]) -> list[ParagraphIR]:
    canonical: list[ParagraphIR] = []
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        clone = paragraph.model_copy(deep=True)
        _canonicalize_paragraph_unit_ids(
            clone,
            unit_id=f"s1.p{paragraph_index}",
            top_level=True,
        )
        canonical.append(clone)
    return canonical


def _set_pdf_node_anchor(node, kind, structural_path: str, *, parent_debug_path: str | None = None) -> None:
    """Reset node_id + native_anchor on an existing IR node."""
    kwargs = _pdf_node_kwargs(kind, structural_path, parent_debug_path=parent_debug_path)
    node.node_id = kwargs["node_id"]
    node.native_anchor = kwargs["native_anchor"]


def _canonicalize_paragraph_unit_ids(
    paragraph: ParagraphIR,
    *,
    unit_id: str,
    top_level: bool,
) -> None:
    _set_pdf_node_anchor(paragraph, "paragraph", unit_id)
    run_index = 0
    image_index = 0
    table_index = 0
    for node in paragraph.content:
        if isinstance(node, RunIR):
            run_index += 1
            _set_pdf_node_anchor(node, "run", f"{unit_id}.r{run_index}", parent_debug_path=unit_id)
            continue
        if isinstance(node, ImageIR):
            image_index += 1
            _set_pdf_node_anchor(node, "image", f"{unit_id}.img{image_index}", parent_debug_path=unit_id)
            continue
        if isinstance(node, TableIR):
            table_index += 1
            table_unit_id = (
                f"{unit_id}.r1.tbl{table_index}"
                if top_level
                else f"{unit_id}.tbl{table_index}"
            )
            _canonicalize_table_unit_ids(node, unit_id=table_unit_id)
    paragraph.recompute_text()


def _canonicalize_table_unit_ids(table: TableIR, *, unit_id: str) -> None:
    _set_pdf_node_anchor(table, "table", unit_id)
    for cell in table.cells:
        cell_unit_id = f"{unit_id}.tr{cell.row_index}.tc{cell.col_index}"
        _set_pdf_node_anchor(cell, "cell", cell_unit_id, parent_debug_path=unit_id)
        for paragraph_index, paragraph in enumerate(cell.paragraphs, start=1):
            _canonicalize_paragraph_unit_ids(
                paragraph,
                unit_id=f"{cell_unit_id}.p{paragraph_index}",
                top_level=False,
            )
        cell.recompute_text()


def _record_table_continuation_link(
    records: list[_OdlTableContinuationLink],
    node: dict[str, Any],
    *,
    paragraph_index: int,
) -> None:
    raw_table_id = _raw_table_id(node.get("id"))
    if raw_table_id is None:
        return
    records.append(
        _OdlTableContinuationLink(
            paragraph_index=paragraph_index,
            raw_table_id=raw_table_id,
            previous_raw_table_id=_raw_table_id(node.get("previous table id")),
            next_raw_table_id=_raw_table_id(node.get("next table id")),
        )
    )


def _apply_table_continuation_links(
    paragraphs: list[ParagraphIR],
    records: list[_OdlTableContinuationLink],
) -> None:
    raw_to_docir_table_id: dict[str, str] = {}
    record_tables: list[tuple[_OdlTableContinuationLink, TableIR]] = []
    for record in records:
        if record.paragraph_index >= len(paragraphs):
            continue
        tables = paragraphs[record.paragraph_index].tables
        if not tables:
            continue
        table = tables[0]
        record_tables.append((record, table))
        if table.node_id is not None:
            raw_to_docir_table_id[record.raw_table_id] = table.node_id

    for record, table in record_tables:
        table.previous_table_id = (
            raw_to_docir_table_id.get(record.previous_raw_table_id)
            if record.previous_raw_table_id is not None
            else None
        )
        table.next_table_id = (
            raw_to_docir_table_id.get(record.next_raw_table_id)
            if record.next_raw_table_id is not None
            else None
        )


def build_doc_ir_from_odl_result(
    raw_document: dict[str, Any],
    *,
    source_path: str | Path | None = None,
    doc_id: str | None = None,
    doc_cls: type[DocIR] | None = None,
    **doc_kwargs: Any,
) -> DocIR:
    """Build canonical DocIR from one ODL raw JSON document.

    Important design choice:
    - top-level content stays flat in ``DocIR.paragraphs``
    - page/bbox information is copied into first-class DocIR fields
    - preview-only raw fields are intentionally *not* mirrored into DocIR
    """
    _fill_missing_bboxes_from_layout_regions(raw_document, _layout_region_bboxes(raw_document))

    assets: dict[str, ImageAsset] = {}
    paragraphs: list[ParagraphIR] = []
    table_continuation_links: list[_OdlTableContinuationLink] = []

    order = 0
    for node in raw_document.get("kids", []):
        node_type = node.get("type")
        unit_id = f"p{order + 1}"
        if node_type == "table":
            order += 1
            node_geometry = _node_geometry(node)
            paragraph_index = len(paragraphs)
            paragraphs.append(
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", unit_id),
                    text="",
                    page_number=_page_number_from_node(node),
                    bbox=node_geometry.bounding_box if node_geometry is not None else None,
                    para_style=_para_style_from_node(node),
                    content=[
                        _table_node_to_ir(
                            node,
                            unit_id=f"{unit_id}.tbl1",
                            assets=assets,
                                        )
                    ],
                )
            )
            _record_table_continuation_link(
                table_continuation_links,
                node,
                paragraph_index=paragraph_index,
            )
            continue
        if node_type == "image":
            order += 1
            paragraphs.append(_image_paragraph(node, unit_id=unit_id, assets=assets))
            continue
        if node_type == "list":
            list_paragraphs = _paragraphs_from_list_node(
                node,
                unit_prefix=unit_id,
                assets=assets,
                )
            if list_paragraphs:
                order += len(list_paragraphs)
                paragraphs.extend(list_paragraphs)
            continue
        if node_type in {"header", "footer"}:
            # Header/footer wrappers are flattened into ordinary paragraphs so
            # downstream consumers do not need a PDF-only container type.
            container_paragraphs = _paragraphs_from_container_node(
                node,
                unit_prefix=unit_id,
                assets=assets,
                )
            if container_paragraphs:
                order += len(container_paragraphs)
                paragraphs.extend(container_paragraphs)
            continue
        if node_type == "text block":
            # `text block` is another wrapper-like construct in ODL output.
            # Its children are emitted directly into the flat paragraph stream.
            for child in node.get("kids", []):
                child_unit_id = f"p{order + 1}"
                if child.get("type") == "table":
                    order += 1
                    child_geometry = _node_geometry(child)
                    paragraph_index = len(paragraphs)
                    paragraphs.append(
                        ParagraphIR(
                            **_pdf_node_kwargs("paragraph", child_unit_id),
                            text="",
                            page_number=_page_number_from_node(child),
                            bbox=child_geometry.bounding_box if child_geometry is not None else None,
                            para_style=_para_style_from_node(child),
                            content=[
                                _table_node_to_ir(
                                    child,
                                    unit_id=f"{child_unit_id}.tbl1",
                                    assets=assets,
                                                        )
                            ],
                        )
                    )
                    _record_table_continuation_link(
                        table_continuation_links,
                        child,
                        paragraph_index=paragraph_index,
                    )
                    continue
                if child.get("type") == "list":
                    list_paragraphs = _paragraphs_from_list_node(
                        child,
                        unit_prefix=child_unit_id,
                        assets=assets,
                                )
                    if list_paragraphs:
                        order += len(list_paragraphs)
                        paragraphs.extend(list_paragraphs)
                    continue
                paragraph = _paragraph_from_text_node(child, unit_id=child_unit_id)
                if paragraph is None:
                    continue
                order += 1
                paragraphs.append(paragraph)
            continue
        paragraph = _paragraph_from_text_node(node, unit_id=unit_id)
        if paragraph is None:
            continue
        order += 1
        paragraphs.append(paragraph)

    resolved_doc_cls = doc_cls or DocIR
    resolved_source_path = str(source_path) if source_path is not None else raw_document.get("file name")
    resolved_doc_id = doc_id or raw_document.get("file name")
    if resolved_doc_id and "." in resolved_doc_id:
        resolved_doc_id = Path(resolved_doc_id).stem
    canonical_paragraphs = _canonicalize_top_level_paragraphs(paragraphs)
    _apply_table_continuation_links(canonical_paragraphs, table_continuation_links)
    return resolved_doc_cls(
        doc_id=resolved_doc_id,
        source_path=resolved_source_path,
        source_doc_type="pdf",
        assets=assets,
        pages=_page_infos_from_odl(raw_document),
        paragraphs=canonical_paragraphs,
        **doc_kwargs,
    )


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in {"true", "1", "yes", "y", "on"}:
            return True
        if stripped in {"false", "0", "no", "n", "off"}:
            return False
    return None


def _normalize_vertical_align(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    mapping = {
        "top": "top",
        "middle": "middle",
        "center": "middle",
        "centre": "middle",
        "bottom": "bottom",
    }
    return mapping.get(stripped)


__all__ = ["build_doc_ir_from_odl_result"]
