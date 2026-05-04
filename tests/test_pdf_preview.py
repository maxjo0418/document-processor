from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

THIS_DIR = Path(__file__).resolve().parent
SRC_ROOT = THIS_DIR.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from document_processor.models import ParagraphIR, RunIR
from document_processor.pdf.odl import build_doc_ir_from_odl_result
from document_processor.pdf.meta import PdfBoundingBox
from document_processor.pdf.preview.analyze import (
    _build_visual_block_candidates,
    _connected_line_components,
    _extract_pdfium_visual_primitives,
)
from document_processor.pdf.preview.context import build_pdf_preview_context, collect_pdfium_visual_block_candidates
from document_processor.pdf.preview.models import (
    PdfPreviewVisualBlockCandidate,
    PdfPreviewVisualPrimitive,
)
from document_processor.pdf.preview.normalize import _is_arrow_connector_paragraph, enrich_pdf_doc_ir


class PdfPreviewTests(unittest.TestCase):
    def test_build_pdf_preview_context_collects_layout_regions_and_table_geometry(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "layout regions": [
                {
                    "region id": "p1-band1-left-column",
                    "region type": "left-column",
                    "page number": 1,
                    "bounding box": [0, 0, 120, 200],
                },
                {
                    "region id": "p1-band1-right-column",
                    "region type": "right-column",
                    "page number": 1,
                    "bounding box": [130, 0, 250, 200],
                },
            ],
            "kids": [
                {
                    "type": "table",
                    "page number": 1,
                    "layout region id": "p1-band1-left-column",
                    "reading order index": 3,
                    "bounding box": [10, 20, 110, 120],
                    "grid row boundaries": [120, 90, 60],
                    "grid column boundaries": [10, 60, 110],
                    "serialized cell count": 4,
                    "logical cell count": 4,
                    "covered logical cell count": 4,
                    "rows": [],
                    "line arts": [
                        {
                            "bounding box": [10, 20, 110, 21],
                        }
                    ],
                }
            ],
        }

        context = build_pdf_preview_context(raw_document)

        self.assertEqual(len(context.layout_regions), 2)
        self.assertEqual(context.layout_regions[0].region_id, "p1-band1-left-column")
        self.assertEqual(context.tables[0].grid_column_boundaries, [10.0, 60.0, 110.0])
        self.assertNotIn("visual_primitives", context.model_dump())

    def test_collect_pdfium_visual_block_candidates_does_not_create_layout_regions(self) -> None:
        class FakePdfDocument:
            def __len__(self) -> int:
                return 1

            def __getitem__(self, index: int) -> object:
                return object()

            def close(self) -> None:
                return None

        candidate = PdfPreviewVisualBlockCandidate(
            page_number=1,
            candidate_type="axis_box",
            bounding_box=PdfBoundingBox(left_pt=10, bottom_pt=20, right_pt=110, top_pt=120),
        )

        with patch.dict(
            sys.modules,
            {"pypdfium2": SimpleNamespace(PdfDocument=lambda path: FakePdfDocument())},
        ), patch(
            "document_processor.pdf.preview.context._extract_pdfium_visual_primitives",
            return_value=[object()],
        ), patch(
            "document_processor.pdf.preview.context._build_visual_block_candidates",
            return_value=[candidate],
        ):
            candidates = collect_pdfium_visual_block_candidates(pdf_path="sample.pdf", page_numbers=[1])

        self.assertEqual(candidates, [candidate])

    def test_enrich_pdf_doc_ir_preserves_missing_page_margins_when_pdf_margin_metadata_missing(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "pages": [
                {"page number": 1, "width pt": 200, "height pt": 250},
            ],
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Body",
                    "page number": 1,
                    "reading order index": 1,
                }
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        context = build_pdf_preview_context(raw_document)

        enrich_pdf_doc_ir(doc, preview_context=context)

        self.assertEqual(len(doc.pages), 1)
        self.assertEqual(
            (
                doc.pages[0].margin_left_pt,
                doc.pages[0].margin_right_pt,
                doc.pages[0].margin_top_pt,
                doc.pages[0].margin_bottom_pt,
            ),
            (None, None, None, None),
        )

    def test_enrich_pdf_doc_ir_reuses_known_column_grid_for_single_sided_column_region(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 2,
            "pages": [
                {"page number": 1, "width pt": 600, "height pt": 800},
                {"page number": 2, "width pt": 600, "height pt": 800},
            ],
            "layout regions": [
                {
                    "region id": "p1-left",
                    "region type": "left-column",
                    "page number": 1,
                    "bounding box": [72, 300, 286, 740],
                },
                {
                    "region id": "p1-right",
                    "region type": "right-column",
                    "page number": 1,
                    "bounding box": [308, 300, 522, 740],
                },
                {
                    "region id": "p2-left",
                    "region type": "left-column",
                    "page number": 2,
                    "bounding box": [72, 250, 287, 740],
                },
            ],
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Left column page one",
                    "page number": 1,
                    "reading order index": 1,
                    "bounding box": [72, 600, 286, 720],
                },
                {
                    "type": "paragraph",
                    "content": "Right column page one",
                    "page number": 1,
                    "reading order index": 2,
                    "bounding box": [308, 600, 522, 720],
                },
                {
                    "type": "paragraph",
                    "content": "Left column page two",
                    "page number": 2,
                    "reading order index": 3,
                    "bounding box": [72, 620, 287, 720],
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        context = build_pdf_preview_context(raw_document)

        enrich_pdf_doc_ir(doc, preview_context=context)

        page_two_paragraph = next(paragraph for paragraph in doc.paragraphs if paragraph.page_number == 2)
        self.assertIsNotNone(page_two_paragraph.para_style)
        self.assertIsNotNone(page_two_paragraph.para_style.column_layout)
        self.assertEqual(page_two_paragraph.para_style.column_layout.count, 2)
        self.assertEqual(page_two_paragraph.para_style.column_layout.column_index, 0)
        self.assertEqual(page_two_paragraph.para_style.column_layout.widths_pt, [214.0, 214.0])
        self.assertEqual(page_two_paragraph.para_style.column_layout.gap_pt, 22.0)

    def test_enrich_pdf_doc_ir_keeps_consecutive_image_strips_as_separate_paragraphs(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "pages": [
                {"page number": 1, "width pt": 300, "height pt": 400},
            ],
            "kids": [
                {
                    "type": "image",
                    "page number": 1,
                    "bounding box": [100, 260, 220, 264],
                    "data": "data:image/png;base64,AAAA",
                    "width px": 120,
                    "height px": 4,
                },
                {
                    "type": "image",
                    "page number": 1,
                    "bounding box": [100, 256, 220, 260],
                    "data": "data:image/png;base64,AAAA",
                    "width px": 120,
                    "height px": 4,
                },
                {
                    "type": "image",
                    "page number": 1,
                    "bounding box": [100, 252, 220, 256],
                    "data": "data:image/png;base64,AAAA",
                    "width px": 120,
                    "height px": 4,
                },
                {
                    "type": "paragraph",
                    "content": "Figure caption",
                    "page number": 1,
                    "reading order index": 4,
                    "bounding box": [100, 236, 220, 246],
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        context = build_pdf_preview_context(raw_document)

        enrich_pdf_doc_ir(doc, preview_context=context)

        self.assertEqual(len(doc.paragraphs), 4)
        self.assertIn("Figure caption", [paragraph.text.strip() for paragraph in doc.paragraphs])
        image_paragraphs = [paragraph for paragraph in doc.paragraphs if len(paragraph.images) == 1]
        self.assertEqual(len(image_paragraphs), 3)

    def test_enrich_pdf_doc_ir_includes_arrow_connector_in_layout_row(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "pages": [{"page number": 1, "width pt": 200, "height pt": 200}],
            "kids": [
                {
                    "type": "table",
                    "page number": 1,
                    "bounding box": [10, 100, 60, 140],
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
                                    "bounding box": [10, 100, 60, 140],
                                    "kids": [{"type": "paragraph", "content": "요건검토", "page number": 1}],
                                }
                            ]
                        }
                    ],
                },
                {
                    "type": "paragraph",
                    "content": "ð",
                    "page number": 1,
                    "bounding box": [70, 112, 78, 128],
                    "spans": [
                        {
                            "type": "text chunk",
                            "content": "ð",
                            "page number": 1,
                            "bounding box": [70, 112, 78, 128],
                        }
                    ],
                },
                {
                    "type": "table",
                    "page number": 1,
                    "bounding box": [90, 100, 140, 140],
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
                                    "bounding box": [90, 100, 140, 140],
                                    "kids": [{"type": "paragraph", "content": "서류평가", "page number": 1}],
                                }
                            ]
                        }
                    ],
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        context = build_pdf_preview_context(raw_document)

        enrich_pdf_doc_ir(doc, preview_context=context)

        layout_rows = [
            paragraph.tables[0]
            for paragraph in doc.paragraphs
            if paragraph.tables
            and paragraph.tables[0].table_style is not None
            and paragraph.tables[0].table_style.render_grid is False
            and paragraph.tables[0].row_count == 1
            and paragraph.tables[0].col_count >= 2
        ]
        self.assertEqual(len(layout_rows), 1)
        self.assertEqual([cell.text for cell in layout_rows[0].cells], ["요건검토", "ð", "서류평가"])

    def test_pdf_layout_row_arrow_connector_whitelist_includes_directional_arrows(self) -> None:
        connectors = ("->", "<-", "→", "←", "↑", "↓", "↔", "↕", "➡", "⬅", "⬆", "⬇", "⇧", "⇩", "ð", "ï")
        for connector in connectors:
            with self.subTest(connector=connector):
                paragraph = ParagraphIR(text=connector, content=[RunIR(text=connector)])

                self.assertTrue(_is_arrow_connector_paragraph(paragraph))

    def test_extract_pdfium_visual_primitives_collects_box_metadata(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                object_type: int,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 1,
            ) -> None:
                self.object_type = object_type
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float = 100.0, height: float = 100.0) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = 1
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        rectangle = _FakeObject(
            _FakeRawObject(
                object_type=_FakeRawModule.FPDF_PAGEOBJ_PATH,
                fill=(230, 235, 255, 255),
                stroke=(40, 40, 40, 255),
                stroke_width=1.5,
                segments=[
                    _FakeSegment(_FakeRawModule.FPDF_SEGMENT_MOVETO, 10.0, 10.0),
                    _FakeSegment(_FakeRawModule.FPDF_SEGMENT_LINETO, 60.0, 10.0),
                    _FakeSegment(_FakeRawModule.FPDF_SEGMENT_LINETO, 60.0, 40.0),
                    _FakeSegment(_FakeRawModule.FPDF_SEGMENT_LINETO, 10.0, 40.0, close=True),
                ],
            ),
            (10.0, 10.0, 60.0, 40.0),
        )

        primitives = _extract_pdfium_visual_primitives(
            _FakePage([rectangle]),
            page_number=3,
            raw_module=_FakeRawModule,
        )

        self.assertEqual(len(primitives), 4)
        self.assertTrue(all(primitive.page_number == 3 for primitive in primitives))
        self.assertEqual(
            {primitive.object_type for primitive in primitives},
            {"axis_box_edge_horizontal", "axis_box_edge_vertical"},
        )
        self.assertTrue(all(primitive.stroke_color == "#282828ff" for primitive in primitives))
        self.assertTrue(all((primitive.stroke_width_pt or 0.0) == 1.5 for primitive in primitives))
        self.assertTrue(all(primitive.has_stroke for primitive in primitives))
        self.assertTrue(all(not primitive.has_fill for primitive in primitives))
        self.assertEqual(
            {frozenset(primitive.candidate_roles) for primitive in primitives},
            {frozenset({"horizontal_line_segment"}), frozenset({"vertical_line_segment"})},
        )

    def test_extract_pdfium_visual_primitives_drops_fill_only_white_rectangle(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 1,
                stroke_mode: int = 0,
            ) -> None:
                self.object_type = 1
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode
                self.stroke_mode = stroke_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float = 100.0, height: float = 100.0) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = obj_raw.stroke_mode
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        white_fill_only_box = _FakeObject(
            _FakeRawObject(
                fill=(255, 255, 255, 255),
                stroke=(0, 0, 0, 255),
                stroke_width=0.5,
                fill_mode=1,
                stroke_mode=0,
                segments=[
                    _FakeSegment(2, 10.0, 10.0),
                    _FakeSegment(0, 70.0, 10.0),
                    _FakeSegment(0, 70.0, 30.0),
                    _FakeSegment(0, 10.0, 30.0, close=True),
                ],
            ),
            (10.0, 10.0, 70.0, 30.0),
        )

        primitives = _extract_pdfium_visual_primitives(
            _FakePage([white_fill_only_box]),
            page_number=1,
            raw_module=_FakeRawModule,
        )

        self.assertEqual(len(primitives), 0)
        self.assertEqual(_build_visual_block_candidates(primitives), [])

    def test_extract_pdfium_visual_primitives_drops_white_stroke_only_box(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 0,
                stroke_mode: int = 1,
            ) -> None:
                self.object_type = 1
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode
                self.stroke_mode = stroke_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float = 100.0, height: float = 100.0) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = obj_raw.stroke_mode
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        white_stroke_box = _FakeObject(
            _FakeRawObject(
                fill=(0, 0, 0, 0),
                stroke=(255, 255, 255, 255),
                stroke_width=0.5,
                fill_mode=0,
                stroke_mode=1,
                segments=[
                    _FakeSegment(2, 10.0, 10.0),
                    _FakeSegment(0, 70.0, 10.0),
                    _FakeSegment(0, 70.0, 30.0),
                    _FakeSegment(0, 10.0, 30.0, close=True),
                ],
            ),
            (10.0, 10.0, 70.0, 30.0),
        )

        primitives = _extract_pdfium_visual_primitives(
            _FakePage([white_stroke_box]),
            page_number=1,
            raw_module=_FakeRawModule,
        )

        self.assertEqual(len(primitives), 0)
        self.assertEqual(_build_visual_block_candidates(primitives), [])

    def test_extract_pdfium_visual_primitives_keeps_only_line_roles(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 0,
            ) -> None:
                self.object_type = 1
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float, height: float) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = 1
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        box = _FakeObject(
            _FakeRawObject(
                fill=(255, 255, 255, 255),
                stroke=(0, 0, 0, 255),
                stroke_width=1.0,
                fill_mode=1,
                segments=[
                    _FakeSegment(2, 10.0, 10.0),
                    _FakeSegment(0, 60.0, 10.0),
                    _FakeSegment(0, 60.0, 40.0),
                    _FakeSegment(0, 10.0, 40.0, close=True),
                ],
            ),
            (10.0, 10.0, 60.0, 40.0),
        )
        attached_rule = _FakeObject(
            _FakeRawObject(
                fill=(0, 0, 0, 0),
                stroke=(0, 0, 0, 255),
                stroke_width=1.0,
                segments=[
                    _FakeSegment(2, 60.0, 18.0),
                    _FakeSegment(0, 90.0, 18.0),
                ],
            ),
            (60.0, 17.5, 90.0, 18.5),
        )
        closed_shape = _FakeObject(
            _FakeRawObject(
                fill=(200, 200, 200, 255),
                stroke=(0, 0, 0, 255),
                stroke_width=1.0,
                fill_mode=1,
                segments=[
                    _FakeSegment(2, 100.0, 20.0),
                    _FakeSegment(0, 120.0, 20.0),
                    _FakeSegment(0, 110.0, 38.0, close=True),
                ],
            ),
            (100.0, 20.0, 120.0, 38.0),
        )
        long_vertical = _FakeObject(
            _FakeRawObject(
                fill=(0, 0, 0, 0),
                stroke=(0, 0, 0, 255),
                stroke_width=1.0,
                segments=[
                    _FakeSegment(2, 150.0, 5.0),
                    _FakeSegment(0, 150.0, 165.0),
                ],
            ),
            (149.5, 5.0, 150.5, 165.0),
        )
        long_horizontal = _FakeObject(
            _FakeRawObject(
                fill=(0, 0, 0, 0),
                stroke=(0, 0, 0, 255),
                stroke_width=1.0,
                segments=[
                    _FakeSegment(2, 10.0, 180.0),
                    _FakeSegment(0, 190.0, 180.0),
                ],
            ),
            (10.0, 179.5, 190.0, 180.5),
        )

        primitives = _extract_pdfium_visual_primitives(
            _FakePage([box, attached_rule, closed_shape, long_vertical, long_horizontal], width=200.0, height=200.0),
            page_number=1,
            raw_module=_FakeRawModule,
        )

        roles = {primitive.draw_order: set(primitive.candidate_roles) for primitive in primitives}
        self.assertEqual(roles[1], {"horizontal_line_segment"})
        self.assertNotIn(2, roles)
        self.assertEqual(roles[3], {"long_vertical_rule", "vertical_line_segment"})
        self.assertEqual(roles[4], {"long_horizontal_rule", "horizontal_line_segment"})
        edge_roles = [
            set(primitive.candidate_roles)
            for primitive in primitives
            if primitive.object_type in {"axis_box_edge_horizontal", "axis_box_edge_vertical"}
        ]
        self.assertEqual(len(edge_roles), 4)
        self.assertEqual(
            {frozenset(role_set) for role_set in edge_roles},
            {frozenset({"horizontal_line_segment"}), frozenset({"vertical_line_segment"})},
        )

    def test_extract_pdfium_visual_primitives_promotes_segmented_rule(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 0,
            ) -> None:
                self.object_type = 1
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float, height: float) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = 1
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        primitives = _extract_pdfium_visual_primitives(
            _FakePage(
                [
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 255, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 10.0, 20.0), _FakeSegment(0, 14.0, 20.0)],
                        ),
                        (10.0, 19.5, 14.0, 20.5),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 255, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 16.0, 20.0), _FakeSegment(0, 20.0, 20.0)],
                        ),
                        (16.0, 19.5, 20.0, 20.5),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 255, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 22.0, 20.0), _FakeSegment(0, 26.0, 20.0)],
                        ),
                        (22.0, 19.5, 26.0, 20.5),
                    ),
                ],
                width=100.0,
                height=100.0,
            ),
            page_number=4,
            raw_module=_FakeRawModule,
        )

        self.assertEqual(len(primitives), 1)
        self.assertEqual(primitives[0].object_type, "segmented_horizontal_rule")
        self.assertEqual(
            set(primitives[0].candidate_roles),
            {"horizontal_line_segment", "segmented_horizontal_rule"},
        )

    def test_extract_pdfium_visual_primitives_promotes_contiguous_micro_fragments(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 0,
            ) -> None:
                self.object_type = 1
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float, height: float) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = 1
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        objects = []
        for index in range(25):
            bottom = 10.0 + index * 1.0
            top = bottom + 0.8
            objects.append(
                _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 255, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 20.0, bottom), _FakeSegment(0, 20.0, top)],
                        ),
                    (19.9, bottom, 20.1, top),
                )
            )

        primitives = _extract_pdfium_visual_primitives(
            _FakePage(objects, width=100.0, height=100.0),
            page_number=5,
            raw_module=_FakeRawModule,
        )

        self.assertEqual(len(primitives), 1)
        self.assertEqual(primitives[0].object_type, "segmented_vertical_rule")
        self.assertEqual(
            set(primitives[0].candidate_roles),
            {"vertical_line_segment", "segmented_vertical_rule"},
        )

    def test_build_visual_block_candidates_promotes_open_frame(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 0,
            ) -> None:
                self.object_type = 1
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float, height: float) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = 1
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        primitives = _extract_pdfium_visual_primitives(
            _FakePage(
                [
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 10.0, 10.0), _FakeSegment(0, 10.0, 50.0)],
                        ),
                        (9.5, 10.0, 10.5, 50.0),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 10.0, 10.0), _FakeSegment(0, 80.0, 10.0)],
                        ),
                        (10.0, 9.5, 80.0, 10.5),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 80.0, 10.0), _FakeSegment(0, 80.0, 50.0)],
                        ),
                        (79.5, 10.0, 80.5, 50.0),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 10.0, 50.0), _FakeSegment(0, 38.0, 50.0)],
                        ),
                        (10.0, 49.5, 38.0, 50.5),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 52.0, 50.0), _FakeSegment(0, 80.0, 50.0)],
                        ),
                        (52.0, 49.5, 80.0, 50.5),
                    ),
                ],
                width=120.0,
                height=100.0,
            ),
            page_number=2,
            raw_module=_FakeRawModule,
        )

        candidates = _build_visual_block_candidates(primitives)
        open_frame_candidates = [candidate for candidate in candidates if candidate.candidate_type == "open_frame"]

        self.assertEqual(len(open_frame_candidates), 1)
        self.assertEqual(open_frame_candidates[0].candidate_type, "open_frame")
        self.assertEqual(open_frame_candidates[0].page_number, 2)
        self.assertEqual(open_frame_candidates[0].primitive_draw_orders, [0, 1, 2, 3, 4])
        self.assertIn("horizontal_line_segment", open_frame_candidates[0].source_roles)
        self.assertIn("vertical_line_segment", open_frame_candidates[0].source_roles)

    def test_build_visual_block_candidates_keeps_axis_box_without_child_cells(self) -> None:
        class _FakeSegment:
            def __init__(self, segment_type: int, x: float, y: float, *, close: bool = False) -> None:
                self.segment_type = segment_type
                self.x = x
                self.y = y
                self.close = close

        class _FakeRawObject:
            def __init__(
                self,
                *,
                fill: tuple[int, int, int, int],
                stroke: tuple[int, int, int, int],
                stroke_width: float,
                segments: list[_FakeSegment],
                fill_mode: int = 0,
            ) -> None:
                self.object_type = 1
                self.fill = fill
                self.stroke = stroke
                self.stroke_width = stroke_width
                self.segments = segments
                self.fill_mode = fill_mode

        class _FakeObject:
            def __init__(self, raw, bounds) -> None:
                self.raw = raw
                self._bounds = bounds

            def get_bounds(self):
                return self._bounds

        class _FakePage:
            def __init__(self, objects, *, width: float, height: float) -> None:
                self._objects = objects
                self._width = width
                self._height = height

            def get_objects(self):
                return self._objects

            def get_width(self):
                return self._width

            def get_height(self):
                return self._height

        class _FakeRawModule:
            FPDF_PAGEOBJ_PATH = 1
            FPDF_PAGEOBJ_SHADING = 2
            FPDF_PAGEOBJ_IMAGE = 3
            FPDF_PAGEOBJ_TEXT = 4
            FPDF_FILLMODE_NONE = 0
            FPDF_SEGMENT_MOVETO = 2
            FPDF_SEGMENT_LINETO = 0

            @staticmethod
            def FPDFPageObj_GetType(obj_raw) -> int:
                return obj_raw.object_type

            @staticmethod
            def FPDFPageObj_GetFillColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.fill
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeColor(obj_raw, red, green, blue, alpha) -> int:
                red.value, green.value, blue.value, alpha.value = obj_raw.stroke
                return 1

            @staticmethod
            def FPDFPageObj_GetStrokeWidth(obj_raw, width) -> int:
                width.value = obj_raw.stroke_width
                return 1

            @staticmethod
            def FPDFPath_CountSegments(obj_raw) -> int:
                return len(obj_raw.segments)

            @staticmethod
            def FPDFPath_GetPathSegment(obj_raw, index: int):
                return obj_raw.segments[index]

            @staticmethod
            def FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke) -> int:
                fill_mode.value = obj_raw.fill_mode
                stroke.value = 1
                return 1

            @staticmethod
            def FPDFPathSegment_GetType(segment) -> int:
                return segment.segment_type

            @staticmethod
            def FPDFPathSegment_GetPoint(segment, x, y) -> int:
                x.value = segment.x
                y.value = segment.y
                return 1

            @staticmethod
            def FPDFPathSegment_GetClose(segment) -> int:
                return 1 if segment.close else 0

        primitives = _extract_pdfium_visual_primitives(
            _FakePage(
                [
                    _FakeObject(
                        _FakeRawObject(
                            fill=(255, 255, 255, 255),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            fill_mode=1,
                            segments=[
                                _FakeSegment(2, 10.0, 10.0),
                                _FakeSegment(0, 90.0, 10.0),
                                _FakeSegment(0, 90.0, 40.0),
                                _FakeSegment(0, 10.0, 40.0, close=True),
                            ],
                        ),
                        (10.0, 10.0, 90.0, 40.0),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 35.0, 10.0), _FakeSegment(0, 35.0, 40.0)],
                        ),
                        (34.5, 10.0, 35.5, 40.0),
                    ),
                    _FakeObject(
                        _FakeRawObject(
                            fill=(0, 0, 0, 0),
                            stroke=(0, 0, 0, 255),
                            stroke_width=1.0,
                            segments=[_FakeSegment(2, 62.0, 10.0), _FakeSegment(0, 62.0, 40.0)],
                        ),
                        (61.5, 10.0, 62.5, 40.0),
                    ),
                ],
                width=120.0,
                height=80.0,
            ),
            page_number=1,
            raw_module=_FakeRawModule,
        )

        candidates = _build_visual_block_candidates(primitives)
        axis_candidates = [candidate for candidate in candidates if candidate.candidate_type == "axis_box"]

        self.assertEqual(len(axis_candidates), 1)
        self.assertEqual(axis_candidates[0].child_cells, [])

    def test_build_visual_block_candidates_skips_open_frame_graph_when_primitive_count_is_too_high(self) -> None:
        primitives = [
            PdfPreviewVisualPrimitive(
                page_number=1,
                draw_order=index,
                object_type="path",
                bounding_box=PdfBoundingBox(
                    left_pt=float(index),
                    bottom_pt=10.0,
                    right_pt=float(index) + 6.0,
                    top_pt=10.8,
                ),
                stroke_color="#000000ff",
                has_stroke=True,
                candidate_roles=["horizontal_line_segment"],
            )
            for index in range(501)
        ]

        candidates = _build_visual_block_candidates(primitives)

        self.assertEqual(candidates, [])

    def test_build_visual_block_candidates_absorbs_long_line_hint_into_open_frame(self) -> None:
        primitives = [
            PdfPreviewVisualPrimitive(
                page_number=1,
                draw_order=0,
                object_type="path",
                bounding_box=PdfBoundingBox(left_pt=10.0, bottom_pt=9.5, right_pt=90.0, top_pt=10.5),
                stroke_color="#000000ff",
                stroke_width_pt=1.0,
                has_stroke=True,
                candidate_roles=["horizontal_line_segment", "long_horizontal_rule"],
            ),
            PdfPreviewVisualPrimitive(
                page_number=1,
                draw_order=1,
                object_type="path",
                bounding_box=PdfBoundingBox(left_pt=9.5, bottom_pt=10.0, right_pt=10.5, top_pt=60.0),
                stroke_color="#000000ff",
                stroke_width_pt=1.0,
                has_stroke=True,
                candidate_roles=["vertical_line_segment"],
            ),
            PdfPreviewVisualPrimitive(
                page_number=1,
                draw_order=2,
                object_type="path",
                bounding_box=PdfBoundingBox(left_pt=89.5, bottom_pt=10.0, right_pt=90.5, top_pt=60.0),
                stroke_color="#000000ff",
                stroke_width_pt=1.0,
                has_stroke=True,
                candidate_roles=["vertical_line_segment"],
            ),
        ]

        candidates = _build_visual_block_candidates(primitives)

        open_frames = [candidate for candidate in candidates if candidate.candidate_type == "open_frame"]
        self.assertEqual(len(open_frames), 1)
        self.assertEqual(open_frames[0].primitive_draw_orders, [0, 1, 2])
        self.assertEqual({candidate.candidate_type for candidate in candidates}, {"open_frame"})

    def test_connected_line_components_uses_1pt5_join_tolerance(self) -> None:
        primitives = [
            PdfPreviewVisualPrimitive(
                page_number=1,
                draw_order=0,
                object_type="path",
                bounding_box=PdfBoundingBox(left_pt=9.5, bottom_pt=10.0, right_pt=10.5, top_pt=50.0),
                stroke_color="#000000ff",
                stroke_width_pt=1.0,
                has_stroke=True,
                candidate_roles=["vertical_line_segment"],
            ),
            PdfPreviewVisualPrimitive(
                page_number=1,
                draw_order=1,
                object_type="path",
                bounding_box=PdfBoundingBox(left_pt=11.3, bottom_pt=49.5, right_pt=60.0, top_pt=50.5),
                stroke_color="#000000ff",
                stroke_width_pt=1.0,
                has_stroke=True,
                candidate_roles=["horizontal_line_segment"],
            ),
        ]

        components = _connected_line_components(primitives)

        self.assertEqual(len(components), 1)
        self.assertEqual(sorted(item.draw_order for item in components[0]), [0, 1])

    def test_build_visual_block_candidates_fast_path_still_groups_long_line_hints_into_open_frame(self) -> None:
        primitives = [
            PdfPreviewVisualPrimitive(
                page_number=1,
                draw_order=index,
                object_type="path",
                bounding_box=PdfBoundingBox(
                    left_pt=float(index),
                    bottom_pt=10.0,
                    right_pt=float(index) + 6.0,
                    top_pt=10.8,
                ),
                stroke_color="#000000ff",
                has_stroke=True,
                candidate_roles=["horizontal_line_segment"],
            )
            for index in range(501)
        ]
        primitives.extend(
            [
                PdfPreviewVisualPrimitive(
                    page_number=1,
                    draw_order=600,
                    object_type="path",
                    bounding_box=PdfBoundingBox(left_pt=10.0, bottom_pt=9.5, right_pt=90.0, top_pt=10.5),
                    stroke_color="#000000ff",
                    stroke_width_pt=1.0,
                    has_stroke=True,
                    candidate_roles=["horizontal_line_segment", "long_horizontal_rule"],
                ),
                PdfPreviewVisualPrimitive(
                    page_number=1,
                    draw_order=601,
                    object_type="path",
                    bounding_box=PdfBoundingBox(left_pt=9.5, bottom_pt=10.0, right_pt=10.5, top_pt=60.0),
                    stroke_color="#000000ff",
                    stroke_width_pt=1.0,
                    has_stroke=True,
                    candidate_roles=["vertical_line_segment", "long_vertical_rule"],
                ),
                PdfPreviewVisualPrimitive(
                    page_number=1,
                    draw_order=602,
                    object_type="path",
                    bounding_box=PdfBoundingBox(left_pt=89.5, bottom_pt=10.0, right_pt=90.5, top_pt=60.0),
                    stroke_color="#000000ff",
                    stroke_width_pt=1.0,
                    has_stroke=True,
                    candidate_roles=["vertical_line_segment", "long_vertical_rule"],
                ),
            ]
        )

        candidates = _build_visual_block_candidates(primitives)

        open_frames = [candidate for candidate in candidates if candidate.candidate_type == "open_frame"]
        self.assertEqual(len(open_frames), 1)
        self.assertEqual(open_frames[0].primitive_draw_orders, [600, 601, 602])
        self.assertEqual({candidate.candidate_type for candidate in candidates}, {"open_frame"})

    def test_enrich_pdf_doc_ir_skips_candidate_overlay_when_it_matches_table_bbox(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "pages": [
                {"page number": 1, "width pt": 250, "height pt": 200},
            ],
            "layout regions": [
                {
                    "region id": "p1-main",
                    "region type": "main",
                    "page number": 1,
                    "bounding box": [0, 0, 250, 200],
                }
            ],
            "kids": [
                {
                    "type": "table",
                    "page number": 1,
                    "layout region id": "p1-main",
                    "reading order index": 1,
                    "bounding box": [10, 20, 110, 120],
                    "number of rows": 1,
                    "number of columns": 2,
                    "rows": [
                        {
                            "cells": [
                                {
                                    "type": "table cell",
                                    "row number": 1,
                                    "column number": 1,
                                    "page number": 1,
                                    "kids": [{"type": "paragraph", "content": "A1", "page number": 1}],
                                },
                                {
                                    "type": "table cell",
                                    "row number": 1,
                                    "column number": 2,
                                    "page number": 1,
                                    "kids": [{"type": "paragraph", "content": "B1", "page number": 1}],
                                },
                            ]
                        }
                    ],
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        context = build_pdf_preview_context(raw_document)
        context.visual_block_candidates.append(
            PdfPreviewVisualBlockCandidate(
                page_number=1,
                candidate_type="axis_box",
                bounding_box=PdfBoundingBox(left_pt=10.0, bottom_pt=20.0, right_pt=110.0, top_pt=120.0),
                primitive_draw_orders=[],
                source_roles=["axis_box"],
                child_cells=[],
            )
        )

        enrich_pdf_doc_ir(doc, preview_context=context)
        html = doc.to_html(title="Preview")

        self.assertIn("A1", html)
        self.assertIn("B1", html)
        self.assertNotIn("pdf-preview-candidate--axis_box", html)

    def test_enrich_pdf_doc_ir_promotes_single_candidate_to_layout_table_and_keeps_leftover_flow(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "pages": [
                {"page number": 1, "width pt": 250, "height pt": 200},
            ],
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Box text",
                    "page number": 1,
                    "bounding box": [72, 52, 148, 88],
                    "reading order index": 1,
                },
                {
                    "type": "paragraph",
                    "content": "Flow text",
                    "page number": 1,
                    "bounding box": [170, 100, 220, 116],
                    "reading order index": 2,
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        context = build_pdf_preview_context(raw_document)
        context.visual_block_candidates.append(
            PdfPreviewVisualBlockCandidate(
                page_number=1,
                candidate_type="axis_box",
                bounding_box=PdfBoundingBox(left_pt=60.0, bottom_pt=40.0, right_pt=160.0, top_pt=120.0),
                primitive_draw_orders=[],
                source_roles=["axis_box"],
                child_cells=[],
            )
        )

        enrich_pdf_doc_ir(doc, preview_context=context)
        html = doc.to_html(title="Preview")

        self.assertEqual(html.count("<table"), 1)
        self.assertIn("width:100.0pt", html)
        self.assertNotIn("height:80.0pt", html)
        self.assertIn("border-top:1px solid #4a4f57", html)
        self.assertIn("Box text", html)
        self.assertIn("Flow text", html)
        self.assertNotIn("pdf-preview-candidate--axis_box", html)
        self.assertNotIn("pdf-preview-page-candidates", html)

    def test_enrich_pdf_doc_ir_promotes_aligned_candidates_to_multicell_layout_table(self) -> None:
        raw_document = {
            "file name": "sample.pdf",
            "number of pages": 1,
            "pages": [
                {"page number": 1, "width pt": 250, "height pt": 200},
            ],
            "kids": [
                {
                    "type": "paragraph",
                    "content": "Left cell",
                    "page number": 1,
                    "bounding box": [62, 54, 106, 86],
                    "reading order index": 1,
                },
                {
                    "type": "paragraph",
                    "content": "Right cell",
                    "page number": 1,
                    "bounding box": [118, 54, 162, 86],
                    "reading order index": 2,
                },
            ],
        }

        doc = build_doc_ir_from_odl_result(raw_document, source_path="sample.pdf")
        context = build_pdf_preview_context(raw_document)
        context.visual_block_candidates.extend(
            [
                PdfPreviewVisualBlockCandidate(
                    page_number=1,
                    candidate_type="axis_box",
                    bounding_box=PdfBoundingBox(left_pt=56.0, bottom_pt=46.0, right_pt=112.0, top_pt=96.0),
                    primitive_draw_orders=[],
                    source_roles=["axis_box"],
                    child_cells=[],
                ),
                PdfPreviewVisualBlockCandidate(
                    page_number=1,
                    candidate_type="open_frame",
                    bounding_box=PdfBoundingBox(left_pt=114.0, bottom_pt=46.0, right_pt=170.0, top_pt=96.0),
                    primitive_draw_orders=[],
                    source_roles=["open_frame"],
                    child_cells=[],
                ),
            ]
        )

        enrich_pdf_doc_ir(doc, preview_context=context)
        html = doc.to_html(title="Preview")

        self.assertEqual(html.count("<table"), 1)
        self.assertEqual(html.count("<td"), 2)
        self.assertIn("width:114.0pt", html)
        self.assertNotIn("height:50.0pt", html)
        self.assertIn("Left cell", html)
        self.assertIn("Right cell", html)
        self.assertNotIn("pdf-preview-candidate--axis_box", html)
        self.assertNotIn("pdf-preview-candidate--open_frame", html)


if __name__ == "__main__":
    unittest.main()
