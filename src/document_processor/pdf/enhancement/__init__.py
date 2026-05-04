from .border_inference import (
    RenderedPdfColorPage,
    infer_cell_background_from_rendered_page,
    render_pdf_pages_to_color,
)
from .enrichment import enrich_pdf_table_backgrounds
from .image_fallback import replace_low_resolution_pdf_image_assets

__all__ = [
    "RenderedPdfColorPage",
    "enrich_pdf_table_backgrounds",
    "infer_cell_background_from_rendered_page",
    "replace_low_resolution_pdf_image_assets",
    "render_pdf_pages_to_color",
]
