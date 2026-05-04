from __future__ import annotations

import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
SRC_ROOT = THIS_DIR.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from document_processor.pdf.meta import PdfBoundingBox
from document_processor.pdf.odl.table_reconstruct import _apply_dotted_splits
from document_processor.pdf.preview.models import PdfPreviewVisualPrimitive


def _dotted_primitive(
    *,
    orientation: str,
    left: float,
    bottom: float,
    right: float,
    top: float,
    stroke_color: str = "#000000ff",
    stroke_width_pt: float = 1.0,
) -> PdfPreviewVisualPrimitive:
    return PdfPreviewVisualPrimitive(
        page_number=1,
        draw_order=1,
        object_type=f"segmented_{orientation}_rule",
        bounding_box=PdfBoundingBox(
            left_pt=left, bottom_pt=bottom, right_pt=right, top_pt=top
        ),
        stroke_color=stroke_color,
        stroke_width_pt=stroke_width_pt,
        has_stroke=True,
        candidate_roles=[f"segmented_{orientation}_rule"],
    )


def _paragraph(text: str, *, left: float, bottom: float, right: float, top: float) -> dict:
    return {
        "type": "paragraph",
        "page number": 1,
        "content": text,
        "bounding box": [left, bottom, right, top],
        "spans": [
            {
                "type": "text chunk",
                "content": text,
                "bounding box": [left, bottom, right, top],
            }
        ],
    }


def _single_cell_table(
    *,
    paragraphs: list[dict],
    table_bbox: tuple[float, float, float, float] = (10.0, 10.0, 110.0, 90.0),
) -> dict:
    left, bottom, right, top = table_bbox
    return {
        "type": "table",
        "page number": 1,
        "bounding box": list(table_bbox),
        "number of rows": 1,
        "number of columns": 1,
        "grid row boundaries": [top, bottom],
        "grid column boundaries": [left, right],
        "rows": [
            {
                "type": "table row",
                "row number": 1,
                "cells": [
                    {
                        "type": "table cell",
                        "page number": 1,
                        "row number": 1,
                        "column number": 1,
                        "row span": 1,
                        "column span": 1,
                        "bounding box": list(table_bbox),
                        "has top border": True,
                        "has bottom border": True,
                        "has left border": True,
                        "has right border": True,
                        "kids": list(paragraphs),
                        "paragraphs": list(paragraphs),
                    }
                ],
            }
        ],
    }


class DottedRuleSplitTests(unittest.TestCase):
    def test_no_dotted_rules_leaves_table_unchanged(self) -> None:
        table = _single_cell_table(
            paragraphs=[_paragraph("Only", left=14.0, bottom=20.0, right=108.0, top=80.0)]
        )
        snapshot = {key: table[key] for key in ("number of rows", "number of columns", "rows")}
        _apply_dotted_splits(table, dotted_h=[], dotted_v=[])
        self.assertEqual(table["number of rows"], snapshot["number of rows"])
        self.assertEqual(table["number of columns"], snapshot["number of columns"])
        self.assertEqual(table["rows"], snapshot["rows"])

    def test_horizontal_dotted_rule_splits_cell_into_two_rows(self) -> None:
        table = _single_cell_table(
            paragraphs=[
                _paragraph("Top", left=14.0, bottom=58.0, right=108.0, top=82.0),
                _paragraph("Bottom", left=14.0, bottom=18.0, right=108.0, top=42.0),
            ]
        )
        dotted = _dotted_primitive(orientation="horizontal", left=10.0, bottom=49.5, right=110.0, top=50.5)
        _apply_dotted_splits(table, dotted_h=[dotted], dotted_v=[])
        self.assertEqual(table["number of rows"], 2)
        self.assertEqual(table["number of columns"], 1)
        rows = table["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["row number"], 1)  # top row
        self.assertEqual(rows[1]["row number"], 2)
        top_cell = rows[0]["cells"][0]
        bottom_cell = rows[1]["cells"][0]
        self.assertEqual(top_cell["paragraphs"][0]["content"], "Top")
        self.assertEqual(bottom_cell["paragraphs"][0]["content"], "Bottom")

    def test_horizontal_dotted_rule_records_border_style_on_split_cells(self) -> None:
        table = _single_cell_table(
            paragraphs=[
                _paragraph("Top", left=14.0, bottom=58.0, right=108.0, top=82.0),
                _paragraph("Bottom", left=14.0, bottom=18.0, right=108.0, top=42.0),
            ]
        )
        dotted = _dotted_primitive(
            orientation="horizontal",
            left=10.0,
            bottom=49.5,
            right=110.0,
            top=50.5,
            stroke_color="#123456ff",
            stroke_width_pt=1.5,
        )
        _apply_dotted_splits(table, dotted_h=[dotted], dotted_v=[])
        top_cell = table["rows"][0]["cells"][0]
        bottom_cell = table["rows"][1]["cells"][0]
        self.assertEqual(top_cell["border bottom"], "1.5px dotted #123456")
        self.assertEqual(bottom_cell["border top"], "1.5px dotted #123456")

    def test_vertical_dotted_rule_splits_cell_into_two_columns(self) -> None:
        table = _single_cell_table(
            paragraphs=[
                _paragraph("Left", left=14.0, bottom=40.0, right=55.0, top=60.0),
                _paragraph("Right", left=65.0, bottom=40.0, right=106.0, top=60.0),
            ]
        )
        dotted = _dotted_primitive(orientation="vertical", left=59.5, bottom=10.0, right=60.5, top=90.0)
        _apply_dotted_splits(table, dotted_h=[], dotted_v=[dotted])
        self.assertEqual(table["number of rows"], 1)
        self.assertEqual(table["number of columns"], 2)
        cells = table["rows"][0]["cells"]
        self.assertEqual(len(cells), 2)
        self.assertEqual(cells[0]["paragraphs"][0]["content"], "Left")
        self.assertEqual(cells[1]["paragraphs"][0]["content"], "Right")

    def test_vertical_dotted_rule_records_border_style_on_split_cells(self) -> None:
        table = _single_cell_table(
            paragraphs=[
                _paragraph("Left", left=14.0, bottom=40.0, right=55.0, top=60.0),
                _paragraph("Right", left=65.0, bottom=40.0, right=106.0, top=60.0),
            ]
        )
        dotted = _dotted_primitive(
            orientation="vertical",
            left=59.5,
            bottom=10.0,
            right=60.5,
            top=90.0,
            stroke_color="#abcdef",
            stroke_width_pt=2.0,
        )
        _apply_dotted_splits(table, dotted_h=[], dotted_v=[dotted])
        cells = table["rows"][0]["cells"]
        self.assertEqual(cells[0]["border right"], "2px dotted #abcdef")
        self.assertEqual(cells[1]["border left"], "2px dotted #abcdef")

    def test_dotted_rule_along_existing_boundary_is_ignored(self) -> None:
        table = _single_cell_table(
            paragraphs=[_paragraph("Only", left=14.0, bottom=20.0, right=108.0, top=80.0)]
        )
        # A dotted rule at the bottom boundary of the table — must not spawn a new row.
        dotted = _dotted_primitive(orientation="horizontal", left=10.0, bottom=9.5, right=110.0, top=10.5)
        _apply_dotted_splits(table, dotted_h=[dotted], dotted_v=[])
        self.assertEqual(table["number of rows"], 1)

    def test_partial_width_dotted_rule_is_ignored(self) -> None:
        table = _single_cell_table(
            paragraphs=[_paragraph("Only", left=14.0, bottom=20.0, right=108.0, top=80.0)]
        )
        # Rule only covers 40pt of the 100pt table width — below the 90% threshold.
        dotted = _dotted_primitive(orientation="horizontal", left=30.0, bottom=49.5, right=70.0, top=50.5)
        _apply_dotted_splits(table, dotted_h=[dotted], dotted_v=[])
        self.assertEqual(table["number of rows"], 1)

    def test_dotted_rule_partial_width_splits_only_covered_cells(self) -> None:
        # Simulates the p24-style case: three cells in one row, with a dotted
        # rule that covers only the right two cells. Left cell must survive
        # as a single cell with rowspan=2; right two cells split into two rows.
        table = {
            "type": "table",
            "page number": 1,
            "bounding box": [50.0, 10.0, 540.0, 90.0],
            "number of rows": 1,
            "number of columns": 3,
            "grid row boundaries": [90.0, 10.0],
            "grid column boundaries": [50.0, 120.0, 200.0, 540.0],
            "rows": [
                {
                    "type": "table row",
                    "row number": 1,
                    "cells": [
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 1,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [50.0, 10.0, 120.0, 90.0],
                            "kids": [
                                _paragraph("Category", left=55.0, bottom=40.0, right=115.0, top=60.0),
                            ],
                            "paragraphs": [
                                _paragraph("Category", left=55.0, bottom=40.0, right=115.0, top=60.0),
                            ],
                        },
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 2,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [120.0, 10.0, 200.0, 90.0],
                            "kids": [
                                _paragraph("MidTop", left=125.0, bottom=55.0, right=195.0, top=85.0),
                                _paragraph("MidBot", left=125.0, bottom=15.0, right=195.0, top=45.0),
                            ],
                            "paragraphs": [
                                _paragraph("MidTop", left=125.0, bottom=55.0, right=195.0, top=85.0),
                                _paragraph("MidBot", left=125.0, bottom=15.0, right=195.0, top=45.0),
                            ],
                        },
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 3,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [200.0, 10.0, 540.0, 90.0],
                            "kids": [
                                _paragraph("RightTop", left=205.0, bottom=55.0, right=535.0, top=85.0),
                                _paragraph("RightBot", left=205.0, bottom=15.0, right=535.0, top=45.0),
                            ],
                            "paragraphs": [
                                _paragraph("RightTop", left=205.0, bottom=55.0, right=535.0, top=85.0),
                                _paragraph("RightBot", left=205.0, bottom=15.0, right=535.0, top=45.0),
                            ],
                        },
                    ],
                }
            ],
        }
        # Rule covers only middle+right columns (x=120-540), at y=50.
        dotted = _dotted_primitive(
            orientation="horizontal",
            left=120.0, bottom=49.5, right=540.0, top=50.5,
        )
        _apply_dotted_splits(table, dotted_h=[dotted], dotted_v=[])
        self.assertEqual(table["number of rows"], 2)
        self.assertEqual(table["number of columns"], 3)

        # Collect cells by (row, col).
        cells_by_pos = {
            (c["row number"], c["column number"]): c
            for row in table["rows"] for c in row["cells"]
        }
        # Category cell must still be one cell with rowspan=2.
        category = cells_by_pos[(1, 1)]
        self.assertEqual(category["row span"], 2)
        self.assertEqual(category["paragraphs"][0]["content"], "Category")

        # Middle column split into two rows.
        self.assertEqual(cells_by_pos[(1, 2)]["paragraphs"][0]["content"], "MidTop")
        self.assertEqual(cells_by_pos[(1, 2)]["row span"], 1)
        self.assertEqual(cells_by_pos[(2, 2)]["paragraphs"][0]["content"], "MidBot")

        # Right column likewise split.
        self.assertEqual(cells_by_pos[(1, 3)]["paragraphs"][0]["content"], "RightTop")
        self.assertEqual(cells_by_pos[(2, 3)]["paragraphs"][0]["content"], "RightBot")
        # Row 2 should have only 2 cells (col 2, col 3); col 1 is occupied by rowspan.
        row2_cells = [c for row in table["rows"] if row["row number"] == 2 for c in row["cells"]]
        self.assertEqual(len(row2_cells), 2)


    def test_vertical_rule_splits_cell_and_horizontal_rule_affects_only_right_sub_column(self) -> None:
        # Mirrors the p32 "동종창업" pattern: the cell is split by a vertical
        # dotted rule into a left sub-column (a rowspan label) and a right
        # sub-column that is further cut by horizontal dotted rules.
        table = {
            "type": "table",
            "page number": 1,
            "bounding box": [50.0, 10.0, 540.0, 90.0],
            "number of rows": 1,
            "number of columns": 2,
            "grid row boundaries": [90.0, 10.0],
            "grid column boundaries": [50.0, 120.0, 540.0],
            "rows": [
                {
                    "type": "table row",
                    "row number": 1,
                    "cells": [
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 1,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [50.0, 10.0, 120.0, 90.0],
                            "kids": [_paragraph("Header", left=55.0, bottom=45.0, right=115.0, top=60.0)],
                            "paragraphs": [_paragraph("Header", left=55.0, bottom=45.0, right=115.0, top=60.0)],
                        },
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 2,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [120.0, 10.0, 540.0, 90.0],
                            "kids": [
                                _paragraph("Label", left=135.0, bottom=45.0, right=195.0, top=60.0),
                                _paragraph("Row1", left=210.0, bottom=75.0, right=535.0, top=85.0),
                                _paragraph("Row2", left=210.0, bottom=55.0, right=535.0, top=65.0),
                                _paragraph("Row3", left=210.0, bottom=35.0, right=535.0, top=45.0),
                                _paragraph("Row4", left=210.0, bottom=15.0, right=535.0, top=25.0),
                            ],
                            "paragraphs": [
                                _paragraph("Label", left=135.0, bottom=45.0, right=195.0, top=60.0),
                                _paragraph("Row1", left=210.0, bottom=75.0, right=535.0, top=85.0),
                                _paragraph("Row2", left=210.0, bottom=55.0, right=535.0, top=65.0),
                                _paragraph("Row3", left=210.0, bottom=35.0, right=535.0, top=45.0),
                                _paragraph("Row4", left=210.0, bottom=15.0, right=535.0, top=25.0),
                            ],
                        },
                    ],
                }
            ],
        }
        # Vertical rule splits the second cell at x=200 (into label sub-column
        # 120-200 and detail sub-column 200-540).
        v_rule = _dotted_primitive(orientation="vertical", left=199.5, bottom=10.0, right=200.5, top=90.0)
        # Three horizontal rules, each covering only the right sub-column.
        h_rules = [
            _dotted_primitive(orientation="horizontal", left=200.0, bottom=69.5, right=540.0, top=70.5),
            _dotted_primitive(orientation="horizontal", left=200.0, bottom=49.5, right=540.0, top=50.5),
            _dotted_primitive(orientation="horizontal", left=200.0, bottom=29.5, right=540.0, top=30.5),
        ]
        _apply_dotted_splits(table, dotted_h=h_rules, dotted_v=[v_rule])

        # Expectation: 4 rows, 3 columns.
        self.assertEqual(table["number of rows"], 4)
        self.assertEqual(table["number of columns"], 3)
        cells_by_pos = {
            (c["row number"], c["column number"]): c
            for row in table["rows"] for c in row["cells"]
        }
        # Header column stays rowspan=4 with single paragraph.
        self.assertEqual(cells_by_pos[(1, 1)]["row span"], 4)
        self.assertEqual(cells_by_pos[(1, 1)]["paragraphs"][0]["content"], "Header")
        # Label sub-column (col 2) stays rowspan=4 with single paragraph.
        self.assertEqual(cells_by_pos[(1, 2)]["row span"], 4)
        self.assertEqual(cells_by_pos[(1, 2)]["paragraphs"][0]["content"], "Label")
        # Right sub-column (col 3) is cut into 4 separate cells.
        self.assertEqual(cells_by_pos[(1, 3)]["paragraphs"][0]["content"], "Row1")
        self.assertEqual(cells_by_pos[(2, 3)]["paragraphs"][0]["content"], "Row2")
        self.assertEqual(cells_by_pos[(3, 3)]["paragraphs"][0]["content"], "Row3")
        self.assertEqual(cells_by_pos[(4, 3)]["paragraphs"][0]["content"], "Row4")


    def test_merged_leaf_content_is_split_when_units_match_sub_bands(self) -> None:
        # Mirrors the p8 list_item pathology: a single leaf whose bbox spans
        # two rows and whose content concatenates "① ... " with "§ 1개사
        # 내외" via a learned bullet character. A detected dotted rule at
        # y=50 divides the cell into two sub-bands.
        merged_leaf = {
            "type": "list item",
            "page number": 1,
            "bounding box": [15.0, 15.0, 105.0, 85.0],
            "content": "① Major task that spans multiple lines of text § 1pt",
        }
        sibling_bullet_ref = _paragraph("§ sibling", left=15.0, bottom=5.0, right=105.0, top=14.0)
        table = {
            "type": "table",
            "page number": 1,
            "bounding box": [10.0, 0.0, 110.0, 90.0],
            "number of rows": 1,
            "number of columns": 1,
            "grid row boundaries": [90.0, 0.0],
            "grid column boundaries": [10.0, 110.0],
            "rows": [
                {
                    "type": "table row",
                    "row number": 1,
                    "cells": [
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 1,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [10.0, 0.0, 110.0, 90.0],
                            "kids": [merged_leaf, sibling_bullet_ref],
                            "paragraphs": [merged_leaf, sibling_bullet_ref],
                        }
                    ],
                }
            ],
        }
        dotted = _dotted_primitive(
            orientation="horizontal",
            left=10.0, bottom=49.5, right=110.0, top=50.5,
        )
        _apply_dotted_splits(table, dotted_h=[dotted], dotted_v=[])
        self.assertEqual(table["number of rows"], 2)
        cells_by_row = {
            row["row number"]: row["cells"][0] for row in table["rows"]
        }
        # Top sub-band keeps only the ① segment.
        top_kids = [k for k in cells_by_row[1]["kids"] if isinstance(k, dict)]
        self.assertTrue(
            any("① Major task" in (k.get("content") or "") and "1pt" not in (k.get("content") or "")
                for k in top_kids),
            f"top sub-band should carry only '①' unit, got: {[k.get('content') for k in top_kids]}",
        )
        # Bottom sub-band gets the § unit.
        bottom_kids = [k for k in cells_by_row[2]["kids"] if isinstance(k, dict)]
        self.assertTrue(
            any("1pt" in (k.get("content") or "")
                for k in bottom_kids),
            f"bottom sub-band should carry '§ 1pt' unit, got: {[k.get('content') for k in bottom_kids]}",
        )


    def test_whitespace_fallback_splits_code_like_tokens(self) -> None:
        # Mirrors p44: the left column of a code/description table has a
        # merged leaf with several short numeric codes separated by spaces
        # ("66 68 69390") and bbox spanning three rows. No bullet chars are
        # present, so the bullet-based split can't match — the fallback
        # strategy must recognise it as a space-separated list of codes and
        # distribute them to three sub-bands.
        merged_codes_leaf = {
            "type": "paragraph",
            "page number": 1,
            "bounding box": [15.0, 15.0, 50.0, 85.0],
            "content": "66 68 69390",
        }
        # Three description paragraphs that dictate the row structure.
        desc_leaves = [
            _paragraph("Real estate", left=55.0, bottom=70.0, right=105.0, top=82.0),
            _paragraph("Rental services", left=55.0, bottom=45.0, right=105.0, top=57.0),
            _paragraph("Legal services", left=55.0, bottom=20.0, right=105.0, top=32.0),
        ]
        table = {
            "type": "table",
            "page number": 1,
            "bounding box": [10.0, 10.0, 110.0, 90.0],
            "number of rows": 1,
            "number of columns": 2,
            "grid row boundaries": [90.0, 10.0],
            "grid column boundaries": [10.0, 52.0, 110.0],
            "rows": [
                {
                    "type": "table row",
                    "row number": 1,
                    "cells": [
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 1,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [10.0, 10.0, 52.0, 90.0],
                            "kids": [merged_codes_leaf],
                            "paragraphs": [merged_codes_leaf],
                        },
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 2,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [52.0, 10.0, 110.0, 90.0],
                            "kids": list(desc_leaves),
                            "paragraphs": list(desc_leaves),
                        },
                    ],
                }
            ],
        }
        rules = [
            _dotted_primitive(
                orientation="horizontal",
                left=10.0, bottom=63.5, right=110.0, top=64.5,
            ),
            _dotted_primitive(
                orientation="horizontal",
                left=10.0, bottom=38.5, right=110.0, top=39.5,
            ),
        ]
        _apply_dotted_splits(table, dotted_h=rules, dotted_v=[])
        self.assertEqual(table["number of rows"], 3)
        cells_by_pos = {
            (c["row number"], c["column number"]): c
            for row in table["rows"] for c in row["cells"]
        }
        # Each code lands in its own row, aligned with the matching description.
        self.assertEqual(cells_by_pos[(1, 1)]["paragraphs"][0]["content"], "66")
        self.assertEqual(cells_by_pos[(2, 1)]["paragraphs"][0]["content"], "68")
        self.assertEqual(cells_by_pos[(3, 1)]["paragraphs"][0]["content"], "69390")

    def test_whitespace_fallback_rejects_korean_prose(self) -> None:
        # A Korean phrase happens to have exactly as many whitespace-
        # separated tokens as there are sub-bands. The fallback must NOT
        # split it because the tokens are pure Korean letters, not codes.
        prose_leaf = {
            "type": "paragraph",
            "page number": 1,
            "bounding box": [15.0, 15.0, 105.0, 85.0],
            "content": "도박기계 사행성 오락기구",
        }
        table = {
            "type": "table",
            "page number": 1,
            "bounding box": [10.0, 10.0, 110.0, 90.0],
            "number of rows": 1,
            "number of columns": 1,
            "grid row boundaries": [90.0, 10.0],
            "grid column boundaries": [10.0, 110.0],
            "rows": [
                {
                    "type": "table row",
                    "row number": 1,
                    "cells": [
                        {
                            "type": "table cell",
                            "page number": 1,
                            "row number": 1,
                            "column number": 1,
                            "row span": 1,
                            "column span": 1,
                            "bounding box": [10.0, 10.0, 110.0, 90.0],
                            "kids": [prose_leaf],
                            "paragraphs": [prose_leaf],
                        }
                    ],
                }
            ],
        }
        rules = [
            _dotted_primitive(
                orientation="horizontal",
                left=10.0, bottom=63.5, right=110.0, top=64.5,
            ),
            _dotted_primitive(
                orientation="horizontal",
                left=10.0, bottom=38.5, right=110.0, top=39.5,
            ),
        ]
        _apply_dotted_splits(table, dotted_h=rules, dotted_v=[])
        # After split detection, the row count would still become 3 (from
        # the detected rules), but the prose leaf must NOT be pre-split —
        # it stays as one leaf assigned to whichever sub-band its center
        # lands in, leaving the others empty.
        self.assertEqual(table["number of rows"], 3)
        all_contents = [
            k.get("content", "")
            for row in table["rows"] for c in row["cells"]
            for k in (c.get("paragraphs") or [])
        ]
        # The full phrase must still appear intact somewhere — i.e., the
        # fallback didn't tokenize it.
        self.assertIn("도박기계 사행성 오락기구", all_contents)


class DottedRuleSplitAdapterIntegrationTests(unittest.TestCase):
    """End-to-end: preprocessed raw table flows through adapter correctly."""

    def test_split_table_converts_to_two_row_table_ir(self) -> None:
        from document_processor.pdf.odl import adapter as odl_adapter

        table = _single_cell_table(
            paragraphs=[
                _paragraph("Top", left=14.0, bottom=58.0, right=108.0, top=82.0),
                _paragraph("Bottom", left=14.0, bottom=18.0, right=108.0, top=42.0),
            ]
        )
        dotted = _dotted_primitive(orientation="horizontal", left=10.0, bottom=49.5, right=110.0, top=50.5)
        _apply_dotted_splits(table, dotted_h=[dotted], dotted_v=[])
        table_ir = odl_adapter._table_node_to_ir(table, unit_id="u", assets={})
        self.assertEqual(table_ir.row_count, 2)
        self.assertEqual(table_ir.col_count, 1)
        cells_by_row = {cell.row_index: cell for cell in table_ir.cells}
        self.assertEqual(cells_by_row[1].text.strip(), "Top")
        self.assertEqual(cells_by_row[2].text.strip(), "Bottom")


if __name__ == "__main__":
    unittest.main()
