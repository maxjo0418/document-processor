from __future__ import annotations

import importlib
from pathlib import Path
import sys
import unittest

THIS_DIR = Path(__file__).resolve().parent
SRC_ROOT = THIS_DIR.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class PdfPreviewModuleApiTests(unittest.TestCase):
    def test_pdf_root_does_not_export_internal_parsing_helpers(self) -> None:
        module = importlib.import_module("document_processor.pdf")

        self.assertEqual(getattr(module, "__all__", []), [])
        self.assertFalse(hasattr(module, "parse_pdf_to_doc_ir"))
        self.assertFalse(hasattr(module, "PdfParseConfig"))
        self.assertFalse(hasattr(module, "export_pdf_local_outputs"))

    def test_preview_submodules_are_importable(self) -> None:
        for module_name in (
            "document_processor.pdf.preview.models",
            "document_processor.pdf.preview.analyze",
            "document_processor.pdf.preview.shared",
            "document_processor.pdf.preview.context",
            "document_processor.pdf.preview.normalize",
        ):
            module = importlib.import_module(module_name)
            self.assertIsNotNone(module)

    def test_preview_root_exports_minimal_public_api(self) -> None:
        module = importlib.import_module("document_processor.pdf.preview")
        expected_exports = {
            "enrich_pdf_doc_ir",
        }
        forbidden_exports = {
            "PdfLayoutRegion",
            "PdfPreviewVisualBlockCandidate",
            "PdfPreviewContext",
            "PdfPreviewTableContext",
            "PdfPreviewVisualPrimitive",
            "build_pdf_preview_context",
            "render_pdf_preview_html",
            "prepare_pdf_for_html",
        }

        self.assertEqual(set(getattr(module, "__all__", [])), expected_exports)
        for export_name in expected_exports:
            self.assertTrue(hasattr(module, export_name))
        for export_name in forbidden_exports:
            self.assertFalse(hasattr(module, export_name))
