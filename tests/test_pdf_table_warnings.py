from __future__ import annotations

import unittest

from document_processor.pdf.diagnostics.table_warnings import detect_pdf_table_warnings


class PdfTableWarningTests(unittest.TestCase):
    def test_detects_dense_lineart_page_without_promoted_table(self) -> None:
        raw_document = {
            "file name": "dense-lineart.pdf",
            "kids": [
                *_text_chunks(page_number=2, count=120, bbox=[80.0, 100.0, 500.0, 700.0]),
            ],
            "line arts": _lineart_grid(page_number=2, x_count=7, y_count=18),
        }

        warnings = detect_pdf_table_warnings(raw_document, source_path="dense-lineart.pdf")

        self.assertEqual(1, len(warnings))
        warning = warnings[0]
        self.assertEqual(2, warning.page_number)
        self.assertEqual(
            "possible_table_mismatch - dense line-art grid was not parsed as a table",
            warning.message,
        )

    def test_detects_adjacent_split_tables_with_tight_gap(self) -> None:
        raw_document = {
            "file name": "split-table.pdf",
            "kids": [
                _table(
                    page_number=1,
                    table_id=10,
                    bbox=[63.78, 682.198, 531.338, 727.614],
                    rows=2,
                    cols=8,
                ),
                _table(
                    page_number=1,
                    table_id=11,
                    bbox=[65.77, 325.956, 529.224, 676.001],
                    rows=16,
                    cols=2,
                ),
            ],
        }

        warnings = detect_pdf_table_warnings(raw_document, source_path="split-table.pdf")

        self.assertEqual(1, len(warnings))
        warning = warnings[0]
        self.assertEqual(1, warning.page_number)
        self.assertEqual(
            "possible_table_mismatch - adjacent tables may be one split table",
            warning.message,
        )

    def test_does_not_flag_adjacent_tables_with_loose_gap(self) -> None:
        raw_document = {
            "file name": "separate-tables.pdf",
            "kids": [
                _table(
                    page_number=1,
                    table_id=10,
                    bbox=[63.78, 690.0, 531.338, 730.0],
                    rows=2,
                    cols=8,
                ),
                _table(
                    page_number=1,
                    table_id=11,
                    bbox=[65.77, 300.0, 529.224, 680.0],
                    rows=16,
                    cols=2,
                ),
            ],
        }

        warnings = detect_pdf_table_warnings(raw_document, source_path="separate-tables.pdf")

        self.assertEqual([], warnings)

    def test_detects_shallow_open_border_table_risk(self) -> None:
        raw_document = {
            "file name": "open-border.pdf",
            "kids": [
                _table(
                    page_number=1,
                    table_id=20,
                    bbox=[50.733, 386.757, 549.964, 482.493],
                    rows=2,
                    cols=15,
                ),
                _table(
                    page_number=1,
                    table_id=21,
                    bbox=[59.188, 72.101, 554.162, 278.716],
                    rows=2,
                    cols=4,
                ),
            ],
            "line arts": [
                {"type": "line art", "page number": 1, "bounding box": [50.733, 71.921, 553.982, 482.113]},
            ],
        }

        warnings = detect_pdf_table_warnings(raw_document, source_path="open-border.pdf")

        self.assertEqual(2, len(warnings))
        warning = warnings[0]
        self.assertEqual(1, warning.page_number)
        self.assertEqual(
            "possible_table_mismatch - open-border/gradient-like table may have missing vertical grid lines",
            warning.message,
        )

    def test_detects_sparse_open_border_candidate_without_promoted_table(self) -> None:
        raw_document = {
            "file name": "sparse-open-border.pdf",
            "kids": [
                *_text_chunks(page_number=8, count=40, bbox=[65.0, 525.0, 545.0, 735.0]),
            ],
            "line arts": [
                {"type": "line art", "page number": 8, "bounding box": [59.368, 717.415, 92.47, 740.81]},
                {"type": "line art", "page number": 8, "bounding box": [92.11, 717.415, 141.764, 740.81]},
                {"type": "line art", "page number": 8, "bounding box": [141.524, 717.415, 190.218, 740.81]},
                {"type": "line art", "page number": 8, "bounding box": [189.858, 717.415, 551.104, 740.81]},
                {"type": "line art", "page number": 8, "bounding box": [59.488, 521.01, 551.104, 740.67]},
                {"type": "line art", "page number": 8, "bounding box": [59.488, 717.415, 92.35, 719.333]},
                {"type": "line art", "page number": 8, "bounding box": [92.35, 717.415, 141.644, 719.333]},
                {"type": "line art", "page number": 8, "bounding box": [141.644, 717.415, 190.098, 719.333]},
                {"type": "line art", "page number": 8, "bounding box": [190.098, 717.415, 550.984, 719.333]},
            ],
        }

        warnings = detect_pdf_table_warnings(raw_document, source_path="sparse-open-border.pdf")

        self.assertEqual(1, len(warnings))
        warning = warnings[0]
        self.assertEqual(8, warning.page_number)
        self.assertEqual(
            "possible_table_mismatch - open-border/gradient-like table may have missing vertical grid lines",
            warning.message,
        )

    def test_does_not_flag_three_row_table_in_large_visual_container(self) -> None:
        raw_document = {
            "file name": "three-row-container.pdf",
            "kids": [
                _table(
                    page_number=737,
                    table_id=30,
                    bbox=[65.568, 690.056, 784.422, 722.817],
                    rows=3,
                    cols=10,
                ),
            ],
            "line arts": [
                {"type": "line art", "page number": 737, "bounding box": [65.568, 690.056, 784.422, 722.817]},
            ],
        }

        warnings = detect_pdf_table_warnings(raw_document, source_path="three-row-container.pdf")

        self.assertEqual([], warnings)


def _table(
    *,
    page_number: int,
    table_id: int,
    bbox: list[float],
    rows: int,
    cols: int,
    missing_outer_borders: bool = False,
) -> dict:
    left, bottom, right, top = bbox
    row_height = (top - bottom) / rows
    col_width = (right - left) / cols
    raw_rows = []
    for row_index in range(rows):
        row_top = top - row_index * row_height
        row_bottom = row_top - row_height
        cells = []
        for col_index in range(cols):
            cell_left = left + col_index * col_width
            cell_right = cell_left + col_width
            cell = {
                "type": "table cell",
                "page number": page_number,
                "row number": row_index + 1,
                "column number": col_index + 1,
                "bounding box": [cell_left, row_bottom, cell_right, row_top],
                "has left border": not (missing_outer_borders and col_index == 0),
                "has right border": not (missing_outer_borders and col_index == cols - 1),
                "has top border": True,
                "has bottom border": True,
                "kids": [{"type": "paragraph", "page number": page_number, "content": f"r{row_index}c{col_index}"}],
            }
            cells.append(cell)
        raw_rows.append({"cells": cells})
    return {
        "type": "table",
        "id": table_id,
        "page number": page_number,
        "bounding box": bbox,
        "number of rows": rows,
        "number of columns": cols,
        "rows": raw_rows,
    }


def _text_chunks(*, page_number: int, count: int, bbox: list[float]) -> list[dict]:
    left, bottom, right, top = bbox
    result = []
    for index in range(count):
        x = left + (index % 10) * ((right - left) / 10)
        y = bottom + (index % 12) * ((top - bottom) / 12)
        result.append(
            {
                "type": "paragraph",
                "page number": page_number,
                "bounding box": [x, y, min(x + 20.0, right), min(y + 8.0, top)],
                "content": f"text-{index}",
            }
        )
    return result


def _lineart_grid(*, page_number: int, x_count: int, y_count: int) -> list[dict]:
    left = 55.0
    bottom = 70.0
    width = 470.0
    height = 710.0
    cell_width = width / x_count
    cell_height = height / y_count
    result = []
    for row_index in range(y_count):
        for col_index in range(x_count):
            x0 = left + col_index * cell_width
            y0 = bottom + row_index * cell_height
            result.append(
                {
                    "type": "line art",
                    "page number": page_number,
                    "bounding box": [x0, y0, x0 + cell_width, y0 + cell_height],
                }
            )
    return result


if __name__ == "__main__":
    unittest.main()
