"""Infer PDF table-cell visual properties from rasterized page content."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..meta import PdfBoundingBox

_BACKGROUND_BUCKET_SIZE = 16
_BACKGROUND_DOMINANCE_THRESHOLD = 0.35
_MAX_BACKGROUND_SAMPLE_STEP = 3


@dataclass(slots=True)
class RenderedPdfColorPage:
    width_px: int
    height_px: int
    stride: int
    pixels: bytes


def render_pdf_pages_to_color(
    pdf_path: str | Path,
    *,
    page_numbers: set[int],
    dpi: int,
) -> dict[int, RenderedPdfColorPage]:
    import pypdfium2 as pdfium

    scale = dpi / 72.0
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        rendered_pages: dict[int, RenderedPdfColorPage] = {}
        for page_number in sorted(page_numbers):
            page = doc[page_number - 1]
            bitmap = page.render(scale=scale, grayscale=False, no_smoothpath=True)
            try:
                rendered_pages[page_number] = RenderedPdfColorPage(
                    width_px=bitmap.width,
                    height_px=bitmap.height,
                    stride=bitmap.stride,
                    pixels=bytes(bitmap.buffer),
                )
            finally:
                bitmap.close()
        return rendered_pages
    finally:
        doc.close()


def infer_cell_background_from_rendered_page(
    page: RenderedPdfColorPage,
    *,
    bbox: PdfBoundingBox,
    page_height_pt: float,
    dpi: int,
) -> str | None:
    left_px, right_px, top_px, bottom_px = _bbox_to_pixel_bounds(
        bbox=bbox,
        page_width_px=page.width_px,
        page_height_px=page.height_px,
        page_height_pt=page_height_pt,
        dpi=dpi,
    )
    if right_px <= left_px or bottom_px < top_px:
        return None

    width_px = right_px - left_px
    height_px = (bottom_px - top_px) + 1
    trim_x = min(max(int(width_px * 0.12), 3), 16)
    trim_y = min(max(int(height_px * 0.12), 3), 16)

    x0 = left_px + trim_x if width_px > trim_x * 2 else left_px
    x1 = right_px - trim_x if width_px > trim_x * 2 else right_px
    y0 = top_px + trim_y if height_px > trim_y * 2 else top_px
    y1 = bottom_px - trim_y if height_px > trim_y * 2 else bottom_px
    if x1 <= x0 or y1 < y0:
        return None

    sample_step = max(1, min(max(width_px, height_px) // 40, _MAX_BACKGROUND_SAMPLE_STEP))
    buckets: dict[tuple[int, int, int], list[int]] = {}
    total = 0
    for y in range(y0, y1 + 1, sample_step):
        row_offset = y * page.stride
        for x in range(x0, x1, sample_step):
            idx = row_offset + (x * 3)
            blue = page.pixels[idx]
            green = page.pixels[idx + 1]
            red = page.pixels[idx + 2]
            bucket = (
                red // _BACKGROUND_BUCKET_SIZE,
                green // _BACKGROUND_BUCKET_SIZE,
                blue // _BACKGROUND_BUCKET_SIZE,
            )
            stats = buckets.setdefault(bucket, [0, 0, 0, 0])
            stats[0] += 1
            stats[1] += red
            stats[2] += green
            stats[3] += blue
            total += 1

    if total <= 0:
        return None

    dominant_bucket, dominant_stats = max(buckets.items(), key=lambda item: item[1][0])
    dominant_count, red_sum, green_sum, blue_sum = dominant_stats
    dominance_ratio = dominant_count / total
    if dominance_ratio < _BACKGROUND_DOMINANCE_THRESHOLD:
        return None

    red = red_sum // dominant_count
    green = green_sum // dominant_count
    blue = blue_sum // dominant_count
    if _is_near_white((red, green, blue)):
        return None
    return f"#{red:02x}{green:02x}{blue:02x}"


def _clamp_px(value: int, *, upper: int) -> int:
    return max(0, min(value, upper))


def _bbox_to_pixel_bounds(
    *,
    bbox: PdfBoundingBox,
    page_width_px: int,
    page_height_px: int,
    page_height_pt: float,
    dpi: int,
) -> tuple[int, int, int, int]:
    scale = dpi / 72.0
    left_px = _clamp_px(int(bbox.left_pt * scale), upper=page_width_px - 1)
    right_px = _clamp_px(int(round(bbox.right_pt * scale)), upper=page_width_px)
    top_px = _clamp_px(int((page_height_pt - bbox.top_pt) * scale), upper=page_height_px - 1)
    bottom_px = _clamp_px(
        int(round((page_height_pt - bbox.bottom_pt) * scale)) - 1,
        upper=page_height_px - 1,
    )
    return left_px, right_px, top_px, bottom_px


def _is_near_white(rgb: tuple[int, int, int]) -> bool:
    red, green, blue = rgb
    return red >= 245 and green >= 245 and blue >= 245


__all__ = [
    "RenderedPdfColorPage",
    "infer_cell_background_from_rendered_page",
    "render_pdf_pages_to_color",
]
