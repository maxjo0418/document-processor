from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

THIS_DIR = Path(__file__).resolve().parent
SRC_ROOT = THIS_DIR.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from document_processor import DocIR, PageInfo, ParagraphIR, TableCellIR, TableIR
import document_processor.pdf.enhancement as pdf_enhancement
from document_processor.pdf.enhancement import (
    RenderedPdfColorPage,
    enrich_pdf_table_backgrounds,
    infer_cell_background_from_rendered_page,
)
from document_processor.pdf.meta import PdfBoundingBox
from document_processor.pdf.preview import enrich_pdf_doc_ir
from document_processor.pdf.preview.models import PdfPreviewContext
from document_processor.style_types import TableStyleInfo
from document_processor.pdf.odl.adapter import _pdf_node_kwargs


def _make_test_color_page(*, width: int = 40, height: int = 40) -> RenderedPdfColorPage:
    pixels = bytearray([255] * (width * height * 3))

    def set_pixel(x: int, y: int, *, red: int, green: int, blue: int) -> None:
        idx = (y * width * 3) + (x * 3)
        pixels[idx] = blue
        pixels[idx + 1] = green
        pixels[idx + 2] = red

    for y in range(10, 30):
        for x in range(10, 30):
            set_pixel(x, y, red=223, green=230, blue=247)

    for y in range(16, 24):
        for x in range(16, 24):
            set_pixel(x, y, red=40, green=40, blue=40)

    return RenderedPdfColorPage(
        width_px=width,
        height_px=height,
        stride=width * 3,
        pixels=bytes(pixels),
    )


class PdfEnrichmentTests(unittest.TestCase):
    def test_pdf_enhancement_does_not_export_table_border_enrichment(self) -> None:
        self.assertFalse(hasattr(pdf_enhancement, "enrich_pdf_table_borders"))
        self.assertFalse(hasattr(pdf_enhancement, "RenderedPdfPage"))
        self.assertFalse(hasattr(pdf_enhancement, "infer_cell_borders_from_rendered_page"))
        self.assertFalse(hasattr(pdf_enhancement, "render_pdf_pages_to_grayscale"))

    def test_infer_cell_background_from_rendered_page_detects_fill_color(self) -> None:
        rendered_page = _make_test_color_page()

        inferred = infer_cell_background_from_rendered_page(
            rendered_page,
            bbox=PdfBoundingBox(left_pt=10.0, bottom_pt=10.0, right_pt=30.0, top_pt=30.0),
            page_height_pt=40.0,
            dpi=72,
        )

        self.assertEqual(inferred, "#dfe6f7")

    def test_enrich_pdf_table_backgrounds_applies_inferred_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "example.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n%fake")

            doc = DocIR(
                source_doc_type="pdf",
                source_path=str(pdf_path),
                pages=[PageInfo(page_number=1, width_pt=40.0, height_pt=40.0)],
                paragraphs=[
                    ParagraphIR(
                        **_pdf_node_kwargs("paragraph", "p1"),
                        page_number=1,
                        content=[
                            TableIR(
                                **_pdf_node_kwargs("table", "p1.tbl1"),
                                table_style=TableStyleInfo(render_grid=True),
                                cells=[
                                    TableCellIR(
                                        **_pdf_node_kwargs("cell", "p1.tbl1.tr1.tc1"),
                                        row_index=1,
                                        col_index=1,
                                        bbox=PdfBoundingBox(
                                            left_pt=10.0,
                                            bottom_pt=10.0,
                                            right_pt=30.0,
                                            top_pt=30.0,
                                        ),
                                    )
                                ],
                            )
                        ],
                    )
                ],
            )

            with patch(
                "document_processor.pdf.enhancement.enrichment.render_pdf_pages_to_color",
                return_value={1: _make_test_color_page()},
            ):
                enrich_pdf_table_backgrounds(doc, pdf_path=pdf_path, dpi=72)

        cell_style = doc.paragraphs[0].tables[0].cells[0].cell_style
        self.assertIsNotNone(cell_style)
        self.assertEqual(cell_style.background, "#dfe6f7")

    def test_enrich_pdf_doc_ir_enriches_pdf_table_backgrounds_by_default(self) -> None:
        doc = DocIR(source_doc_type="pdf", source_path="/tmp/example.pdf")

        with patch("document_processor.pdf.preview.normalize.enrich_pdf_table_backgrounds") as enrich_backgrounds:
            enrich_pdf_doc_ir(doc)

        enrich_backgrounds.assert_called_once_with(doc)

    def test_docir_to_html_routes_pdf_through_common_renderer(self) -> None:
        doc = DocIR(source_doc_type="pdf", source_path="/tmp/example.pdf")

        with patch("document_processor.html_exporter.render_html_document", return_value="<html>preview</html>") as render_html:
            html = doc.to_html()

        self.assertEqual(html, "<html>preview</html>")
        render_html.assert_called_once()
        self.assertEqual(render_html.call_args.args[0].source_doc_type, "pdf")
