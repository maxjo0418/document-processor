from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

THIS_DIR = Path(__file__).resolve().parent
SRC_ROOT = THIS_DIR.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from document_processor import DocIR, ParaStyleInfo, ParagraphIR, RunIR
from document_processor.pdf.config import PdfParseConfig
from document_processor.pdf.local_outputs import export_pdf_local_outputs
from document_processor.pdf.odl import (
    build_doc_ir_from_odl_result,
    convert_pdf_local,
    resolve_odl_jar_path,
)
from document_processor.pdf.pipeline import parse_pdf_to_doc_ir
from document_processor.pdf.parsing import PageClass, PageDecision, PageProfile, PdfProfile
from document_processor.pdf.preview.context import build_pdf_preview_context
from document_processor.pdf.preview.models import PdfPreviewContext
from document_processor.pdf.odl.adapter import _pdf_node_kwargs


class PdfPipelineTests(unittest.TestCase):
    def test_pdf_parse_config_does_not_expose_raster_border_enrichment(self) -> None:
        self.assertNotIn("infer_table_borders", PdfParseConfig.model_fields)
        self.assertNotIn("table_border_dpi", PdfParseConfig.model_fields)

    def test_pdf_parse_config_exposes_only_simple_public_options(self) -> None:
        self.assertEqual(
            set(PdfParseConfig.model_fields),
            {"pages", "include_header_footer", "image_quality", "image_output"},
        )

    def test_pdf_parse_config_rejects_odl_internal_options(self) -> None:
        with self.assertRaises(ValueError):
            PdfParseConfig.model_validate({"table_method": "default"})

    def test_build_doc_ir_from_odl_result_prefers_explicit_table_cell_border_css(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "kids": [
                {
                    "type": "table",
                    "page number": 1,
                    "bounding box": [10, 10, 110, 90],
                    "number of rows": 1,
                    "number of columns": 1,
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "page number": 1,
                                    "row number": 1,
                                    "column number": 1,
                                    "bounding box": [10, 10, 110, 90],
                                    "has top border": True,
                                    "has bottom border": True,
                                    "border top": "1.5px dotted #123456",
                                    "kids": [{"type": "paragraph", "content": "A1", "page number": 1}],
                                }
                            ]
                        }
                    ],
                }
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")

        style = doc.paragraphs[0].tables[0].cells[0].cell_style
        self.assertEqual(style.border_top, "1.5px dotted #123456")
        self.assertEqual(style.border_bottom, "1px solid")

    def test_build_doc_ir_from_odl_result_maps_table_continuation_ids_to_docir_table_ids(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "kids": [
                {
                    "type": "table",
                    "id": 7,
                    "page number": 1,
                    "bounding box": [10, 10, 110, 90],
                    "number of rows": 1,
                    "number of columns": 1,
                    "next table id": 11,
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "page number": 1,
                                    "row number": 1,
                                    "column number": 1,
                                    "bounding box": [10, 10, 110, 90],
                                    "kids": [{"type": "paragraph", "content": "A1", "page number": 1}],
                                }
                            ]
                        }
                    ],
                },
                {
                    "type": "table",
                    "id": 11,
                    "page number": 2,
                    "bounding box": [10, 10, 110, 90],
                    "number of rows": 1,
                    "number of columns": 1,
                    "previous table id": 7,
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "page number": 2,
                                    "row number": 1,
                                    "column number": 1,
                                    "bounding box": [10, 10, 110, 90],
                                    "kids": [{"type": "paragraph", "content": "B1", "page number": 2}],
                                }
                            ]
                        }
                    ],
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")

        first_table = doc.paragraphs[0].tables[0]
        second_table = doc.paragraphs[1].tables[0]
        self.assertEqual(first_table.next_table_id, second_table.node_id)
        self.assertIsNone(first_table.previous_table_id)
        self.assertEqual(second_table.previous_table_id, first_table.node_id)
        self.assertIsNone(second_table.next_table_id)
        self.assertNotEqual(first_table.next_table_id, "11")
        self.assertNotEqual(second_table.previous_table_id, "7")

    def test_parse_pdf_to_doc_ir_applies_preview_context_before_returning_doc(self) -> None:
        doc = DocIR(doc_id="sample", source_doc_type="pdf")
        preview_context = PdfPreviewContext()

        with patch(
            "document_processor.pdf.pipeline._parse_pdf_to_doc_ir_with_preview",
            return_value=(doc, preview_context),
        ), patch("document_processor.pdf.preview.normalize.enrich_pdf_doc_ir") as enrich_pdf:
            enrich_pdf.return_value = doc
            result = parse_pdf_to_doc_ir("sample.pdf")

        self.assertIs(result, doc)
        enrich_pdf.assert_called_once_with(doc, preview_context=preview_context)

    def test_pdf_docir_to_html_uses_common_html_renderer(self) -> None:
        doc = DocIR(
            doc_id="sample",
            source_doc_type="pdf",
            paragraphs=[
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", "s1.p1"),
                    content=[RunIR(**_pdf_node_kwargs("run", "s1.p1.r1"), text="PDF body")],
                )
            ],
        )

        with patch(
            "document_processor.pdf.preview.normalize.enrich_pdf_doc_ir",
        ) as enrich_pdf, patch(
            "document_processor.html_exporter.render_html_document",
            return_value="<html>common</html>",
        ) as render_html:
            html = doc.to_html(title="Preview", debug_layout=True)

        self.assertEqual(html, "<html>common</html>")
        enrich_pdf.assert_not_called()
        render_html.assert_called_once()
        self.assertEqual(render_html.call_args.args[0].source_doc_type, "pdf")
        self.assertEqual(render_html.call_args.kwargs["title"], "Preview")
        self.assertTrue(render_html.call_args.kwargs["debug_layout"])

    def test_docir_from_file_pdf_uses_common_builder_without_duplicate_style_extraction(self) -> None:
        built_doc = DocIR(
            doc_id="sample",
            source_path="sample.pdf",
            source_doc_type="pdf",
            paragraphs=[
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", "s1.p1"),
                    para_style=ParaStyleInfo(align="center"),
                )
            ],
        )
        with patch("document_processor.core.document_ir_parser.build_doc_ir_from_file", return_value=built_doc) as build_doc, patch(
            "document_processor.core.style_extractor.extract_styles",
        ) as extract_styles:
            result = DocIR.from_file(BytesIO(b"%PDF-1.7\n%fake"), doc_type="pdf")

        self.assertEqual(result.source_doc_type, "pdf")
        self.assertEqual(result.paragraphs[0].para_style.align, "center")
        build_doc.assert_called_once()
        parsed_path = build_doc.call_args.args[0]
        self.assertTrue(str(parsed_path).endswith(".pdf"))
        self.assertEqual(build_doc.call_args.kwargs["doc_type"], "pdf")
        extract_styles.assert_not_called()

    def test_docir_from_file_pdf_to_html_renders_preview_content(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "pages": [{"page number": 1, "width pt": 200, "height pt": 120}],
            "layout regions": [
                {
                    "region id": "p1-main",
                    "region type": "main",
                    "page number": 1,
                    "bounding box": [0, 0, 200, 120],
                }
            ],
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Preview body",
                    "page number": 1,
                    "layout region id": "p1-main",
                    "reading order index": 1,
                },
                {
                    "type": "table",
                    "page number": 1,
                    "layout region id": "p1-main",
                    "reading order index": 2,
                    "bounding box": [20, 30, 180, 90],
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "row number": 1,
                                    "column number": 1,
                                    "page number": 1,
                                    "kids": [{"type": "paragraph", "content": "A1", "page number": 1}],
                                }
                            ]
                        }
                    ],
                },
            ],
        }
        profile = PdfProfile(
            page_count=1,
            avg_chars_per_page=10.0,
            normal_text_ratio=1.0,
            text_readable=True,
            text_readable_page_ratio=1.0,
            page_profiles=[
                PageProfile(
                    page_number=1,
                    char_count=10,
                    normal_text_ratio=1.0,
                    replacement_char_ratio=0.0,
                    text_readable=True,
                    image_area_ratio=0.0,
                    image_area_in_content_ratio=0.0,
                    page_width_pt=200.0,
                    page_height_pt=120.0,
                )
            ],
        )

        with patch("document_processor.pdf.pipeline.probe_pdf", return_value=profile), patch(
            "document_processor.pdf.pipeline.run_odl_json", return_value=raw_document
        ):
            doc = DocIR.from_file(BytesIO(b"%PDF-1.7\n%fake"), doc_type="pdf")
            html = doc.to_html(title="Preview")

        self.assertIn("Preview body", html)
        self.assertEqual(html.count('<section class="document-page"'), 1)
        self.assertNotIn("document-region-band--columns", html)

    def test_build_doc_ir_from_odl_result_builds_paragraph_table_and_asset(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 2,
            "author": "Hancom",
            "title": "Sample PDF",
            "creation date": "2026-04-09T09:00:00Z",
            "modification date": "2026-04-09T10:00:00Z",
            "pages": [
                {"page number": 1, "width pt": 612, "height pt": 792},
                {"page number": 2, "width pt": 612, "height pt": 792},
            ],
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Hello PDF",
                    "page number": 1,
                    "id": 101,
                    "bounding box": [10, 20, 30, 40],
                    "layout region id": "p1-main",
                    "reading order index": 1,
                    "heading level": 2,
                    "font": "Noto Serif KR",
                    "text color": "#112233",
                    "font size": 11,
                },
                {
                    "type": "formula",
                    "content": "\\frac{a}{b}",
                    "page number": 1,
                    "id": 111,
                    "layout region id": "p1-main",
                    "reading order index": 2,
                },
                {
                    "type": "list",
                    "numbering style": "ordered",
                    "previous list id": 10,
                    "next list id": 12,
                    "list items": [
                        {
                            "type": "list item",
                            "content": "First item",
                            "page number": 1,
                            "id": 112,
                            "layout region id": "p1-main",
                            "reading order index": 3,
                        }
                    ],
                },
                {
                    "type": "table",
                    "page number": 2,
                    "id": 202,
                    "bounding box": [200, 210, 260, 310],
                    "layout region id": "p2-main",
                    "reading order index": 4,
                    "number of rows": 1,
                    "number of columns": 1,
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "id": 303,
                                    "row number": 1,
                                    "column number": 1,
                                    "page number": 2,
                                    "bounding box": [210, 220, 250, 300],
                                    "layout region id": "p2-main",
                                    "reading order index": 5,
                                    "has top border": True,
                                    "has bottom border": True,
                                    "has left border": True,
                                    "has right border": True,
                                    "kids": [
                                        {
                                            "type": "paragraph",
                                            "content": "A1",
                                            "page number": 2,
                                        }
                                    ],
                                }
                            ]
                        }
                    ],
                },
                {
                    "type": "image",
                    "page number": 2,
                    "bounding box": [300, 100, 420, 140],
                    "data": "data:image/png;base64,QUJD",
                    "width px": 120,
                    "height px": 40,
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")

        self.assertEqual(doc.source_doc_type, "pdf")
        self.assertIsNone(doc.meta)
        self.assertEqual([page.page_number for page in doc.pages], [1, 2])
        self.assertEqual(doc.paragraphs[0].native_anchor.debug_path, "s1.p1")
        self.assertEqual(doc.paragraphs[0].text, "Hello PDF")
        self.assertEqual(doc.paragraphs[0].bbox.left_pt, 10.0)
        self.assertIsNone(doc.paragraphs[0].meta)
        self.assertEqual(doc.paragraphs[0].runs[0].bbox.left_pt, 10.0)
        self.assertEqual(doc.paragraphs[0].runs[0].native_anchor.debug_path, "s1.p1.r1")
        self.assertIsNone(doc.paragraphs[0].runs[0].meta)
        self.assertEqual(doc.paragraphs[0].runs[0].run_style.font_family, "Noto Serif KR")
        self.assertEqual(doc.paragraphs[1].native_anchor.debug_path, "s1.p2")
        self.assertEqual(doc.paragraphs[1].text, "\\frac{a}{b}")
        self.assertIsNone(doc.paragraphs[1].meta)
        self.assertEqual(doc.paragraphs[2].native_anchor.debug_path, "s1.p3")
        self.assertEqual(doc.paragraphs[2].text, "First item")
        self.assertIsNone(doc.paragraphs[2].meta)
        self.assertEqual(doc.paragraphs[3].native_anchor.debug_path, "s1.p4")
        self.assertEqual(doc.paragraphs[3].tables[0].cells[0].text, "A1")
        self.assertEqual(doc.paragraphs[3].bbox.left_pt, 200.0)
        self.assertEqual(doc.paragraphs[3].tables[0].native_anchor.debug_path, "s1.p4.r1.tbl1")
        self.assertEqual(doc.paragraphs[3].tables[0].bbox.left_pt, 200.0)
        self.assertIsNone(doc.paragraphs[3].tables[0].meta)
        self.assertTrue(doc.paragraphs[3].tables[0].table_style.render_grid)
        self.assertEqual(doc.paragraphs[3].tables[0].cells[0].native_anchor.debug_path, "s1.p4.r1.tbl1.tr1.tc1")
        self.assertEqual(doc.paragraphs[3].tables[0].cells[0].bbox.left_pt, 210.0)
        self.assertIsNone(doc.paragraphs[3].tables[0].cells[0].meta)
        self.assertEqual(
            doc.paragraphs[3].tables[0].cells[0].paragraphs[0].native_anchor.debug_path,
            "s1.p4.r1.tbl1.tr1.tc1.p1",
        )
        self.assertEqual(doc.paragraphs[3].tables[0].cells[0].cell_style.border_top, "1px solid")
        self.assertEqual(doc.paragraphs[3].tables[0].cells[0].cell_style.border_right, "1px solid")
        self.assertIn("odl-img-p5", doc.assets)
        self.assertEqual(doc.paragraphs[4].native_anchor.debug_path, "s1.p5")
        self.assertEqual(doc.paragraphs[4].images[0].native_anchor.debug_path, "s1.p5.img1")
        self.assertEqual(doc.paragraphs[4].images[0].image_id, "odl-img-p5")
        self.assertEqual(doc.paragraphs[4].bbox.left_pt, 300.0)
        self.assertEqual(doc.paragraphs[4].images[0].bbox.left_pt, 300.0)
        self.assertFalse(hasattr(doc.paragraphs[4].images[0], "meta"))
        self.assertIsNone(doc.assets["odl-img-p5"].meta)

    def test_build_doc_ir_from_odl_result_preserves_text_whitespace_and_header_footer_children(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "kids": [
                {
                    "type": "paragraph",
                    "content": "  Hello PDF  ",
                    "page number": 1,
                    "id": 101,
                },
                {
                    "type": "paragraph",
                    "content": "   ",
                    "page number": 1,
                    "id": 102,
                },
                {
                    "type": "header",
                    "page number": 1,
                    "id": 201,
                    "kids": [
                        {
                            "type": "paragraph",
                            "content": "Header line",
                            "page number": 1,
                            "id": 202,
                            "font": "Noto Sans KR",
                            "font size": 9,
                        }
                    ],
                },
                {
                    "type": "table",
                    "page number": 1,
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "row number": 1,
                                    "column number": 1,
                                    "page number": 1,
                                    "kids": [
                                        {
                                            "type": "paragraph",
                                            "content": "  A1  ",
                                            "page number": 1,
                                        }
                                    ],
                                }
                            ]
                        }
                    ],
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")

        self.assertEqual(len(doc.paragraphs), 3)
        self.assertEqual(doc.paragraphs[0].text, "  Hello PDF  ")
        self.assertEqual(doc.paragraphs[1].text, "Header line")
        self.assertEqual(doc.paragraphs[1].page_number, 1)
        self.assertIsNone(doc.paragraphs[1].meta)
        self.assertIsNone(doc.paragraphs[1].runs[0].meta)
        self.assertEqual(doc.paragraphs[1].runs[0].run_style.font_family, "Noto Sans KR")
        self.assertEqual(doc.paragraphs[1].runs[0].run_style.size_pt, 9.0)
        self.assertEqual(doc.paragraphs[2].tables[0].cells[0].text, "  A1  ")
        self.assertEqual(doc.paragraphs[2].tables[0].cells[0].paragraphs[0].text, "  A1  ")

    def test_build_doc_ir_from_odl_result_uses_additive_spans_for_multi_run_text(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "kids": [
                {
                    "type": "heading",
                    "content": "Hello PDF",
                    "page number": 1,
                    "id": 101,
                    "heading level": 1,
                    "font": "Parent Font",
                    "font size": 18,
                    "text color": "#112233",
                    "spans": [
                        {
                            "type": "text chunk",
                            "content": "Hello",
                            "page number": 1,
                            "bounding box": [1, 2, 3, 4],
                            "font": "Span Font",
                            "font size": 19,
                            "text color": "#abcdef",
                            "font weight": 700,
                        },
                        {
                            "type": "text chunk",
                            "content": " ",
                            "page number": 1,
                            "bounding box": [3, 2, 4, 4],
                        },
                        {
                            "type": "text chunk",
                            "content": "PDF",
                            "page number": 1,
                            "bounding box": [4, 2, 6, 4],
                            "italic angle": 12,
                            "underline": True,
                        },
                    ],
                },
                {
                    "type": "table",
                    "page number": 1,
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "row number": 1,
                                    "column number": 1,
                                    "page number": 1,
                                    "kids": [
                                        {
                                            "type": "paragraph",
                                            "content": "A1",
                                            "page number": 1,
                                            "font": "Cell Font",
                                            "font size": 10,
                                            "spans": [
                                                {
                                                    "type": "text chunk",
                                                    "content": "A",
                                                    "page number": 1,
                                                    "font": "Cell Span Font",
                                                    "font size": 11,
                                                },
                                                {
                                                    "type": "text chunk",
                                                    "content": "1",
                                                    "page number": 1,
                                                },
                                            ],
                                        }
                                    ],
                                }
                            ]
                        }
                    ],
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")

        self.assertEqual(doc.paragraphs[0].text, "Hello PDF")
        self.assertEqual([run.text for run in doc.paragraphs[0].runs], ["Hello", " ", "PDF"])
        self.assertEqual(doc.paragraphs[0].runs[0].run_style.font_family, "Span Font")
        self.assertEqual(doc.paragraphs[0].runs[0].run_style.size_pt, 19.0)
        self.assertEqual(doc.paragraphs[0].runs[0].run_style.color, "#abcdef")
        self.assertTrue(doc.paragraphs[0].runs[0].run_style.bold)
        self.assertEqual(doc.paragraphs[0].runs[1].run_style.font_family, "Parent Font")
        self.assertEqual(doc.paragraphs[0].runs[1].run_style.size_pt, 18.0)
        self.assertTrue(doc.paragraphs[0].runs[2].run_style.italic)
        self.assertTrue(doc.paragraphs[0].runs[2].run_style.underline)
        self.assertIsNone(doc.paragraphs[0].runs[0].meta)
        self.assertEqual(doc.paragraphs[0].runs[0].bbox.left_pt, 1.0)
        self.assertEqual(doc.paragraphs[1].tables[0].cells[0].text, "A1")
        self.assertEqual(
            [run.text for run in doc.paragraphs[1].tables[0].cells[0].paragraphs[0].runs],
            ["A", "1"],
        )
        self.assertEqual(
            doc.paragraphs[1].tables[0].cells[0].paragraphs[0].runs[0].run_style.font_family,
            "Cell Span Font",
        )
        self.assertEqual(
            doc.paragraphs[1].tables[0].cells[0].paragraphs[0].runs[1].run_style.font_family,
            "Cell Font",
        )

    def test_build_doc_ir_from_odl_result_merges_adjacent_spans_with_same_style(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Hello World",
                    "page number": 1,
                    "font": "Base Font",
                    "font size": 12,
                    "text color": "#111111",
                    "spans": [
                        {
                            "type": "text chunk",
                            "content": "Hello",
                            "page number": 1,
                            "bounding box": [1, 2, 3, 4],
                            "font": "Base Font",
                            "font size": 12,
                            "text color": "#111111",
                        },
                        {
                            "type": "text chunk",
                            "content": " ",
                            "page number": 1,
                            "bounding box": [3, 2, 4, 4],
                        },
                        {
                            "type": "text chunk",
                            "content": "World",
                            "page number": 1,
                            "bounding box": [4, 2, 7, 4],
                            "font": "Base Font",
                            "font size": 12,
                            "text color": "#111111",
                        },
                    ],
                }
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")

        self.assertEqual(doc.paragraphs[0].text, "Hello World")
        self.assertEqual(len(doc.paragraphs[0].runs), 1)
        self.assertEqual(doc.paragraphs[0].runs[0].text, "Hello World")
        self.assertEqual(doc.paragraphs[0].runs[0].bbox.left_pt, 1.0)
        self.assertEqual(doc.paragraphs[0].runs[0].bbox.right_pt, 7.0)

    def test_build_doc_ir_from_odl_result_prefers_node_text_when_spans_flatten_newlines(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "kids": [
                {
                    "type": "paragraph",
                    "content": "64\n65\n66 68 69390",
                    "page number": 1,
                    "font": "Base Font",
                    "font size": 12,
                    "spans": [
                        {"type": "text chunk", "content": "64", "page number": 1},
                        {"type": "text chunk", "content": " ", "page number": 1},
                        {"type": "text chunk", "content": "65", "page number": 1},
                        {"type": "text chunk", "content": " ", "page number": 1},
                        {"type": "text chunk", "content": "66", "page number": 1},
                    ],
                }
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")

        self.assertEqual(doc.paragraphs[0].text, "64\n65\n66 68 69390")
        self.assertEqual(len(doc.paragraphs[0].runs), 1)
        self.assertEqual(doc.paragraphs[0].runs[0].text, "64\n65\n66 68 69390")

    def test_build_doc_ir_from_odl_result_keeps_soft_visual_wraps_as_spaces(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "kids": [
                {
                    "type": "paragraph",
                    "content": "First line Second line",
                    "page number": 1,
                    "font": "Base Font",
                    "font size": 12,
                    "spans": [
                        {
                            "type": "text chunk",
                            "content": "First line",
                            "page number": 1,
                            "bounding box": [10, 80, 58, 92],
                        },
                        {
                            "type": "text chunk",
                            "content": "Second line",
                            "page number": 1,
                            "bounding box": [10, 62, 72, 74],
                        },
                    ],
                }
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        html = doc.to_html()

        self.assertEqual(doc.paragraphs[0].text, "First line Second line")
        self.assertEqual([run.text for run in doc.paragraphs[0].runs], ["First line", " Second line"])
        self.assertNotIn("<br>Second line", html)

    def test_build_doc_ir_from_odl_result_expands_explicit_wide_space_spans(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "kids": [
                {
                    "type": "paragraph",
                    "content": "금 원정(\\ )",
                    "page number": 1,
                    "font": "Base Font",
                    "font size": 12,
                    "spans": [
                        {
                            "type": "text chunk",
                            "content": "금",
                            "page number": 1,
                            "bounding box": [10, 80, 20, 92],
                        },
                        {
                            "type": "text chunk",
                            "content": " ",
                            "page number": 1,
                            "bounding box": [20, 80, 80, 92],
                        },
                        {
                            "type": "text chunk",
                            "content": "원정(\\",
                            "page number": 1,
                            "bounding box": [80, 80, 112, 92],
                        },
                        {
                            "type": "text chunk",
                            "content": " ",
                            "page number": 1,
                            "bounding box": [112, 80, 172, 92],
                        },
                        {
                            "type": "text chunk",
                            "content": ")",
                            "page number": 1,
                            "bounding box": [172, 80, 176, 92],
                        },
                    ],
                }
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        html = doc.to_html()

        self.assertEqual(doc.paragraphs[0].text, "금          원정(\\          )")
        self.assertEqual([run.text for run in doc.paragraphs[0].runs], ["금", "          ", "원정(\\", "          ", ")"])
        self.assertNotIn("text-decoration:underline", html)
        self.assertIn("&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;", html)

    def test_parse_pdf_to_doc_ir_uses_probe_for_page_sizes_and_filters_scan_pages(self) -> None:
        profile = PdfProfile(
            page_count=3,
            avg_chars_per_page=30.0,
            normal_text_ratio=0.8,
            text_readable=True,
            text_readable_page_ratio=2 / 3,
            page_profiles=[
                PageProfile(
                    page_number=1,
                    char_count=0,
                    normal_text_ratio=0.0,
                    replacement_char_ratio=0.0,
                    text_readable=False,
                    image_area_ratio=1.0,
                    image_area_in_content_ratio=1.0,
                    page_width_pt=612.0,
                    page_height_pt=792.0,
                ),
                PageProfile(
                    page_number=2,
                    char_count=40,
                    normal_text_ratio=0.8,
                    replacement_char_ratio=0.0,
                    text_readable=True,
                    image_area_ratio=0.1,
                    image_area_in_content_ratio=0.1,
                    page_width_pt=612.0,
                    page_height_pt=792.0,
                ),
                PageProfile(
                    page_number=3,
                    char_count=35,
                    normal_text_ratio=0.7,
                    replacement_char_ratio=0.0,
                    text_readable=True,
                    image_area_ratio=0.1,
                    image_area_in_content_ratio=0.1,
                    page_width_pt=612.0,
                    page_height_pt=792.0,
                ),
            ],
        )
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 3,
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Structured page 2",
                    "page number": 2,
                },
                {
                    "type": "paragraph",
                    "content": "Structured page 3",
                    "page number": 3,
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n%fake")

            with patch("document_processor.pdf.pipeline.probe_pdf", return_value=profile):
                with patch("document_processor.pdf.pipeline.run_odl_json", return_value=raw_document) as run_odl, patch(
                    "document_processor.pdf.pipeline.build_pdf_preview_context"
                ) as build_preview_context:
                    build_preview_context.return_value = PdfPreviewContext()
                    doc = parse_pdf_to_doc_ir(pdf_path)

        self.assertEqual(run_odl.call_args.kwargs, {})
        self.assertEqual(run_odl.call_args.args[1]["pages"], [2, 3])
        self.assertEqual(run_odl.call_args.args[1]["image_output"], "embedded")
        self.assertEqual(build_preview_context.call_args.args, (raw_document,))
        self.assertEqual(build_preview_context.call_args.kwargs, {})
        self.assertEqual([page.page_number for page in doc.pages], [1, 2, 3])
        self.assertEqual([paragraph.page_number for paragraph in doc.paragraphs], [2, 3])
        self.assertIsNone(doc.meta)
        self.assertFalse(hasattr(doc, "get_pdf_preview_context"))

    def test_parse_pdf_to_doc_ir_maps_simple_public_config_to_odl_options(self) -> None:
        profile = PdfProfile(
            page_count=3,
            avg_chars_per_page=30.0,
            normal_text_ratio=0.8,
            text_readable=True,
            text_readable_page_ratio=1.0,
            page_profiles=[
                PageProfile(
                    page_number=1,
                    char_count=30,
                    normal_text_ratio=0.8,
                    replacement_char_ratio=0.0,
                    text_readable=True,
                    image_area_ratio=0.1,
                    image_area_in_content_ratio=0.1,
                    page_width_pt=612.0,
                    page_height_pt=792.0,
                ),
                PageProfile(
                    page_number=2,
                    char_count=40,
                    normal_text_ratio=0.8,
                    replacement_char_ratio=0.0,
                    text_readable=True,
                    image_area_ratio=0.1,
                    image_area_in_content_ratio=0.1,
                    page_width_pt=612.0,
                    page_height_pt=792.0,
                ),
                PageProfile(
                    page_number=3,
                    char_count=35,
                    normal_text_ratio=0.7,
                    replacement_char_ratio=0.0,
                    text_readable=True,
                    image_area_ratio=0.1,
                    image_area_in_content_ratio=0.1,
                    page_width_pt=612.0,
                    page_height_pt=792.0,
                ),
            ],
        )
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 3,
            "kids": [{"type": "paragraph", "content": "Structured page 2", "page number": 2}],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n%fake")

            with patch("document_processor.pdf.pipeline.probe_pdf", return_value=profile):
                with patch("document_processor.pdf.pipeline.run_odl_json", return_value=raw_document) as run_odl, patch(
                    "document_processor.pdf.pipeline.build_pdf_preview_context"
                ) as build_preview_context:
                    build_preview_context.return_value = PdfPreviewContext()
                    parse_pdf_to_doc_ir(
                        pdf_path,
                        config={
                            "pages": "2-3",
                            "include_header_footer": True,
                            "image_quality": "max",
                            "image_output": "off",
                        },
                    )

        odl_config = run_odl.call_args.args[1]
        self.assertEqual(odl_config["pages"], [2, 3])
        self.assertEqual(odl_config["include_header_footer"], True)
        self.assertEqual(odl_config["image_quality"], "max")
        self.assertEqual(odl_config["image_output"], "off")

    def test_resolve_odl_jar_path_uses_vendored_jar(self) -> None:
        jar_path = resolve_odl_jar_path()

        self.assertTrue(jar_path.exists())
        self.assertEqual(jar_path.name, "opendataloader-pdf-cli-2.2.1.jar")

    def test_convert_pdf_local_runs_vendored_jar_and_returns_expected_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n%fake")
            output_dir = Path(tmp_dir) / "out"

            def fake_run(command, **kwargs):
                self.assertEqual(
                    command[:4],
                    ["java", "-Djava.awt.headless=true", "-jar", str(resolve_odl_jar_path())],
                )
                self.assertIn("--format", command)
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "sample.json").write_text('{"ok": true}', encoding="utf-8")
                (output_dir / "sample.html").write_text("<p>ok</p>", encoding="utf-8")
                (output_dir / "sample.md").write_text("# ok", encoding="utf-8")
                return None

            with patch("document_processor.pdf.odl.runner.subprocess.run", side_effect=fake_run) as run_cli:
                outputs = convert_pdf_local(
                    pdf_path,
                    output_dir=output_dir,
                    formats=["json", "html", "markdown"],
                    config={"pages": [2, 3]},
                )

        run_cli.assert_called_once()
        self.assertEqual(outputs["json"].name, "sample.json")
        self.assertEqual(outputs["html"].name, "sample.html")
        self.assertEqual(outputs["markdown"].name, "sample.md")

    def test_convert_pdf_local_passes_simple_image_quality_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n%fake")
            output_dir = Path(tmp_dir) / "out"

            def fake_run(command, **kwargs):
                self.assertIn("--image-pixel-size", command)
                self.assertEqual(command[command.index("--image-pixel-size") + 1], "2400")
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "sample.json").write_text('{"ok": true}', encoding="utf-8")
                return None

            with patch("document_processor.pdf.odl.runner.subprocess.run", side_effect=fake_run):
                outputs = convert_pdf_local(
                    pdf_path,
                    output_dir=output_dir,
                    formats=["json"],
                    config={"image_quality": "high"},
                )

        self.assertEqual(outputs["json"].name, "sample.json")

    def test_export_pdf_local_outputs_returns_readable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "native"

            def fake_convert(path, *, output_dir, formats, config):
                self.assertEqual(Path(path).suffix, ".pdf")
                self.assertEqual(list(formats), ["json", "html", "markdown"])
                output_dir.mkdir(parents=True, exist_ok=True)
                json_path = output_dir / "sample.json"
                html_path = output_dir / "sample.html"
                markdown_path = output_dir / "sample.md"
                json_path.write_text('{"source": "odl"}', encoding="utf-8")
                html_path.write_text("<article>native</article>", encoding="utf-8")
                markdown_path.write_text("# native", encoding="utf-8")
                return {
                    "json": json_path,
                    "html": html_path,
                    "markdown": markdown_path,
                }

            with patch(
                "document_processor.pdf.local_outputs.convert_pdf_local",
                side_effect=fake_convert,
            ):
                outputs = export_pdf_local_outputs(
                    BytesIO(b"%PDF-1.7\n%fake"),
                    output_dir=output_dir,
                )

                self.assertEqual(outputs.read_json()["source"], "odl")
                self.assertEqual(outputs.read_text("html"), "<article>native</article>")
                self.assertEqual(outputs.read_text("markdown"), "# native")
                self.assertEqual(outputs.html_path.name, "sample.html")
                self.assertEqual(outputs.markdown_path.name, "sample.md")
