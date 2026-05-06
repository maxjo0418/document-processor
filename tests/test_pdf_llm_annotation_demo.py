from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from document_processor import AnnotationValidationIssue, DocAnnotation


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "demo_pdf_llm_annotations.py"


def _load_demo_module():
    spec = importlib.util.spec_from_file_location("demo_pdf_llm_annotations", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PdfLlmAnnotationDemoTests(unittest.TestCase):
    def test_annotations_from_payload_builds_pdf_annotations(self) -> None:
        demo = _load_demo_module()

        annotations = demo.annotations_from_payload(
            {
                "annotations": [
                    {
                        "target_id": "p_1",
                        "selected_text": "Important",
                        "note": "Important",
                        "color": "#FFF176",
                    },
                    {
                        "target_id": "p_2",
                        "note": "Study this",
                        "color": "#FFF176",
                    },
                ]
            }
        )

        self.assertEqual(len(annotations), 2)
        self.assertTrue(all(isinstance(annotation, DocAnnotation) for annotation in annotations))
        self.assertEqual(annotations[0].selected_text, "Important")
        self.assertEqual(annotations[1].note, "Study this")

    def test_annotations_from_payload_normalizes_short_hex_color(self) -> None:
        demo = _load_demo_module()

        annotations = demo.annotations_from_payload(
            {
                "annotations": [
                    {
                        "target_id": "p_1",
                        "note": "Study this",
                        "color": "#EEE",
                    }
                ]
            }
        )

        self.assertEqual(annotations[0].color, "#EEEEEE")

    def test_filter_invalid_annotations_drops_target_not_found_issues(self) -> None:
        demo = _load_demo_module()
        annotations = [
            DocAnnotation(target_id="p_valid", note="valid"),
            DocAnnotation(target_id="p_missing", note="missing"),
        ]

        filtered = demo.filter_invalid_annotations(
            annotations,
            [
                AnnotationValidationIssue(
                    code="target_not_found",
                    target_id="p_missing",
                    message="missing",
                )
            ],
        )

        self.assertEqual([annotation.target_id for annotation in filtered], ["p_valid"])

    def test_response_json_schema_restricts_target_ids(self) -> None:
        demo = _load_demo_module()

        schema = demo.response_json_schema(["p_1", "p_2"])

        target_id_schema = schema["properties"]["annotations"]["items"]["properties"]["target_id"]
        color_schema = schema["properties"]["annotations"]["items"]["properties"]["color"]
        self.assertEqual(target_id_schema["enum"], ["p_1", "p_2"])
        self.assertEqual(color_schema["pattern"], "^#[0-9A-Fa-f]{6}$")


if __name__ == "__main__":
    unittest.main()
