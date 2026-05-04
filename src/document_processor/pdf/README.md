# document-processor PDF

PDF parsing uses the same public `DocIR` API as DOCX/HWP/HWPX.

Start by parsing the PDF into a `DocIR`. Semantic output and HTML rendering are
meaningful after this parsing/enrichment step has produced the document IR.

```python
from document_processor import DocIR

doc = DocIR.from_file("/path/to/file.pdf", doc_type="pdf")

semantic = doc.to_semantic().model_dump(mode="json", exclude_none=True)
html = doc.to_html(title="PDF Preview")
```

Optional PDF config is intentionally small:

```python
doc = DocIR.from_file(
    "/path/to/file.pdf",
    doc_type="pdf",
    config={
        "pages": "1,3,5-7",
        "include_header_footer": False,
        "image_quality": "high",  # standard, high, max
        "image_output": "embedded",  # embedded, external, off
    },
)
```

## Semantic Output

Use semantic output from the parsed `DocIR` for chunking, RAG, and downstream
processing.

```python
semantic = doc.to_semantic()
semantic_dict = semantic.model_dump(mode="json", exclude_none=True)
semantic_json = semantic.model_dump_json(exclude_none=True, indent=2)
```

## HTML Preview

Use HTML output from the parsed `DocIR` for preview rendering.

```python
html = doc.to_html(title="PDF Preview")
```
