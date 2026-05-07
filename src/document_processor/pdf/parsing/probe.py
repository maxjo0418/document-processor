"""Cheap PDF probe utilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from ...logging_config import get_logger

NORMAL_TEXT_MIN_RATIO = 0.5
SCANNED_MAX_CHARS = 20
REPLACEMENT_CHAR_THRESHOLD = 0.3

FULL_OCR_IMAGE_RATIO_THRESHOLD = 0.95
FULL_OCR_CONTENT_IMAGE_RATIO_THRESHOLD = 0.9
GIBBERISH_MIN_CHARS = 50
GIBBERISH_NORMAL_TEXT_MAX_RATIO = 0.1

_HANGUL_RANGE = re.compile(r"[\uAC00-\uD7AF\u3131-\u318E]")
_LATIN_RANGE = re.compile(r"[A-Za-z0-9]")
logger = get_logger(__name__)


@dataclass(slots=True)
class PageProfile:
    page_number: int
    char_count: int
    normal_text_ratio: float
    replacement_char_ratio: float
    text_readable: bool
    path_count: int = 0
    image_area_ratio: float = 0.0
    image_area_in_content_ratio: float = 0.0
    has_suspicious_regions: bool = False
    page_width_pt: float | None = None
    page_height_pt: float | None = None


@dataclass(slots=True)
class PdfProfile:
    page_count: int
    avg_chars_per_page: float
    normal_text_ratio: float
    text_readable: bool
    text_readable_page_ratio: float = 0.0
    page_profiles: list[PageProfile] = field(default_factory=list)


def normal_text_ratio(text: str) -> float:
    non_space = [ch for ch in text if not ch.isspace()]
    if not non_space:
        return 0.0

    normal_count = sum(
        1 for ch in non_space
        if _HANGUL_RANGE.match(ch) or _LATIN_RANGE.match(ch)
    )
    return normal_count / len(non_space)


def replacement_char_ratio(text: str) -> float:
    non_space = [ch for ch in text if not ch.isspace()]
    if not non_space:
        return 0.0
    replacement_count = sum(1 for ch in non_space if ch == "\uFFFD")
    return replacement_count / len(non_space)


def _extract_text_signals(page) -> tuple[int, float, float, bool]:  # noqa: ANN001
    text = page.get_textpage().get_text_range()
    char_count = len(text.strip())
    text_ratio = normal_text_ratio(text)
    replacement_ratio = replacement_char_ratio(text)
    text_readable = (
        text_ratio >= NORMAL_TEXT_MIN_RATIO
        and char_count > SCANNED_MAX_CHARS
        and replacement_ratio < REPLACEMENT_CHAR_THRESHOLD
    )
    return char_count, text_ratio, replacement_ratio, text_readable


def _clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, value))


def _union_bounds(
    current: tuple[float, float, float, float] | None,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if current is None:
        return bounds
    return (
        min(current[0], bounds[0]),
        min(current[1], bounds[1]),
        max(current[2], bounds[2]),
        max(current[3], bounds[3]),
    )


def _extract_text_union_bounds(page) -> tuple[float, float, float, float] | None:  # noqa: ANN001
    textpage = page.get_textpage()
    rect_count = textpage.count_rects()
    text_bounds: tuple[float, float, float, float] | None = None
    for rect_index in range(rect_count):
        left, bottom, right, top = textpage.get_rect(rect_index)
        width = max(0.0, right - left)
        height = max(0.0, top - bottom)
        if width <= 0.0 or height <= 0.0:
            continue
        text_bounds = _union_bounds(text_bounds, (left, bottom, right, top))
    return text_bounds


def _extract_visual_signals(page) -> tuple[int, float, float, bool, float, float]:  # noqa: ANN001
    import pypdfium2.raw as raw

    page_width = page.get_width() or 1.0
    page_height = page.get_height() or 1.0
    page_area = max(page_width * page_height, 1.0)

    path_count = 0
    image_area = 0.0
    content_bounds: tuple[float, float, float, float] | None = _extract_text_union_bounds(page)

    for obj in page.get_objects():
        bounds = obj.get_bounds()
        if bounds is None:
            continue

        left, bottom, right, top = bounds
        width = max(0.0, right - left)
        height = max(0.0, top - bottom)
        area = width * height
        obj_type = raw.FPDFPageObj_GetType(obj.raw)
        if area > 0.0:
            content_bounds = _union_bounds(content_bounds, bounds)

        if obj_type == raw.FPDF_PAGEOBJ_IMAGE:
            image_area += area
        elif obj_type in (raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING):
            path_count += 1

    image_area_ratio = _clamp_ratio(image_area / page_area)
    if content_bounds is not None:
        content_width = max(0.0, content_bounds[2] - content_bounds[0])
        content_height = max(0.0, content_bounds[3] - content_bounds[1])
        content_bbox_area = max(content_width * content_height, 1.0)
    else:
        content_bbox_area = page_area
    image_area_in_content_ratio = _clamp_ratio(image_area / content_bbox_area)
    has_suspicious_regions = (
        path_count >= 6
        or (image_area_ratio >= 0.10 and path_count >= 3)
    )

    return (
        path_count,
        image_area_ratio,
        image_area_in_content_ratio,
        has_suspicious_regions,
        page_width,
        page_height,
    )


def _probe_single_page(page, page_no: int) -> PageProfile:  # noqa: ANN001
    char_count, text_ratio, replacement_ratio, text_readable = _extract_text_signals(page)
    (
        path_count,
        image_area_ratio,
        image_area_in_content_ratio,
        has_suspicious_regions,
        page_width,
        page_height,
    ) = _extract_visual_signals(page)
    return PageProfile(
        page_number=page_no + 1,
        char_count=char_count,
        normal_text_ratio=text_ratio,
        replacement_char_ratio=replacement_ratio,
        text_readable=text_readable,
        path_count=path_count,
        image_area_ratio=image_area_ratio,
        image_area_in_content_ratio=image_area_in_content_ratio,
        has_suspicious_regions=has_suspicious_regions,
        page_width_pt=page_width,
        page_height_pt=page_height,
    )


def _probe_pdf_serial(path: Path, *, page_count: int) -> list[PageProfile]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        return [
            _probe_single_page(doc[page_no], page_no)
            for page_no in range(page_count)
        ]
    finally:
        doc.close()


def probe_pdf(path: Path | str) -> PdfProfile | None:
    path = Path(path)
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path))
        page_count = len(doc)
        doc.close()

        # Keep probe deterministic and lightweight. The per-page work is small
        # enough that a serial pass is simpler than maintaining a parallel path.
        final_profiles = _probe_pdf_serial(path, page_count=page_count)

        total_chars = sum(pp.char_count for pp in final_profiles)
        pages_with_readable_text = sum(1 for pp in final_profiles if pp.text_readable)
        weighted_ratio = (
            sum(pp.normal_text_ratio * pp.char_count for pp in final_profiles) / total_chars
            if total_chars > 0
            else 0.0
        )
        avg_chars = total_chars / page_count if page_count > 0 else 0.0
        readable_ratio = pages_with_readable_text / page_count if page_count > 0 else 0.0

        return PdfProfile(
            page_count=page_count,
            avg_chars_per_page=avg_chars,
            normal_text_ratio=weighted_ratio,
            text_readable=(
                weighted_ratio >= NORMAL_TEXT_MIN_RATIO and avg_chars > SCANNED_MAX_CHARS
            ),
            text_readable_page_ratio=readable_ratio,
            page_profiles=final_profiles,
        )
    except Exception:
        logger.exception("PDF probe failed for %s", path.name)
        return None


__all__ = [
    "FULL_OCR_CONTENT_IMAGE_RATIO_THRESHOLD",
    "FULL_OCR_IMAGE_RATIO_THRESHOLD",
    "GIBBERISH_MIN_CHARS",
    "GIBBERISH_NORMAL_TEXT_MAX_RATIO",
    "PageProfile",
    "PdfProfile",
    "probe_pdf",
]
