"""Structural document IR models."""

from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
from pathlib import Path
from typing import Any, BinaryIO, Generic, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, Field, computed_field

from .io_utils import TemporarySourcePath, coerce_source_to_supported_value, get_source_name, infer_doc_type
from .logging_config import configure_logging, get_logger
from .style_types import CellStyleInfo, ObjectPlacementInfo, ParaStyleInfo, RunStyleInfo, TableStyleInfo

T = TypeVar("T", bound=BaseModel)
NodeKind: TypeAlias = Literal["paragraph", "run", "image", "table", "cell"]
SemanticBlockKind: TypeAlias = Literal["paragraph", "table", "image"]
logger = get_logger(__name__)


_NODE_ID_PREFIXES: dict[NodeKind, str] = {
    "paragraph": "p",
    "run": "r",
    "image": "img",
    "table": "tbl",
    "cell": "cell",
}


def _stable_node_id(kind: NodeKind, structural_path: str) -> str:
    digest = hashlib.sha1(f"{kind}:{structural_path}".encode("utf-8")).hexdigest()[:16]
    return f"{_NODE_ID_PREFIXES[kind]}_{digest}"


def _text_hash(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class NativeAnchor(BaseModel):
    """Original native-document location for a stable DocIR node."""

    source_doc_type: str | None = None
    node_kind: NodeKind
    debug_path: str
    parent_debug_path: str | None = None
    part_name: str | None = None
    structural_path: str | None = None
    text_hash: str | None = None


def _node_anchor_path(node) -> str:
    anchor = getattr(node, "native_anchor", None)
    if anchor is not None:
        if anchor.structural_path:
            return anchor.structural_path
        if anchor.debug_path:
            return anchor.debug_path
    node_id = getattr(node, "node_id", None)
    if node_id:
        return node_id
    raise ValueError("Node has no node_id or native anchor path.")


def _node_debug_path(node) -> str:
    anchor = getattr(node, "native_anchor", None)
    if anchor is not None and anchor.debug_path:
        return anchor.debug_path
    node_id = getattr(node, "node_id", None)
    return node_id or ""


def _make_native_anchor(
    kind: NodeKind,
    structural_path: str,
    *,
    source_doc_type: str | None = None,
    parent_debug_path: str | None = None,
    part_name: str | None = None,
    text: str | None = None,
) -> NativeAnchor:
    return NativeAnchor(
        source_doc_type=source_doc_type,
        node_kind=kind,
        debug_path=structural_path,
        parent_debug_path=parent_debug_path,
        part_name=part_name,
        structural_path=structural_path,
        text_hash=_text_hash(text),
    )


def _anchored_node_id(kind: NodeKind, structural_path: str) -> str:
    return _stable_node_id(kind, structural_path)


class BoundingBox(BaseModel):
    """Generic layout bounding box in page coordinates."""

    left_pt: float
    bottom_pt: float
    right_pt: float
    top_pt: float


class RunIR(BaseModel, Generic[T]):
    """Smallest style-preserving text unit."""

    model_config = {"validate_assignment": True, "extra": "forbid"}
    meta: T | None = None

    node_id: str | None = None
    text: str = ""
    bbox: BoundingBox | None = None
    run_style: RunStyleInfo | None = None
    native_anchor: NativeAnchor | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.node_id is None and self.native_anchor is not None:
            self.node_id = _stable_node_id("run", _node_anchor_path(self))


class ImageAsset(BaseModel, Generic[T]):
    """Binary image asset stored once per document."""

    model_config = {"validate_assignment": True, "extra": "forbid"}
    meta: T | None = None

    mime_type: str
    filename: str | None = None
    data_base64: str
    intrinsic_width_px: int | None = None
    intrinsic_height_px: int | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        data: bytes,
        mime_type: str,
        filename: str | None = None,
        intrinsic_width_px: int | None = None,
        intrinsic_height_px: int | None = None,
    ) -> "ImageAsset[T]":
        return cls(
            mime_type=mime_type,
            filename=filename,
            data_base64=base64.b64encode(data).decode("ascii"),
            intrinsic_width_px=intrinsic_width_px,
            intrinsic_height_px=intrinsic_height_px,
        )

    def bytes_data(self) -> bytes:
        return base64.b64decode(self.data_base64.encode("ascii"))

    def as_data_url(self) -> str:
        return f"data:{self.mime_type};base64,{self.data_base64}"


class PageInfo(BaseModel):
    """Document page metadata used for approximate paged rendering."""

    page_number: int
    width_pt: float | None = None
    height_pt: float | None = None
    margin_left_pt: float | None = None
    margin_right_pt: float | None = None
    margin_top_pt: float | None = None
    margin_bottom_pt: float | None = None


class SemanticBlockIR(BaseModel):
    """Chunking/search-friendly content block."""

    node_id: str | None = None
    debug_path: str | None = None
    kind: SemanticBlockKind
    page_number: int | None = None
    bbox: BoundingBox | None = None
    text: str = ""
    previous_table_id: str | None = None
    next_table_id: str | None = None


class SemanticIR(BaseModel):
    """Lightweight semantic projection of DocIR."""

    doc_id: str | None = None
    source_path: str | None = None
    source_doc_type: str | None = None
    blocks: list[SemanticBlockIR] = Field(default_factory=list)


class ImageIR(BaseModel, Generic[T]):
    """Image placement node inside paragraph-like content."""

    model_config = {"validate_assignment": True, "extra": "forbid"}

    node_id: str | None = None
    image_id: str
    alt_text: str | None = None
    title: str | None = None
    bbox: BoundingBox | None = None
    display_width_pt: float | None = None
    display_height_pt: float | None = None
    placement: ObjectPlacementInfo | None = None
    native_anchor: NativeAnchor | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.node_id is None and self.native_anchor is not None:
            self.node_id = _stable_node_id("image", _node_anchor_path(self))


class ParagraphIR(BaseModel, Generic[T]):
    """Structural paragraph unit used both at the document level and inside table cells."""

    model_config = {"validate_assignment": True, "extra": "forbid"}
    meta: T | None = None

    node_id: str | None = None
    text: str = ""
    page_number: int | None = None
    bbox: BoundingBox | None = None
    para_style: ParaStyleInfo | None = None
    content: list["ParagraphContentNode"] = Field(default_factory=list)
    native_anchor: NativeAnchor | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.node_id is None and self.native_anchor is not None:
            self.node_id = _stable_node_id("paragraph", _node_anchor_path(self))

    @property
    def runs(self) -> list[RunIR]:
        return [item for item in self.content if isinstance(item, RunIR)]

    @property
    def images(self) -> list[ImageIR]:
        return [item for item in self.content if isinstance(item, ImageIR)]

    @property
    def tables(self) -> list["TableIR"]:
        return [item for item in self.content if isinstance(item, TableIR)]

    def append_content(self, node: "ParagraphContentNode") -> None:
        self.content.append(node)

    def extend_content(self, nodes: list["ParagraphContentNode"]) -> None:
        self.content.extend(nodes)

    def sort_content(self, *, key) -> None:
        self.content.sort(key=key)

    def iter_all_runs(self, *, include_table_runs: bool = True):
        yield from self.runs
        if not include_table_runs:
            return
        for table in self.tables:
            for cell in table.cells:
                for cell_paragraph in cell.paragraphs:
                    yield from cell_paragraph.iter_all_runs(include_table_runs=True)

    def recompute_text(self) -> None:
        parts: list[str] = []
        if self.runs:
            parts.append("".join(run.text for run in self.runs))
        for table in self.tables:
            cell_texts = [cell.text for cell in table.cells if cell.text]
            if cell_texts:
                parts.append("\n".join(cell_texts))

        self.text = "\n".join(part for part in parts if part) if self.tables else "".join(run.text for run in self.runs)


class TableCellIR(BaseModel, Generic[T]):
    """Table cell node."""

    model_config = {"validate_assignment": True, "extra": "forbid"}
    meta: T | None = None

    node_id: str | None = None
    row_index: int
    col_index: int
    text: str = ""
    bbox: BoundingBox | None = None
    cell_style: CellStyleInfo | None = None
    paragraphs: list["ParagraphIR"] = Field(default_factory=list)
    native_anchor: NativeAnchor | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.node_id is None and self.native_anchor is not None:
            self.node_id = _stable_node_id("cell", _node_anchor_path(self))

    def recompute_text(self) -> None:
        self.text = "\n".join(p.text for p in self.paragraphs)


class TableIR(BaseModel, Generic[T]):
    """Nested table node under a paragraph."""

    model_config = {"validate_assignment": True}
    meta: T | None = None

    node_id: str | None = None
    previous_table_id: str | None = None
    next_table_id: str | None = None
    row_count: int = 0
    col_count: int = 0
    bbox: BoundingBox | None = None
    table_style: TableStyleInfo | None = None
    cells: list[TableCellIR] = Field(default_factory=list)
    native_anchor: NativeAnchor | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.node_id is None and self.native_anchor is not None:
            self.node_id = _stable_node_id("table", _node_anchor_path(self))

    @computed_field
    def markdown(self) -> str:
        return _render_table_markdown(self)


def _source_log_label(source: object) -> str:
    source_name = get_source_name(source)
    if source_name is not None:
        return source_name
    if isinstance(source, bytes):
        return f"<bytes:{len(source)}>"
    return f"<{type(source).__name__}>"


def _log_doc_ir_summary(message: str, doc_ir: "DocIR") -> None:
    logger.info(
        "%s: source_doc_type=%s source_path=%s paragraphs=%d pages=%d assets=%d",
        message,
        doc_ir.source_doc_type,
        doc_ir.source_path,
        len(doc_ir.paragraphs),
        len(doc_ir.pages),
        len(doc_ir.assets),
    )


class DocIR(BaseModel, Generic[T]):
    """Top-level structural document IR."""

    meta: T | None = None

    identity_version: int = 1
    doc_id: str | None = None
    source_path: str | None = None
    source_doc_type: str | None = None
    assets: dict[str, ImageAsset[T]] = Field(default_factory=dict)
    pages: list[PageInfo] = Field(default_factory=list)
    paragraphs: list[ParagraphIR] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        self.ensure_node_identity()

    @classmethod
    def configure_logging(
        cls,
        level: int | str = "WARNING",
        *,
        log_file: str | Path | None = None,
        console: bool = True,
    ):
        """Configure the package logger used by DocIR and helper modules."""
        return configure_logging(level, log_file=log_file, console=console)

    @computed_field
    @property
    def has_page_metadata(self) -> bool:
        return bool(self.pages)

    def get_image_asset(self, image_or_id: ImageIR | str) -> ImageAsset[T] | None:
        image_id = image_or_id if isinstance(image_or_id, str) else image_or_id.image_id
        return self.assets.get(image_id)

    def to_semantic(self) -> SemanticIR:
        """Return a lightweight semantic projection for chunking/search."""
        self.ensure_node_identity()
        return SemanticIR(
            doc_id=self.doc_id,
            source_path=self.source_path,
            source_doc_type=self.source_doc_type,
            blocks=_semantic_blocks(self),
        )

    def ensure_node_identity(self) -> "DocIR":
        """Populate stable node IDs and native anchors for all addressable nodes."""

        def ensure(
            node,
            kind: NodeKind,
            *,
            fallback_path: str,
            parent_debug_path: str | None,
            text: str | None = None,
        ) -> None:
            anchor_path = fallback_path
            if node.native_anchor is not None:
                anchor_path = node.native_anchor.structural_path or node.native_anchor.debug_path or fallback_path
            if node.node_id is None:
                node.node_id = _stable_node_id(kind, anchor_path)
            if node.native_anchor is None:
                node.native_anchor = NativeAnchor(
                    source_doc_type=self.source_doc_type,
                    node_kind=kind,
                    debug_path=anchor_path,
                    parent_debug_path=parent_debug_path,
                    structural_path=anchor_path,
                    text_hash=_text_hash(text),
                )
                return

            if not node.native_anchor.debug_path:
                node.native_anchor.debug_path = anchor_path
            if node.native_anchor.structural_path is None:
                node.native_anchor.structural_path = anchor_path
            if node.native_anchor.source_doc_type is None:
                node.native_anchor.source_doc_type = self.source_doc_type
            if node.native_anchor.parent_debug_path is None:
                node.native_anchor.parent_debug_path = parent_debug_path
            if node.native_anchor.text_hash is None:
                node.native_anchor.text_hash = _text_hash(text)

        def walk_paragraph(paragraph: ParagraphIR, *, fallback_path: str, parent_debug_path: str | None) -> None:
            ensure(
                paragraph,
                "paragraph",
                fallback_path=fallback_path,
                parent_debug_path=parent_debug_path,
                text=paragraph.text,
            )
            paragraph_path = _node_anchor_path(paragraph)
            run_index = 0
            image_index = 0
            table_index = 0
            for item in paragraph.content:
                if isinstance(item, RunIR):
                    run_index += 1
                    ensure(
                        item,
                        "run",
                        fallback_path=f"{paragraph_path}.r{run_index}",
                        parent_debug_path=paragraph_path,
                        text=item.text,
                    )
                elif isinstance(item, ImageIR):
                    image_index += 1
                    ensure(
                        item,
                        "image",
                        fallback_path=f"{paragraph_path}.img{image_index}",
                        parent_debug_path=paragraph_path,
                    )
                elif isinstance(item, TableIR):
                    table_index += 1
                    walk_table(
                        item,
                        fallback_path=f"{paragraph_path}.r1.tbl{table_index}",
                        parent_debug_path=paragraph_path,
                    )

        def walk_table(table: TableIR, *, fallback_path: str, parent_debug_path: str | None) -> None:
            ensure(table, "table", fallback_path=fallback_path, parent_debug_path=parent_debug_path)
            table_path = _node_anchor_path(table)
            for cell in table.cells:
                cell_path = f"{table_path}.tr{cell.row_index}.tc{cell.col_index}"
                ensure(cell, "cell", fallback_path=cell_path, parent_debug_path=table_path, text=cell.text)
                resolved_cell_path = _node_anchor_path(cell)
                for paragraph_index, paragraph in enumerate(cell.paragraphs, start=1):
                    walk_paragraph(
                        paragraph,
                        fallback_path=f"{resolved_cell_path}.p{paragraph_index}",
                        parent_debug_path=resolved_cell_path,
                    )

        for paragraph_index, paragraph in enumerate(self.paragraphs, start=1):
            walk_paragraph(paragraph, fallback_path=f"s1.p{paragraph_index}", parent_debug_path=None)
        return self

    @classmethod
    def from_file(
        cls,
        source: str | Path | bytes | BinaryIO,
        *,
        doc_type: str = "auto",
        include_tables: bool = True,
        skip_empty: bool = False,
        metadata: dict[str, Any] | None = None,
        doc_id: str | None = None,
        **doc_kwargs: Any,
    ) -> "DocIR":
        """Build document IR from a path, bytes, or binary file object."""
        from .core.document_ir_parser import build_doc_ir_from_file
        from .core.style_extractor import extract_styles

        resolved_doc_type = infer_doc_type(source, doc_type)  # type: ignore[arg-type]
        source_name = get_source_name(source)
        resolved_source_path = source_name
        logger.info("Building DocIR from %s (doc_type=%s)", _source_log_label(source), resolved_doc_type)

        if resolved_doc_type == "pdf":
            with TemporarySourcePath(source, suffix=".pdf") as source_path:
                logger.debug("Parsing PDF source at %s", source_path)
                doc_ir = build_doc_ir_from_file(
                    source_path,
                    doc_type="pdf",
                    skip_empty=skip_empty,
                    include_tables=include_tables,
                    source_path=resolved_source_path,
                    metadata=metadata,
                    doc_id=doc_id,
                    doc_cls=cls,
                    **doc_kwargs,
                )
            doc_ir.source_doc_type = resolved_doc_type
            if resolved_source_path is not None:
                doc_ir.source_path = resolved_source_path
            doc_ir.ensure_node_identity()
            _log_doc_ir_summary("Built DocIR", doc_ir)
            return doc_ir

        if resolved_doc_type == "hwp":
            with TemporarySourcePath(source, suffix=".hwp") as source_path:
                logger.debug("Parsing HWP source at %s", source_path)
                doc_ir = build_doc_ir_from_file(
                    source_path,
                    doc_type="hwp",
                    skip_empty=skip_empty,
                    include_tables=include_tables,
                    source_path=resolved_source_path,
                    metadata=metadata,
                    doc_id=doc_id,
                    doc_cls=cls,
                    **doc_kwargs,
                )
                style_map = extract_styles(
                    source_path,
                    doc_type="hwp",
                    include_tables=include_tables,
                )
        else:
            supported_source = coerce_source_to_supported_value(source, doc_type=resolved_doc_type)
            logger.debug("Parsing %s source through structured parser", resolved_doc_type)
            doc_ir = build_doc_ir_from_file(
                supported_source,
                doc_type=resolved_doc_type,
                skip_empty=skip_empty,
                include_tables=include_tables,
                source_path=resolved_source_path,
                metadata=metadata,
                doc_id=doc_id,
                doc_cls=cls,
                **doc_kwargs,
            )
            style_map = extract_styles(
                supported_source,
                doc_type=resolved_doc_type,
                include_tables=include_tables,
            )

        from .builder import apply_style_map_to_doc_ir

        apply_style_map_to_doc_ir(doc_ir, style_map)
        doc_ir.source_doc_type = resolved_doc_type
        if resolved_source_path is not None:
            doc_ir.source_path = resolved_source_path
        doc_ir.ensure_node_identity()
        _log_doc_ir_summary("Built DocIR", doc_ir)
        return doc_ir

    @classmethod
    def from_mapping(
        cls,
        mapping: dict[str, str],
        *,
        style_map=None,
        source_path: str | Path | None = None,
        source_doc_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        doc_id: str | None = None,
        **doc_kwargs: Any,
    ) -> "DocIR":
        """Build document IR from a run-level mapping."""
        from .builder import build_doc_ir_from_mapping

        logger.info("Building DocIR from mapping with %d run(s)", len(mapping))
        doc_ir = build_doc_ir_from_mapping(
            mapping,
            style_map=style_map,
            source_path=source_path,
            source_doc_type=source_doc_type,
            metadata=metadata,
            doc_id=doc_id,
            doc_cls=cls,
            **doc_kwargs,
        )
        doc_ir.ensure_node_identity()
        _log_doc_ir_summary("Built DocIR from mapping", doc_ir)
        return doc_ir

    def to_html(self, *, title: str | None = None, debug_layout: bool = False) -> str:
        """Render this document IR as styled HTML."""
        from .render_prep import prepare_doc_ir_for_html
        from .html_exporter import render_html_document

        logger.info("Rendering DocIR to HTML (debug_layout=%s)", debug_layout)
        prepare_doc_ir_for_html(self)
        return render_html_document(self, title=title, debug_layout=debug_layout)


ParagraphContentNode: TypeAlias = RunIR | ImageIR | TableIR

ParagraphIR.model_rebuild()
TableCellIR.model_rebuild()
TableIR.model_rebuild()


def _semantic_blocks(doc: DocIR) -> list[SemanticBlockIR]:
    blocks: list[SemanticBlockIR] = []
    for paragraph in doc.paragraphs:
        blocks.extend(_paragraph_semantic_blocks(paragraph))
    return blocks


def _paragraph_semantic_blocks(paragraph: ParagraphIR) -> list[SemanticBlockIR]:
    blocks: list[SemanticBlockIR] = []
    paragraph_text = _paragraph_text_for_semantic(paragraph)
    if paragraph_text:
        blocks.append(
            SemanticBlockIR(
                node_id=paragraph.node_id,
                debug_path=_node_debug_path(paragraph),
                kind="paragraph",
                page_number=paragraph.page_number,
                bbox=paragraph.bbox,
                text=paragraph_text,
            )
        )

    for image in paragraph.images:
        blocks.append(
            SemanticBlockIR(
                node_id=image.node_id,
                debug_path=_node_debug_path(image),
                kind="image",
                page_number=paragraph.page_number,
                bbox=image.bbox,
                text=_image_semantic_text(image),
            )
        )

    for table in paragraph.tables:
        blocks.append(
            SemanticBlockIR(
                node_id=table.node_id,
                debug_path=_node_debug_path(table),
                kind="table",
                page_number=_table_page_number(table, paragraph.page_number),
                bbox=table.bbox,
                text=table.markdown,
                previous_table_id=table.previous_table_id,
                next_table_id=table.next_table_id,
            )
        )

    return blocks


def _paragraph_text_for_semantic(paragraph: ParagraphIR) -> str:
    run_text = "".join(run.text for run in paragraph.runs).strip()
    if run_text:
        return run_text
    if not paragraph.images and not paragraph.tables:
        return paragraph.text.strip()
    return ""


def _image_semantic_text(image: ImageIR) -> str:
    return image.alt_text or image.title or f"[image:{image.image_id}]"


def _table_page_number(table: TableIR, fallback: int | None) -> int | None:
    for cell in table.cells:
        for paragraph in cell.paragraphs:
            if paragraph.page_number is not None:
                return paragraph.page_number
    return fallback


def _cell_rowspan(cell: TableCellIR) -> int:
    if cell.cell_style is None or cell.cell_style.rowspan is None:
        return 1
    return max(cell.cell_style.rowspan, 1)


def _cell_colspan(cell: TableCellIR) -> int:
    if cell.cell_style is None or cell.cell_style.colspan is None:
        return 1
    return max(cell.cell_style.colspan, 1)


def _image_markdown_placeholder(image: ImageIR) -> str:
    label = image.alt_text or image.title or image.image_id
    return f"[image:{label}]"


def _paragraph_list_marker_prefix(paragraph: ParagraphIR) -> str:
    style = paragraph.para_style
    list_info = style.list_info if style is not None else None
    if list_info is None or not list_info.marker:
        return ""
    indent = "  " * max(list_info.level, 0)
    return f"{indent}{list_info.marker} "


def _paragraph_markdown_text(
    paragraph: ParagraphIR,
    *,
    nested_tables: "OrderedDict[str, TableIR]",
) -> str:
    parts: list[str] = []
    for node in paragraph.content:
        if isinstance(node, RunIR):
            parts.append(node.text)
        elif isinstance(node, ImageIR):
            parts.append(_image_markdown_placeholder(node))
        elif isinstance(node, TableIR):
            table_path = _node_debug_path(node)
            nested_tables.setdefault(table_path, node)
            parts.append(f"[tbl:{table_path}]")
    text = "".join(parts).strip()
    if not text:
        return text
    return f"{_paragraph_list_marker_prefix(paragraph)}{text}"


def _escape_markdown_cell_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _cell_markdown_text(
    cell: TableCellIR,
    *,
    nested_tables: "OrderedDict[str, TableIR]",
) -> str:
    paragraph_texts = [
        text
        for paragraph in cell.paragraphs
        if (text := _paragraph_markdown_text(paragraph, nested_tables=nested_tables))
    ]
    return _escape_markdown_cell_text("<br><br>".join(paragraph_texts))


def _table_grid(table: TableIR) -> tuple[list[list[TableCellIR | None]], int, int]:
    if not table.cells:
        return [], 0, 0

    max_row = max(cell.row_index + _cell_rowspan(cell) - 1 for cell in table.cells)
    max_col = max(cell.col_index + _cell_colspan(cell) - 1 for cell in table.cells)
    grid: list[list[TableCellIR | None]] = [[None for _ in range(max_col)] for _ in range(max_row)]

    for cell in sorted(table.cells, key=lambda c: (c.row_index, c.col_index, _node_debug_path(c))):
        for row in range(cell.row_index - 1, cell.row_index - 1 + _cell_rowspan(cell)):
            for col in range(cell.col_index - 1, cell.col_index - 1 + _cell_colspan(cell)):
                grid[row][col] = cell

    return grid, max_row, max_col


def _render_table_markdown(
    table: TableIR,
    *,
    visited: set[str] | None = None,
) -> str:
    seen = visited if visited is not None else set()
    table_path = _node_debug_path(table)
    if table_path in seen:
        return f"[tbl:{table_path}]"
    seen.add(table_path)

    grid, _max_row, max_col = _table_grid(table)
    if max_col == 0:
        return ""

    nested_tables: OrderedDict[str, TableIR] = OrderedDict()
    headers = [f"col{idx}" for idx in range(1, max_col + 1)]
    lines = [
        f"| {' | '.join(headers)} |",
        f"| {' | '.join('---' for _ in headers)} |",
    ]

    for row in grid:
        cells = [
            _cell_markdown_text(cell, nested_tables=nested_tables) if cell is not None else ""
            for cell in row
        ]
        lines.append(f"| {' | '.join(cells)} |")

    sections = ["\n".join(lines)]
    for nested_table in nested_tables.values():
        nested_markdown = _render_table_markdown(nested_table, visited=seen)
        if nested_markdown:
            sections.append(f"[tbl:{_node_debug_path(nested_table)}]\n{nested_markdown}")

    return "\n\n".join(section for section in sections if section)


__all__ = [
    "BoundingBox",
    "DocIR",
    "ImageAsset",
    "ImageIR",
    "NativeAnchor",
    "NodeKind",
    "PageInfo",
    "ParagraphContentNode",
    "ParagraphIR",
    "RunIR",
    "TableCellIR",
    "TableIR",
]
