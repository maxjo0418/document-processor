"""Format-agnostic style models for structural document IR."""

from __future__ import annotations

from pydantic import BaseModel, Field

_PRIVATE_USE_RANGES = (
    range(0xE000, 0xF900),
    range(0xF0000, 0x100000),
    range(0x100000, 0x110000),
)
_BULLET_MARKER_MAP = {
    "\u00b7": "\u2022",
    "\uf06c": "\u2022",
    "\uf06e": "\u25aa",
    "\uf075": "\u25c6",
    "\uf0a7": "\u25aa",
    "\uf0b7": "\u2022",
    "\uf0d8": "\u25e6",
    "\uf0fc": "\u2713",
}


def _is_private_use_char(char: str) -> bool:
    codepoint = ord(char)
    return any(codepoint in private_range for private_range in _PRIVATE_USE_RANGES)


def normalize_bullet_marker(marker: str | None) -> str:
    """Return a browser-safe Unicode bullet for Symbol/Wingdings marker text."""
    if not marker:
        return "\u2022"

    stripped = marker.strip()
    if not stripped:
        return "\u2022"
    if stripped in _BULLET_MARKER_MAP:
        return _BULLET_MARKER_MAP[stripped]

    translated = "".join(_BULLET_MARKER_MAP.get(char, char) for char in stripped)
    if translated != stripped:
        return translated
    if any(_is_private_use_char(char) for char in stripped):
        return "\u2022"
    return stripped


def normalize_list_marker(marker: str | None, marker_type: str | None = None) -> str | None:
    if marker is None:
        return None
    if (marker_type or "").lower() == "bullet" or any(_is_private_use_char(char) for char in marker):
        return normalize_bullet_marker(marker)
    return marker


class RunStyleInfo(BaseModel):
    """Text-level formatting for a single run."""

    font_family: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    superscript: bool = False
    subscript: bool = False
    color: str | None = None
    highlight: str | None = None
    size_pt: float | None = None
    hidden: bool = False


class ColumnLayoutInfo(BaseModel):
    """Paragraph column layout metadata."""

    count: int | None = None
    column_index: int | None = None
    gap_pt: float | None = None
    widths_pt: list[float] = Field(default_factory=list)
    gaps_pt: list[float] = Field(default_factory=list)
    equal_width: bool | None = None


class ListItemInfo(BaseModel):
    """Resolved paragraph list marker metadata."""

    list_id: str | None = None
    level: int = 0
    marker: str | None = None
    marker_type: str | None = None
    marker_text: str | None = None


class ParaStyleInfo(BaseModel):
    """Paragraph-level formatting."""

    align: str | None = None
    left_indent_pt: float | None = None
    right_indent_pt: float | None = None
    first_line_indent_pt: float | None = None
    hanging_indent_pt: float | None = None
    render_tag: str | None = None
    column_layout: ColumnLayoutInfo | None = None
    list_info: ListItemInfo | None = None


class CellStyleInfo(BaseModel):
    """Table cell formatting."""

    background: str | None = None
    vertical_align: str | None = None
    horizontal_align: str | None = None
    width_pt: float | None = None
    height_pt: float | None = None
    padding_top_pt: float | None = None
    padding_right_pt: float | None = None
    padding_bottom_pt: float | None = None
    padding_left_pt: float | None = None
    border_top: str | None = None
    border_bottom: str | None = None
    border_left: str | None = None
    border_right: str | None = None
    diagonal_tl_br: str | None = None
    diagonal_tr_bl: str | None = None
    rowspan: int = 1
    colspan: int = 1


class ObjectPlacementInfo(BaseModel):
    """Format-agnostic object placement metadata for floating tables and images."""

    mode: str | None = None
    wrap: str | None = None
    text_flow: str | None = None
    x_relative_to: str | None = None
    y_relative_to: str | None = None
    x_align: str | None = None
    y_align: str | None = None
    x_offset_pt: float | None = None
    y_offset_pt: float | None = None
    margin_top_pt: float | None = None
    margin_right_pt: float | None = None
    margin_bottom_pt: float | None = None
    margin_left_pt: float | None = None
    allow_overlap: bool | None = None
    flow_with_text: bool | None = None
    z_order: int | None = None


class TableStyleInfo(BaseModel):
    """Table-level metadata."""

    row_count: int = 0
    col_count: int = 0
    width_pt: float | None = None
    height_pt: float | None = None
    placement: ObjectPlacementInfo | None = None
    render_grid: bool = False


class StyleMap(BaseModel):
    """Style lookup map keyed by native structural paths."""

    runs: dict[str, RunStyleInfo] = Field(default_factory=dict)
    paragraphs: dict[str, ParaStyleInfo] = Field(default_factory=dict)
    cells: dict[str, CellStyleInfo] = Field(default_factory=dict)
    tables: dict[str, TableStyleInfo] = Field(default_factory=dict)


__all__ = [
    "ColumnLayoutInfo",
    "ListItemInfo",
    "ObjectPlacementInfo",
    "RunStyleInfo",
    "ParaStyleInfo",
    "CellStyleInfo",
    "TableStyleInfo",
    "StyleMap",
    "normalize_bullet_marker",
    "normalize_list_marker",
]
