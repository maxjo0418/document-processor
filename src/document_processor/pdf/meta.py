"""ODL/PDF normalization helpers."""

from __future__ import annotations

import re
from typing import Any

from ..models import BoundingBox

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3}([0-9A-Fa-f]{3})?([0-9A-Fa-f]{2})?$")
_RGB_COLOR_RE = re.compile(r"^rgba?\([^)]+\)$")
_NAMED_COLOR_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")
_BRACKETED_NUMBER_RE = re.compile(r"^\[\s*([^\]]+)\s*\]$")


PdfBoundingBox = BoundingBox


def node_value(node: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = node.get(key)
        if value is not None:
            return value
    return None


def coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def coerce_bbox(value: Any) -> PdfBoundingBox | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    components = [coerce_float(component) for component in value]
    if any(component is None for component in components):
        return None
    left, bottom, right, top = components
    return PdfBoundingBox(
        left_pt=left,
        bottom_pt=bottom,
        right_pt=right,
        top_pt=top,
    )


def normalize_align(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    if not stripped:
        return None
    mapping = {
        "left": "left",
        "start": "left",
        "center": "center",
        "centre": "center",
        "middle": "center",
        "right": "right",
        "end": "right",
        "justify": "justify",
        "justified": "justify",
    }
    return mapping.get(stripped)


def pixels_to_points(value_px: Any, dpi: Any) -> float | None:
    px = coerce_float(value_px)
    dpi_value = coerce_float(dpi)
    if px is None or dpi_value is None or dpi_value <= 0:
        return None
    return px * 72.0 / dpi_value


def sanitize_css_color(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if _HEX_COLOR_RE.fullmatch(stripped):
            return stripped
        if _RGB_COLOR_RE.fullmatch(stripped):
            return stripped
        if _NAMED_COLOR_RE.fullmatch(stripped):
            return stripped
        bracket_match = _BRACKETED_NUMBER_RE.fullmatch(stripped)
        if bracket_match:
            components = [
                component.strip()
                for component in bracket_match.group(1).split(",")
                if component.strip()
            ]
            return _components_to_css_color(components)
        return None
    if isinstance(value, (list, tuple)):
        return _components_to_css_color(list(value))
    return None


def _components_to_css_color(components: list[Any]) -> str | None:
    if not components:
        return None

    numeric_components: list[float] = []
    for component in components:
        try:
            numeric_components.append(float(component))
        except (TypeError, ValueError):
            return None

    if len(numeric_components) == 1:
        rgb = [_normalize_color_component(numeric_components[0])] * 3
        return _rgb_to_hex(rgb)

    if len(numeric_components) == 3:
        rgb = [_normalize_color_component(component) for component in numeric_components]
        return _rgb_to_hex(rgb)

    if len(numeric_components) == 4:
        rgb = [_normalize_color_component(component) for component in numeric_components[:3]]
        alpha = numeric_components[3]
        alpha = alpha if 0.0 <= alpha <= 1.0 else max(0.0, min(alpha / 255.0, 1.0))
        return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {alpha:.3f})"

    return None


def _normalize_color_component(value: float) -> int:
    if 0.0 <= value <= 1.0:
        return max(0, min(round(value * 255), 255))
    return max(0, min(round(value), 255))


def _rgb_to_hex(rgb: list[int]) -> str:
    return "#" + "".join(f"{component:02x}" for component in rgb)


def extract_text_from_odl_children(children: list[dict[str, Any]]) -> str:
    parts = [extract_text_from_odl_node(child) for child in children]
    return "\n".join(part for part in parts if part)


def extract_text_from_odl_node(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    if node_type in {"paragraph", "heading", "caption", "list item", "formula"}:
        return node.get("content", "")
    if node_type == "table":
        rows: list[str] = []
        for row in node.get("rows", []):
            cells = [
                extract_text_from_odl_children(cell.get("kids", []))
                for cell in row.get("cells", [])
            ]
            rows.append(" | ".join(cell.strip() for cell in cells if cell.strip()))
        return "\n".join(row for row in rows if row)
    if node_type == "list":
        items = [
            _extract_text_from_odl_list_item(item)
            for item in node.get("list items", [])
        ]
        return "\n".join(item for item in items if item)
    if node_type in {"header", "footer", "text block"}:
        return extract_text_from_odl_children(node.get("kids", []))
    return ""


def _extract_text_from_odl_list_item(node: dict[str, Any]) -> str:
    parts: list[str] = []
    content = node.get("content")
    if isinstance(content, str) and content:
        parts.append(content)
    child_text = extract_text_from_odl_children(node.get("kids", []))
    if child_text:
        parts.append(child_text)
    return "\n".join(parts)


__all__ = [
    "PdfBoundingBox",
    "coerce_float",
    "coerce_bbox",
    "coerce_int",
    "extract_text_from_odl_children",
    "extract_text_from_odl_node",
    "node_value",
    "normalize_align",
    "pixels_to_points",
    "sanitize_css_color",
]
