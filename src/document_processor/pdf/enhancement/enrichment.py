"""PDF-specific post-processing to enrich DocIR render fidelity."""

from __future__ import annotations

from pathlib import Path

from ...models import DocIR, ParagraphIR, TableCellIR, TableIR
from ...style_types import CellStyleInfo
from .border_inference import (
    infer_cell_background_from_rendered_page,
    render_pdf_pages_to_color,
)


def enrich_pdf_table_backgrounds(
    doc_ir: DocIR,
    *,
    pdf_path: str | Path | None = None,
    dpi: int = 96,
) -> DocIR:
    if (doc_ir.source_doc_type or "").lower() != "pdf":
        return doc_ir

    resolved_pdf_path = Path(pdf_path or doc_ir.source_path or "").expanduser()
    if not resolved_pdf_path.exists():
        return doc_ir

    page_heights = {
        page.page_number: page.height_pt
        for page in doc_ir.pages
        if page.height_pt is not None
    }
    candidates = list(_iter_table_cell_candidates(doc_ir, page_heights=page_heights))
    if not candidates:
        return doc_ir

    rendered_pages = render_pdf_pages_to_color(
        resolved_pdf_path,
        page_numbers={candidate[0] for candidate in candidates},
        dpi=dpi,
    )

    for page_number, cell, page_height_pt in candidates:
        rendered_page = rendered_pages.get(page_number)
        cell_meta = getattr(cell, "meta", None)
        cell_bbox = getattr(cell, "bbox", None) or getattr(cell_meta, "bounding_box", None)
        if rendered_page is None or cell_bbox is None:
            continue

        inferred = infer_cell_background_from_rendered_page(
            rendered_page,
            bbox=cell_bbox,
            page_height_pt=page_height_pt,
            dpi=dpi,
        )
        if inferred is None:
            continue

        style = cell.cell_style.model_copy(deep=True) if cell.cell_style is not None else CellStyleInfo()
        if style.background is None:
            style.background = inferred
            cell.cell_style = style

    return doc_ir


def _iter_table_cell_candidates(
    doc_ir: DocIR,
    *,
    page_heights: dict[int, float],
):
    for paragraph in doc_ir.paragraphs:
        yield from _iter_paragraph_table_candidates(paragraph, page_heights=page_heights)


def _iter_paragraph_table_candidates(
    paragraph: ParagraphIR,
    *,
    page_heights: dict[int, float],
):
    for table in paragraph.tables:
        yield from _iter_table_candidates(
            table,
            page_number=paragraph.page_number,
            page_heights=page_heights,
        )


def _iter_table_candidates(
    table: TableIR,
    *,
    page_number: int | None,
    page_heights: dict[int, float],
):
    for cell in table.cells:
        cell_bbox = getattr(cell, "bbox", None)
        page_height_pt = page_heights.get(page_number)
        if page_number is not None and cell_bbox is not None and page_height_pt is not None:
            yield page_number, cell, page_height_pt
        for paragraph in cell.paragraphs:
            yield from _iter_paragraph_table_candidates(paragraph, page_heights=page_heights)


__all__ = ["enrich_pdf_table_backgrounds"]
