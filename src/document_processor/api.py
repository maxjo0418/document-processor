from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
from pathlib import Path

from pydantic import ValidationError

from .annotations import _Annotation, _render_annotated_html
from .api_types import (
    AnnotationValidationIssue,
    AnnotationValidationResult,
    AppliedEditResult,
    ApplyDocumentEditsResult,
    ApplyPdfAnnotationsResult,
    DocAnnotation,
    DocumentContextResult,
    DocumentEdit,
    DocumentInput,
    DocumentParagraphContext,
    DocumentRunContext,
    EditableTarget,
    EditValidationIssue,
    EditValidationResult,
    ListEditableTargetsResult,
    ReadDocumentResult,
    ResolvedTextAnnotation,
    ReviewHtmlResult,
    StyleEdit,
    StructuralEdit,
    TargetKind,
    TextAnnotation,
    TextEdit,
)
from .edit_engine import (
    EditValidationError,
    _EditEngineResult,
    _apply_document_edits_to_source,
    _apply_style_edits_to_source,
    _apply_text_edits_to_source,
    _build_doc_ir_index,
    _iter_doc_ir_paragraphs,
)
from .io_utils import SourceDocType, infer_doc_type
from .models import DocIR, ImageIR, NativeAnchor, ParagraphIR, RunIR, TableCellIR, TableIR, _anchored_node_id
from .pdf.annotations import resolve_pdf_annotations_for_doc, write_pdf_annotations

_WRITEBACK_SOURCE_TYPES = {"docx", "hwpx", "hwp"}
_OUTPUT_FILENAME_SUFFIXES = {".docx", ".hwpx"}
_TEXT_TARGET_KINDS = {"paragraph", "run", "cell"}


def _text_hash(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _first_table_cell(table: TableIR) -> TableCellIR | None:
    for cell in table.iter_cells():
        return cell
    return None


@dataclass
class _ResolvedDocument:
    doc: DocIR
    source_path: str | None
    source_doc_type: str | None
    source_name: str | None
    native_source_path: str | None = None
    native_source_bytes: bytes | None = None

    @property
    def has_native_source(self) -> bool:
        return self.native_source_path is not None or self.native_source_bytes is not None


@dataclass(frozen=True)
class _TargetIdentity:
    kind: TargetKind
    node_id: str
    native_anchor: NativeAnchor | None = None
    parent_paragraph_id: str | None = None


@dataclass
class _TargetIdentityIndex:
    by_identifier: dict[str, _TargetIdentity]


@dataclass(frozen=True)
class _ResolvedTextEdit:
    edit: TextEdit
    identity: _TargetIdentity


@dataclass(frozen=True)
class _ResolvedTextAnnotation:
    annotation: TextAnnotation
    identity: _TargetIdentity


@dataclass(frozen=True)
class _ResolvedStructuralEdit:
    operation: StructuralEdit
    identity: _TargetIdentity


@dataclass(frozen=True)
class _ResolvedStyleEdit:
    edit: StyleEdit
    identity: _TargetIdentity


def read_document(
    *,
    document: DocumentInput | None = None,
    source_path: str | Path | None = None,
    start: int = 0,
    limit: int = 50,
    include_runs: bool = True,
) -> ReadDocumentResult:
    if start < 0:
        raise ValueError("start must be greater than or equal to 0.")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500.")

    resolved = _resolve_document_args(document=document, source_path=source_path)
    paragraphs = list(_iter_doc_ir_paragraphs(resolved.doc.paragraphs))
    end = min(len(paragraphs), start + limit)
    selected = paragraphs[start:end]
    next_start = end if end < len(paragraphs) else None
    return ReadDocumentResult(
        source_path=resolved.source_path,
        source_doc_type=resolved.source_doc_type,
        source_name=resolved.source_name,
        start=start,
        limit=limit,
        total_paragraphs=len(paragraphs),
        next_start=next_start,
        paragraphs=[_paragraph_context(paragraph, include_runs=include_runs) for paragraph in selected],
    )


def get_document_context(
    *,
    document: DocumentInput | None = None,
    source_path: str | Path | None = None,
    target_ids: list[str] | None = None,
    before: int = 1,
    after: int = 1,
    include_runs: bool = True,
) -> DocumentContextResult:
    if before < 0:
        raise ValueError("before must be greater than or equal to 0.")
    if after < 0:
        raise ValueError("after must be greater than or equal to 0.")

    resolved = _resolve_document_args(document=document, source_path=source_path)
    identity_index = _build_target_identity_index(resolved.doc)
    paragraphs = list(_iter_doc_ir_paragraphs(resolved.doc.paragraphs))
    paragraph_indices = {paragraph.node_id: offset for offset, paragraph in enumerate(paragraphs)}
    run_to_paragraph = {run.node_id: paragraph for paragraph in paragraphs for run in paragraph.runs}
    image_to_paragraph = {image.node_id: paragraph for paragraph in paragraphs for image in paragraph.images}
    cell_to_anchor_paragraph = {
        cell.node_id: cell.paragraphs[0]
        for cell in _iter_doc_ir_cells(resolved.doc.paragraphs)
        if cell.node_id is not None and cell.paragraphs and cell.paragraphs[0].node_id is not None
    }
    table_to_anchor_paragraph: dict[str, ParagraphIR] = {}
    for table in _iter_doc_ir_tables(resolved.doc.paragraphs):
        first_cell = _first_table_cell(table)
        if table.node_id is not None and first_cell is not None and first_cell.paragraphs:
            table_to_anchor_paragraph[table.node_id] = first_cell.paragraphs[0]

    selected_indices: set[int] = set()
    missing_target_ids: list[str] = []
    for target_id in target_ids or []:
        identity = identity_index.by_identifier.get(target_id)
        if identity is None:
            missing_target_ids.append(target_id)
            continue
        node_id = identity.node_id
        if node_id in paragraph_indices:
            anchor_index = paragraph_indices[node_id]
        elif node_id in run_to_paragraph:
            anchor_index = paragraph_indices[run_to_paragraph[node_id].node_id]
        elif node_id in image_to_paragraph:
            anchor_index = paragraph_indices[image_to_paragraph[node_id].node_id]
        elif node_id in cell_to_anchor_paragraph:
            anchor_index = paragraph_indices[cell_to_anchor_paragraph[node_id].node_id]
        elif node_id in table_to_anchor_paragraph:
            anchor_index = paragraph_indices[table_to_anchor_paragraph[node_id].node_id]
        else:
            missing_target_ids.append(target_id)
            continue
        start = max(0, anchor_index - before)
        end = min(len(paragraphs), anchor_index + after + 1)
        selected_indices.update(range(start, end))

    ordered_indices = sorted(selected_indices)
    return DocumentContextResult(
        source_path=resolved.source_path,
        source_doc_type=resolved.source_doc_type,
        source_name=resolved.source_name,
        paragraphs=[_paragraph_context(paragraphs[index], include_runs=include_runs) for index in ordered_indices],
        missing_target_ids=missing_target_ids,
    )


def _iter_doc_ir_cells(paragraphs: list[ParagraphIR]):
    for paragraph in paragraphs:
        for table in paragraph.tables:
            yield from _iter_doc_ir_table_cells(table)


def _iter_doc_ir_table_cells(table: TableIR):
    for cell in table.iter_cells():
        yield cell
        for paragraph in cell.paragraphs:
            for nested_table in paragraph.tables:
                yield from _iter_doc_ir_table_cells(nested_table)


def _iter_doc_ir_tables(paragraphs: list[ParagraphIR]):
    for paragraph in paragraphs:
        for table in paragraph.tables:
            yield table
            for cell in table.iter_cells():
                yield from _iter_doc_ir_tables(cell.paragraphs)


def list_editable_targets(
    *,
    document: DocumentInput | None = None,
    source_path: str | Path | None = None,
    target_ids: list[str] | None = None,
    target_kinds: list[TargetKind] | None = None,
    include_child_runs: bool = False,
    only_writable: bool = True,
    max_targets: int | None = 200,
) -> ListEditableTargetsResult:
    if max_targets is not None and max_targets < 1:
        raise ValueError("max_targets must be greater than or equal to 1.")

    resolved = _resolve_document_args(document=document, source_path=source_path)
    identity_index = _build_target_identity_index(resolved.doc)
    requested_ids = target_ids or []
    requested_kinds = target_kinds or ["paragraph", "cell", "run"]
    requested_target_ids = {
        identity.node_id
        for identifier in requested_ids
        if (identity := identity_index.by_identifier.get(identifier)) is not None
    }

    targets = _collect_editable_targets(
        resolved.doc,
        target_kinds=requested_kinds,
        only_writable=only_writable,
        exact_target_ids=requested_target_ids if requested_ids else None,
        include_child_runs=include_child_runs,
        max_targets=max_targets,
    )

    missing_target_ids = [
        target_id
        for target_id in requested_ids
        if target_id not in identity_index.by_identifier
    ]
    return ListEditableTargetsResult(
        source_path=resolved.source_path,
        source_doc_type=resolved.source_doc_type,
        source_name=resolved.source_name,
        targets=targets,
        missing_target_ids=missing_target_ids,
    )


def validate_document_edits(
    *,
    document: DocumentInput | None = None,
    source_path: str | Path | None = None,
    edits: Sequence[DocumentEdit],
) -> EditValidationResult:
    resolved = _resolve_document_args(document=document, source_path=source_path)
    return _validate_document_edits_for_doc(
        resolved.doc,
        edits,
        include_writeback_support=resolved.has_native_source,
    )


def apply_document_edits(
    *,
    document: DocumentInput | None = None,
    source_path: str | Path | None = None,
    edits: Sequence[DocumentEdit],
    dry_run: bool = False,
    output_path: str | None = None,
    output_filename: str | None = None,
    return_doc_ir: bool = False,
) -> ApplyDocumentEditsResult:
    resolved = _resolve_document_args(document=document, source_path=source_path)
    validation = _validate_document_apply_request(
        resolved,
        edits,
        output_path=output_path,
        output_filename=output_filename,
    )
    if not validation.ok:
        return ApplyDocumentEditsResult(
            ok=False,
            source_doc_type=resolved.source_doc_type,
            source_name=resolved.source_name,
            edit_results=_edit_results_from_validation(edits, validation),
            validation=validation,
        )

    try:
        preview_result = _apply_mixed_edits_to_doc_ir(
            resolved.doc,
            edits,
            doc_type=resolved.source_doc_type or "auto",
            source_name=resolved.source_name,
        )
        if dry_run:
            return ApplyDocumentEditsResult(
                ok=True,
                source_doc_type=resolved.source_doc_type,
                source_name=resolved.source_name,
                updated_doc_ir=preview_result.updated_doc_ir if return_doc_ir else None,
                edits_applied=0,
                operations_applied=0,
                styles_applied=0,
                modified_target_ids=preview_result.modified_target_ids,
                created_target_ids=preview_result.created_target_ids,
                removed_target_ids=preview_result.removed_target_ids,
                modified_run_ids=preview_result.modified_run_ids,
                edit_results=preview_result.edit_results,
                warnings=preview_result.warnings,
                validation=validation,
            )

        if resolved.has_native_source:
            internal_result = _apply_mixed_edits_to_native_source(
                resolved,
                edits,
                output_path=output_path,
                output_filename=output_filename,
                doc_type=resolved.source_doc_type or "auto",
                source_name=resolved.source_name,
            )
        else:
            internal_result = preview_result
    except EditValidationError as exc:
        validation = EditValidationResult(
            ok=False,
            issues=[_issue_from_edit_exception(exc)],
        )
        return ApplyDocumentEditsResult(
            ok=False,
            source_doc_type=resolved.source_doc_type,
            source_name=resolved.source_name,
            edit_results=_edit_results_from_validation(edits, validation),
            validation=validation,
        )

    return ApplyDocumentEditsResult(
        ok=True,
        source_doc_type=internal_result.source_doc_type or resolved.source_doc_type,
        source_name=resolved.source_name,
        output_path=internal_result.output_path,
        output_filename=internal_result.output_filename,
        output_bytes=internal_result.output_bytes,
        updated_doc_ir=preview_result.updated_doc_ir if (return_doc_ir or not resolved.has_native_source) else None,
        edits_applied=preview_result.edits_applied,
        operations_applied=internal_result.operations_applied or preview_result.operations_applied,
        styles_applied=internal_result.styles_applied or preview_result.styles_applied,
        modified_target_ids=preview_result.modified_target_ids,
        created_target_ids=preview_result.created_target_ids,
        removed_target_ids=preview_result.removed_target_ids,
        modified_run_ids=preview_result.modified_run_ids,
        edit_results=internal_result.edit_results,
        warnings=[*preview_result.warnings, *internal_result.warnings],
        validation=validation,
    )


def render_review_html(
    *,
    document: DocumentInput | None = None,
    source_path: str | Path | None = None,
    annotations: list[TextAnnotation],
    title: str = "Review",
) -> ReviewHtmlResult:
    resolved = _resolve_document_args(document=document, source_path=source_path)
    validation, resolved_annotations = _validate_text_annotations_for_doc(resolved.doc, annotations)
    if not validation.ok:
        return ReviewHtmlResult(ok=False, validation=validation)

    resolved_annotation_edits, _issues = _resolve_text_annotations_for_doc(resolved.doc, annotations)
    html = _render_annotated_html(
        resolved.doc,
        [_to_render_annotation(resolved_annotation) for resolved_annotation in resolved_annotation_edits],
        title=title,
    )
    return ReviewHtmlResult(
        ok=True,
        html=html,
        resolved_annotations=resolved_annotations,
        validation=validation,
    )


def apply_pdf_annotations(
    *,
    document: DocumentInput | None = None,
    source_path: str | None = None,
    annotations: list[DocAnnotation],
    output_path: str | None = None,
    output_filename: str | None = None,
) -> ApplyPdfAnnotationsResult:
    resolved = _resolve_document_args(document=document, source_path=source_path)
    validation = _validate_pdf_annotation_apply_request(
        resolved,
        annotations,
        output_path=output_path,
        output_filename=output_filename,
    )
    if not validation.ok:
        return ApplyPdfAnnotationsResult(
            ok=False,
            validation=validation,
        )

    try:
        result = write_pdf_annotations(
            source_path=resolved.native_source_path,
            source_bytes=resolved.native_source_bytes,
            doc=resolved.doc,
            annotations=annotations,
            output_path=output_path,
            output_filename=output_filename,
        )
    except ValueError as exc:
        return ApplyPdfAnnotationsResult(
            ok=False,
            validation=AnnotationValidationResult(
                ok=False,
                issues=[AnnotationValidationIssue(code="invalid_operation", message=str(exc))],
            ),
        )

    return ApplyPdfAnnotationsResult(
        ok=True,
        output_path=result.output_path,
        output_filename=result.output_filename,
        output_bytes=result.output_bytes,
        annotations_applied=result.annotations_applied,
        validation=validation,
    )


def validate_pdf_annotations(
    *,
    document: DocumentInput | None = None,
    source_path: str | None = None,
    annotations: list[DocAnnotation],
) -> AnnotationValidationResult:
    resolved = _resolve_document_args(document=document, source_path=source_path)
    return _validate_pdf_annotations_for_doc(resolved.doc, annotations)


def validate_text_annotations(
    *,
    document: DocumentInput | None = None,
    source_path: str | Path | None = None,
    annotations: list[TextAnnotation],
) -> AnnotationValidationResult:
    resolved = _resolve_document_args(document=document, source_path=source_path)
    validation, _resolved_annotations = _validate_text_annotations_for_doc(resolved.doc, annotations)
    return validation


def _resolve_document_args(
    *,
    document: DocumentInput | None,
    source_path: str | Path | None,
) -> _ResolvedDocument:
    if document is not None and source_path is not None:
        raise ValueError("Specify either document or source_path, not both.")
    if document is None:
        if source_path is None:
            raise ValueError("Provide document or source_path.")
        document = DocumentInput(source_path=source_path)
    return _resolve_document_input(document)


def _resolve_document_input(document_input: DocumentInput) -> _ResolvedDocument:
    native_source_path = str(document_input.source_path) if document_input.source_path is not None else None
    native_source_bytes = document_input.source_bytes
    resolved_source_name = (
        document_input.source_name
        or (Path(native_source_path).name if native_source_path is not None else None)
    )

    if document_input.doc_ir is not None:
        doc = document_input.doc_ir
        doc.ensure_node_identity()
        return _ResolvedDocument(
            doc=doc,
            source_path=native_source_path or doc.source_path,
            source_doc_type=doc.source_doc_type,
            source_name=resolved_source_name or (Path(doc.source_path).name if doc.source_path else None),
            native_source_path=native_source_path,
            native_source_bytes=native_source_bytes,
        )

    if native_source_path is not None:
        doc = DocIR.from_file(Path(native_source_path), doc_type=document_input.source_doc_type)
    elif native_source_bytes is not None:
        doc = DocIR.from_file(native_source_bytes, doc_type=document_input.source_doc_type)
    else:
        raise ValueError("DocumentInput did not provide a usable source.")

    return _ResolvedDocument(
        doc=doc.ensure_node_identity(),
        source_path=doc.source_path,
        source_doc_type=doc.source_doc_type,
        source_name=resolved_source_name or (Path(doc.source_path).name if doc.source_path else None),
        native_source_path=native_source_path,
        native_source_bytes=native_source_bytes,
    )


def _native_apply_source(resolved: _ResolvedDocument) -> DocIR | str | bytes:
    if resolved.native_source_path is not None:
        return resolved.native_source_path
    if resolved.native_source_bytes is not None:
        return resolved.native_source_bytes
    return resolved.doc


def _register_target_identity(
    by_identifier: dict[str, _TargetIdentity],
    identity: _TargetIdentity,
) -> None:
    by_identifier[identity.node_id] = identity


def _build_target_identity_index(doc: DocIR) -> _TargetIdentityIndex:
    doc.ensure_node_identity()
    by_identifier: dict[str, _TargetIdentity] = {}

    def register_paragraph(paragraph: ParagraphIR, *, parent_paragraph: ParagraphIR | None = None) -> None:
        identity = _TargetIdentity(
            kind="paragraph",
            node_id=paragraph.node_id,
            native_anchor=paragraph.native_anchor,
            parent_paragraph_id=parent_paragraph.node_id if parent_paragraph is not None else None,
        )
        _register_target_identity(by_identifier, identity)
        for run in paragraph.runs:
            register_run(run, paragraph)
        for image in paragraph.images:
            register_image(image, paragraph)
        for table in paragraph.tables:
            register_table(table)

    def register_run(run: RunIR, paragraph: ParagraphIR) -> None:
        identity = _TargetIdentity(
            kind="run",
            node_id=run.node_id,
            native_anchor=run.native_anchor,
            parent_paragraph_id=paragraph.node_id,
        )
        _register_target_identity(by_identifier, identity)

    def register_image(image: ImageIR, paragraph: ParagraphIR) -> None:
        identity = _TargetIdentity(
            kind="image",
            node_id=image.node_id,
            native_anchor=image.native_anchor,
            parent_paragraph_id=paragraph.node_id,
        )
        _register_target_identity(by_identifier, identity)

    def register_table(table: TableIR) -> None:
        identity = _TargetIdentity(
            kind="table",
            node_id=table.node_id,
            native_anchor=table.native_anchor,
        )
        _register_target_identity(by_identifier, identity)
        for cell in table.iter_cells():
            register_cell(cell)

    def register_cell(cell: TableCellIR) -> None:
        identity = _TargetIdentity(
            kind="cell",
            node_id=cell.node_id,
            native_anchor=cell.native_anchor,
        )
        _register_target_identity(by_identifier, identity)
        for paragraph in cell.paragraphs:
            register_paragraph(paragraph)

    for paragraph in doc.paragraphs:
        register_paragraph(paragraph)

    return _TargetIdentityIndex(by_identifier=by_identifier)


def _resolve_text_edits_for_doc(
    doc: DocIR,
    edits: list[TextEdit],
) -> tuple[list[_ResolvedTextEdit], list[EditValidationIssue]]:
    identity_index = _build_target_identity_index(doc)
    resolved: list[_ResolvedTextEdit] = []
    issues: list[EditValidationIssue] = []
    for edit in edits:
        identity = identity_index.by_identifier.get(edit.target_id)
        if identity is None:
            issues.append(
                EditValidationIssue(
                    code="target_not_found",
                    target_kind=edit.target_kind,
                    target_id=edit.target_id,
                    message=f"Target does not exist: {edit.target_id}.",
                    expected_text_hash=edit.expected_text_hash,
                )
            )
            continue
        if identity.kind not in _TEXT_TARGET_KINDS:
            issues.append(
                EditValidationIssue(
                    code="target_kind_mismatch",
                    target_kind=identity.kind,
                    target_id=edit.target_id,
                    message=f"{edit.target_id} is a {identity.kind} target, not a text-editable paragraph, run, or cell target.",
                    expected_text_hash=edit.expected_text_hash,
                )
            )
            continue
        if edit.target_kind is not None and identity.kind != edit.target_kind:
            issues.append(
                EditValidationIssue(
                    code="target_kind_mismatch",
                    target_kind=edit.target_kind,
                    target_id=edit.target_id,
                    message=f"{edit.target_id} is a {identity.kind} target, not a {edit.target_kind} target.",
                    expected_text_hash=edit.expected_text_hash,
                )
            )
            continue
        resolved_edit = edit.model_copy(update={"target_kind": identity.kind})
        resolved.append(_ResolvedTextEdit(edit=resolved_edit, identity=identity))
    return resolved, issues


def _resolve_text_annotations_for_doc(
    doc: DocIR,
    annotations: list[TextAnnotation],
) -> tuple[list[_ResolvedTextAnnotation], list[AnnotationValidationIssue]]:
    identity_index = _build_target_identity_index(doc)
    resolved: list[_ResolvedTextAnnotation] = []
    issues: list[AnnotationValidationIssue] = []
    for annotation in annotations:
        identity = identity_index.by_identifier.get(annotation.target_id)
        if identity is None:
            issues.append(
                AnnotationValidationIssue(
                    code="target_not_found",
                    target_kind=annotation.target_kind,
                    target_id=annotation.target_id,
                    message=f"Annotation target does not exist: {annotation.target_id}.",
                    selected_text=annotation.selected_text,
                    occurrence_index=annotation.occurrence_index,
                )
            )
            continue
        resolved.append(_ResolvedTextAnnotation(annotation=annotation, identity=identity))
    return resolved, issues


def _resolve_structural_edits_for_doc(
    doc: DocIR,
    operations: list[StructuralEdit],
) -> tuple[list[_ResolvedStructuralEdit], list[EditValidationIssue]]:
    identity_index = _build_target_identity_index(doc)
    resolved: list[_ResolvedStructuralEdit] = []
    issues: list[EditValidationIssue] = []
    for operation in operations:
        identity = identity_index.by_identifier.get(operation.target_id)
        if identity is None:
            issues.append(
                EditValidationIssue(
                    code="target_not_found",
                    target_id=operation.target_id,
                    operation=operation.operation,
                    message=f"Target does not exist: {operation.target_id}.",
                    expected_text_hash=operation.expected_text_hash,
                )
            )
            continue
        resolved.append(_ResolvedStructuralEdit(operation=operation, identity=identity))
    return resolved, issues


def _resolve_style_edits_for_doc(
    doc: DocIR,
    edits: list[StyleEdit],
) -> tuple[list[_ResolvedStyleEdit], list[EditValidationIssue]]:
    identity_index = _build_target_identity_index(doc)
    resolved: list[_ResolvedStyleEdit] = []
    issues: list[EditValidationIssue] = []
    for edit in edits:
        identity = identity_index.by_identifier.get(edit.target_id)
        if identity is None:
            issues.append(
                EditValidationIssue(
                    code="target_not_found",
                    target_kind=edit.target_kind,
                    target_id=edit.target_id,
                    message=f"Target does not exist: {edit.target_id}.",
                )
            )
            continue
        if edit.target_kind is not None and identity.kind != edit.target_kind:
            issues.append(
                EditValidationIssue(
                    code="target_kind_mismatch",
                    target_kind=edit.target_kind,
                    target_id=edit.target_id,
                    message=f"{edit.target_id} is a {identity.kind} target, not a {edit.target_kind} target.",
                )
            )
            continue
        try:
            resolved_edit = StyleEdit.model_validate(
                {**edit.model_dump(), "target_kind": identity.kind}
            )
        except ValidationError as exc:
            issues.append(
                EditValidationIssue(
                    code="invalid_style",
                    target_kind=identity.kind,
                    target_id=edit.target_id,
                    message=str(exc),
                )
            )
            continue
        resolved.append(_ResolvedStyleEdit(edit=resolved_edit, identity=identity))
    return resolved, issues


def _validate_pdf_annotation_apply_request(
    resolved: _ResolvedDocument,
    annotations: list[DocAnnotation],
    *,
    output_path: str | None,
    output_filename: str | None,
) -> AnnotationValidationResult:
    validation = _validate_pdf_annotations_for_doc(resolved.doc, annotations)
    issues = list(validation.issues)

    if resolved.source_doc_type != "pdf":
        issues.append(
            AnnotationValidationIssue(
                code="unsupported_source_doc_type",
                message=f"apply_pdf_annotations only supports PDF sources, got {resolved.source_doc_type!r}.",
            )
        )

    if resolved.native_source_path is None and resolved.native_source_bytes is None:
        issues.append(
            AnnotationValidationIssue(
                code="native_source_required",
                message="apply_pdf_annotations requires source_path or source_bytes so annotations can be written back.",
            )
        )

    issues.extend(_validate_pdf_annotation_output_options(output_path=output_path, output_filename=output_filename))

    if resolved.native_source_path is not None:
        source = Path(resolved.native_source_path)
        requested = _requested_output_path_for_pdf_annotations(
            source,
            output_path=output_path,
            output_filename=output_filename,
        )
        if _same_path(source, requested):
            issues.append(
                AnnotationValidationIssue(
                    code="output_path_conflicts_with_source",
                    message=(
                        f"Output path would overwrite the source file: {requested}. "
                        "Pick a different output_path or output_filename."
                    ),
                )
            )

    return AnnotationValidationResult(ok=not issues, issues=issues)


def _requested_output_path_for_pdf_annotations(
    source: Path,
    *,
    output_path: str | None,
    output_filename: str | None,
) -> Path:
    if output_path is not None:
        return Path(output_path)
    if output_filename is not None:
        return source.with_name(output_filename)
    return source.with_name(f"{source.stem}_annotated.pdf")


def _validate_pdf_annotation_output_options(
    *,
    output_path: str | None,
    output_filename: str | None,
) -> list[AnnotationValidationIssue]:
    issues: list[AnnotationValidationIssue] = []
    if output_path is not None and output_filename is not None:
        issues.append(
            AnnotationValidationIssue(
                code="invalid_operation",
                message="Specify either output_path or output_filename, not both.",
            )
        )
    if output_filename is not None:
        filename = output_filename.strip()
        if not filename:
            issues.append(AnnotationValidationIssue(code="invalid_operation", message="output_filename must not be empty."))
        else:
            pure = Path(filename)
            if pure.is_absolute() or pure.name != filename or filename in {".", ".."}:
                issues.append(
                    AnnotationValidationIssue(
                        code="invalid_operation",
                        message="output_filename must be a filename only, without directory segments.",
                    )
                )
    return issues


def _validate_pdf_annotations_for_doc(
    doc: DocIR,
    annotations: list[DocAnnotation],
) -> AnnotationValidationResult:
    _resolved, issues = resolve_pdf_annotations_for_doc(doc, annotations)
    return AnnotationValidationResult(ok=not issues, issues=issues)


def _validate_document_apply_request(
    resolved: _ResolvedDocument,
    edits: Sequence[DocumentEdit],
    *,
    output_path: str | None,
    output_filename: str | None,
) -> EditValidationResult:
    issues = _validate_document_edits_for_doc(
        resolved.doc,
        edits,
        include_writeback_support=resolved.has_native_source,
    ).issues

    issues.extend(_validate_output_options(output_path=output_path, output_filename=output_filename))
    issues.extend(_validate_output_filename_extension(output_filename, resolved.source_doc_type))

    if not resolved.has_native_source and (output_path is not None or output_filename is not None):
        issues.append(
            EditValidationIssue(
                code="native_source_required",
                message="output_path and output_filename require a native source document.",
            )
        )

    if resolved.native_source_path is not None:
        issues.extend(
            _validate_apply_output_request(
                Path(resolved.native_source_path),
                resolved.source_doc_type,
                output_path=output_path,
                output_filename=output_filename,
            ).issues
        )

    return EditValidationResult(ok=not issues, issues=issues)


def _issue_to_exception(issue: EditValidationIssue) -> EditValidationError:
    return EditValidationError(
        issue.message,
        code=issue.code,
        target_kind=issue.target_kind,
        target_id=issue.target_id,
        operation=issue.operation,
        expected_text_hash=issue.expected_text_hash,
        current_text_hash=issue.current_text_hash,
        current_text=issue.current_text,
    )


def _extend_unique(values: list[str], additions: list[str]) -> None:
    for value in additions:
        if value not in values:
            values.append(value)


def _merge_engine_result(target: _EditEngineResult, step: _EditEngineResult) -> None:
    target.edits_applied += step.edits_applied
    target.operations_applied += step.operations_applied
    target.styles_applied += step.styles_applied
    _extend_unique(target.modified_target_ids, step.modified_target_ids)
    _extend_unique(target.created_target_ids, step.created_target_ids)
    _extend_unique(target.removed_target_ids, step.removed_target_ids)
    _extend_unique(target.modified_run_ids, step.modified_run_ids)
    _extend_unique(target.warnings, step.warnings)


def _edit_result_from_step(
    *,
    edit_index: int,
    edit: DocumentEdit,
    step: _EditEngineResult,
    warnings: list[str] | None = None,
) -> AppliedEditResult:
    return AppliedEditResult(
        edit_index=edit_index,
        client_edit_id=getattr(edit, "client_edit_id", None),
        edit_type=edit.edit_type,
        ok=True,
        target_id=getattr(edit, "target_id", None),
        target_kind=getattr(edit, "target_kind", None),
        operation=getattr(edit, "operation", None),
        edits_applied=step.edits_applied,
        operations_applied=step.operations_applied,
        styles_applied=step.styles_applied,
        modified_target_ids=step.modified_target_ids,
        created_target_ids=step.created_target_ids,
        removed_target_ids=step.removed_target_ids,
        modified_run_ids=step.modified_run_ids,
        warnings=warnings if warnings is not None else step.warnings,
    )


def _edit_result_from_issue(
    *,
    edit_index: int,
    edit: DocumentEdit,
    issue: EditValidationIssue,
) -> AppliedEditResult:
    return AppliedEditResult(
        edit_index=edit_index,
        client_edit_id=getattr(edit, "client_edit_id", None),
        edit_type=edit.edit_type,
        ok=False,
        target_id=getattr(edit, "target_id", None),
        target_kind=issue.target_kind or getattr(edit, "target_kind", None),
        operation=getattr(edit, "operation", None),
        validation_issue=issue,
    )


def _edit_index_for_issue(edits: Sequence[DocumentEdit], issue: EditValidationIssue) -> int | None:
    for index, edit in enumerate(edits):
        if issue.target_id is not None and getattr(edit, "target_id", None) != issue.target_id:
            continue
        if issue.operation is not None and getattr(edit, "operation", None) != issue.operation:
            continue
        return index
    return None


def _edit_results_from_validation(
    edits: Sequence[DocumentEdit],
    validation: EditValidationResult,
) -> list[AppliedEditResult]:
    results: list[AppliedEditResult] = []
    emitted_indexes: set[int] = set()
    for issue in validation.issues:
        index = _edit_index_for_issue(edits, issue)
        if index is None or index in emitted_indexes:
            continue
        results.append(_edit_result_from_issue(edit_index=index, edit=edits[index], issue=issue))
        emitted_indexes.add(index)
    return results


def _canonical_text_edit_for_doc(doc: DocIR, edit: TextEdit, *, native: bool) -> TextEdit:
    resolved_edits, issues = _resolve_text_edits_for_doc(doc, [edit])
    if issues:
        raise _issue_to_exception(issues[0])
    return _to_canonical_text_edit(resolved_edits[0], native=native)


def _canonical_structural_edit_for_doc(doc: DocIR, edit: StructuralEdit, *, native: bool) -> StructuralEdit:
    resolved_operations, issues = _resolve_structural_edits_for_doc(doc, [edit])
    if issues:
        raise _issue_to_exception(issues[0])
    return _to_canonical_structural_edit(resolved_operations[0], native=native)


def _canonical_style_edit_for_doc(doc: DocIR, edit: StyleEdit, *, native: bool) -> StyleEdit:
    resolved_edits, issues = _resolve_style_edits_for_doc(doc, [edit])
    if issues:
        raise _issue_to_exception(issues[0])
    return _to_canonical_style_edit(resolved_edits[0], native=native)


def _apply_mixed_edits_to_doc_ir(
    doc: DocIR,
    edits: Sequence[DocumentEdit],
    *,
    doc_type: SourceDocType = "auto",
    source_name: str | None = None,
) -> _EditEngineResult:
    current_doc = doc.model_copy(deep=True)
    current_doc.ensure_node_identity()
    result = _EditEngineResult(source_doc_type=current_doc.source_doc_type or (None if doc_type == "auto" else doc_type))

    for edit_index, edit in enumerate(edits):
        if isinstance(edit, TextEdit):
            canonical_edit = _canonical_text_edit_for_doc(current_doc, edit, native=False)
            step = _apply_text_edits_to_source(
                current_doc,
                [canonical_edit],
                doc_type=current_doc.source_doc_type or doc_type,
                source_name=source_name,
            )
        elif isinstance(edit, StructuralEdit):
            canonical_edit = _canonical_structural_edit_for_doc(current_doc, edit, native=False)
            step = _apply_document_edits_to_source(
                current_doc,
                [canonical_edit],
                doc_type=current_doc.source_doc_type or doc_type,
                source_name=source_name,
            )
        else:
            canonical_edit = _canonical_style_edit_for_doc(current_doc, edit, native=False)
            step = _apply_style_edits_to_source(
                current_doc,
                [canonical_edit],
                doc_type=current_doc.source_doc_type or doc_type,
                source_name=source_name,
            )
        if step.updated_doc_ir is None:
            raise EditValidationError("Edit preview did not return updated DocIR.")
        _merge_engine_result(result, step)
        result.edit_results.append(_edit_result_from_step(edit_index=edit_index, edit=canonical_edit, step=step))
        current_doc = step.updated_doc_ir

    result.updated_doc_ir = current_doc
    return result


def _apply_mixed_edits_to_native_source(
    resolved: _ResolvedDocument,
    edits: Sequence[DocumentEdit],
    *,
    output_path: str | None,
    output_filename: str | None,
    doc_type: SourceDocType = "auto",
    source_name: str | None = None,
) -> _EditEngineResult:
    if resolved.native_source_path is not None:
        source_path = Path(resolved.native_source_path)
        current_bytes = source_path.read_bytes()
        current_source_name = source_path.name
    elif resolved.native_source_bytes is not None:
        current_bytes = resolved.native_source_bytes
        current_source_name = source_name or resolved.source_name
    else:
        return _apply_mixed_edits_to_doc_ir(resolved.doc, edits, doc_type=doc_type, source_name=source_name)

    current_doc_type: SourceDocType = resolved.source_doc_type or doc_type
    mapping_doc = resolved.doc.model_copy(deep=True)
    mapping_doc.ensure_node_identity()
    result = _EditEngineResult(source_doc_type=resolved.source_doc_type)

    for edit_index, edit in enumerate(edits):
        if isinstance(edit, TextEdit):
            native_edit = _canonical_text_edit_for_doc(mapping_doc, edit, native=True)
            preview_edit = _canonical_text_edit_for_doc(mapping_doc, edit, native=False)
            step = _apply_text_edits_to_source(
                current_bytes,
                [native_edit],
                doc_type=current_doc_type,
                source_name=current_source_name,
            )
            preview = _apply_text_edits_to_source(
                mapping_doc,
                [preview_edit],
                doc_type=mapping_doc.source_doc_type or current_doc_type,
                source_name=current_source_name,
            )
        elif isinstance(edit, StructuralEdit):
            native_edit = _canonical_structural_edit_for_doc(mapping_doc, edit, native=True)
            preview_edit = _canonical_structural_edit_for_doc(mapping_doc, edit, native=False)
            step = _apply_document_edits_to_source(
                current_bytes,
                [native_edit],
                doc_type=current_doc_type,
                source_name=current_source_name,
            )
            preview = _apply_document_edits_to_source(
                mapping_doc,
                [preview_edit],
                doc_type=mapping_doc.source_doc_type or current_doc_type,
                source_name=current_source_name,
            )
        else:
            native_edit = _canonical_style_edit_for_doc(mapping_doc, edit, native=True)
            preview_edit = _canonical_style_edit_for_doc(mapping_doc, edit, native=False)
            step = _apply_style_edits_to_source(
                current_bytes,
                [native_edit],
                doc_type=current_doc_type,
                source_name=current_source_name,
            )
            preview = _apply_style_edits_to_source(
                mapping_doc,
                [preview_edit],
                doc_type=mapping_doc.source_doc_type or current_doc_type,
                source_name=current_source_name,
            )

        if step.output_bytes is None:
            raise EditValidationError("Native edit did not return output bytes.")
        if preview.updated_doc_ir is None:
            raise EditValidationError("Edit preview did not return updated DocIR.")
        _merge_engine_result(result, step)
        result.edit_results.append(
            _edit_result_from_step(
                edit_index=edit_index,
                edit=preview_edit,
                step=preview,
                warnings=[*preview.warnings, *step.warnings],
            )
        )
        current_bytes = step.output_bytes
        current_doc_type = infer_doc_type(current_bytes, "auto")
        current_source_name = step.output_filename or _default_output_filename(
            source_name=current_source_name,
            source_doc_type=current_doc_type,
        )
        mapping_doc = preview.updated_doc_ir

    if resolved.native_source_path is not None:
        requested_final_path = _requested_output_path_for_native_source(
            Path(resolved.native_source_path),
            output_path=output_path,
            output_filename=output_filename,
        )
        final_path = _normalize_output_path_for_source_doc_type(requested_final_path, resolved.source_doc_type)
        if requested_final_path.suffix.lower() != final_path.suffix.lower():
            if resolved.source_doc_type == "hwp":
                result.warnings.append(f"HWP sources are written back as HWPX; adjusted output path to {final_path}.")
            else:
                result.warnings.append(
                    f"{str(resolved.source_doc_type).upper()} write-back keeps the native {final_path.suffix} format; "
                    f"adjusted output path to {final_path}."
                )
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(current_bytes)
        result.output_path = str(final_path)
        result.output_filename = final_path.name
    else:
        result.output_bytes = current_bytes
        result.output_filename = output_filename or _default_output_filename(
            source_name=resolved.source_name,
            source_doc_type=current_doc_type,
        )

    result.source_doc_type = current_doc_type
    return result


def _validate_text_edits_for_doc(
    doc: DocIR,
    edits: list[TextEdit],
    *,
    include_writeback_support: bool,
) -> EditValidationResult:
    issues: list[EditValidationIssue] = []
    index = _build_doc_ir_index(doc)
    resolved_edits, resolution_issues = _resolve_text_edits_for_doc(doc, edits)
    issues.extend(resolution_issues)

    if include_writeback_support and doc.source_doc_type not in _WRITEBACK_SOURCE_TYPES:
        issues.append(
            EditValidationIssue(
                code="unsupported_source_doc_type",
                message=(
                    "Native write-back is currently supported only for docx, hwp, and hwpx; "
                    f"got {doc.source_doc_type!r}."
                ),
            )
        )

    for resolved_edit in resolved_edits:
        issues.extend(_validate_single_text_edit(index, resolved_edit))

    return EditValidationResult(ok=not issues, issues=issues)


def _validate_document_edits_for_doc(
    doc: DocIR,
    edits: Sequence[DocumentEdit],
    *,
    include_writeback_support: bool,
) -> EditValidationResult:
    issues: list[EditValidationIssue] = []

    if include_writeback_support and doc.source_doc_type not in _WRITEBACK_SOURCE_TYPES:
        issues.append(
            EditValidationIssue(
                code="unsupported_source_doc_type",
                message=(
                    "Native write-back is currently supported only for docx, hwp, and hwpx; "
                    f"got {doc.source_doc_type!r}."
                ),
            )
        )

    if issues:
        return EditValidationResult(ok=False, issues=issues)

    try:
        _apply_mixed_edits_to_doc_ir(
            doc,
            doc_type=doc.source_doc_type or "auto",
            source_name=doc.source_path,
            edits=edits,
        )
    except EditValidationError as exc:
        issues.append(_issue_from_edit_exception(exc))

    return EditValidationResult(ok=not issues, issues=issues)


def _validate_single_text_edit(index, resolved_edit: _ResolvedTextEdit) -> list[EditValidationIssue]:
    edit = resolved_edit.edit
    target_id = resolved_edit.identity.node_id

    if edit.target_kind == "paragraph":
        paragraph = index.paragraphs.get(target_id)
        if paragraph is None:
            if target_id in index.runs:
                return [
                    EditValidationIssue(
                        code="target_kind_mismatch",
                        target_kind=edit.target_kind,
                        target_id=target_id,
                        message=f"{target_id} is a run target, not a paragraph target.",
                    )
                ]
            if target_id in index.cells:
                return [
                    EditValidationIssue(
                        code="target_kind_mismatch",
                        target_kind=edit.target_kind,
                        target_id=target_id,
                        message=f"{target_id} is a cell target, not a paragraph target.",
                    )
                ]
            return [
                EditValidationIssue(
                    code="target_not_found",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=f"Paragraph target does not exist: {target_id}.",
                )
            ]
        if paragraph.has_non_run_content:
            return [
                EditValidationIssue(
                    code="mixed_content_not_supported",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=f"Paragraph target has mixed content and is not safely writable: {target_id}.",
                    expected_text_hash=edit.expected_text_hash,
                    current_text_hash=_text_hash(paragraph.text),
                    current_text=paragraph.text,
                )
            ]
        current_text_hash = _text_hash(paragraph.text)
        if current_text_hash != edit.expected_text_hash:
            return [
                EditValidationIssue(
                    code="text_hash_mismatch",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=f"Paragraph text hash mismatch for {target_id}.",
                    expected_text_hash=edit.expected_text_hash,
                    current_text_hash=current_text_hash,
                    current_text=paragraph.text,
                )
            ]
        return []

    if edit.target_kind == "cell":
        cell = index.cells.get(target_id)
        if cell is None:
            if target_id in index.paragraphs:
                return [
                    EditValidationIssue(
                        code="target_kind_mismatch",
                        target_kind=edit.target_kind,
                        target_id=target_id,
                        message=f"{target_id} is a paragraph target, not a cell target.",
                    )
                ]
            if target_id in index.runs:
                return [
                    EditValidationIssue(
                        code="target_kind_mismatch",
                        target_kind=edit.target_kind,
                        target_id=target_id,
                        message=f"{target_id} is a run target, not a cell target.",
                    )
                ]
            return [
                EditValidationIssue(
                    code="target_not_found",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=f"Cell target does not exist: {target_id}.",
                )
            ]

        writable, writable_reason = _cell_writable(cell)
        if not writable:
            return [
                EditValidationIssue(
                    code="mixed_content_not_supported",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=writable_reason or f"Cell target is not safely writable: {target_id}.",
                    expected_text_hash=edit.expected_text_hash,
                    current_text_hash=_text_hash(cell.text),
                    current_text=cell.text,
                )
            ]
        current_text_hash = _text_hash(cell.text)
        if current_text_hash != edit.expected_text_hash:
            return [
                EditValidationIssue(
                    code="text_hash_mismatch",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=f"Cell text hash mismatch for {target_id}.",
                    expected_text_hash=edit.expected_text_hash,
                    current_text_hash=current_text_hash,
                    current_text=cell.text,
                )
            ]
        expected_paragraphs = len(cell.paragraphs)
        new_paragraphs = len(edit.new_text.split("\n"))
        if new_paragraphs != expected_paragraphs:
            return [
                EditValidationIssue(
                    code="paragraph_count_mismatch",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=(
                        f"Cell text replacement must preserve paragraph count for {target_id}: "
                        f"expected {expected_paragraphs} line(s), got {new_paragraphs}."
                    ),
                    expected_text_hash=edit.expected_text_hash,
                    current_text_hash=current_text_hash,
                    current_text=cell.text,
                )
            ]
        return []

    run = index.runs.get(target_id)
    if run is None:
        if target_id in index.paragraphs:
            return [
                EditValidationIssue(
                    code="target_kind_mismatch",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=f"{target_id} is a paragraph target, not a run target.",
                )
            ]
        if target_id in index.cells:
            return [
                EditValidationIssue(
                    code="target_kind_mismatch",
                    target_kind=edit.target_kind,
                    target_id=target_id,
                    message=f"{target_id} is a cell target, not a run target.",
                )
            ]
        return [
            EditValidationIssue(
                code="target_not_found",
                target_kind=edit.target_kind,
                target_id=target_id,
                message=f"Run target does not exist: {target_id}.",
            )
        ]
    current_text_hash = _text_hash(run.text)
    if current_text_hash != edit.expected_text_hash:
        return [
            EditValidationIssue(
                code="text_hash_mismatch",
                target_kind=edit.target_kind,
                target_id=target_id,
                message=f"Run text hash mismatch for {target_id}.",
                expected_text_hash=edit.expected_text_hash,
                current_text_hash=current_text_hash,
                current_text=run.text,
            )
        ]
    return []


def _validate_apply_output_request(
    source: Path,
    source_doc_type: str | None,
    *,
    output_path: str | None,
    output_filename: str | None,
) -> EditValidationResult:
    final_output_path = _final_output_path_for_native_source(
        source,
        source_doc_type,
        output_path=output_path,
        output_filename=output_filename,
    )

    if _same_path(source, final_output_path):
        return EditValidationResult(
            ok=False,
            issues=[
                EditValidationIssue(
                    code="output_path_conflicts_with_source",
                    message=(
                        f"Output path would overwrite the source file: {final_output_path}. "
                        "Pick a different output_path or output_filename."
                    ),
                )
            ],
        )

    return EditValidationResult()


def _validate_output_options(*, output_path: str | None, output_filename: str | None) -> list[EditValidationIssue]:
    issues: list[EditValidationIssue] = []
    if output_path is not None and output_filename is not None:
        issues.append(
            EditValidationIssue(
                code="invalid_operation",
                message="Specify either output_path or output_filename, not both.",
            )
        )
    if output_filename is not None:
        filename = output_filename.strip()
        if not filename:
            issues.append(
                EditValidationIssue(
                    code="invalid_operation",
                    message="output_filename must not be empty.",
                )
            )
        else:
            pure = Path(filename)
            if pure.is_absolute() or pure.name != filename or filename in {".", ".."}:
                issues.append(
                    EditValidationIssue(
                        code="invalid_operation",
                        message="output_filename must be a filename only, without directory segments.",
                    )
                )
    return issues


def _validate_output_filename_extension(
    output_filename: str | None,
    source_doc_type: str | None,
) -> list[EditValidationIssue]:
    if output_filename is None:
        return []

    filename = output_filename.strip()
    pure = Path(filename)
    if not filename or pure.is_absolute() or pure.name != filename or filename in {".", ".."}:
        return []

    suffix = pure.suffix.lower()
    if suffix not in _OUTPUT_FILENAME_SUFFIXES:
        allowed = ", ".join(sorted(_OUTPUT_FILENAME_SUFFIXES))
        return [
            EditValidationIssue(
                code="invalid_operation",
                message=f"output_filename must end with a supported write-back extension: {allowed}.",
            )
        ]

    expected_suffix = _output_suffix_for_source_doc_type(source_doc_type)
    if expected_suffix in _OUTPUT_FILENAME_SUFFIXES and suffix != expected_suffix:
        source_label = str(source_doc_type or "document").upper()
        return [
            EditValidationIssue(
                code="invalid_operation",
                message=(
                    f"output_filename extension {suffix!r} does not match {source_label} write-back; "
                    f"use {expected_suffix!r}."
                ),
            )
        ]

    return []


def _issue_from_edit_exception(exc: EditValidationError) -> EditValidationIssue:
    return EditValidationIssue(
        code=getattr(exc, "code", "invalid_operation"),
        target_kind=getattr(exc, "target_kind", None),
        target_id=getattr(exc, "target_id", None),
        operation=getattr(exc, "operation", None),
        message=str(exc),
        expected_text_hash=getattr(exc, "expected_text_hash", None),
        current_text_hash=getattr(exc, "current_text_hash", None),
        current_text=getattr(exc, "current_text", None),
    )


def _normalize_output_path_for_source_doc_type(output_path: Path, source_doc_type: str | None) -> Path:
    if source_doc_type == "docx" and output_path.suffix.lower() != ".docx":
        return output_path.with_suffix(".docx")
    if source_doc_type in {"hwpx", "hwp"} and output_path.suffix.lower() != ".hwpx":
        return output_path.with_suffix(".hwpx")
    return output_path


def _output_suffix_for_source_doc_type(source_doc_type: str | None) -> str:
    if source_doc_type == "docx":
        return ".docx"
    if source_doc_type in {"hwpx", "hwp"}:
        return ".hwpx"
    return ".bin"


def _default_output_filename(*, source_name: str | None, source_doc_type: str | None) -> str:
    suffix = _output_suffix_for_source_doc_type(source_doc_type)
    if source_name:
        source_path = Path(source_name)
        return f"{source_path.stem}_edited{suffix}"
    return f"document_edited{suffix}"


def _final_output_path_for_native_source(
    source: Path,
    source_doc_type: str | None,
    *,
    output_path: str | None,
    output_filename: str | None,
) -> Path:
    target = _requested_output_path_for_native_source(
        source,
        output_path=output_path,
        output_filename=output_filename,
    )
    return _normalize_output_path_for_source_doc_type(target, source_doc_type)


def _requested_output_path_for_native_source(
    source: Path,
    *,
    output_path: str | None,
    output_filename: str | None,
) -> Path:
    if output_path is not None:
        return Path(output_path)
    if output_filename is not None:
        return source.with_name(output_filename)
    return source.with_name(_default_output_filename(source_name=source.name, source_doc_type=infer_doc_type(source, "auto")))


def _validate_text_annotations_for_doc(
    doc: DocIR,
    annotations: list[TextAnnotation],
) -> tuple[AnnotationValidationResult, list[ResolvedTextAnnotation]]:
    doc.ensure_node_identity()
    paragraphs = list(_iter_doc_ir_paragraphs(doc.paragraphs))
    paragraph_map = {paragraph.node_id: paragraph for paragraph in paragraphs}
    run_map = {run.node_id: run for paragraph in paragraphs for run in paragraph.runs}
    resolved_annotations, resolution_issues = _resolve_text_annotations_for_doc(doc, annotations)

    issues: list[AnnotationValidationIssue] = list(resolution_issues)
    resolved: list[ResolvedTextAnnotation] = []

    for resolved_annotation in resolved_annotations:
        annotation = resolved_annotation.annotation
        target_id = resolved_annotation.identity.node_id
        if annotation.target_kind == "paragraph":
            paragraph = paragraph_map.get(target_id)
            if paragraph is None:
                if target_id in run_map:
                    issues.append(
                        AnnotationValidationIssue(
                            code="target_kind_mismatch",
                            target_kind=annotation.target_kind,
                            target_id=target_id,
                            message=f"{target_id} is a run target, not a paragraph target.",
                        )
                    )
                else:
                    issues.append(
                        AnnotationValidationIssue(
                            code="target_not_found",
                            target_kind=annotation.target_kind,
                            target_id=target_id,
                            message=f"Paragraph target does not exist: {target_id}.",
                        )
                    )
                continue
            if paragraph.tables or paragraph.images:
                issues.append(
                    AnnotationValidationIssue(
                        code="mixed_content_not_supported",
                        target_kind=annotation.target_kind,
                        target_id=target_id,
                        message=f"Paragraph annotations do not support mixed content: {target_id}.",
                        current_text=paragraph.text,
                    )
                )
                continue
            text = paragraph.text or ""
        else:
            run = run_map.get(target_id)
            if run is None:
                if target_id in paragraph_map:
                    issues.append(
                        AnnotationValidationIssue(
                            code="target_kind_mismatch",
                            target_kind=annotation.target_kind,
                            target_id=target_id,
                            message=f"{target_id} is a paragraph target, not a run target.",
                        )
                    )
                else:
                    issues.append(
                        AnnotationValidationIssue(
                            code="target_not_found",
                            target_kind=annotation.target_kind,
                            target_id=target_id,
                            message=f"Run target does not exist: {target_id}.",
                        )
                    )
                continue
            text = run.text

        start, end, match_text, resolved_occurrence_index, issue = _resolve_text_annotation_span(
            text=text,
            annotation=annotation,
        )
        if issue is not None:
            issues.append(
                AnnotationValidationIssue(
                    code=issue["code"],
                    target_kind=annotation.target_kind,
                    target_id=target_id,
                    message=issue["message"],
                    selected_text=annotation.selected_text,
                    occurrence_index=annotation.occurrence_index,
                    match_count=issue.get("match_count"),
                    current_text=text,
                )
            )
            continue

        resolved.append(
            ResolvedTextAnnotation(
                target_kind=annotation.target_kind,
                target_id=target_id,
                selected_text=match_text,
                occurrence_index=resolved_occurrence_index,
                start=start,
                end=end,
                label=annotation.label,
                color=annotation.color,
                note=annotation.note,
            )
        )

    return AnnotationValidationResult(ok=not issues, issues=issues), resolved


def _paragraph_context(paragraph: ParagraphIR, *, include_runs: bool) -> DocumentParagraphContext:
    writable, _reason = _paragraph_writable(paragraph)
    text = paragraph.text or ""
    return DocumentParagraphContext(
        node_id=paragraph.node_id,
        text=text,
        text_hash=_text_hash(text),
        display_text=_paragraph_display_text(paragraph),
        page_number=paragraph.page_number,
        list_info=paragraph.para_style.list_info if paragraph.para_style is not None else None,
        has_tables=bool(paragraph.tables),
        has_images=bool(paragraph.images),
        writable_as_paragraph=writable,
        native_anchor=paragraph.native_anchor,
        runs=_run_contexts(paragraph) if include_runs else [],
    )


def _paragraph_display_text(paragraph: ParagraphIR) -> str:
    text = paragraph.text or ""
    list_info = paragraph.para_style.list_info if paragraph.para_style is not None else None
    if list_info is None or not list_info.marker:
        return text
    indent = "  " * max(list_info.level, 0)
    return f"{indent}{list_info.marker} {text}".rstrip()


def _run_contexts(paragraph: ParagraphIR) -> list[DocumentRunContext]:
    contexts: list[DocumentRunContext] = []
    cursor = 0
    for run in paragraph.runs:
        start = cursor
        end = start + len(run.text)
        contexts.append(
            DocumentRunContext(
                node_id=run.node_id,
                text=run.text,
                text_hash=_text_hash(run.text),
                start=start,
                end=end,
                native_anchor=run.native_anchor,
            )
        )
        cursor = end
    return contexts


def _collect_editable_targets(
    doc: DocIR,
    *,
    target_kinds: list[TargetKind],
    only_writable: bool,
    exact_target_ids: set[str] | None = None,
    include_child_runs: bool = False,
    max_targets: int | None = None,
) -> list[EditableTarget]:
    doc.ensure_node_identity()
    results: list[EditableTarget] = []
    requested_parent_ids = exact_target_ids or set()
    paragraph_to_cell = {
        paragraph.node_id: cell
        for cell in _iter_doc_ir_cells(doc.paragraphs)
        for paragraph in cell.paragraphs
    }
    cell_to_table_position = {
        cell.node_id: (table, row_index, col_index)
        for table in _iter_doc_ir_tables(doc.paragraphs)
        for row_index, col_index, cell in table.iter_cell_positions()
    }
    emitted_cell_ids: set[str] = set()
    for paragraph in _iter_doc_ir_paragraphs(doc.paragraphs):
        parent_cell = paragraph_to_cell.get(paragraph.node_id)
        if parent_cell is not None and parent_cell.node_id not in emitted_cell_ids:
            cell_requested = exact_target_ids is None or parent_cell.node_id in exact_target_ids
            cell_writable, cell_writable_reason = _cell_writable(parent_cell)
            parent_table_position = cell_to_table_position.get(parent_cell.node_id)
            parent_table = parent_table_position[0] if parent_table_position is not None else None
            row_index = parent_table_position[1] if parent_table_position is not None else None
            column_index = parent_table_position[2] if parent_table_position is not None else None
            cell_style = parent_cell.cell_style
            if "cell" in target_kinds and cell_requested:
                if not only_writable or cell_writable:
                    results.append(
                        EditableTarget(
                            target_kind="cell",
                            target_id=parent_cell.node_id,
                            parent_paragraph_id=paragraph.node_id,
                            parent_table_id=parent_table.node_id if parent_table is not None else None,
                            row_index=row_index,
                            column_index=column_index,
                            rowspan=max(cell_style.rowspan, 1) if cell_style is not None else 1,
                            colspan=max(cell_style.colspan, 1) if cell_style is not None else 1,
                            current_text=parent_cell.text,
                            text_hash=_text_hash(parent_cell.text),
                            page_number=paragraph.page_number,
                            native_anchor=parent_cell.native_anchor,
                            writable=cell_writable,
                            writable_reason=cell_writable_reason,
                        )
                    )
            emitted_cell_ids.add(parent_cell.node_id)

        paragraph_requested = exact_target_ids is None or paragraph.node_id in exact_target_ids
        writable, writable_reason = _paragraph_writable(paragraph)

        if "paragraph" in target_kinds and paragraph_requested:
            if not only_writable or writable:
                results.append(
                        EditableTarget(
                            target_kind="paragraph",
                            target_id=paragraph.node_id,
                            current_text=paragraph.text or "",
                            text_hash=_text_hash(paragraph.text or ""),
                            page_number=paragraph.page_number,
                            native_anchor=paragraph.native_anchor,
                        writable=writable,
                        writable_reason=writable_reason,
                    )
                )

        if "run" in target_kinds:
            for run in paragraph.runs:
                run_requested = exact_target_ids is None or run.node_id in exact_target_ids
                inherited_request = include_child_runs and (
                    paragraph.node_id in requested_parent_ids
                    or (parent_cell is not None and parent_cell.node_id in requested_parent_ids)
                )
                if run_requested or inherited_request:
                    results.append(
                        EditableTarget(
                            target_kind="run",
                            target_id=run.node_id,
                            parent_paragraph_id=paragraph.node_id,
                            current_text=run.text,
                            text_hash=_text_hash(run.text),
                            page_number=paragraph.page_number,
                            native_anchor=run.native_anchor,
                            writable=True,
                        )
                    )

        if "table" in target_kinds:
            for table in paragraph.tables:
                table_requested = exact_target_ids is None or table.node_id in exact_target_ids
                if table_requested:
                    results.append(
                        EditableTarget(
                            target_kind="table",
                            target_id=table.node_id,
                            parent_paragraph_id=paragraph.node_id,
                            row_count=table.row_count,
                            column_count=table.col_count,
                            current_text=table.markdown,
                            text_hash=_text_hash(table.markdown),
                            page_number=paragraph.page_number,
                            native_anchor=table.native_anchor,
                            writable=True,
                        )
                    )

        if "image" in target_kinds:
            for image in paragraph.images:
                image_requested = exact_target_ids is None or image.node_id in exact_target_ids
                if image_requested:
                    results.append(
                        EditableTarget(
                            target_kind="image",
                            target_id=image.node_id,
                            parent_paragraph_id=paragraph.node_id,
                            current_text=image.alt_text or image.title or "",
                            text_hash=_text_hash(image.alt_text or image.title or ""),
                            page_number=paragraph.page_number,
                            native_anchor=image.native_anchor,
                            writable=True,
                        )
                    )

        if max_targets is not None and len(results) >= max_targets:
            return results[:max_targets]
    return results


def _paragraph_writable(paragraph: ParagraphIR) -> tuple[bool, str | None]:
    if paragraph.tables or paragraph.images:
        return False, "Paragraph contains tables or images."
    return True, None


def _cell_writable(cell) -> tuple[bool, str | None]:
    if not cell.paragraphs:
        return False, "Cell does not contain editable paragraphs."
    if any(_paragraph_has_non_run_content(paragraph) for paragraph in cell.paragraphs):
        return False, "Cell contains nested tables or images."
    if any(not paragraph.runs for paragraph in cell.paragraphs):
        return False, "Cell contains a paragraph without editable runs."
    return True, None


def _paragraph_has_non_run_content(paragraph) -> bool:
    if hasattr(paragraph, "has_non_run_content"):
        return bool(paragraph.has_non_run_content)
    return bool(paragraph.tables or paragraph.images)


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _native_identifier_for_identity(identity: _TargetIdentity) -> str:
    if identity.native_anchor is not None and identity.native_anchor.structural_path:
        return _anchored_node_id(identity.kind, identity.native_anchor.structural_path)
    return identity.node_id


def _to_canonical_text_edit(resolved_edit: _ResolvedTextEdit, *, native: bool) -> TextEdit:
    edit = resolved_edit.edit
    target_id = _native_identifier_for_identity(resolved_edit.identity) if native else resolved_edit.identity.node_id
    return edit.model_copy(update={"target_id": target_id})


def _to_canonical_structural_edit(resolved_operation: _ResolvedStructuralEdit, *, native: bool) -> StructuralEdit:
    operation = resolved_operation.operation
    target_id = _native_identifier_for_identity(resolved_operation.identity) if native else resolved_operation.identity.node_id
    return operation.model_copy(update={"target_id": target_id})


def _to_canonical_style_edit(resolved_edit: _ResolvedStyleEdit, *, native: bool) -> StyleEdit:
    edit = resolved_edit.edit
    target_id = _native_identifier_for_identity(resolved_edit.identity) if native else resolved_edit.identity.node_id
    return edit.model_copy(update={"target_id": target_id})


def _to_render_annotation(resolved_annotation: _ResolvedTextAnnotation) -> _Annotation:
    annotation = resolved_annotation.annotation
    return _Annotation(
        target_id=resolved_annotation.identity.node_id,
        selected_text=annotation.selected_text,
        occurrence_index=annotation.occurrence_index,
        label=annotation.label,
        color=annotation.color,
        note=annotation.note,
    )


def _resolve_text_annotation_span(
    *,
    text: str,
    annotation: TextAnnotation,
) -> tuple[int, int, str, int | None, dict[str, object] | None]:
    if annotation.selected_text is None:
        return 0, len(text), text, None, None

    matches = _find_text_occurrences(text, annotation.selected_text)
    if not matches:
        return 0, 0, "", None, {
            "code": "selected_text_not_found",
            "message": (
                f"Selected text does not occur in target {annotation.target_id}: "
                f"{annotation.selected_text!r}."
            ),
        }

    if annotation.occurrence_index is None:
        if len(matches) > 1:
            return 0, 0, "", None, {
                "code": "selected_text_ambiguous",
                "message": (
                    f"Selected text is ambiguous in target {annotation.target_id}; "
                    "specify occurrence_index."
                ),
                "match_count": len(matches),
            }
        occurrence_index = 0
    elif annotation.occurrence_index >= len(matches):
        return 0, 0, "", None, {
            "code": "occurrence_index_out_of_bounds",
            "message": (
                f"occurrence_index {annotation.occurrence_index} is out of bounds for "
                f"{annotation.target_id}; found {len(matches)} match(es)."
            ),
            "match_count": len(matches),
        }
    else:
        occurrence_index = annotation.occurrence_index

    start = matches[occurrence_index]
    end = start + len(annotation.selected_text)
    return start, end, annotation.selected_text, occurrence_index, None


def _find_text_occurrences(text: str, selected_text: str) -> list[int]:
    matches: list[int] = []
    search_from = 0
    while True:
        index = text.find(selected_text, search_from)
        if index < 0:
            return matches
        matches.append(index)
        search_from = index + 1


__all__ = [
    "AnnotationValidationIssue",
    "AnnotationValidationResult",
    "ApplyDocumentEditsResult",
    "ApplyPdfAnnotationsResult",
    "DocumentContextResult",
    "DocumentEdit",
    "DocumentInput",
    "DocAnnotation",
    "DocumentParagraphContext",
    "DocumentRunContext",
    "EditableTarget",
    "EditValidationIssue",
    "EditValidationResult",
    "ListEditableTargetsResult",
    "ReadDocumentResult",
    "ResolvedTextAnnotation",
    "ReviewHtmlResult",
    "StyleEdit",
    "StructuralEdit",
    "TargetKind",
    "TextAnnotation",
    "TextEdit",
    "apply_document_edits",
    "apply_pdf_annotations",
    "get_document_context",
    "list_editable_targets",
    "read_document",
    "render_review_html",
    "validate_document_edits",
    "validate_pdf_annotations",
    "validate_text_annotations",
]
