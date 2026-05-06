# Removed Legacy Edit API

This package no longer exposes the old low-level text edit DTOs or direct edit
engine functions. The public edit API is now the flattened `apply_document_edits`
surface intended for LLM tool calling.

## Removed Names

The following names were removed from the package API:

- `RunTextEdit`
- `ParagraphTextEdit`
- `CellTextEdit`
- `ApplyEditsResult`
- `validate_edit_commands`
- `apply_edits_to_doc_ir`
- `apply_edits_to_file`
- `apply_edits_to_bytes`
- `apply_edits_to_source`

The removed DTOs used target-specific id fields such as `run_id`,
`paragraph_id`, and `cell_id`, plus `old_text`. Those shapes were replaced by a
single edit schema:

```python
TextEdit(
    target_id="r_10b2809a0c03f6e1",
    expected_text_hash="hash-returned-by-read-or-list",
    new_text="new text",
    reason="optional note",
)
```

## Supported Replacement

Use these public models and functions instead:

- `TextEdit`
- `StructuralEdit`
- `EditValidationResult`
- `ApplyDocumentEditsResult`
- `validate_document_edits`
- `apply_document_edits`

Example:

```python
from document_processor import (
    DocumentInput,
    TextEdit,
    apply_document_edits,
)

result = apply_document_edits(
    document=DocumentInput(source_path="/path/to/source.docx"),
    edits=[
        TextEdit(
            target_id="p_15cb9ef0efc99b82",
            expected_text_hash="hash-returned-by-read-or-list",
            new_text="Updated paragraph text.",
        )
    ],
    output_path="/path/to/source_edited.docx",
    return_doc_ir=True,
)
```

For in-memory `DocIR` editing, pass the IR through `DocumentInput`:

```python
from document_processor import (
    DocumentInput,
    TextEdit,
    apply_document_edits,
)

result = apply_document_edits(
    document=DocumentInput(doc_ir=doc),
    edits=[
        TextEdit(
            target_id=doc.paragraphs[0].runs[0].node_id,
            expected_text_hash=doc.paragraphs[0].runs[0].native_anchor.text_hash,
            new_text="Replacement text",
        )
    ],
    return_doc_ir=True,
)

updated_doc = result.updated_doc_ir
```

## Migration Notes

- Replace `old_text` with `expected_text_hash` from `read_document`,
  `get_document_context`, or `list_editable_targets`.
- Replace `run_id`, `paragraph_id`, or `cell_id` with `target_id`.
- Omit `target_kind` unless you want a compatibility assertion.
- Use `DocumentInput(source_path=...)` for path-backed native write-back.
- Use `DocumentInput(source_bytes=..., source_name=...)` for bytes-backed native
  write-back.
- Use `DocumentInput(doc_ir=...)` for DocIR-only edits.

The lower-level native write-back helpers still exist internally, but they are
not part of the public API. This keeps external callers and LLM tools aligned on
stable DocIR `node_id` targets rather than native package locators or
implementation-specific edit objects.
