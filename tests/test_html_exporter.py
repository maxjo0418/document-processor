from __future__ import annotations

from pathlib import Path
import sys
import unittest

THIS_DIR = Path(__file__).resolve().parent
SRC_ROOT = THIS_DIR.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from document_processor import (
    DocIR,
    DocumentInput,
    TextAnnotation,
    render_review_html,
)
from document_processor.models import ImageAsset, ImageIR, NativeAnchor, PageInfo, ParagraphIR, RunIR, TableCellIR, TableIR
from document_processor.style_types import CellStyleInfo, ColumnLayoutInfo, ListItemInfo, ParaStyleInfo, RunStyleInfo, TableStyleInfo
from document_processor.pdf.odl.adapter import _pdf_node_kwargs


def _render_review_html_for_doc(doc: DocIR, annotations: list[TextAnnotation] | None = None) -> str:
    result = render_review_html(
        document=DocumentInput(doc_ir=doc),
        annotations=annotations or [],
    )
    assert result.ok
    assert result.html is not None
    return result.html


class HtmlExporterTests(unittest.TestCase):
    def test_export_html_preserves_spans_for_native_covered_slot_duplicates(self) -> None:
        def paragraph(text: str) -> ParagraphIR:
            return ParagraphIR(content=[RunIR(text=text)])

        def cell(path: str, text: str, style: CellStyleInfo | None = None) -> TableCellIR:
            table_cell = TableCellIR(
                native_anchor=NativeAnchor(
                    node_kind="cell",
                    debug_path=path,
                    structural_path=path,
                ),
                cell_style=style,
                paragraphs=[paragraph(text)],
            )
            table_cell.recompute_text()
            return table_cell

        table = TableIR(
            row_count=2,
            col_count=3,
            cells=[
                [
                    cell("s1.p1.r1.tbl1.tr1.tc1", "Merged", CellStyleInfo(rowspan=2, colspan=2)),
                    cell("s1.p1.r1.tbl1.tr1.tc2", "Merged"),
                    cell("s1.p1.r1.tbl1.tr1.tc3", "Right"),
                ],
                [
                    cell("s1.p1.r1.tbl1.tr2.tc1", "Merged"),
                    cell("s1.p1.r1.tbl1.tr2.tc2", "Merged"),
                    cell("s1.p1.r1.tbl1.tr2.tc3", "Bottom"),
                ],
            ],
        )
        doc = DocIR(paragraphs=[ParagraphIR(content=[table])])

        self.assertIs(table.cells[0][1], table.cells[0][0])
        self.assertIs(table.cells[1][0], table.cells[0][0])
        self.assertIs(table.cells[1][1], table.cells[0][0])
        for rendered in (doc.to_html(), _render_review_html_for_doc(doc)):
            self.assertIn('colspan="2"', rendered)
            self.assertIn('rowspan="2"', rendered)
            self.assertEqual(rendered.count("<td"), 3)
            self.assertEqual(rendered.count("Merged"), 1)

    def test_export_html_preserves_docx_vertical_merges_from_file(self) -> None:
        doc = DocIR.from_file(THIS_DIR / "doc_samples/new_test/style_test_sample.docx")

        def iter_tables(paragraph: ParagraphIR):
            for table in paragraph.tables:
                yield table
                for cell in table.iter_cells():
                    for cell_paragraph in cell.paragraphs:
                        yield from iter_tables(cell_paragraph)

        tables = [table for paragraph in doc.paragraphs for table in iter_tables(paragraph)]
        merged_tables = [
            table
            for table in tables
            if any(cell.cell_style and cell.cell_style.rowspan > 1 for cell in table.iter_cells())
        ]

        self.assertEqual(len(merged_tables), 2)
        for table in merged_tables:
            self.assertIs(table.cells[2][1], table.cells[1][1])

        html = doc.to_html()
        self.assertEqual(html.count('rowspan="2"'), 2)

    def test_export_html_renders_run_and_paragraph_styles(self) -> None:
        doc = DocIR(doc_id="sample",
            paragraphs=[
                ParagraphIR(para_style=ParaStyleInfo(align="center", first_line_indent_pt=12.0),
                    content=[
                        RunIR(text="Hello  world",
                            run_style=RunStyleInfo(font_family="Noto Serif KR",
                                bold=True,
                                italic=True,
                                underline=True,
                                color="#112233",
                                size_pt=11.0,
                            ),
                        )
                    ],
                )
            ],
        )

        html = doc.to_html(title="Preview")

        self.assertIn("<title>Preview</title>", html)
        self.assertIn("text-align:center", html)
        self.assertIn("text-indent:12.0pt", html)
        self.assertIn("font-family:Noto Serif KR", html)
        self.assertIn("font-size:11.0pt", html)
        self.assertIn("color:#112233", html)
        self.assertIn("<b>Hello", html)
        self.assertIn("<i><b>", html)
        self.assertIn("&nbsp;&nbsp;", html)

    def test_export_html_renders_newlines_inside_runs_as_line_breaks(self) -> None:
        doc = DocIR(
            paragraphs=[
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", "s1.p1"),
                    content=[RunIR(**_pdf_node_kwargs("run", "s1.p1.r1"), text="Alpha\nBeta")],
                )
            ],
        )

        html = doc.to_html()

        self.assertIn("Alpha<br>Beta", html)

    def test_export_html_renders_tables_and_cell_styles(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(content=[
                        TableIR(table_style=TableStyleInfo(width_pt=240.0),
                            cells=[
                                [
                                    TableCellIR(
                                    cell_style=CellStyleInfo(background="#ffeeaa",
                                        horizontal_align="center",
                                        width_pt=120.0,
                                        height_pt=36.0,
                                        border_top="1px solid #000",
                                        border_bottom="1px solid #000",
                                        border_left="5pt single #000",
                                        border_right="1px solid #000",
                                        colspan=2,
                                    ),
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="A1")],
                                        )
                                    ],
                                    ),
                                ],
                                [
                                    TableCellIR(
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="B1")],
                                        )
                                    ],
                                    ),
                                ],
                            ],
                        )
                    ],
                )
            ]
        )

        html = doc.to_html(title="Table Preview")

        self.assertIn("<table", html)
        self.assertIn('colspan="2"', html)
        self.assertIn("background-color:#ffeeaa", html)
        self.assertIn("text-align:center", html)
        self.assertIn("A1", html)
        self.assertIn("B1", html)
        self.assertIn("width:240.0pt", html)
        self.assertIn("width:120.0pt", html)
        self.assertIn("height:36.0pt", html)
        self.assertIn("border-left:5pt solid #000", html)
        self.assertIn("margin-left:0", html)
        self.assertIn("margin-right:auto", html)

    def test_export_html_prioritizes_native_cell_alignment_over_cell_paragraph_alignment(self) -> None:
        doc = DocIR(source_doc_type="docx",
            paragraphs=[
                ParagraphIR(content=[
                        TableIR(cells=[
                                [
                                    TableCellIR(
                                    cell_style=CellStyleInfo(horizontal_align="left", vertical_align="center"),
                                    paragraphs=[
                                        ParagraphIR(para_style=ParaStyleInfo(align="right"),
                                            content=[RunIR(text="Explicit cell alignment")],
                                        )
                                    ],
                                    ),
                                ],
                                [
                                    TableCellIR(
                                    paragraphs=[
                                        ParagraphIR(para_style=ParaStyleInfo(align="center"),
                                            content=[RunIR(text="Default cell alignment")],
                                        )
                                    ],
                                    ),
                                ],
                            ],
                        )
                    ],
                )
            ],
        )

        for rendered in (doc.to_html(), _render_review_html_for_doc(doc)):
            self.assertIn("Explicit cell alignment", rendered)
            self.assertIn("Default cell alignment", rendered)
            self.assertIn("vertical-align:middle", rendered)
            self.assertIn("text-align:left", rendered)
            self.assertIn("text-align:center", rendered)
            self.assertNotIn("text-align:right", rendered)

    def test_export_html_emits_fixed_columns_for_spanned_cell_widths(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(content=[
                        TableIR(col_count=3,
                            table_style=TableStyleInfo(width_pt=120.0, col_count=3),
                            cells=[
                                [
                                    TableCellIR(
                                    cell_style=CellStyleInfo(width_pt=30.0, vertical_align="center"),
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="Label")],
                                        )
                                    ],
                                    ),
                                    TableCellIR(
                                    cell_style=CellStyleInfo(width_pt=90.0, colspan=2),
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="Value")],
                                        )
                                    ],
                                    ),
                                ],
                            ],
                        )
                    ],
                )
            ]
        )

        html = doc.to_html()
        annotated_html = _render_review_html_for_doc(doc)

        for rendered in (html, annotated_html):
            self.assertIn("table-layout:fixed", rendered)
            self.assertIn("<colgroup>", rendered)
            self.assertIn('<col style="width:30.0pt" />', rendered)
            self.assertEqual(rendered.count('<col style="width:45.0pt" />'), 2)
            self.assertIn("box-sizing:border-box", rendered)
            self.assertIn("padding:0", rendered)
            self.assertIn("td p", rendered)
            self.assertIn("line-height: 1.0", rendered)
            self.assertIn("vertical-align:middle", rendered)
            self.assertNotIn("vertical-align:center", rendered)

    def test_export_html_renders_explicit_cell_padding(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(content=[
                        TableIR(cells=[
                                [
                                    TableCellIR(
                                    cell_style=CellStyleInfo(padding_top_pt=1.4,
                                        padding_right_pt=5.1,
                                        padding_bottom_pt=1.4,
                                        padding_left_pt=5.1,
                                    ),
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="Padded")],
                                        )
                                    ],
                                    )
                                ]
                            ],
                        )
                    ],
                )
            ]
        )

        html = doc.to_html()
        annotated_html = _render_review_html_for_doc(doc)

        for rendered in (html, annotated_html):
            self.assertIn("padding:1.4pt 5.1pt 1.4pt 5.1pt", rendered)
            self.assertNotIn("padding:4px 6px", rendered)

    def test_export_html_renders_cell_diagonal_borders(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(content=[
                        TableIR(cells=[
                                [
                                    TableCellIR(
                                    cell_style=CellStyleInfo(diagonal_tl_br="1px solid #000000",
                                        diagonal_tr_bl="1px dashed #ff0000",
                                        border_top="1px solid #000000",
                                        border_bottom="1px solid #000000",
                                        border_left="1px solid #000000",
                                        border_right="1px solid #000000",
                                    ),
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="Diag")],
                                        )
                                    ],
                                    )
                                ]
                            ],
                        )
                    ],
                )
            ]
        )

        html = doc.to_html()

        self.assertIn("background-image:url(data:image/svg+xml;base64,", html)
        self.assertNotIn('background-image:url("data:image/svg+xml,', html)
        self.assertIn("Diag", html)

    def test_export_html_leaves_justify_table_left_aligned_by_default(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(para_style=ParaStyleInfo(align="justify"),
                    content=[
                        TableIR(cells=[
                                [
                                    TableCellIR(
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="Cell")],
                                        )
                                    ],
                                    )
                                ]
                            ],
                        )
                    ],
                )
            ]
        )

        html = doc.to_html()

        self.assertIn("<table", html)
        self.assertIn("margin-left:0", html)
        self.assertIn("margin-right:auto", html)

    def test_export_html_renders_table_grid_when_table_style_requests_render_grid(self) -> None:
        doc = DocIR(
            source_doc_type="pdf",
            paragraphs=[
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", "s1.p1"),
                    content=[
                        TableIR(
                            **_pdf_node_kwargs("table", "s1.p1.r1.tbl1"),
                            table_style=TableStyleInfo(render_grid=True),
                            cells=[
                                [
                                    TableCellIR(
                                    **_pdf_node_kwargs("cell", "s1.p1.r1.tbl1.tr1.tc1"),
                                    paragraphs=[
                                        ParagraphIR(
                                            **_pdf_node_kwargs("paragraph", "s1.p1.r1.tbl1.tr1.tc1.p1"),
                                            content=[RunIR(**_pdf_node_kwargs("run", "x"), text="A1")],
                                        )
                                    ],
                                    ),
                                    TableCellIR(
                                    **_pdf_node_kwargs("cell", "s1.p1.r1.tbl1.tr1.tc2"),
                                    paragraphs=[
                                        ParagraphIR(
                                            **_pdf_node_kwargs("paragraph", "s1.p1.r1.tbl1.tr1.tc2.p1"),
                                            content=[RunIR(**_pdf_node_kwargs("run", "y"), text="B1")],
                                        )
                                    ],
                                    ),
                                ],
                            ],
                        )
                    ],
                )
            ],
        )

        html = doc.to_html()

        self.assertIn("border-top:1px solid #4a4f57", html)
        self.assertIn("border-right:1px solid #4a4f57", html)
        self.assertIn("A1", html)
        self.assertIn("B1", html)

    def test_export_html_renders_pdf_heading_tag_from_paragraph_style(self) -> None:
        doc = DocIR(
            source_doc_type="pdf",
            paragraphs=[
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", "s1.p1"),
                    para_style=ParaStyleInfo(render_tag="h2"),
                    content=[RunIR(**_pdf_node_kwargs("run", "x"), text="Heading")],
                )
            ],
        )

        html = doc.to_html()

        self.assertIn("<h2", html)
        self.assertIn("Heading</h2>", html)

    def test_export_html_uses_image_display_size(self) -> None:
        doc = DocIR(assets={
                "img1": ImageAsset(mime_type="image/png",
                    filename="x.png",
                    data_base64="AAAA",
                    intrinsic_width_px=1,
                    intrinsic_height_px=1,
                )
            },
            paragraphs=[
                ParagraphIR(content=[
                        ImageIR(image_id="img1",
                            display_width_pt=72.0,
                            display_height_pt=36.0,
                        )
                    ],
                )
            ],
        )

        html = doc.to_html()

        self.assertIn("<img ", html)
        self.assertIn("width:72.0pt", html)
        self.assertIn("height:36.0pt", html)

    def test_export_html_renders_multi_image_paragraph_inline(self) -> None:
        doc = DocIR(
            assets={
                "img1": ImageAsset(mime_type="image/png", filename="a.png", data_base64="AAAA"),
                "img2": ImageAsset(mime_type="image/png", filename="b.png", data_base64="AAAA"),
                "img3": ImageAsset(mime_type="image/png", filename="c.png", data_base64="AAAA"),
            },
            paragraphs=[
                ParagraphIR(
                    **_pdf_node_kwargs("paragraph", "s1.p1"),
                    content=[
                        ImageIR(**_pdf_node_kwargs("image", "s1.p1.img1"), image_id="img1", display_width_pt=72.0, display_height_pt=4.0),
                        ImageIR(**_pdf_node_kwargs("image", "s1.p1.img2"), image_id="img2", display_width_pt=72.0, display_height_pt=4.0),
                        ImageIR(**_pdf_node_kwargs("image", "s1.p1.img3"), image_id="img3", display_width_pt=72.0, display_height_pt=4.0),
                    ],
                )
            ],
        )

        html = doc.to_html()

        self.assertNotIn("line-height:0", html)
        self.assertNotIn("display:block", html)
        self.assertEqual(html.count("<img "), 3)

    def test_export_html_renders_bordered_page_frames_when_page_metadata_exists(self) -> None:
        doc = DocIR(pages=[
                PageInfo(page_number=1, width_pt=595.3, height_pt=841.9, margin_top_pt=72.0, margin_right_pt=72.0, margin_bottom_pt=72.0, margin_left_pt=72.0),
                PageInfo(page_number=2, width_pt=595.3, height_pt=841.9, margin_top_pt=72.0, margin_right_pt=72.0, margin_bottom_pt=72.0, margin_left_pt=72.0),
            ],
            paragraphs=[
                ParagraphIR(page_number=1, content=[RunIR(text="First page")]),
                ParagraphIR(page_number=2, content=[RunIR(text="Second page")]),
            ],
        )

        html = doc.to_html()

        self.assertIn('class="document-page"', html)
        self.assertIn('data-page-number="1"', html)
        self.assertIn('data-page-number="2"', html)
        self.assertIn("border:1px solid #222", html)
        self.assertIn("width:595.3pt", html)
        self.assertIn("min-height:841.9pt", html)
        self.assertIn("padding:72.0pt 72.0pt 72.0pt 72.0pt", html)
        self.assertIn("First page", html)
        self.assertIn("Second page", html)

    def test_export_html_groups_paragraphs_by_column_layout(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(content=[RunIR(text="Title")]),
                ParagraphIR(para_style=ParaStyleInfo(column_layout=ColumnLayoutInfo(count=2, gap_pt=18.0)),
                    content=[RunIR(text="Column body one")],
                ),
                ParagraphIR(para_style=ParaStyleInfo(column_layout=ColumnLayoutInfo(count=2, gap_pt=18.0)),
                    content=[RunIR(text="Column body two")],
                ),
                ParagraphIR(content=[RunIR(text="Footer")]),
            ],
        )

        for html in (doc.to_html(), _render_review_html_for_doc(doc)):
            self.assertIn('class="document-column-group"', html)
            self.assertIn('data-column-count="2"', html)
            self.assertIn("column-count:2", html)
            self.assertIn("column-gap:18.0pt", html)
            self.assertLess(html.index("Title"), html.index('class="document-column-group"'))
            self.assertLess(html.index('class="document-column-group"'), html.index("Column body one"))
            self.assertLess(html.index("Column body two"), html.index("Footer"))

    def test_export_html_renders_indexed_column_group_when_one_side_is_empty(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(
                    para_style=ParaStyleInfo(
                        column_layout=ColumnLayoutInfo(count=2, column_index=0, gap_pt=18.0)
                    ),
                    content=[RunIR(text="Only left column")],
                ),
            ],
        )

        html = doc.to_html()

        self.assertIn('document-column-group--indexed', html)
        self.assertIn('data-column-index="1">', html)
        self.assertIn('data-column-index="2">&nbsp;</div>', html)
        self.assertIn("Only left column", html)

    def test_export_html_renders_paragraph_list_markers(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(
                    para_style=ParaStyleInfo(list_info=ListItemInfo(list_id="list-1", level=1, marker="a)")),
                    content=[RunIR(text="Nested list item")],
                )
            ],
        )

        for html in (doc.to_html(), _render_review_html_for_doc(doc)):
            self.assertIn('class="document-list-marker"', html)
            self.assertIn(">a)</span>", html)
            self.assertIn("Nested list item", html)

    def test_export_html_normalizes_private_use_bullet_markers(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(
                    para_style=ParaStyleInfo(
                        list_info=ListItemInfo(list_id="list-1", level=0, marker="\uf06c", marker_type="bullet")
                    ),
                    content=[RunIR(text="Bullet list item")],
                )
            ],
        )

        for html in (doc.to_html(), _render_review_html_for_doc(doc)):
            self.assertIn(">•</span>", html)
            self.assertIn("Bullet list item", html)
            self.assertNotIn("\uf06c", html)

    def test_export_html_clamps_negative_first_line_indent_inside_table_cells(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(content=[
                        TableIR(cells=[
                                [
                                    TableCellIR(
                                    paragraphs=[
                                        ParagraphIR(para_style=ParaStyleInfo(align="center",
                                                first_line_indent_pt=-159.3,
                                            ),
                                            content=[RunIR(text="스토리")],
                                        )
                                    ],
                                    )
                                ]
                            ],
                        )
                    ],
                )
            ]
        )

        html = doc.to_html()

        self.assertIn("text-indent:0.0pt", html)
        self.assertNotIn("text-indent:-159.3pt", html)

    def test_export_html_clamps_negative_first_line_indent_for_top_level_paragraphs(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(para_style=ParaStyleInfo(first_line_indent_pt=-27.6),
                    content=[RunIR(text="Bullet-like text")],
                )
            ]
        )

        html = doc.to_html()

        self.assertIn("text-indent:0.0pt", html)
        self.assertNotIn("text-indent:-27.6pt", html)

    def test_export_html_preserves_hanging_indent_inside_left_indent(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(para_style=ParaStyleInfo(left_indent_pt=24.0, first_line_indent_pt=-6.0),
                    content=[RunIR(text="Bullet-like text")],
                )
            ]
        )

        html = doc.to_html()

        self.assertIn("padding-left:24.0pt", html)
        self.assertIn("text-indent:-6.0pt", html)

    def test_export_html_limits_hanging_indent_to_left_edge(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(para_style=ParaStyleInfo(left_indent_pt=12.0, right_indent_pt=-4.0, first_line_indent_pt=-30.0),
                    content=[RunIR(text="Contained text")],
                )
            ]
        )

        html = doc.to_html()

        self.assertIn("padding-left:12.0pt", html)
        self.assertIn("padding-right:0.0pt", html)
        self.assertIn("text-indent:-12.0pt", html)
        self.assertNotIn("text-indent:-30.0pt", html)

    def test_review_html_uses_same_indent_clamp(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(para_style=ParaStyleInfo(first_line_indent_pt=-27.6),
                    content=[RunIR(text="Review text")],
                )
            ]
        )

        html = _render_review_html_for_doc(
            doc,
            [TextAnnotation(target_kind="paragraph", target_id=doc.paragraphs[0].node_id, label="Review")],
        )

        self.assertIn("text-indent:0.0pt", html)
        self.assertNotIn("text-indent:-27.6pt", html)

    def test_export_html_debug_layout_adds_measurement_overlays(self) -> None:
        doc = DocIR(pages=[PageInfo(page_number=1, width_pt=595.3, height_pt=841.9, margin_left_pt=72.0)],
            paragraphs=[
                ParagraphIR(page_number=1,
                    content=[
                        TableIR(table_style=TableStyleInfo(width_pt=240.0),
                            cells=[
                                [
                                    TableCellIR(
                                    cell_style=CellStyleInfo(width_pt=120.0, height_pt=36.0),
                                    paragraphs=[
                                        ParagraphIR(content=[RunIR(text="A1")],
                                        )
                                    ],
                                    )
                                ]
                            ],
                        )
                    ],
                )
            ],
        )

        html = doc.to_html(debug_layout=True)

        self.assertIn('class="document-debug-layout"', html)
        self.assertIn("data-debug-label=\"page 1: 595.3pt x 841.9pt", html)
        self.assertIn("data-debug-label=\"table s1.p1.r1.tbl1: 240.0pt x auto", html)
        self.assertIn("data-debug-label=\"cell s1.p1.r1.tbl1.tr1.tc1: 120.0pt x 36.0pt", html)
        self.assertIn("getBoundingClientRect", html)

    def test_export_html_renders_nested_tables(self) -> None:
        doc = DocIR(paragraphs=[
                ParagraphIR(content=[
                        TableIR(cells=[
                                [
                                    TableCellIR(
                                    paragraphs=[
                                        ParagraphIR(content=[
                                                RunIR(text="Outer"),
                                                TableIR(cells=[
                                                        [
                                                            TableCellIR(
                                                            paragraphs=[
                                                                ParagraphIR(content=[RunIR(text="Inner")],
                                                                )
                                                            ],
                                                            )
                                                        ]
                                                    ],
                                                )
                                            ],
                                        )
                                    ],
                                    )
                                ]
                            ],
                        )
                    ],
                )
            ]
        )

        html = doc.to_html()

        self.assertGreaterEqual(html.count("<table"), 2)
        self.assertIn("Outer", html)
        self.assertIn("Inner", html)


if __name__ == "__main__":
    unittest.main()
