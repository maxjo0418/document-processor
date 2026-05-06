# API Reference

This document describes the main public Python APIs exported by
`document_processor`.

## Common Imports

Import directly from the package root:

```python
from document_processor import (
    DocIR,
    DocumentEdit,
    DocumentInput,
    HwpxDocument,
    NativeAnchor,
    NodeKind,
    ObjectPlacementInfo,
    StyleEdit,
    StructuralEdit,
    TextAnnotation,
    TextEdit,
    apply_document_edits,
    configure_logging,
    get_document_context,
    get_logger,
    list_editable_targets,
    read_document,
    render_review_html,
    validate_document_edits,
    validate_text_annotations,
)
```

The package root also exports the IR models, style models, result DTOs, target
kind aliases, diagram helpers, and `build_doc_ir_from_mapping`.

## Logging

The package root initializes a logger named `document_processor` with level
`WARNING` and console output.

#### `configure_logging(level=logging.WARNING, *, log_file=None, console=True, file_mode="a", log_format=..., date_format=..., propagate=False) -> logging.Logger`

Configure the package-wide logger used by `DocIR` and helpers.

- `level`: numeric or string level such as `"INFO"` or `"DEBUG"`.
- `log_file`: optional file path. When set, logs are also written to that file.
- `console`: keep or remove the managed console handler.
- `propagate`: set to `True` when an application wants root logging handlers to
  process `document_processor` records.

Repeated calls update the managed handlers instead of adding duplicates.

#### `get_logger(name=None) -> logging.Logger`

Return the package logger or a child logger. Use `get_logger(__name__)` in
package functions and helper modules instead of `print()`.

`DocIR.configure_logging(...)` is a convenience wrapper around
`configure_logging(...)`.

## Core IR Models

See [PDF Parser DocIR Integration](pdf-parser-docir-integration.md) for guidance on
building stable IDs and native anchors from an external PDF parser.

### `DocIR`

Top-level structural document model.

Key fields:

- `doc_id: str | None`
- `source_path: str | None`
- `source_doc_type: str | None`
- `identity_version: int`
- `assets: dict[str, ImageAsset]`
- `pages: list[PageInfo]`
- `paragraphs: list[ParagraphIR]`

All addressable IR nodes expose:

- `node_id`: stable opaque id intended for LLM tool calls and edit/annotation targets.

Nodes parsed from a native document also receive `native_anchor`, which records the
source document type, debug path, parent debug path, native part name, structural
path, and a source text hash where available. Use `node_id` in exposed tool-call
APIs. Native/package locators live only under `native_anchor`.

Key methods:

#### `DocIR.from_file(source, *, doc_type="auto", include_tables=True, skip_empty=False, metadata=None, doc_id=None, **doc_kwargs) -> DocIR`

Parse a document into `DocIR`.

Accepted `source` values:

- `str | Path`
- `bytes`
- binary file object

Supported document types:

- `docx`
- `hwpx`
- `hwp`
- `pdf`

Notes:

- `doc_type="auto"` infers the type from the filename or bytes.
- `pdf` uses the `document_processor.pdf` parsing pipeline. Bytes and binary
  file objects are materialized to a temporary PDF path internally.
- For `.hwp`, the package converts through HWPX internally before building `DocIR`.

#### `DocIR.from_mapping(mapping, *, style_map=None, source_path=None, source_doc_type=None, metadata=None, doc_id=None, **doc_kwargs) -> DocIR`

Build a `DocIR` from a run-level mapping such as:

```python
{
    "s1.p1.r1": "Hello ",
    "s1.p1.r2": "World",
}
```

This is useful for tests, fixtures, or synthetic documents.

#### `DocIR.to_html(*, title=None, debug_layout=False) -> str`

Render the document as styled HTML using the built-in exporter.

Set `debug_layout=True` to add visual outlines and data labels for pages,
tables, cells, and paragraphs. The debug view also measures rendered element
sizes in the browser so extracted point values can be compared with actual HTML
layout.

Paragraph indents are clamped during HTML rendering so negative or hanging
indents cannot start text outside the page/table-cell content edge. Valid
hanging indents are preserved when the positive left indent is large enough.
Table cell padding is rendered from `CellStyleInfo` when extracted from source
cell margins such as HWPX `hp:cellMargin` or DOCX `w:tcMar`.
Top-level consecutive paragraphs with the same multi-column
`ParaStyleInfo.column_layout` are wrapped in a CSS multi-column group when
rendered to HTML. Paragraphs with `ParaStyleInfo.list_info` render their
resolved list marker before the editable paragraph text.

### `ParagraphIR`

Paragraph-like structural node.

Important fields:

- `node_id`
- `text`
- `page_number`
- `para_style`
- `content`

Computed/content helpers:

- `.runs`
- `.images`
- `.tables`
- `.iter_all_runs(...)`
- `.recompute_text()`

### `RunIR`

Smallest text unit that preserves run-level styling.

Important fields:

- `node_id`
- `text`
- `run_style`

### `TableIR`

Nested table node under a paragraph.

Important fields:

- `node_id`
- `row_count`
- `col_count`
- `table_style`
- `cells`

Computed helper:

- `.markdown`

### Supporting Models

- `ImageAsset`
- `ImageIR`
- `PageInfo`
- `TableCellIR`
- `NativeAnchor`
- `CellStyleInfo`
- `ColumnLayoutInfo`
- `ListItemInfo`
- `ObjectPlacementInfo`
- `ParaStyleInfo`
- `RunStyleInfo`
- `TableStyleInfo`
- `StyleMap`

#### `NativeAnchor`

Native/source-location metadata attached to addressable nodes.

Fields:

- `source_doc_type`: source format such as `docx`, `hwpx`, `hwp`, or parser-defined values.
- `node_kind`: one of `paragraph`, `run`, `image`, `table`, or `cell`.
- `debug_path`: human-readable internal path for diagnostics and native write-back tracing.
- `parent_debug_path`: debug path of the containing native/IR node when available.
- `part_name`: package part or source segment name, such as `word/document.xml`,
  `Contents/section0.xml`, or `page:3`.
- `structural_path`: optional parser-native structural locator.
- `text_hash`: SHA-1 hash of the source text for drift detection.

`NativeAnchor` helps a writer or external parser reconnect a stable `node_id` to
native structures. It is returned for inspection, but LLM edit and annotation calls
should still target `node_id`.

#### `CellStyleInfo`

Cell-level formatting for `TableCellIR.cell_style`.

Important fields:

- `background`
- `vertical_align`
- `horizontal_align`
- `width_pt`
- `height_pt`
- `padding_top_pt`
- `padding_right_pt`
- `padding_bottom_pt`
- `padding_left_pt`
- `border_top`
- `border_bottom`
- `border_left`
- `border_right`
- `diagonal_tl_br`
- `diagonal_tr_bl`
- `rowspan`
- `colspan`

HWPX `hp:cellMargin` and DOCX `w:tcMar`/`w:tblCellMar` are represented as
cell padding fields in points. Paragraph indents remain in `ParaStyleInfo`.

#### `ParaStyleInfo`

Paragraph-level formatting for `ParagraphIR.para_style`.

Important fields:

- `align`
- `left_indent_pt`
- `right_indent_pt`
- `first_line_indent_pt`
- `hanging_indent_pt`
- `column_layout`
- `list_info`

`column_layout` is a `ColumnLayoutInfo` object used for active section/text-column
layout. It is omitted for ordinary single-column paragraphs.

`list_info` is a `ListItemInfo` object containing resolved paragraph-list display
metadata. It is intentionally per paragraph rather than a separate list node, so
the structural IR remains paragraph-first.

#### `ColumnLayoutInfo`

Column layout metadata for paragraph rendering.

Important fields:

- `count`
- `gap_pt`
- `widths_pt`
- `gaps_pt`
- `equal_width`

#### `ListItemInfo`

Resolved list marker metadata for a paragraph.

Important fields:

- `list_id`
- `level`
- `marker`
- `marker_type`
- `marker_text`

For DOCX, this is resolved from `w:numPr` and `word/numbering.xml`. For HWPX,
this is resolved from `hh:paraPr/hh:heading` and header `hh:numbering` or
`hh:bullet` definitions.

#### `ObjectPlacementInfo`

Format-agnostic placement metadata for floating tables and images.

Important fields:

- `mode`: `inline` or `floating`
- `wrap`: text wrapping behavior such as `square`, `tight`, `top_bottom`,
  `behind_text`, or `in_front_of_text`
- `text_flow`: side selection for text around the object
- `x_relative_to`, `y_relative_to`
- `x_align`, `y_align`
- `x_offset_pt`, `y_offset_pt`
- `margin_top_pt`, `margin_right_pt`, `margin_bottom_pt`, `margin_left_pt`
- `allow_overlap`
- `flow_with_text`
- `z_order`

This model is attached as `TableStyleInfo.placement` and `ImageIR.placement`.
Style edits can write these fields to native DOCX/HWPX. Full extraction from
native floating placement and HTML rendering of wrapping/absolute placement are
still limited; dimensions render to HTML, but placement currently remains
primarily native-write-back metadata.

## Source/Input Models

### `DocumentInput`

Stateless input wrapper for read/edit/annotation APIs.

Fields:

- `source_path: str | None`
- `source_bytes: bytes | None`
- `doc_ir: DocIR | None`
- `source_doc_type: Literal["auto", "hwp", "hwpx", "docx", "pdf"]`
- `source_name: str | None`

Rules:

- Provide at least one of `source_path`, `source_bytes`, or `doc_ir`.
- `source_path` and `source_bytes` cannot both be set.
- `doc_ir` may be combined with native source data when you want in-memory reads plus native write-back.

## Stateless Read/Edit API

These functions operate on `DocumentInput` and are intended for public API usage.

### `read_document(*, document=None, source_path=None, start=0, limit=50, include_runs=True) -> ReadDocumentResult`

Read a bounded paragraph window from a document. This is the preferred tool-call entry
point when an LLM needs to inspect a document incrementally.

Input fields:

- `document`
- `source_path`
- `start`
- `limit`
- `include_runs`

Response fields:

- `source_path`
- `source_doc_type`
- `source_name`
- `start`
- `limit`
- `total_paragraphs`
- `next_start`
- `paragraphs`

Each paragraph contains:

- `text`: editable paragraph text without generated list markers.
- `display_text`: readable text with resolved list markers prefixed when present.
- `list_info`: optional resolved list marker metadata.

When `include_runs=True`, each run includes `start` and `end` offsets relative to
the raw paragraph `text` so callers can map readable text spans back to editable
run IDs.

### `get_document_context(*, document=None, source_path=None, target_ids=None, before=1, after=1, include_runs=True) -> DocumentContextResult`

Return surrounding paragraph context for paragraph or run ids.

Input fields:

- `document`
- `source_path`
- `target_ids`
- `before`
- `after`
- `include_runs`

Response fields:

- `source_path`
- `source_doc_type`
- `source_name`
- `paragraphs`
- `missing_target_ids`

### `list_editable_targets(*, document=None, source_path=None, target_ids=None, target_kinds=None, include_child_runs=False, only_writable=True, max_targets=200) -> ListEditableTargetsResult`

Enumerate paragraph, run, cell, table, and image targets that can be edited safely.

Input fields:

- `document`
- `source_path`
- `target_ids`
- `target_kinds`
- `include_child_runs`
- `only_writable`
- `max_targets`

Response fields:

- `source_path`
- `source_doc_type`
- `source_name`
- `targets`
- `missing_target_ids`

### `validate_document_edits(*, document=None, source_path=None, edits) -> EditValidationResult`

Validate text, structural, and style edit operations against the current document state.

Validation checks include:

- target exists
- target kind is valid for the requested operation
- exact text edits match `expected_text`
- exact paragraph text edits do not target mixed table/image paragraphs
- exact cell text edits preserve existing cell paragraph count
- optional `expected_text` guards match
- inserted table rows are rectangular
- inserted row/column values match the target table shape
- style targets exist and match `target_kind`
- style fields are valid for the selected target kind
- native write-back type is supported when native source data is present

### `apply_document_edits(*, document=None, source_path=None, edits, dry_run=False, output_path=None, output_filename=None, return_doc_ir=False) -> ApplyDocumentEditsResult`

Validate and apply text, structural, and style edits in one ordered batch.

The `edits` list accepts `TextEdit`, `StructuralEdit`, and `StyleEdit` objects.
Text edits perform exact replacements. Style edits use a flat optional-field
schema so the same model can be exposed directly as an LLM tool schema. Structural
edit operations include:

- `insert_paragraph`
- `remove_paragraph`
- `insert_run`
- `remove_run`
- `insert_table`
- `remove_table`
- `set_cell_text`
- `insert_table_row`
- `remove_table_row`
- `insert_table_column`
- `remove_table_column`

Input fields:

- `document`
- `source_path`
- `edits`
- `dry_run`
- `output_path`
- `output_filename`
- `return_doc_ir`

Response fields:

- `ok`
- `source_doc_type`
- `source_name`
- `output_path`
- `output_filename`
- `output_bytes`
- `updated_doc_ir`
- `edits_applied`
- `operations_applied`
- `styles_applied`
- `modified_target_ids`
- `created_target_ids`
- `removed_target_ids`
- `modified_run_ids`
- `warnings`
- `validation`

For native write-back, the API resolves public `node_id` targets through each
node's current `native_anchor.structural_path`. Mixed batches are applied in
list order. When `updated_doc_ir` is requested or returned, existing `node_id`
values are preserved after structural edits and native anchors are refreshed to
the new physical document paths.

Behavior by input type:

- `DocumentInput(doc_ir=...)`: returns `updated_doc_ir`; no native file output is produced.
- `DocumentInput(source_path=...)`: writes to `output_path` or a default sibling `*_edited.*` file.
- `DocumentInput(source_bytes=...)`: returns `output_bytes` and `output_filename`;
  it does not write a filesystem path.

Output options:

- `output_path` and `output_filename` are mutually exclusive.
- `output_filename` must be a filename only, not a directory path.
- Output options require a native `source_path` or `source_bytes`; DocIR-only
  edits cannot produce native document files.
- For bytes-backed input, use `output_filename` to name the returned bytes.
  `output_path` is only written for path-backed input.
- Path-backed edits normalize output suffixes to the native output format
  (`.docx` for DOCX, `.hwpx` for HWP/HWPX) and include a warning when the
  requested suffix is adjusted.
- `dry_run=True` validates and previews edits without native output. Applied
  counters are returned as `0`; preview target id lists and warnings are still
  populated, and `updated_doc_ir` is included only when `return_doc_ir=True`.

Native write-back is currently supported for `docx`, `hwpx`, and `hwp`.
For `.hwp`, edited output is written as `.hwpx`.

### `validate_text_annotations(*, document=None, source_path=None, annotations) -> AnnotationValidationResult`

Validate annotation targets and selected text without rendering HTML.

### `render_review_html(*, document=None, source_path=None, annotations, title="Review") -> ReviewHtmlResult`

Render annotated review HTML from `DocIR`, bytes, or a source path.

Input fields:

- `document`
- `source_path`
- `annotations`
- `title`

Response fields:

- `ok`
- `html`
- `resolved_annotations`
- `validation`

## Edit/Annotation DTOs

### `TextEdit`

Fields:

- `edit_type: Literal["text"] = "text"`
- `target_kind: Literal["paragraph", "run", "cell"]`
- `target_id: str`
- `expected_text: str`
- `new_text: str`
- `reason: str = ""`

Use the `node_id` returned by `read_document`, `get_document_context`, or
`list_editable_targets` as `target_id`.

Cell text edits replace the full text of a table cell. For multi-paragraph cells, `new_text`
must contain the same number of newline-separated lines as the current cell text; the API
does not create or delete paragraphs inside cells.

### `StructuralEdit`

Fields:

- `edit_type: Literal["structural"] = "structural"`
- `operation`
- `target_id`
- `position`
- `expected_text`
- `text`
- `rows`
- `values`
- `row_index`
- `column_index`
- `reason`

Use stable `node_id` values as `target_id`.

Operation field usage:

- `insert_paragraph`: target a paragraph with `position="before"|"after"` or a
  cell with `position="start"|"end"`; set `text`.
- `remove_paragraph`: target a paragraph; optional `expected_text`.
- `insert_run`: target a run with `position="before"|"after"` or a paragraph
  with `position="start"|"end"`; set `text`.
- `remove_run`: target a run; optional `expected_text`.
- `insert_table`: target a paragraph with `position="before"|"after"`; set
  rectangular `rows`.
- `remove_table`: target a table.
- `set_cell_text`: target a cell; set `text`. Newlines create cell paragraphs.
- `insert_table_row`: target a cell to anchor its row, or a table with
  `row_index`; set optional row `values`.
- `remove_table_row`: target a cell to remove its row, or a table with
  `row_index`.
- `insert_table_column`: target a cell to anchor its column, or a table with
  `column_index`; set optional column `values`.
- `remove_table_column`: target a cell to remove its column, or a table with
  `column_index`.

Inserted native tables use conservative visible defaults when the caller does
not provide style options: fixed non-zero column widths, cell padding, and a
black grid border. HWPX tables are created with `treatAsChar="1"` so Hancom
treats them as inline tables rather than floating wrapped objects. Inserted
rows and columns clone the nearest row/cell properties when available and fall
back to the same defaults only when the target table lacks usable properties.

### `StyleEdit`

Fields:

- `edit_type: Literal["style"] = "style"`
- `target_kind: Literal["paragraph", "run", "cell", "table", "image"]`
- `target_id`
- `reason`
- run style fields: `bold`, `italic`, `underline`, `strikethrough`,
  `superscript`, `subscript`, `color`, `highlight`, `font_size_pt`
- paragraph style fields: `paragraph_align`, `left_indent_pt`,
  `right_indent_pt`, `first_line_indent_pt`, `hanging_indent_pt`
- cell/image size fields: `width_pt`, `height_pt`
- cell style fields: `background`, `vertical_align`, `horizontal_align`,
  `padding_top_pt`, `padding_right_pt`, `padding_bottom_pt`,
  `padding_left_pt`, `border_top`, `border_right`, `border_bottom`,
  `border_left`
- table/image placement fields: `placement_mode`, `wrap`, `text_flow`,
  `x_relative_to`, `y_relative_to`, `x_align`, `y_align`, `x_offset_pt`,
  `y_offset_pt`, `margin_top_pt`, `margin_right_pt`, `margin_bottom_pt`,
  `margin_left_pt`, `allow_overlap`, `flow_with_text`, `z_order`
- `clear_fields: list[str]`

All style fields default to `None`, and `None` means "leave unchanged".
Use `clear_fields` to remove a nullable style value. For booleans, `False`
means "set false".

Native write-back notes:

- DOCX supports common run, paragraph, cell, table placement, and image
  size/placement fields.
- HWPX supports common run fields (`bold`, `italic`, `underline`,
  `strikethrough`, `color`, `font_size_pt`), paragraph alignment/indent fields,
  cell background/alignment/size/padding/border fields, table placement fields,
  and image size/placement fields. Run, paragraph, and cell style write-back
  clones header style records before updating target references.
- Table style edits do not accept `width_pt` or `height_pt`. Set cell
  `width_pt`/`height_pt` on the relevant cell targets instead. Use
  `list_editable_targets(target_kinds=["cell"])` to get each cell's table id,
  row index, column index, and span metadata.
- In both `updated_doc_ir` previews and native DOCX/HWPX write-back, a cell
  `width_pt` edit updates the target cell's logical column, and a cell
  `height_pt` edit updates the target row. Other cell style fields apply only
  to the targeted cell.
- Cell border fields accept CSS-style values such as `"1px solid #445566"` and
  native-style values such as `"1pt single #445566"`. HTML rendering normalizes
  `single` to CSS `solid`; DOCX/HWPX write-back maps border values to native
  border records.

### `TextAnnotation`

Fields:

- `target_kind: Literal["paragraph", "run"]`
- `target_id: str`
- `selected_text: str | None`
- `occurrence_index: int | None`
- `label: str`
- `color: str = "#FFFF00"`
- `note: str = ""`

Behavior:

- If `selected_text` is omitted, the full target is annotated.
- If `selected_text` appears multiple times, provide `occurrence_index`.
- Canonical `start` / `end` offsets are computed by the backend and returned in `ResolvedTextAnnotation`.

### `EditableTarget`

Fields:

- `target_kind`
- `target_id`
- `parent_paragraph_id`
- `parent_table_id`
- `row_index`
- `column_index`
- `row_count`
- `column_count`
- `rowspan`
- `colspan`
- `current_text`
- `page_number`
- `native_anchor`
- `writable`
- `writable_reason`

For `cell` targets, `parent_table_id`, `row_index`, `column_index`, `rowspan`,
and `colspan` identify the cell's table coordinates. Row and column indexes are
1-based. For `table` targets, `row_count` and `column_count` describe the
current table shape.

### `DocumentRunContext`

Fields:

- `node_id`
- `text`
- `start`
- `end`
- `native_anchor`

`start` and `end` are character offsets into the containing
`DocumentParagraphContext.text`.

## Edit API Boundary

Use `TextEdit` for exact text replacements, `StructuralEdit` for
insert/remove/table operations, and `StyleEdit` for flattened style mutations.
Pass them through `validate_document_edits` and `apply_document_edits` with
flattened keyword arguments:

```python
result = apply_document_edits(
    document=document,
    edits=[TextEdit(...), StructuralEdit(...), StyleEdit(...)],
    return_doc_ir=True,
)
```

These DTOs and functions are the supported public surface for LLM tool calling
and structured outputs.

The older low-level edit DTOs and direct engine entrypoints were removed to keep
DocIR editing centered on stable `target_id` values. See
[Removed Legacy Edit API](removed-legacy-edit-api.md) for the removed names and
migration shape.

## Annotation API Boundary

Use `TextAnnotation`, `validate_text_annotations`, and `render_review_html` for
annotations. These DTOs and functions are the supported public surface for LLM
tool calling and structured outputs.

The older low-level annotation DTOs and direct renderer/resolver entrypoints
were removed to keep annotation tooling centered on stable `target_id` values.
See [Removed Legacy Annotation API](removed-legacy-annotation-api.md) for the
removed names and migration shape.

## Diagram Helpers

### `draw_model_diagram(...)`

Render the Pydantic model graph to a file.

### `create_model_diagram(...)`

Return the generated diagram object.

## Current Limits

- PDF parsing is implemented through the `document_processor.pdf` pipeline, but
  native PDF write-back is not supported.
- Exact text paragraph edits are blocked when the paragraph contains tables or images.
- Native write-back is limited to same-format `docx`, `hwpx`, and `hwp -> hwpx`.
- Annotation matching is exact-string based within the selected paragraph or run.
- Floating table/image placement write-back exists for DOCX/HWPX, but native
  placement extraction and HTML rendering of wrapping/absolute placement are not
  complete yet.
