# Usage Guide

This guide shows common `document_processor` workflows with executable Python
examples.

## Installation

```bash
pip install document-processor
```

For local development against a checkout:

```bash
uv pip install -e /path/to/document-processor
```

For model diagrams:

```bash
pip install "document-processor[viz]"
```

## 1. Parse A Native Document

### From a file path

```python
from document_processor import DocIR

doc = DocIR.from_file("/path/to/contract.docx")

print(doc.source_doc_type)
print(doc.source_path)
print(len(doc.paragraphs))
print(doc.paragraphs[0].node_id, doc.paragraphs[0].text)
```

### From bytes

```python
from pathlib import Path

from document_processor import DocIR

doc_bytes = Path("/path/to/contract.hwpx").read_bytes()
doc = DocIR.from_file(doc_bytes, doc_type="hwpx")

print(doc.source_doc_type)
print(doc.paragraphs[0].text)
```

### From a binary file object

```python
from document_processor import DocIR

with open("/path/to/contract.docx", "rb") as handle:
    doc = DocIR.from_file(handle)

print(doc.paragraphs[0].text)
```

## Logging

The package logger is `document_processor`. It is initialized automatically with
level `WARNING` and a console handler. Enable more detail or file output once at
application startup:

```python
from document_processor import DocIR, configure_logging, get_logger

configure_logging(level="INFO")
configure_logging(level="DEBUG", log_file="logs/document-processor.log")

doc = DocIR.from_file("/path/to/contract.docx")

logger = get_logger(__name__)
logger.info("Loaded %d paragraphs", len(doc.paragraphs))
```

Inside package helpers, use child loggers instead of `print()`:

```python
from document_processor import get_logger

logger = get_logger(__name__)


def helper() -> None:
    logger.debug("Detailed helper state")
    logger.warning("Recoverable issue")
```

`DocIR.configure_logging(...)` is an equivalent convenience wrapper around
`configure_logging(...)`.

## 2. Build A Synthetic `DocIR`

This is useful for tests, prototyping, and examples.

```python
from document_processor import DocIR

doc = DocIR.from_mapping(
    {
        "s1.p1.r1": "Hello ",
        "s1.p1.r2": "World",
        "s1.p2.r1": "Second paragraph",
    },
    source_doc_type="docx",
)

print(doc.paragraphs[0].text)
print([run.node_id for run in doc.paragraphs[0].runs])
```

## 3. Inspect The IR

```python
from document_processor import DocIR

doc = DocIR.from_file("/path/to/contract.docx")

for paragraph in doc.paragraphs[:3]:
    marker = paragraph.para_style.list_info.marker if paragraph.para_style and paragraph.para_style.list_info else ""
    print(paragraph.node_id, paragraph.page_number, marker, paragraph.text)
    for run in paragraph.runs:
        print(" ", run.node_id, repr(run.text))
```

Useful helpers:

- `paragraph.runs`
- `paragraph.images`
- `paragraph.tables`
- `paragraph.para_style.list_info` for resolved list markers
- `paragraph.para_style.column_layout` for multi-column layout
- `table.table_style.placement` for floating table placement metadata when present
- `image.placement` for floating image placement metadata when present
- `table.markdown`
- `doc.pages`

## 4. Render HTML

### Standard document preview

```python
from document_processor import DocIR

doc = DocIR.from_file("/path/to/contract.docx")
html = doc.to_html(title="Preview")

with open("preview.html", "w", encoding="utf-8") as handle:
    handle.write(html)
```

For layout investigation, render an instrumented preview:

```python
debug_html = doc.to_html(title="Layout Debug", debug_layout=True)
```

The debug view outlines pages, tables, cells, and paragraphs, then annotates
each element with declared point sizes and measured browser-rendered sizes.
HTML rendering also clamps negative paragraph indents so text starts within the
page or table-cell content edge. Table cell margins from the source document
are exposed as `CellStyleInfo.padding_*_pt` and rendered as cell padding.
Table/image dimensions render to HTML, but floating placement and text wrapping
metadata is not fully projected to HTML yet.

### Annotated review preview

```python
from document_processor import (
    DocIR,
    DocumentInput,
    TextAnnotation,
    render_review_html,
)

doc = DocIR.from_mapping({"s1.p1.r1": "Hello ", "s1.p1.r2": "World"})
paragraph_id = doc.paragraphs[0].node_id

review = render_review_html(
    document=DocumentInput(doc_ir=doc),
    annotations=[
        TextAnnotation(
            target_kind="paragraph",
            target_id=paragraph_id,
            selected_text="World",
            label="Focus",
            color="#FFEE88",
            note="Review this phrase",
        )
    ],
    title="Annotated Review",
)
html = review.html
```

If the same substring repeats inside the target, set `occurrence_index`
instead of guessing offsets:

```python
doc = DocIR.from_mapping({"s1.p1.r1": "Beta Beta Beta"})
run_id = doc.paragraphs[0].runs[0].node_id

review = render_review_html(
    document=DocumentInput(doc_ir=doc),
    annotations=[
        TextAnnotation(
            target_kind="run",
            target_id=run_id,
            selected_text="Beta",
            occurrence_index=1,
            label="Second match",
        )
    ],
)
html = review.html
```

## 5. Use The Stateless Edit API

The high-level edit API works with `DocumentInput`.

### Path-backed workflow

```python
from document_processor import (
    DocumentInput,
    TextEdit,
    apply_document_edits,
    read_document,
)

preview = read_document(
    document=DocumentInput(source_path="/path/to/contract.docx"),
    start=0,
    limit=5,
    include_runs=True,
)
first_paragraph_id = preview.paragraphs[0].node_id

result = apply_document_edits(
    document=DocumentInput(source_path="/path/to/contract.docx"),
    edits=[
        TextEdit(
            target_kind="paragraph",
            target_id=first_paragraph_id,
            expected_text="Hello World",
            new_text="Hello Legal World",
            reason="Expand wording",
        )
    ],
    output_filename="contract_reviewed.docx",
    return_doc_ir=True,
)

print(result.ok)
print(result.output_path)
print(result.modified_target_ids)
print(result.updated_doc_ir.paragraphs[0].text)
```

### Bytes-backed workflow

```python
from pathlib import Path

from document_processor import (
    DocumentInput,
    DocIR,
    TextEdit,
    apply_document_edits,
    read_document,
)

source_bytes = Path("/path/to/contract.docx").read_bytes()
document = DocumentInput(
    source_bytes=source_bytes,
    source_name="contract.docx",
)
preview = read_document(
    document=document,
    start=0,
    limit=1,
    include_runs=False,
)

result = apply_document_edits(
    document=document,
    edits=[
        TextEdit(
            target_kind="paragraph",
            target_id=preview.paragraphs[0].node_id,
            expected_text="Hello World",
            new_text="Hello Contract World",
            reason="Clarify wording",
        )
    ],
    return_doc_ir=True,
)

edited_doc = DocIR.from_file(result.output_bytes, doc_type="docx")
print(result.output_filename)
print(edited_doc.paragraphs[0].text)
```

### `DocIR`-only workflow

Use this when you want in-memory updates without native file output.

```python
from document_processor import (
    DocumentInput,
    DocIR,
    TextEdit,
    apply_document_edits,
)

doc = DocIR.from_mapping(
    {
        "s1.p1.r1": "Hello ",
        "s1.p1.r2": "World",
        "s1.p2.r1.tbl1.tr1.tc1.p1.r1": "Old cell text",
    },
    source_doc_type="docx",
)
first_paragraph_id = doc.paragraphs[0].node_id

result = apply_document_edits(
    document=DocumentInput(doc_ir=doc),
    edits=[
        TextEdit(
            target_kind="paragraph",
            target_id=first_paragraph_id,
            expected_text="Hello World",
            new_text="Hello Contract World",
        )
    ],
)

print(result.updated_doc_ir.paragraphs[0].text)
print(result.output_path)
print(result.output_bytes)
```

Output behavior depends on the input source:

- `DocumentInput(doc_ir=...)` returns an edited `DocIR` only; it cannot write a
  native DOCX/HWPX file because there is no original package to patch.
- `DocumentInput(source_path=...)` writes to `output_path`, `output_filename`,
  or a default sibling `*_edited.*` file.
- `DocumentInput(source_bytes=...)` returns `output_bytes` and
  `output_filename`; it does not write a filesystem path.
- `output_path` and `output_filename` are mutually exclusive, and
  `output_filename` must be a filename only.
- For bytes-backed input, use `output_filename` to name the returned bytes.
  `output_path` is only written for path-backed input.
- HWP inputs are written back as HWPX. `dry_run=True` validates and previews the
  edit batch without producing native output.

### Cell text edits

Use `target_kind="cell"` to replace all editable text in a table cell. Multi-paragraph
cells must keep the same number of newline-separated text lines; this avoids creating or
deleting native document paragraphs during a cell edit.

```python
from document_processor import (
    DocumentInput,
    TextEdit,
    apply_document_edits,
    list_editable_targets,
)

document = DocumentInput(source_path="/path/to/contract.docx")
cells = list_editable_targets(
    document=document,
    target_kinds=["cell"],
    max_targets=10,
)
first_cell_id = cells.targets[0].target_id

result = apply_document_edits(
    document=document,
    edits=[
        TextEdit(
            target_kind="cell",
            target_id=first_cell_id,
            expected_text="Old cell text",
            new_text="Updated cell text",
        )
    ],
)

print(result.modified_target_ids)
```

### Structural edits

Use `StructuralEdit` for insert/remove operations and table shape changes. These
operations still target stable `node_id` values.

`TextEdit(target_kind="cell")` preserves the existing paragraph count inside a
cell. `StructuralEdit(operation="set_cell_text")` is the API to rebuild a cell's
paragraphs from newline-separated text.

Inserted tables are not bare XML shells. Native DOCX/HWPX write-back gives new
tables a visible black grid, cell padding, and non-zero geometry. HWPX tables
are written as inline objects (`treatAsChar="1"` / 글자처럼 취급). Inserted rows
and columns inherit the nearest row/cell properties where the source table has
them.

```python
from document_processor import (
    DocumentInput,
    StructuralEdit,
    apply_document_edits,
    list_editable_targets,
)

document = DocumentInput(source_path="/path/to/contract.docx")
targets = list_editable_targets(
    document=document,
    target_kinds=["paragraph", "table", "cell"],
    only_writable=False,
)

paragraph_id = next(t.target_id for t in targets.targets if t.target_kind == "paragraph")
cell_id = next(t.target_id for t in targets.targets if t.target_kind == "cell")

result = apply_document_edits(
    document=document,
    edits=[
        StructuralEdit(
            operation="insert_paragraph",
            target_id=paragraph_id,
            position="after",
            text="Inserted review note.",
        ),
        StructuralEdit(
            operation="set_cell_text",
            target_id=cell_id,
            expected_text="Old cell text",
            text="Line one\nLine two",
        ),
        StructuralEdit(
            operation="insert_table_row",
            target_id=cell_id,
            position="after",
            values=["New left", "New right"],
        ),
    ],
    output_filename="contract_structural_edit.docx",
    return_doc_ir=True,
)

print(result.output_path)
print(result.created_target_ids)
print(result.updated_doc_ir.paragraphs[0].native_anchor.structural_path)
```

For native write-back, existing `node_id` values remain stable in the returned
`updated_doc_ir`; `native_anchor.structural_path` is refreshed to the new
physical path after inserts/removes.

### Style edits

Use `StyleEdit` for flattened style mutations. Every style field is optional,
which makes the DTO suitable for LLM tool schemas: set only the fields you want
to change, and use `clear_fields` when a nullable style value should be removed.

```python
from document_processor import (
    DocumentInput,
    StyleEdit,
    apply_document_edits,
    list_editable_targets,
)

document = DocumentInput(source_path="/path/to/contract.docx")
targets = list_editable_targets(
    document=document,
    target_kinds=["run", "paragraph", "cell", "table", "image"],
    only_writable=False,
)

run_id = next(t.target_id for t in targets.targets if t.target_kind == "run")
cell_target = next(t for t in targets.targets if t.target_kind == "cell")
cell_id = cell_target.target_id
table_id = next(t.target_id for t in targets.targets if t.target_kind == "table")

result = apply_document_edits(
    document=document,
    edits=[
        StyleEdit(
            target_kind="run",
            target_id=run_id,
            bold=True,
            color="#445566",
            font_size_pt=16,
        ),
        StyleEdit(
            target_kind="cell",
            target_id=cell_id,
            background="#FFF2CC",
            vertical_align="middle",
            horizontal_align="center",
            padding_left_pt=6,
            padding_right_pt=6,
            width_pt=120,
            height_pt=36,
            border_top="1pt single #445566",
            border_right="1pt single #445566",
            border_bottom="1pt single #445566",
            border_left="1pt single #445566",
        ),
        StyleEdit(
            target_kind="table",
            target_id=table_id,
            placement_mode="floating",
            wrap="square",
            x_relative_to="page",
            y_relative_to="paragraph",
            x_offset_pt=18,
            y_offset_pt=12,
        ),
    ],
    output_filename="contract_style_edit.docx",
    return_doc_ir=True,
)

print(result.styles_applied)
print(result.modified_target_ids)
```

Supported target-specific fields:

- run: `bold`, `italic`, `underline`, `strikethrough`, `superscript`,
  `subscript`, `color`, `highlight`, `font_size_pt`
- paragraph: `paragraph_align`, `left_indent_pt`, `right_indent_pt`,
  `first_line_indent_pt`, `hanging_indent_pt`
- cell: `background`, `vertical_align`, `horizontal_align`, padding, borders,
  `width_pt`, `height_pt`
- table: `placement_mode`, `wrap`, `text_flow`, relative anchors, alignment,
  offsets, outside margins, overlap, flow, and `z_order`
- image: `width_pt`, `height_pt`, `placement_mode`, `wrap`, `text_flow`,
  relative anchors, alignment, offsets, outside margins, overlap, flow, and
  `z_order`

DOCX native write-back supports common run, paragraph, cell, table placement,
and image size/placement fields. HWPX native write-back supports common run
fields (`bold`, `italic`, `underline`, `strikethrough`, `color`,
`font_size_pt`), paragraph alignment/indent fields, cell
background/alignment/size/padding/border fields, table placement fields, and
image size/placement fields.

Cell targets returned by `list_editable_targets` include `parent_table_id`,
`row_index`, `column_index`, `rowspan`, and `colspan`. Use those fields to find
the cell id for a row/column coordinate before applying cell `width_pt` or
`height_pt`.

In `DocIR`, table cells are row-major and two-dimensional:
`table.cells[0][0]` is the first row's first cell. Merged-cell covered
coordinates point to the same `TableCellIR` object as the merge origin, so a
cell spanning columns 2-3 appears at both `table.cells[row][1]` and
`table.cells[row][2]`.

Floating placement write-back is native-format oriented. The edited DOCX/HWPX
file receives placement XML, and the preview `updated_doc_ir` contains the
requested placement object. Re-parsing native files and rendering HTML currently
preserves dimensions more completely than floating placement/wrapping metadata.

Table style edits do not accept `width_pt` or `height_pt`. Use cell style edits
for table geometry.

For both `updated_doc_ir` previews and native DOCX/HWPX write-back, a cell
`width_pt` edit updates the target cell's logical column, and a cell
`height_pt` edit updates the target row. Other cell style fields such as
background, padding, borders, and alignment apply only to the targeted cell.
Border values may use CSS-style strings such as `"1px solid #445566"` or
native-style strings such as `"1pt single #445566"`; HTML rendering normalizes
`single` to CSS `solid`.

## 6. Inspect Context Before Editing

Use this before emitting exact-match edits.

```python
from document_processor import (
    DocumentInput,
    get_document_context,
)

context = get_document_context(
    document=DocumentInput(source_path="/path/to/contract.docx"),
    target_ids=["r_3f1ff7241702452b"],
    before=1,
    after=1,
    include_runs=True,
)

for paragraph in context.paragraphs:
    print(paragraph.node_id, paragraph.text)
    for run in paragraph.runs:
        print(" ", run.node_id, run.start, run.end, repr(run.text))
```

## 7. List Safe Edit Targets

```python
from document_processor import (
    DocumentInput,
    list_editable_targets,
)

targets = list_editable_targets(
    document=DocumentInput(source_path="/path/to/contract.docx"),
    target_kinds=["cell", "run"],
    include_child_runs=True,
)

for target in targets.targets:
    print(target.target_id, target.target_kind, repr(target.current_text))
```

## 8. Validate Edits Before Applying

```python
from document_processor import (
    DocumentInput,
    TextEdit,
    validate_document_edits,
)

validation = validate_document_edits(
    document=DocumentInput(source_path="/path/to/contract.docx"),
    edits=[
        TextEdit(
            target_kind="run",
            target_id="r_3f1ff7241702452b",
            expected_text="wrong text",
            new_text="updated text",
        )
    ],
)

print(validation.ok)
for issue in validation.issues:
    print(issue.code, issue.message, issue.current_text)
```

## 9. Render Review HTML Through The Stateless API

```python
from document_processor import (
    DocumentInput,
    TextAnnotation,
    render_review_html,
)

review = render_review_html(
    document=DocumentInput(source_path="/path/to/contract.docx"),
    annotations=[
        TextAnnotation(
            target_kind="paragraph",
            target_id="p_15cb9ef0efc99b82",
            selected_text="계약기간",
            label="Key clause",
            color="#FFD966",
            note="Human review requested",
        )
    ],
    title="Contract Review",
)

with open("review.html", "w", encoding="utf-8") as handle:
    handle.write(review.html)
```

## 10. Edit Through Structured DTOs

Exact text replacements should use `TextEdit`. Structural changes should use
`StructuralEdit`. Style changes should use `StyleEdit`. All three go through
`validate_document_edits` and `apply_document_edits` with flattened keyword
arguments. This keeps LLM tool calls on structured edit DTOs and avoids exposing
internal native write-back plumbing.

```python
from document_processor import (
    DocumentInput,
    StyleEdit,
    TextEdit,
    apply_document_edits,
)

result = apply_document_edits(
    document=DocumentInput(source_path="/path/to/contract.hwpx"),
    edits=[
        TextEdit(
            target_kind="run",
            target_id="r_10b2809a0c03f6e1",
            expected_text="World",
            new_text="HWPX",
        ),
        StyleEdit(
            target_kind="run",
            target_id="r_10b2809a0c03f6e1",
            bold=True,
            color="#445566",
        )
    ],
    return_doc_ir=True,
)

print(result.output_path)
print(result.modified_target_ids)
```

The removed low-level edit engine names are documented in
[Removed Legacy Edit API](removed-legacy-edit-api.md).

The removed low-level annotation names are documented in
[Removed Legacy Annotation API](removed-legacy-annotation-api.md).

## 11. Add Custom Metadata

All IR nodes expose a `.meta` field for Pydantic-based metadata.

```python
from pydantic import BaseModel

from document_processor import DocIR


class ReviewMeta(BaseModel):
    risk_level: str
    reviewer_note: str


doc = DocIR.from_mapping({"s1.p1.r1": "Clause text"})
doc.paragraphs[0].meta = ReviewMeta(
    risk_level="medium",
    reviewer_note="Needs legal review",
)

print(doc.paragraphs[0].meta)
```

## 12. Current Limits

- PDF parsing is implemented through the `document_processor.pdf` pipeline, but
  native PDF write-back is not supported.
- External PDF parsers can also build `DocIR` with stable `node_id` values as
  described in [PDF Parser DocIR Integration](pdf-parser-docir-integration.md).
- Native write-back is same-format only for `docx`, `hwpx`, and `hwp -> hwpx`.
- Exact text paragraph edits are rejected when the paragraph contains tables or images.
- Annotation selection is exact-text based; use `occurrence_index` when the same substring repeats in a target.
- Floating table/image placement write-back exists for DOCX/HWPX, but native
  placement extraction and HTML rendering of wrapping/absolute placement are not complete yet.

## Suggested Workflow

For LLM or review tooling:

1. Read source through `read_document(...)` or parse source into `DocIR`.
2. Use returned `node_id` values, `get_document_context(...)`, or `list_editable_targets(...)`.
3. Emit exact `TextEdit` objects for text replacements, `StructuralEdit`
   objects for insert/remove/table operations, or `StyleEdit` objects for
   flattened style mutations.
4. Call `validate_document_edits(...)`.
5. Call `apply_document_edits(...)`.
6. Call `render_review_html(...)` for human review.
