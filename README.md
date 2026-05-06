# document-processor

Batteries-included structural document IR parser for `hwp`, `hwpx`, `docx`, and `pdf`.
Provides a unified Pydantic-based data model for document structure, styles, and
content, along with APIs for parsing, editing, annotation, and HTML export.

**Requires Python 3.13+**

Additional docs:

- [PDF parser README](src/document_processor/pdf/README.md)

## Installation

```bash
pip install document-processor
```

Local development:

```bash
uv pip install -e /path/to/document-processor
```

### Dependencies

| Package       | Purpose                            |
| ------------- | ---------------------------------- |
| `pydantic`    | IR models and validation           |
| `python-docx` | DOCX parsing and native write-back |
| `jpype1`      | HWP conversion via Java interop    |
| `pypdfium2`   | PDF probing and preview enrichment |

PDF parsing also uses the vendored OpenDataLoader CLI JAR included under
`src/document_processor/pdf/odl/vendor`.

## Quick start

```python
from document_processor import DocIR

doc = DocIR.from_file("/path/to/file.docx")

print(doc.paragraphs[0].text)
print(doc.paragraphs[0].runs[0].run_style.bold)

html = doc.to_html(title="Preview")
```

The package covers:

- document parsing (DOCX, HWPX, HWP, PDF)
- style extraction (fonts, colors, alignment, spacing, borders, ...)
- structural IR creation
- embedded image extraction for `docx`, `hwpx`, and parser-provided PDF assets
- canonical PDF parsing and local OpenDataLoader artifact export
- stateless text, structural, and style editing with native file write-back
- annotation resolution and review HTML rendering

PDF parsing uses the same high-level entry point:

```python
doc = DocIR.from_file("/path/to/file.pdf", doc_type="pdf")
```

## Logging

`document_processor` initializes a package logger named `document_processor` at
import time. The default level is `WARNING` and logs go to the console.

```python
from document_processor import DocIR, configure_logging, get_logger

configure_logging(level="INFO")
configure_logging(level="DEBUG", log_file="logs/docir.log")

doc = DocIR.from_file("/path/to/file.docx")

logger = get_logger(__name__)
logger.warning("Handled document %s", doc.doc_id)
```

You can also configure the same logger through `DocIR`:

```python
DocIR.configure_logging(level="INFO", log_file="logs/docir.log")
```

## Custom metadata

All IR models include a `.meta` field for attaching processing metadata
(e.g. for LLMs, RAG, analysis).

```python
for file_ in files:
    doc = DocIR.from_file(file_)

    class MyMetaData(BaseModel):
        a: int = 1
        b: str = "test"

    metainfo = MyMetaData(a=2)
    doc.paragraphs[0].runs[0].meta = metainfo

    with \
        open((out_dir / file_.stem).with_suffix(".json"), "w", encoding="utf-8") as json_f, \
        open((out_dir / file_.stem).with_suffix(".html"), "w", encoding="utf-8") as html_f:

        json.dump(doc.model_dump(mode="json"), json_f, indent=4, ensure_ascii=False)
        html_f.write(doc.to_html())

    print(f"completed: {file_}")
```

> **Note:** Metadata objects must extend Pydantic `BaseModel`. Otherwise a validation error is raised.

## Images in the IR

Parsed image binaries are stored once on `DocIR.assets`, and paragraph-like nodes keep ordered
`content` entries so text, tables, and images can be rendered in source order.
Image dimensions render to HTML. Floating placement metadata is represented on
`ImageIR.placement` when present, but wrapping/absolute placement is not fully
projected to HTML yet.

```python
from document_processor import DocIR

doc = DocIR.from_file("/path/to/file.docx")
first_asset = next(iter(doc.assets.values()))
html = doc.to_html()
```

## Paragraph layout metadata

Paragraph styling stays attached to `ParagraphIR.para_style`. Multi-column
layout is stored in `para_style.column_layout`, and resolved DOCX/HWPX list
markers are stored in `para_style.list_info`. Raw `paragraph.text` remains the
editable text without generated numbering; `read_document(...)` also returns
`display_text` with the marker prefixed for LLM-readable context.


## Editing documents

The stateless edit API lets you apply text, structural, and style edits to documents.
Edits are validated before application, and results can be returned as an
updated `DocIR`, written back to the native file format, or returned as bytes.
Native write-back is supported for DOCX, HWPX, and HWP-to-HWPX output. PDF
sources can be parsed, inspected, rendered to HTML, and edited in memory via
`DocumentInput(doc_ir=doc)`, but they are not written back as PDF files.
Text and style edit target kinds are inferred from `target_id`; provide
`target_kind` only when you want the API to reject mismatched ids explicitly.

```python
from document_processor import (
    DocumentInput,
    StyleEdit,
    StructuralEdit,
    TextEdit,
    apply_document_edits,
    read_document,
)

document = DocumentInput(source_path="/path/to/file.docx")
preview = read_document(document=document, start=0, limit=1)

result = apply_document_edits(
    document=document,
    edits=[TextEdit(
        target_id=preview.paragraphs[0].node_id,
        expected_text_hash=preview.paragraphs[0].text_hash,
        new_text="new text",
    )],
)
```

Related helpers:

- `get_document_context()` &mdash; fetch surrounding paragraphs for target IDs
- `list_editable_targets()` &mdash; enumerate safe paragraph, run, cell, table, and image targets
- `validate_document_edits()` &mdash; validate text replacements, insert/remove operations, table edits, and style edits

Structural edits use the same stable `node_id` targets:

```python
result = apply_document_edits(
    document=document,
    edits=[
        StructuralEdit(
            operation="insert_paragraph",
            target_id=preview.paragraphs[0].node_id,
            position="after",
            text="Inserted paragraph",
        ),
    ],
    return_doc_ir=True,
)
```

Inserted DOCX/HWPX tables receive basic visible table defaults: non-zero
geometry, cell padding, and a black grid. HWPX table inserts are written as
inline objects (`treatAsChar="1"`), and inserted rows/columns inherit nearby
row/cell properties when possible.

Style edits use a flat DTO designed for LLM tool schemas:

```python
result = apply_document_edits(
    document=document,
    edits=[
        StyleEdit(
            target_id=preview.paragraphs[0].runs[0].node_id,
            bold=True,
            color="#445566",
            font_size_pt=16,
        ),
    ],
    return_doc_ir=True,
)
```

`StyleEdit` can target runs, paragraphs, cells, tables, and images. Cell style
edits include background, alignment, padding, borders, and dimensions. Cell
`width_pt` is applied as column geometry and cell `height_pt` as row geometry.
Border fields accept CSS-style values such as `"1px solid #445566"` and
native-style values such as `"1pt single #445566"`.
Table style edits cover floating placement fields; image style edits include
dimensions and floating placement fields for native DOCX/HWPX write-back.


## Annotations and review HTML

Resolve text annotations against a document and render a highlighted review page:

```python
from document_processor import (
    DocumentInput,
    TextAnnotation,
    read_document,
    render_review_html,
)

document = DocumentInput(source_path="/path/to/file.docx")
preview = read_document(document=document, start=0, limit=1)

result = render_review_html(
    document=document,
    annotations=[TextAnnotation(
        target_kind="paragraph",
        target_id=preview.paragraphs[0].node_id,
        selected_text="some phrase",
        label="Needs revision",
    )],
)

html = result.html
```

## Exporting HTML

Render a parsed document to styled HTML:

```python
from document_processor import DocIR

doc = DocIR.from_file("/path/to/file.docx")
html = doc.to_html(title="Preview")
debug_html = doc.to_html(title="Layout Debug", debug_layout=True)
```

The debug layout view outlines pages, tables, cells, and paragraphs, and labels
declared point dimensions next to browser-rendered sizes. HTML rendering clamps
negative paragraph indents so text stays inside the page or table-cell content
edge. Source cell margins are available on `CellStyleInfo.padding_*_pt` and are
rendered as table-cell padding.
## Visualizing the models

Install the visualization extra first:

```bash
pip install "document-processor[viz]"

# might need compiler flags depending on version, might error out
CFLAGS="-Wno-error=incompatible-pointer-types" ... install
```

Erdantic also needs Graphviz available on the system.

Render the default `DocIR` model diagram:

```bash
document-processor-diagram --out docir.svg
```

Render a package-scope diagram with IR fields/methods plus the main `core/`
modules:

```bash
document-processor-diagram --kind package --out package.svg
```

Render a custom model by dotted import path:

```bash
document-processor-diagram --model document_processor.DocIR --out docir.png
```

Or use the Python helper:

```python
from document_processor import draw_model_diagram

draw_model_diagram(out="docir.svg")
```

---

ERD for the pydantic models

![diagram](docir.svg)
