from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import document_processor
from document_processor import (
    BoundingBox,
    DocIR,
    DocAnnotation,
    DocumentInput,
    PageInfo,
    ParagraphIR,
    RunIR,
    TableCellIR,
    TableIR,
    apply_pdf_annotations,
    validate_pdf_annotations,
)


class PdfAnnotationApiTests(unittest.TestCase):
    def test_pdf_specific_validation_types_are_not_public_exports(self) -> None:
        self.assertFalse(hasattr(document_processor, "PdfAnnotationValidationCode"))
        self.assertFalse(hasattr(document_processor, "PdfAnnotationValidationIssue"))
        self.assertFalse(hasattr(document_processor, "PdfAnnotationValidationResult"))

    @staticmethod
    def _write_blank_pdf(path: Path) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.write(path)

    @staticmethod
    def _build_pdf_doc(source_path: Path) -> DocIR:
        return DocIR(
            doc_id="sample",
            source_path=str(source_path),
            source_doc_type="pdf",
            pages=[PageInfo(page_number=1, width_pt=300, height_pt=300)],
            paragraphs=[
                ParagraphIR(
                    node_id="p_target",
                    text="Important paragraph",
                    page_number=1,
                    bbox=BoundingBox(left_pt=50, bottom_pt=100, right_pt=200, top_pt=140),
                    content=[
                        RunIR(
                            node_id="r_important",
                            text="Important",
                            bbox=BoundingBox(left_pt=50, bottom_pt=100, right_pt=110, top_pt=140),
                        ),
                        RunIR(
                            node_id="r_paragraph",
                            text=" paragraph",
                            bbox=BoundingBox(left_pt=110, bottom_pt=100, right_pt=200, top_pt=140),
                        ),
                    ],
                )
            ],
        )

    def test_apply_pdf_annotations_infers_highlight_and_note_annotations(self) -> None:
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            output = Path(tmp) / "annotated.pdf"
            self._write_blank_pdf(source)
            doc = self._build_pdf_doc(source)

            result = apply_pdf_annotations(
                document=DocumentInput(source_path=str(source), doc_ir=doc),
                annotations=[
                    DocAnnotation(
                        target_id="p_target",
                        color="#FFCC80",
                        metadata={"subject": "Exam point", "opacity": 0.42},
                    ),
                    DocAnnotation(
                        target_id="p_target",
                        note="Add a study note here.",
                        color="#80DEEA",
                        metadata={"icon": "/Comment", "subject": "Lecture memo", "opacity": 0.9},
                    ),
                ],
                output_path=str(output),
            )

            self.assertTrue(result.ok, result.validation.issues)
            self.assertEqual(result.annotations_applied, 2)
            self.assertEqual(result.output_path, str(output))

            page = PdfReader(output).pages[0]
            annotations = [annotation.get_object() for annotation in page["/Annots"]]
            subtypes = [annotation["/Subtype"] for annotation in annotations]
            self.assertIn("/Highlight", subtypes)
            self.assertIn("/Text", subtypes)
            self.assertIn("/Popup", subtypes)

            highlight = next(annotation for annotation in annotations if annotation["/Subtype"] == "/Highlight")
            self.assertEqual(highlight["/Subj"], "Exam point")
            self.assertEqual(round(float(highlight["/CA"]), 2), 0.42)
            self.assertIn("/QuadPoints", highlight)

            note = next(annotation for annotation in annotations if annotation["/Subtype"] == "/Text")
            self.assertEqual(note["/Contents"], "Add a study note here.")
            self.assertEqual(note["/Name"], "/Comment")
            self.assertEqual(note["/Subj"], "Lecture memo")
            self.assertEqual(round(float(note["/CA"]), 2), 0.9)
            self.assertEqual([round(float(value), 3) for value in note["/C"]], [0.502, 0.871, 0.918])
            self.assertLess(float(note["/Rect"][2]), 50)

            popup = next(annotation for annotation in annotations if annotation["/Subtype"] == "/Popup")
            self.assertEqual(popup["/Parent"].get_object()["/Subtype"], "/Text")
            self.assertEqual(note["/Popup"].get_object()["/Subtype"], "/Popup")

    def test_apply_pdf_annotations_uses_selected_text_as_highlight_even_with_note(self) -> None:
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            output = Path(tmp) / "annotated.pdf"
            self._write_blank_pdf(source)
            doc = self._build_pdf_doc(source)

            result = apply_pdf_annotations(
                document=DocumentInput(source_path=str(source), doc_ir=doc),
                annotations=[
                    DocAnnotation(
                        target_id="p_target",
                        selected_text="Important",
                        note="Focus on this term.",
                        color="#FFCC80",
                    ),
                ],
                output_path=str(output),
            )

            self.assertTrue(result.ok, result.validation.issues)
            page = PdfReader(output).pages[0]
            annotations = [annotation.get_object() for annotation in page["/Annots"]]
            self.assertEqual(len(annotations), 1)
            highlight = annotations[0]
            self.assertEqual(highlight["/Subtype"], "/Highlight")
            self.assertEqual(highlight["/Contents"], "Focus on this term.")
            self.assertEqual([round(float(value), 1) for value in highlight["/Rect"]], [50.0, 100.0, 110.0, 140.0])

    def test_validate_pdf_annotations_rejects_selected_text_without_exact_run_bbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            self._write_blank_pdf(source)
            doc = self._build_pdf_doc(source)

            validation = validate_pdf_annotations(
                document=DocumentInput(source_path=str(source), doc_ir=doc),
                annotations=[DocAnnotation(target_id="p_target", selected_text="portant")],
            )

            self.assertFalse(validation.ok)
            self.assertEqual(validation.issues[0].code, "missing_bbox")

    def test_validate_pdf_annotations_handles_unrelated_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            self._write_blank_pdf(source)
            doc = self._build_pdf_doc(source)
            doc.paragraphs.append(
                ParagraphIR(
                    node_id="p_table",
                    page_number=1,
                    bbox=BoundingBox(left_pt=50, bottom_pt=50, right_pt=250, top_pt=90),
                    content=[
                        TableIR(
                            node_id="tbl_sample",
                            row_count=1,
                            col_count=1,
                            bbox=BoundingBox(left_pt=50, bottom_pt=50, right_pt=250, top_pt=90),
                            cells=[
                                [
                                    TableCellIR(
                                        node_id="cell_sample",
                                        text="Table cell",
                                        bbox=BoundingBox(left_pt=55, bottom_pt=55, right_pt=245, top_pt=85),
                                    )
                                ]
                            ],
                        )
                    ],
                )
            )

            validation = validate_pdf_annotations(
                document=DocumentInput(source_path=str(source), doc_ir=doc),
                annotations=[DocAnnotation(target_id="p_target", color="#FFCC80")],
            )

            self.assertTrue(validation.ok, validation.issues)


if __name__ == "__main__":
    unittest.main()
