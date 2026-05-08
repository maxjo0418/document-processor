from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, Field, model_validator

from .io_utils import SourceDocType
from .models import DocIR, NativeAnchor
from .style_types import ListItemInfo

TargetKind = Literal["paragraph", "run", "cell", "table", "image"]
TextTargetKind = Literal["paragraph", "run", "cell"]
StyleTargetKind = Literal["paragraph", "run", "cell", "table", "image"]
AnnotationTargetKind = Literal["paragraph", "run"]
StructuralOperationKind = Literal[
    "insert_paragraph",
    "remove_paragraph",
    "insert_run",
    "remove_run",
    "insert_table",
    "remove_table",
    "set_cell_text",
    "insert_table_row",
    "remove_table_row",
    "insert_table_column",
    "remove_table_column",
]
InsertPosition = Literal["before", "after", "start", "end"]
EditValidationCode = Literal[
    "target_not_found",
    "target_kind_mismatch",
    "text_hash_mismatch",
    "mixed_content_not_supported",
    "paragraph_count_mismatch",
    "invalid_operation",
    "invalid_position",
    "invalid_table_shape",
    "index_out_of_bounds",
    "unsupported_source_doc_type",
    "output_path_conflicts_with_source",
    "native_source_required",
    "invalid_style",
]
AnnotationValidationCode = Literal[
    "target_not_found",
    "target_kind_mismatch",
    "mixed_content_not_supported",
    "missing_bbox",
    "missing_page_number",
    "selected_text_not_found",
    "selected_text_ambiguous",
    "occurrence_index_out_of_bounds",
    "unsupported_source_doc_type",
    "native_source_required",
    "output_path_conflicts_with_source",
    "invalid_operation",
]


class DocumentInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    source_path: str | Path | None = Field(default=None, description="Filesystem path to the source document.")
    source_bytes: bytes | None = Field(default=None, description="Raw document bytes for stateless upload-style calls.")
    doc_ir: DocIR | None = Field(default=None, description="Pre-parsed DocIR for read/in-memory edit flows.")
    source_doc_type: SourceDocType = Field(
        default="auto",
        description="Explicit source document type when it cannot be inferred.",
    )
    source_name: str | None = Field(
        default=None,
        description="Optional filename for bytes-backed documents.",
    )

    @model_validator(mode="after")
    def _validate_sources(self) -> "DocumentInput":
        source_count = sum(
            1
            for value in (self.source_path, self.source_bytes, self.doc_ir)
            if value is not None
        )
        if source_count == 0:
            raise ValueError("Provide at least one of source_path, source_bytes, or doc_ir.")
        if self.source_path is not None and self.source_bytes is not None:
            raise ValueError("Specify either source_path or source_bytes, not both.")
        return self


class TextEdit(BaseModel):
    model_config = {"extra": "forbid"}

    edit_type: Literal["text"] = Field(default="text", description="Discriminator for mixed edit batches.")
    client_edit_id: str | None = Field(default=None, description="Optional caller-provided id for correlating edit results.")
    target_kind: TextTargetKind | None = Field(
        default=None,
        description="Optional compatibility assertion. When omitted, target kind is inferred from target_id.",
    )
    target_id: str = Field(description="Stable opaque node id from the parsed document.")
    expected_text_hash: str = Field(description="Hash of the exact current text that must match before the edit is applied.")
    new_text: str = Field(description="Replacement text for the target.")
    reason: str = Field(default="", description="Short rationale for the change.")


class StructuralEdit(BaseModel):
    model_config = {"extra": "forbid"}

    edit_type: Literal["structural"] = Field(default="structural", description="Discriminator for mixed edit batches.")
    client_edit_id: str | None = Field(default=None, description="Optional caller-provided id for correlating edit results.")
    operation: StructuralOperationKind = Field(description="Structural edit operation to apply.")
    target_id: str = Field(description="Stable node_id used as the operation anchor.")
    position: InsertPosition = Field(
        default="after",
        description=(
            "Insertion position. Paragraph/table operations use before/after; "
            "run operations can use before/after for run targets or start/end for paragraph targets."
        ),
    )
    expected_text_hash: str | None = Field(
        default=None,
        description="Optional current text hash guard for remove and set operations.",
    )
    text: str | None = Field(
        default=None,
        description="Text for inserted paragraphs/runs or replacement cell text.",
    )
    rows: list[list[str]] | None = Field(
        default=None,
        description="Rectangular text matrix for insert_table.",
    )
    values: list[str] | None = Field(
        default=None,
        description="Texts for inserted table rows or columns.",
    )
    row_index: int | None = Field(
        default=None,
        ge=1,
        description="Optional 1-based table row index when target_id is a table.",
    )
    column_index: int | None = Field(
        default=None,
        ge=1,
        description="Optional 1-based table column index when target_id is a table.",
    )
    reason: str = Field(default="", description="Short rationale for the change.")


_STYLE_COMMON_FIELDS = {
    "width_pt",
    "height_pt",
}
_STYLE_PLACEMENT_FIELDS = {
    "placement_mode",
    "wrap",
    "text_flow",
    "x_relative_to",
    "y_relative_to",
    "x_align",
    "y_align",
    "x_offset_pt",
    "y_offset_pt",
    "margin_top_pt",
    "margin_right_pt",
    "margin_bottom_pt",
    "margin_left_pt",
    "allow_overlap",
    "flow_with_text",
    "z_order",
}
_STYLE_FIELDS_BY_TARGET_KIND: dict[str, set[str]] = {
    "run": {
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "superscript",
        "subscript",
        "color",
        "highlight",
        "font_size_pt",
    },
    "paragraph": {
        "paragraph_align",
        "left_indent_pt",
        "right_indent_pt",
        "first_line_indent_pt",
        "hanging_indent_pt",
    },
    "cell": {
        *_STYLE_COMMON_FIELDS,
        "background",
        "vertical_align",
        "horizontal_align",
        "padding_top_pt",
        "padding_right_pt",
        "padding_bottom_pt",
        "padding_left_pt",
        "border_top",
        "border_right",
        "border_bottom",
        "border_left",
    },
    "table": {
        *_STYLE_PLACEMENT_FIELDS,
    },
    "image": {
        *_STYLE_COMMON_FIELDS,
        *_STYLE_PLACEMENT_FIELDS,
    },
}


class StyleEdit(BaseModel):
    """Flat style mutation model designed to be used directly as an LLM tool schema."""

    model_config = {"extra": "forbid"}

    edit_type: Literal["style"] = Field(default="style", description="Discriminator for mixed edit batches.")
    client_edit_id: str | None = Field(default=None, description="Optional caller-provided id for correlating edit results.")
    target_kind: StyleTargetKind | None = Field(
        default=None,
        description="Optional compatibility assertion. When omitted, target kind is inferred from target_id.",
    )
    target_id: str = Field(description="Stable opaque node id from the parsed document.")
    reason: str = Field(default="", description="Short rationale for the change.")

    bold: bool | None = Field(default=None, description="Run bold state. None leaves the current value unchanged.")
    italic: bool | None = Field(default=None, description="Run italic state. None leaves the current value unchanged.")
    underline: bool | None = Field(default=None, description="Run underline state. None leaves the current value unchanged.")
    strikethrough: bool | None = Field(default=None, description="Run strikethrough state. None leaves the current value unchanged.")
    superscript: bool | None = Field(default=None, description="Run superscript state. None leaves the current value unchanged.")
    subscript: bool | None = Field(default=None, description="Run subscript state. None leaves the current value unchanged.")
    color: str | None = Field(default=None, description="Run text color as #RRGGBB. None leaves the current value unchanged.")
    highlight: str | None = Field(default=None, description="Run highlight color/name. None leaves the current value unchanged.")
    font_size_pt: float | None = Field(default=None, gt=0, description="Run font size in points.")

    paragraph_align: Literal["left", "center", "right", "justify"] | None = Field(default=None, description="Paragraph horizontal alignment.")
    left_indent_pt: float | None = Field(default=None, description="Paragraph left indent in points.")
    right_indent_pt: float | None = Field(default=None, description="Paragraph right indent in points.")
    first_line_indent_pt: float | None = Field(default=None, description="Paragraph first-line indent in points.")
    hanging_indent_pt: float | None = Field(default=None, ge=0, description="Paragraph hanging indent in points.")

    width_pt: float | None = Field(default=None, ge=0, description="Cell or image display width in points.")
    height_pt: float | None = Field(default=None, ge=0, description="Cell or image display height in points.")

    background: str | None = Field(default=None, description="Cell background color as #RRGGBB.")
    vertical_align: Literal["top", "middle", "bottom"] | None = Field(default=None, description="Cell vertical alignment.")
    horizontal_align: Literal["left", "center", "right", "justify"] | None = Field(default=None, description="Cell horizontal alignment.")
    padding_top_pt: float | None = Field(default=None, ge=0, description="Cell top padding in points.")
    padding_right_pt: float | None = Field(default=None, ge=0, description="Cell right padding in points.")
    padding_bottom_pt: float | None = Field(default=None, ge=0, description="Cell bottom padding in points.")
    padding_left_pt: float | None = Field(default=None, ge=0, description="Cell left padding in points.")
    border_top: str | None = Field(default=None, description="Cell top border style.")
    border_right: str | None = Field(default=None, description="Cell right border style.")
    border_bottom: str | None = Field(default=None, description="Cell bottom border style.")
    border_left: str | None = Field(default=None, description="Cell left border style.")

    placement_mode: Literal["inline", "floating"] | None = Field(default=None, description="Object placement mode for tables/images.")
    wrap: Literal["none", "square", "tight", "through", "top_bottom", "behind_text", "in_front_of_text"] | None = Field(
        default=None,
        description="Text wrapping mode for floating tables/images.",
    )
    text_flow: Literal["both_sides", "left", "right", "largest"] | None = Field(default=None, description="Side(s) where text may flow around the object.")
    x_relative_to: Literal["page", "margin", "column", "paragraph", "character"] | None = Field(default=None, description="Horizontal positioning base.")
    y_relative_to: Literal["page", "margin", "paragraph", "line"] | None = Field(default=None, description="Vertical positioning base.")
    x_align: Literal["left", "center", "right", "inside", "outside"] | None = Field(default=None, description="Horizontal alignment relative to x_relative_to.")
    y_align: Literal["top", "center", "bottom", "inside", "outside"] | None = Field(default=None, description="Vertical alignment relative to y_relative_to.")
    x_offset_pt: float | None = Field(default=None, description="Horizontal offset in points.")
    y_offset_pt: float | None = Field(default=None, description="Vertical offset in points.")
    margin_top_pt: float | None = Field(default=None, ge=0, description="Object top text distance/margin in points.")
    margin_right_pt: float | None = Field(default=None, ge=0, description="Object right text distance/margin in points.")
    margin_bottom_pt: float | None = Field(default=None, ge=0, description="Object bottom text distance/margin in points.")
    margin_left_pt: float | None = Field(default=None, ge=0, description="Object left text distance/margin in points.")
    allow_overlap: bool | None = Field(default=None, description="Whether this floating object may overlap other objects.")
    flow_with_text: bool | None = Field(default=None, description="Whether this floating object follows text flow where supported.")
    z_order: int | None = Field(default=None, description="Object z-order where supported.")

    clear_fields: list[str] = Field(default_factory=list, description="Style field names to clear. None means unchanged, so clearing is explicit.")

    @model_validator(mode="after")
    def _validate_style_fields(self) -> "StyleEdit":
        style_fields = set().union(*_STYLE_FIELDS_BY_TARGET_KIND.values())
        supplied = {
            name
            for name in style_fields
            if getattr(self, name) is not None
        }
        clear_fields = set(self.clear_fields)
        unknown_clear_fields = clear_fields - style_fields
        if unknown_clear_fields:
            raise ValueError(f"clear_fields contains unknown style fields: {sorted(unknown_clear_fields)}.")
        if self.target_kind is not None:
            allowed = _STYLE_FIELDS_BY_TARGET_KIND[self.target_kind]
            illegal = (supplied | clear_fields) - allowed
            if illegal:
                raise ValueError(f"{self.target_kind} style edits do not support fields: {sorted(illegal)}.")
        if not supplied and not clear_fields:
            raise ValueError("StyleEdit must set or clear at least one style field.")
        if self.superscript and self.subscript:
            raise ValueError("superscript and subscript cannot both be true.")
        return self


DocumentEdit: TypeAlias = Annotated[TextEdit | StructuralEdit | StyleEdit, Field(discriminator="edit_type")]


class TextAnnotation(BaseModel):
    target_kind: AnnotationTargetKind = Field(description="Whether this annotation targets a paragraph or a run.")
    target_id: str = Field(description="Stable opaque node id from the parsed document.")
    selected_text: str | None = Field(
        default=None,
        description="Exact substring to highlight inside the target. Omit to annotate the full target text.",
    )
    occurrence_index: int | None = Field(
        default=None,
        ge=0,
        description="Optional zero-based occurrence index when selected_text appears multiple times.",
    )
    label: str = Field(description="Short label shown in the review UI.")
    color: str = Field(default="#FFFF00", description="Highlight color.")
    note: str = Field(default="", description="Optional explanation shown on hover.")

    @model_validator(mode="after")
    def _validate_selection(self) -> "TextAnnotation":
        if self.selected_text == "":
            raise ValueError("selected_text must not be empty.")
        if self.selected_text is None and self.occurrence_index is not None:
            raise ValueError("occurrence_index requires selected_text.")
        return self


class DocAnnotation(BaseModel):
    target_id: str = Field(description="DocIR node_id to annotate.")
    note: str | None = Field(default=None, description="Optional annotation note text.")
    color: str = Field(default="#FFF176", description="Default annotation color as #RRGGBB.")
    selected_text: str | None = Field(
        default=None,
        description="Exact substring to highlight inside the target. Omit to annotate the full target location.",
    )
    occurrence_index: int | None = Field(
        default=None,
        ge=0,
        description="Optional zero-based occurrence index when selected_text appears multiple times.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Opaque caller metadata.")

    @model_validator(mode="after")
    def _validate_selection(self) -> "DocAnnotation":
        if self.selected_text == "":
            raise ValueError("selected_text must not be empty.")
        if self.selected_text is None and self.occurrence_index is not None:
            raise ValueError("occurrence_index requires selected_text.")
        return self


class EditableTarget(BaseModel):
    target_kind: TargetKind
    target_id: str
    parent_paragraph_id: str | None = None
    parent_table_id: str | None = None
    row_index: int | None = Field(default=None, description="1-based table row index for cell targets.")
    column_index: int | None = Field(default=None, description="1-based table column index for cell targets.")
    row_count: int | None = Field(default=None, description="Table row count for table targets.")
    column_count: int | None = Field(default=None, description="Table column count for table targets.")
    rowspan: int | None = Field(default=None, description="Cell row span for cell targets.")
    colspan: int | None = Field(default=None, description="Cell column span for cell targets.")
    current_text: str
    text_hash: str | None = None
    page_number: int | None = None
    native_anchor: NativeAnchor | None = None
    writable: bool = True
    writable_reason: str | None = None


class EditValidationIssue(BaseModel):
    code: EditValidationCode
    target_kind: TargetKind | None = None
    target_id: str | None = None
    operation: StructuralOperationKind | None = None
    message: str
    expected_text_hash: str | None = None
    current_text_hash: str | None = None
    current_text: str | None = None


class EditValidationResult(BaseModel):
    ok: bool = True
    issues: list[EditValidationIssue] = Field(default_factory=list)


class AppliedEditResult(BaseModel):
    edit_index: int
    client_edit_id: str | None = None
    edit_type: Literal["text", "structural", "style"]
    ok: bool = True
    target_id: str | None = None
    target_kind: TargetKind | None = None
    operation: StructuralOperationKind | None = None
    edits_applied: int = 0
    operations_applied: int = 0
    styles_applied: int = 0
    modified_target_ids: list[str] = Field(default_factory=list)
    created_target_ids: list[str] = Field(default_factory=list)
    removed_target_ids: list[str] = Field(default_factory=list)
    modified_run_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    validation_issue: EditValidationIssue | None = None


class ApplyDocumentEditsResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    ok: bool = True
    source_doc_type: str | None = None
    source_name: str | None = None
    output_path: str | None = None
    output_filename: str | None = None
    output_bytes: bytes | None = None
    updated_doc_ir: DocIR | None = None
    edits_applied: int = 0
    operations_applied: int = 0
    styles_applied: int = 0
    modified_target_ids: list[str] = Field(default_factory=list)
    created_target_ids: list[str] = Field(default_factory=list)
    removed_target_ids: list[str] = Field(default_factory=list)
    modified_run_ids: list[str] = Field(default_factory=list)
    edit_results: list[AppliedEditResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    validation: EditValidationResult = Field(default_factory=EditValidationResult)


class AnnotationValidationIssue(BaseModel):
    code: AnnotationValidationCode
    target_kind: AnnotationTargetKind | None = None
    target_id: str | None = None
    message: str
    selected_text: str | None = None
    occurrence_index: int | None = None
    match_count: int | None = None
    current_text: str | None = None


class AnnotationValidationResult(BaseModel):
    ok: bool = True
    issues: list[AnnotationValidationIssue] = Field(default_factory=list)


class ResolvedTextAnnotation(BaseModel):
    target_kind: AnnotationTargetKind
    target_id: str
    selected_text: str
    occurrence_index: int | None = None
    start: int
    end: int
    label: str
    color: str
    note: str


class ReviewHtmlResult(BaseModel):
    ok: bool = True
    html: str | None = None
    resolved_annotations: list[ResolvedTextAnnotation] = Field(default_factory=list)
    validation: AnnotationValidationResult = Field(default_factory=AnnotationValidationResult)


class ApplyPdfAnnotationsResult(BaseModel):
    ok: bool = True
    output_path: str | None = None
    output_filename: str | None = None
    output_bytes: bytes | None = None
    annotations_applied: int = 0
    warnings: list[str] = Field(default_factory=list)
    validation: AnnotationValidationResult = Field(default_factory=AnnotationValidationResult)


class DocumentRunContext(BaseModel):
    node_id: str
    text: str
    text_hash: str | None = None
    start: int = Field(default=0, description="Start offset of this run in the containing paragraph text.")
    end: int = Field(default=0, description="End offset of this run in the containing paragraph text.")
    native_anchor: NativeAnchor | None = None


class DocumentParagraphContext(BaseModel):
    node_id: str
    text: str
    text_hash: str | None = None
    display_text: str = ""
    page_number: int | None = None
    list_info: ListItemInfo | None = None
    has_tables: bool = False
    has_images: bool = False
    writable_as_paragraph: bool = False
    native_anchor: NativeAnchor | None = None
    runs: list[DocumentRunContext] = Field(default_factory=list)


class DocumentContextResult(BaseModel):
    source_path: str | None = None
    source_doc_type: str | None = None
    source_name: str | None = None
    paragraphs: list[DocumentParagraphContext] = Field(default_factory=list)
    missing_target_ids: list[str] = Field(default_factory=list)


class ReadDocumentResult(BaseModel):
    source_path: str | None = None
    source_doc_type: str | None = None
    source_name: str | None = None
    start: int = 0
    limit: int = 50
    total_paragraphs: int = 0
    next_start: int | None = None
    paragraphs: list[DocumentParagraphContext] = Field(default_factory=list)


class ListEditableTargetsResult(BaseModel):
    source_path: str | None = None
    source_doc_type: str | None = None
    source_name: str | None = None
    targets: list[EditableTarget] = Field(default_factory=list)
    missing_target_ids: list[str] = Field(default_factory=list)


__all__ = [
    "AnnotationTargetKind",
    "AnnotationValidationCode",
    "AnnotationValidationIssue",
    "AnnotationValidationResult",
    "ApplyPdfAnnotationsResult",
    "AppliedEditResult",
    "ApplyDocumentEditsResult",
    "DocumentContextResult",
    "DocumentEdit",
    "DocumentInput",
    "DocAnnotation",
    "DocumentParagraphContext",
    "DocumentRunContext",
    "ReadDocumentResult",
    "EditableTarget",
    "EditValidationCode",
    "EditValidationIssue",
    "EditValidationResult",
    "InsertPosition",
    "ListEditableTargetsResult",
    "ResolvedTextAnnotation",
    "ReviewHtmlResult",
    "StyleEdit",
    "StyleTargetKind",
    "TargetKind",
    "TextTargetKind",
    "TextAnnotation",
    "TextEdit",
    "StructuralEdit",
    "StructuralOperationKind",
]
