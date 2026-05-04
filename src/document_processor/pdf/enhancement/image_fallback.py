"""Raster fallback for low-resolution PDF image assets."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ...core.document_ir_parser import _image_dimensions_from_bytes
from ...models import DocIR, ImageAsset, ImageIR, ParagraphIR, TableIR
from ..meta import PdfBoundingBox

_LOW_RES_IMAGE_PX_PER_PT_THRESHOLD = 1.1
_RASTER_FALLBACK_DPI = 216


@dataclass(frozen=True)
class _ImageCandidate:
    page_number: int
    image: ImageIR


@dataclass(frozen=True)
class _RenderedBitmap:
    width_px: int
    height_px: int
    stride: int
    pixels: bytes
    mode: str
    n_channels: int


def replace_low_resolution_pdf_image_assets(
    doc_ir: DocIR,
    pdf_path: str | Path,
    *,
    threshold_px_per_pt: float = _LOW_RES_IMAGE_PX_PER_PT_THRESHOLD,
    dpi: int = _RASTER_FALLBACK_DPI,
) -> DocIR:
    """Replace only undersampled PDF image assets with bbox crops from source PDF."""
    candidates_by_page = _low_resolution_image_candidates(
        doc_ir,
        threshold_px_per_pt=threshold_px_per_pt,
    )
    if not candidates_by_page:
        return doc_ir

    import pypdfium2 as pdfium

    pages = {page.page_number: page for page in doc_ir.pages}
    scale = dpi / 72.0
    pdf_doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for page_number, candidates in sorted(candidates_by_page.items()):
            page_info = pages.get(page_number)
            if page_info is None or page_info.height_pt is None:
                continue
            pdf_page = pdf_doc[page_number - 1]
            bitmap = pdf_page.render(scale=scale, grayscale=False)
            try:
                rendered = _RenderedBitmap(
                    width_px=bitmap.width,
                    height_px=bitmap.height,
                    stride=bitmap.stride,
                    pixels=bytes(bitmap.buffer),
                    mode=getattr(bitmap, "mode", "BGR"),
                    n_channels=getattr(bitmap, "n_channels", 3),
                )
                for candidate in candidates:
                    cropped = _crop_bbox_to_png_bytes(
                        rendered,
                        bbox=candidate.image.bbox,
                        page_height_pt=page_info.height_pt,
                        dpi=dpi,
                    )
                    if cropped is None:
                        continue
                    data, width_px, height_px = cropped
                    old_asset = doc_ir.assets.get(candidate.image.image_id)
                    doc_ir.assets[candidate.image.image_id] = ImageAsset.from_bytes(
                        data=data,
                        mime_type="image/png",
                        filename=old_asset.filename if old_asset is not None else None,
                        intrinsic_width_px=width_px,
                        intrinsic_height_px=height_px,
                    )
            finally:
                bitmap.close()
    finally:
        pdf_doc.close()

    return doc_ir


def _low_resolution_image_candidates(
    doc_ir: DocIR,
    *,
    threshold_px_per_pt: float,
) -> dict[int, list[_ImageCandidate]]:
    candidates_by_page: dict[int, list[_ImageCandidate]] = {}
    for paragraph, image in _iter_images(doc_ir.paragraphs):
        page_number = paragraph.page_number
        if page_number is None or image.bbox is None:
            continue
        px_per_pt = _asset_px_per_pt(doc_ir, image)
        if px_per_pt is None or px_per_pt >= threshold_px_per_pt:
            continue
        candidates_by_page.setdefault(page_number, []).append(
            _ImageCandidate(page_number=page_number, image=image)
        )
    return candidates_by_page


def _iter_images(paragraphs: Iterable[ParagraphIR]) -> Iterable[tuple[ParagraphIR, ImageIR]]:
    for paragraph in paragraphs:
        for node in paragraph.content:
            if isinstance(node, ImageIR):
                yield paragraph, node
            elif isinstance(node, TableIR):
                for cell in node.cells:
                    yield from _iter_images(cell.paragraphs)


def _asset_px_per_pt(doc_ir: DocIR, image: ImageIR) -> float | None:
    display_width_pt = image.display_width_pt
    display_height_pt = image.display_height_pt
    if not display_width_pt or not display_height_pt:
        return None

    asset = doc_ir.assets.get(image.image_id)
    if asset is None:
        return None

    width_px = asset.intrinsic_width_px
    height_px = asset.intrinsic_height_px
    if width_px is None or height_px is None:
        width_px, height_px = _image_dimensions_from_bytes(asset.bytes_data())
    if not width_px or not height_px:
        return None

    return min(width_px / display_width_pt, height_px / display_height_pt)


def _crop_bbox_to_png_bytes(
    bitmap: _RenderedBitmap,
    *,
    bbox: PdfBoundingBox | None,
    page_height_pt: float,
    dpi: int,
) -> tuple[bytes, int, int] | None:
    if bbox is None:
        return None

    left_px, right_px, top_px, bottom_px = _bbox_to_pixel_bounds(
        bbox=bbox,
        page_width_px=bitmap.width_px,
        page_height_px=bitmap.height_px,
        page_height_pt=page_height_pt,
        dpi=dpi,
    )
    if right_px <= left_px or bottom_px <= top_px:
        return None

    width_px = right_px - left_px
    height_px = bottom_px - top_px
    png_bytes = _encode_bitmap_crop_as_png(
        bitmap,
        left_px=left_px,
        top_px=top_px,
        width_px=width_px,
        height_px=height_px,
    )
    return png_bytes, width_px, height_px


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
    bottom_px = _clamp_px(int(round((page_height_pt - bbox.bottom_pt) * scale)), upper=page_height_px)
    return left_px, right_px, top_px, bottom_px


def _clamp_px(value: int, *, upper: int) -> int:
    return max(0, min(value, upper))


def _encode_bitmap_crop_as_png(
    bitmap: _RenderedBitmap,
    *,
    left_px: int,
    top_px: int,
    width_px: int,
    height_px: int,
) -> bytes:
    raw_rows = bytearray()
    color_type = 2
    for y in range(top_px, top_px + height_px):
        raw_rows.append(0)
        row_offset = y * bitmap.stride
        for x in range(left_px, left_px + width_px):
            idx = row_offset + (x * bitmap.n_channels)
            if bitmap.mode == "BGR":
                blue, green, red = bitmap.pixels[idx: idx + 3]
                raw_rows.extend((red, green, blue))
            elif bitmap.mode == "BGRA":
                blue, green, red, alpha = bitmap.pixels[idx: idx + 4]
                raw_rows.extend((red, green, blue, alpha))
                color_type = 6
            elif bitmap.mode == "RGB":
                raw_rows.extend(bitmap.pixels[idx: idx + 3])
            elif bitmap.mode == "RGBA":
                raw_rows.extend(bitmap.pixels[idx: idx + 4])
                color_type = 6
            else:
                blue, green, red = bitmap.pixels[idx: idx + 3]
                raw_rows.extend((red, green, blue))

    return _encode_png(
        width=width_px,
        height=height_px,
        color_type=color_type,
        raw_scanlines=bytes(raw_rows),
    )


def _encode_png(*, width: int, height: int, color_type: int, raw_scanlines: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", ihdr),
            chunk(b"IDAT", zlib.compress(raw_scanlines)),
            chunk(b"IEND", b""),
        ]
    )


__all__ = ["replace_low_resolution_pdf_image_assets"]
