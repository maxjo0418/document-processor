from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from document_processor import (  # noqa: E402
    DocIR,
    DocumentInput,
    EditableTarget,
    StyleEdit,
    StructuralEdit,
    TargetKind,
    TextAnnotation,
    TextEdit,
    apply_document_edits,
    configure_logging,
    get_logger,
    list_editable_targets,
    render_review_html,
    validate_document_edits,
    validate_text_annotations,
)


logger = get_logger(__name__)

TARGET_KIND_PRIORITY = {
    "run": 0,
    "paragraph": 1,
    "cell": 2,
}


def validation_issues_to_dicts(validation) -> list[dict[str, Any]]:
    return [issue.model_dump(mode="json") for issue in validation.issues]


def require_validation_ok(label: str, validation) -> None:
    if validation.ok:
        return
    raise RuntimeError(f"{label} failed:\n{json.dumps(validation_issues_to_dicts(validation), indent=2)}")


def require_result_ok(label: str, result) -> None:
    require_validation_ok(label, result.validation)
    if result.ok:
        return
    raise RuntimeError(f"{label} failed without validation issues.")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_html(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def iter_paragraph_texts(paragraphs) -> Any:
    for paragraph in paragraphs:
        yield paragraph.text
        for table in paragraph.tables:
            yield from iter_table_texts(table)


def iter_table_texts(table) -> Any:
    for cell in table.cells:
        yield cell.text
        for paragraph in cell.paragraphs:
            yield paragraph.text
            for nested_table in paragraph.tables:
                yield from iter_table_texts(nested_table)


def collect_doc_texts(doc: DocIR) -> list[str]:
    return [text for text in iter_paragraph_texts(doc.paragraphs) if text]


def target_kinds_for_arg(target_kind: str) -> list[TargetKind]:
    if target_kind == "auto":
        return ["run", "paragraph", "cell"]
    return [target_kind]  # type: ignore[list-item]


def default_comprehensive_output_path(source_path: Path, output_dir: Path) -> Path:
    suffix = source_path.suffix or ".out"
    return output_dir / f"{source_path.stem}_comprehensive_edit{suffix}"


def default_style_output_path(source_path: Path, output_dir: Path, source_doc_type: str) -> Path:
    suffix = ".hwpx" if source_doc_type == "hwp" else source_path.suffix or ".out"
    return output_dir / f"{source_path.stem}_style_edit{suffix}"


def default_bytes_output_filename(source_path: Path, *, marker: str = "manual_edit_bytes") -> str:
    suffix = source_path.suffix or ".bin"
    return f"{source_path.stem}_{marker}{suffix}"


def default_style_bytes_output_filename(source_path: Path, source_doc_type: str) -> str:
    suffix = ".hwpx" if source_doc_type == "hwp" else source_path.suffix or ".bin"
    return f"{source_path.stem}_style_edit_bytes{suffix}"


def output_doc_type_for_name(output_name: str | Path | None, source_doc_type: str) -> str:
    if output_name is not None:
        suffix = Path(output_name).suffix.lower()
        if suffix == ".docx":
            return "docx"
        if suffix == ".hwpx":
            return "hwpx"
        if suffix == ".hwp":
            return "hwp"
    if source_doc_type == "hwp":
        return "hwpx"
    return source_doc_type


def parse_output_doc_ir(source, *, output_name: str | Path | None, source_doc_type: str) -> DocIR:
    doc_type = output_doc_type_for_name(output_name, source_doc_type)
    doc = DocIR.from_file(source, doc_type=doc_type)  # type: ignore[arg-type]
    return doc.ensure_node_identity()


def select_edit_target(
    *,
    document: DocumentInput,
    target_kind: str,
    target_id: str | None,
    contains: str | None,
    target_index: int,
) -> tuple[EditableTarget, list[EditableTarget]]:
    target_result = list_editable_targets(
        document=document,
        target_ids=[target_id] if target_id else [],
        target_kinds=target_kinds_for_arg(target_kind),
        only_writable=True,
        include_child_runs=False,
        max_targets=None,
    )
    if target_result.missing_target_ids:
        raise RuntimeError(f"Missing target ids: {target_result.missing_target_ids}")

    candidates = [
        target
        for target in target_result.targets
        if target.current_text.strip()
        and (contains is None or contains in target.current_text)
    ]
    candidates.sort(key=lambda target: TARGET_KIND_PRIORITY[target.target_kind])
    if not candidates:
        raise RuntimeError(
            "No writable non-empty targets matched. Try --target-kind, --target-id, or --contains."
        )
    if target_index < 0 or target_index >= len(candidates):
        raise RuntimeError(f"--target-index {target_index} is out of range for {len(candidates)} candidate(s).")
    return candidates[target_index], candidates


def resolve_annotation_target(
    *,
    document: DocumentInput,
    edit_target: EditableTarget,
) -> EditableTarget:
    if edit_target.target_kind in {"paragraph", "run"}:
        return edit_target

    child_targets = list_editable_targets(
        document=document,
        target_ids=[edit_target.target_id],
        target_kinds=["run"],
        include_child_runs=True,
        only_writable=True,
        max_targets=None,
    )
    child_runs = [target for target in child_targets.targets if target.current_text.strip()]
    if not child_runs:
        raise RuntimeError(f"Cell target has no non-empty child run to annotate: {edit_target.target_id}")
    return child_runs[0]


def resolve_annotation_selection(
    *,
    annotation_target: EditableTarget,
    selected_text: str | None,
    occurrence_index: int | None,
) -> tuple[str | None, int | None, int]:
    text = annotation_target.current_text
    if selected_text is None:
        stripped = text.strip()
        if not stripped:
            return None, None, 0
        selected_text = stripped.split()[0] if stripped.split() else stripped[: min(len(stripped), 16)]

    occurrences = []
    search_from = 0
    while selected_text:
        index = text.find(selected_text, search_from)
        if index < 0:
            break
        occurrences.append(index)
        search_from = index + 1

    if not occurrences:
        raise RuntimeError(
            f"Annotation selected text {selected_text!r} was not found in target {annotation_target.target_id}."
        )

    resolved_occurrence_index = occurrence_index
    if resolved_occurrence_index is None and len(occurrences) > 1:
        resolved_occurrence_index = 0
    if resolved_occurrence_index is not None and resolved_occurrence_index >= len(occurrences):
        raise RuntimeError(
            f"--annotation-occurrence-index {resolved_occurrence_index} is out of range for "
            f"{len(occurrences)} occurrence(s) of {selected_text!r}."
        )
    return selected_text, resolved_occurrence_index, len(occurrences)


def write_review_html(
    *,
    document: DocumentInput,
    annotations: list[TextAnnotation],
    output_path: Path,
    title: str,
) -> list[dict[str, Any]]:
    annotation_validation = validate_text_annotations(
        document=document,
        annotations=annotations,
    )
    require_validation_ok("validate_text_annotations", annotation_validation)

    review_result = render_review_html(
        document=document,
        annotations=annotations,
        title=title,
    )
    require_result_ok("render_review_html", review_result)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(review_result.html or "", encoding="utf-8")
    return [
        resolved.model_dump(mode="json")
        for resolved in review_result.resolved_annotations
    ]


def run_annotation_suite(
    *,
    document: DocumentInput,
    annotation_target: EditableTarget,
    output_dir: Path,
    source_stem: str,
    selected_text: str | None,
    occurrence_index: int | None,
    label: str,
    color: str,
    note: str,
) -> dict[str, Any]:
    full_review_path = output_dir / f"{source_stem}_review_full.html"
    selected_review_path = output_dir / f"{source_stem}_review_selected.html"
    target_kind = annotation_target.target_kind
    if target_kind not in {"paragraph", "run"}:
        raise RuntimeError(f"Annotation target must be a paragraph or run, got {target_kind!r}.")

    full_annotation = TextAnnotation(
        target_kind=target_kind,  # type: ignore[arg-type]
        target_id=annotation_target.target_id,
        label=f"{label} (full target)",
        color=color,
        note=note,
    )
    full_resolved = write_review_html(
        document=document,
        annotations=[full_annotation],
        output_path=full_review_path,
        title=f"Manual Review Full Target: {Path(document.source_path or document.source_name or 'document').name}",
    )

    resolved_selected_text, resolved_occurrence_index, occurrence_count = resolve_annotation_selection(
        annotation_target=annotation_target,
        selected_text=selected_text,
        occurrence_index=occurrence_index,
    )
    selected_annotation = TextAnnotation(
        target_kind=target_kind,  # type: ignore[arg-type]
        target_id=annotation_target.target_id,
        selected_text=resolved_selected_text,
        occurrence_index=resolved_occurrence_index,
        label=label,
        color=color,
        note=note,
    )
    selected_resolved = write_review_html(
        document=document,
        annotations=[selected_annotation],
        output_path=selected_review_path,
        title=f"Manual Review Selected Text: {Path(document.source_path or document.source_name or 'document').name}",
    )

    return {
        "full_target": {
            "review_html": str(full_review_path),
            "annotation": full_annotation.model_dump(mode="json"),
            "resolved": full_resolved,
        },
        "selected_text": {
            "review_html": str(selected_review_path),
            "annotation": selected_annotation.model_dump(mode="json"),
            "occurrence_count": occurrence_count,
            "resolved": selected_resolved,
        },
    }


def build_text_edit(*, edit_target: EditableTarget, replacement: str) -> TextEdit:
    return TextEdit(
        target_kind=edit_target.target_kind,
        target_id=edit_target.target_id,
        expected_text=edit_target.current_text,
        new_text=replacement,
        reason="Manual edit smoke check.",
    )


def resolve_updated_target(*, document: DocumentInput, target: EditableTarget) -> EditableTarget:
    result = list_editable_targets(
        document=document,
        target_ids=[target.target_id],
        target_kinds=[target.target_kind],
        only_writable=False,
        include_child_runs=False,
        max_targets=None,
    )
    if result.targets:
        return result.targets[0]

    candidates = list_editable_targets(
        document=document,
        target_kinds=[target.target_kind],
        only_writable=False,
        include_child_runs=False,
        max_targets=None,
    ).targets

    structural_path = target.native_anchor.structural_path if target.native_anchor is not None else None
    if structural_path is not None:
        anchored_matches = [
            candidate
            for candidate in candidates
            if candidate.native_anchor is not None and candidate.native_anchor.structural_path == structural_path
        ]
        exact_text_matches = [
            candidate for candidate in anchored_matches if candidate.current_text == target.current_text
        ]
        if len(exact_text_matches) == 1:
            return exact_text_matches[0]
        if len(anchored_matches) == 1:
            return anchored_matches[0]

    text_matches = [candidate for candidate in candidates if candidate.current_text == target.current_text]
    if len(text_matches) == 1:
        return text_matches[0]

    raise RuntimeError(
        "Updated target is missing after edits and could not be re-mapped "
        f"by structural path/text: {target.target_id}"
    )


def requested_post_edit_selected_text(*, annotation_target: EditableTarget, selected_text: str | None) -> str | None:
    if selected_text and selected_text in annotation_target.current_text:
        return selected_text
    return None


def write_doc_html(*, doc: DocIR, output_path: Path, title: str) -> str:
    write_html(output_path, doc.to_html(title=title))
    return str(output_path)


def iter_doc_ir_tables(doc: DocIR) -> Any:
    for paragraph in doc.paragraphs:
        yield from iter_paragraph_tables(paragraph)


def iter_doc_ir_paragraph_nodes(doc: DocIR) -> Any:
    yield from iter_paragraph_node_tree(doc.paragraphs)


def iter_paragraph_node_tree(paragraphs) -> Any:
    for paragraph in paragraphs:
        yield paragraph
        for table in paragraph.tables:
            for cell in table.cells:
                yield from iter_paragraph_node_tree(cell.paragraphs)


def iter_doc_ir_images(doc: DocIR) -> Any:
    for paragraph in iter_doc_ir_paragraph_nodes(doc):
        yield from paragraph.images


def iter_paragraph_tables(paragraph) -> Any:
    for table in paragraph.tables:
        yield table
        for cell in table.cells:
            for cell_paragraph in cell.paragraphs:
                yield from iter_paragraph_tables(cell_paragraph)


def table_shape(table) -> tuple[int, int]:
    row_count = table.row_count or max((cell.row_index for cell in table.cells), default=0)
    col_count = table.col_count or max((cell.col_index for cell in table.cells), default=0)
    return row_count, col_count


def table_is_rectangular(table) -> bool:
    row_count, col_count = table_shape(table)
    if row_count <= 0 or col_count <= 0:
        return False
    coordinates = {(cell.row_index, cell.col_index) for cell in table.cells}
    return len(coordinates) == row_count * col_count and all(
        (row_index, col_index) in coordinates
        for row_index in range(1, row_count + 1)
        for col_index in range(1, col_count + 1)
    )


def find_doc_ir_cell_table(doc: DocIR, cell_id: str) -> tuple[Any, Any] | None:
    for table in iter_doc_ir_tables(doc):
        for cell in table.cells:
            if cell.node_id == cell_id:
                return table, cell
    return None


def find_doc_ir_node(doc: DocIR, target_kind: str, target_id: str) -> Any | None:
    if target_kind == "paragraph":
        for paragraph in iter_doc_ir_paragraph_nodes(doc):
            if paragraph.node_id == target_id:
                return paragraph
    elif target_kind == "run":
        for paragraph in iter_doc_ir_paragraph_nodes(doc):
            for run in paragraph.runs:
                if run.node_id == target_id:
                    return run
    elif target_kind == "cell":
        table_and_cell = find_doc_ir_cell_table(doc, target_id)
        return table_and_cell[1] if table_and_cell is not None else None
    elif target_kind == "table":
        for table in iter_doc_ir_tables(doc):
            if table.node_id == target_id:
                return table
    elif target_kind == "image":
        for image in iter_doc_ir_images(doc):
            if image.node_id == target_id:
                return image
    return None


def select_structural_paragraph_target(
    *,
    document: DocumentInput,
    preferred_target: EditableTarget,
) -> tuple[EditableTarget, list[EditableTarget]]:
    if preferred_target.target_kind == "paragraph":
        return preferred_target, [preferred_target]

    if preferred_target.parent_paragraph_id:
        parent_result = list_editable_targets(
            document=document,
            target_ids=[preferred_target.parent_paragraph_id],
            target_kinds=["paragraph"],
            only_writable=False,
            max_targets=None,
        )
        if parent_result.targets:
            return parent_result.targets[0], parent_result.targets

    result = list_editable_targets(
        document=document,
        target_kinds=["paragraph"],
        only_writable=True,
        max_targets=None,
    )
    candidates = [target for target in result.targets if target.current_text.strip()]
    if not candidates:
        fallback = list_editable_targets(
            document=document,
            target_kinds=["paragraph"],
            only_writable=False,
            max_targets=None,
        )
        candidates = fallback.targets
    if not candidates:
        raise RuntimeError("No paragraph target was available for structural edit tests.")
    return candidates[0], candidates


def select_structural_cell_target(
    *,
    document: DocumentInput,
    source_doc_ir: DocIR,
    target_id: str | None,
    contains: str | None,
    target_index: int,
) -> tuple[EditableTarget | None, Any | None, Any | None, list[EditableTarget], str | None]:
    result = list_editable_targets(
        document=document,
        target_ids=[target_id] if target_id else [],
        target_kinds=["cell"],
        only_writable=True,
        max_targets=None,
    )
    if result.missing_target_ids:
        raise RuntimeError(f"Missing structural cell target ids: {result.missing_target_ids}")

    candidates: list[tuple[EditableTarget, Any, Any]] = []
    for target in result.targets:
        if contains is not None and contains not in target.current_text:
            continue
        table_and_cell = find_doc_ir_cell_table(source_doc_ir, target.target_id)
        if table_and_cell is None:
            continue
        table, cell = table_and_cell
        candidates.append((target, table, cell))

    if not candidates:
        if target_id or contains:
            raise RuntimeError("No structural cell target matched --cell-target-id or --cell-contains.")
        return None, None, None, result.targets, "No table cell target was found."

    if target_index < 0 or target_index >= len(candidates):
        raise RuntimeError(
            f"--cell-target-index {target_index} is out of range for {len(candidates)} structural cell candidate(s)."
        )
    target, table, cell = candidates[target_index]
    return target, table, cell, [candidate[0] for candidate in candidates], None


def build_structural_operations(
    *,
    paragraph_target: EditableTarget,
    cell_target: EditableTarget | None,
    cell_table,
    paragraph_text: str,
    run_text: str,
    cell_text: str,
    table_rows: list[list[str]],
) -> tuple[list[StructuralEdit], list[str], dict[str, Any]]:
    operations = [
        StructuralEdit(
            operation="insert_run",
            target_id=paragraph_target.target_id,
            position="end",
            text=run_text,
            reason="Manual structural run insertion smoke check.",
        ),
        StructuralEdit(
            operation="insert_table",
            target_id=paragraph_target.target_id,
            position="after",
            rows=table_rows,
            reason="Manual structural table insertion smoke check.",
        ),
        StructuralEdit(
            operation="insert_paragraph",
            target_id=paragraph_target.target_id,
            position="after",
            text=paragraph_text,
            reason="Manual structural paragraph insertion smoke check.",
        ),
    ]
    expected_markers = [paragraph_text, run_text, table_rows[-1][-1]]
    table_summary: dict[str, Any] = {
        "cell_operations_added": False,
        "axis_operations_added": False,
        "axis_skip_reason": None,
    }

    if cell_target is None or cell_table is None:
        table_summary["cell_skip_reason"] = "No existing table cell was available."
        return operations, expected_markers, table_summary

    operations.append(
        StructuralEdit(
            operation="set_cell_text",
            target_id=cell_target.target_id,
            expected_text=cell_target.current_text,
            text=cell_text,
            reason="Manual structural cell replacement smoke check.",
        )
    )
    expected_markers.append(cell_text)
    table_summary["cell_operations_added"] = True

    row_count, col_count = table_shape(cell_table)
    table_summary["source_table_shape"] = {
        "row_count": row_count,
        "col_count": col_count,
        "rectangular": table_is_rectangular(cell_table),
    }
    if not table_is_rectangular(cell_table):
        table_summary["axis_skip_reason"] = "Existing table is not rectangular."
        return operations, expected_markers, table_summary
    if row_count <= 0 or col_count <= 0:
        table_summary["axis_skip_reason"] = "Existing table has no measurable shape."
        return operations, expected_markers, table_summary

    row_values = [f"Manual row c{index}" for index in range(1, col_count + 1)]
    operations.append(
        StructuralEdit(
            operation="insert_table_row",
            target_id=cell_target.target_id,
            position="after",
            values=row_values,
            reason="Manual structural table row insertion smoke check.",
        )
    )
    expected_markers.append(row_values[-1])

    column_values = [f"Manual column r{index}" for index in range(1, row_count + 2)]
    operations.append(
        StructuralEdit(
            operation="insert_table_column",
            target_id=cell_target.target_id,
            position="after",
            values=column_values,
            reason="Manual structural table column insertion smoke check.",
        )
    )
    expected_markers.append(column_values[-1])
    table_summary["axis_operations_added"] = True
    return operations, expected_markers, table_summary


def select_first_style_target(document: DocumentInput, target_kind: TargetKind) -> EditableTarget | None:
    result = list_editable_targets(
        document=document,
        target_kinds=[target_kind],
        only_writable=True,
        include_child_runs=False,
        max_targets=None,
    )
    targets = [target for target in result.targets if target.target_id]
    if target_kind == "run":
        targets = [target for target in targets if target.current_text.strip()]
    return targets[0] if targets else None


def build_style_edits(
    *,
    document: DocumentInput,
    source_doc_type: str,
) -> tuple[list[StyleEdit], list[dict[str, Any]], list[str]]:
    edits: list[StyleEdit] = []
    selected_targets: list[dict[str, Any]] = []
    skipped: list[str] = []

    def add_target(target: EditableTarget | None, edit_kwargs: dict[str, Any]) -> None:
        if target is None:
            return
        edit = StyleEdit(
            target_kind=target.target_kind,  # type: ignore[arg-type]
            target_id=target.target_id,
            **edit_kwargs,
        )
        edits.append(edit)
        selected_targets.append(target.model_dump(mode="json"))

    add_target(
        select_first_style_target(document, "run"),
        {
            "bold": True,
            "color": "#445566",
            "font_size_pt": 32,
            "reason": "Manual run style smoke check.",
        },
    )
    add_target(
        select_first_style_target(document, "paragraph"),
        {
            "paragraph_align": "right",
            "left_indent_pt": 0,
            "reason": "Manual paragraph style smoke check.",
        },
    )

    cell_target = select_first_style_target(document, "cell")
    add_target(
        cell_target,
        {
            "background": "#FFF2CC",
            "vertical_align": "bottom",
            "horizontal_align": "right",
            "width_pt": 50,
            "height_pt": 100,
            "padding_left_pt": 6,
            "padding_right_pt": 6,
            "border_top": "1pt single #445566",
            "border_right": "1pt single #445566",
            "border_bottom": "1pt single #445566",
            "border_left": "1pt single #445566",
            "reason": "Manual cell style smoke check.",
        },
    )

    image_placement = {
        "width_pt": 144,
        "reason": "Manual image size style smoke check.",
    }
    add_target(select_first_style_target(document, "image"), image_placement)

    if not any(edit.target_kind == "cell" for edit in edits):
        skipped.append("No table cell target was available for cell style testing.")
    if not any(edit.target_kind == "image" for edit in edits):
        skipped.append("No image target was available for floating image style testing.")

    return edits, selected_targets, skipped


def summarize_style_target_state(doc: DocIR, edit: StyleEdit) -> dict[str, Any]:
    node = find_doc_ir_node(doc, edit.target_kind, edit.target_id)
    if node is None:
        return {"target_kind": edit.target_kind, "target_id": edit.target_id, "missing": True}
    if edit.target_kind == "run":
        style = node.run_style.model_dump(mode="json", exclude_none=True) if node.run_style is not None else None
        return {"target_kind": edit.target_kind, "target_id": edit.target_id, "text": node.text, "style": style}
    if edit.target_kind == "paragraph":
        style = node.para_style.model_dump(mode="json", exclude_none=True) if node.para_style is not None else None
        return {"target_kind": edit.target_kind, "target_id": edit.target_id, "text": node.text, "style": style}
    if edit.target_kind == "cell":
        style = node.cell_style.model_dump(mode="json", exclude_none=True) if node.cell_style is not None else None
        return {"target_kind": edit.target_kind, "target_id": edit.target_id, "text": node.text, "style": style}
    if edit.target_kind == "table":
        style = node.table_style.model_dump(mode="json", exclude_none=True) if node.table_style is not None else None
        return {"target_kind": edit.target_kind, "target_id": edit.target_id, "style": style}
    if edit.target_kind == "image":
        placement = node.placement.model_dump(mode="json", exclude_none=True) if node.placement is not None else None
        return {
            "target_kind": edit.target_kind,
            "target_id": edit.target_id,
            "display_width_pt": node.display_width_pt,
            "display_height_pt": node.display_height_pt,
            "placement": placement,
        }
    return {"target_kind": edit.target_kind, "target_id": edit.target_id}


def operation_summary(result) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "edits_applied": result.edits_applied,
        "operations_applied": result.operations_applied,
        "styles_applied": result.styles_applied,
        "modified_target_ids": result.modified_target_ids,
        "created_target_ids": result.created_target_ids,
        "removed_target_ids": result.removed_target_ids,
        "warnings": result.warnings,
    }


def assert_doc_contains_markers(doc: DocIR, markers: list[str], *, label: str) -> list[str]:
    texts = collect_doc_texts(doc)
    missing = [marker for marker in markers if marker and not any(marker in text for text in texts)]
    if missing:
        raise RuntimeError(f"{label} did not contain expected marker(s): {missing}")
    return texts


def run_comprehensive_edit_suite(
    *,
    source_path: Path,
    source_doc_type: str,
    source_doc_ir: DocIR,
    document: DocumentInput,
    output_dir: Path,
    edit_target: EditableTarget,
    annotation_selected_text: str | None,
    annotation_occurrence_index: int | None,
    annotation_label: str,
    annotation_color: str,
    annotation_note: str,
    replacement: str | None,
    append_text: str,
    cell_target_id: str | None,
    cell_contains: str | None,
    cell_target_index: int,
    structural_paragraph_text: str,
    structural_run_text: str,
    structural_cell_text: str,
) -> dict[str, Any]:
    paragraph_target, paragraph_candidates = select_structural_paragraph_target(
        document=document,
        preferred_target=edit_target,
    )
    cell_target, cell_table, _cell, cell_candidates, cell_skip_reason = select_structural_cell_target(
        document=document,
        source_doc_ir=source_doc_ir,
        target_id=cell_target_id,
        contains=cell_contains,
        target_index=cell_target_index,
    )
    if cell_target is not None and cell_target.target_id == edit_target.target_id:
        cell_target = None
        cell_table = None
        cell_skip_reason = "Skipped structural cell operations because the text edit target is the same cell."

    new_text = replacement if replacement is not None else f"{edit_target.current_text}{append_text}"
    text_edit = build_text_edit(edit_target=edit_target, replacement=new_text)
    structural_operations, structural_markers, table_summary = build_structural_operations(
        paragraph_target=paragraph_target,
        cell_target=cell_target,
        cell_table=cell_table,
        paragraph_text=structural_paragraph_text,
        run_text=structural_run_text,
        cell_text=structural_cell_text,
        table_rows=[
            ["Comprehensive table A1", "Comprehensive table B1"],
            ["Comprehensive table A2", "Comprehensive table B2"],
        ],
    )
    operations = [text_edit, *structural_operations]
    expected_markers = [new_text, *structural_markers]

    validation = validate_document_edits(document=document, edits=operations)
    require_validation_ok("validate_document_edits comprehensive", validation)

    suites: dict[str, Any] = {}
    output_specs = {
        "native_file": {
            "output_path": default_comprehensive_output_path(source_path, output_dir),
            "output_filename": None,
            "source_document": document,
        },
        "bytes": {
            "output_path": None,
            "output_filename": default_bytes_output_filename(source_path, marker="comprehensive_edit_bytes"),
            "source_document": DocumentInput(
                source_bytes=source_path.read_bytes(),
                source_doc_type=source_doc_type,  # type: ignore[arg-type]
                source_name=source_path.name,
            ),
        },
    }

    for suite_name, spec in output_specs.items():
        result = apply_document_edits(
            document=spec["source_document"],
            edits=operations,
            output_path=None if spec["output_path"] is None else str(spec["output_path"]),
            output_filename=spec["output_filename"],
            return_doc_ir=True,
        )
        require_result_ok(f"apply_document_edits comprehensive {suite_name}", result)
        if result.updated_doc_ir is None:
            raise RuntimeError(f"Comprehensive {suite_name} edit did not return updated_doc_ir.")

        updated_doc = result.updated_doc_ir
        assert_doc_contains_markers(updated_doc, expected_markers, label=f"Comprehensive {suite_name} preview")

        if suite_name == "bytes":
            if result.output_bytes is None:
                raise RuntimeError("Comprehensive bytes edit did not return output_bytes.")
            saved_output_path = output_dir / (result.output_filename or spec["output_filename"])
            saved_output_path.write_bytes(result.output_bytes)
        else:
            saved_output_path = Path(result.output_path or spec["output_path"])
            if not saved_output_path.exists():
                raise RuntimeError(f"Comprehensive edited output file was not created: {saved_output_path}")

        reparsed = parse_output_doc_ir(
            result.output_bytes if suite_name == "bytes" else saved_output_path,
            output_name=result.output_filename or saved_output_path,
            source_doc_type=source_doc_type,
        )
        reparsed_texts = assert_doc_contains_markers(
            reparsed,
            expected_markers,
            label=f"Comprehensive {suite_name} output",
        )

        html_output = write_doc_html(
            doc=reparsed,
            output_path=output_dir
            / f"{source_path.stem}_comprehensive_edit{'_bytes' if suite_name == 'bytes' else ''}.html",
            title=f"Comprehensive Edit ({suite_name}): {saved_output_path.name}",
        )

        preview_document = DocumentInput(
            doc_ir=updated_doc,
            source_name=saved_output_path.name,
            source_doc_type=updated_doc.source_doc_type or source_doc_type,  # type: ignore[arg-type]
        )
        preview_updated_edit_target = resolve_updated_target(document=preview_document, target=edit_target)
        preview_updated_annotation_target = resolve_annotation_target(
            document=preview_document,
            edit_target=preview_updated_edit_target,
        )

        reparsed_document = DocumentInput(
            doc_ir=reparsed,
            source_path=str(saved_output_path),
            source_name=saved_output_path.name,
            source_doc_type=reparsed.source_doc_type or source_doc_type,  # type: ignore[arg-type]
        )
        updated_edit_target = resolve_updated_target(document=reparsed_document, target=preview_updated_edit_target)
        updated_annotation_target = resolve_updated_target(
            document=reparsed_document,
            target=preview_updated_annotation_target,
        )
        annotation_suite = run_annotation_suite(
            document=reparsed_document,
            annotation_target=updated_annotation_target,
            output_dir=output_dir,
            source_stem=f"{source_path.stem}_comprehensive_edit{'_bytes' if suite_name == 'bytes' else ''}",
            selected_text=requested_post_edit_selected_text(
                annotation_target=updated_annotation_target,
                selected_text=annotation_selected_text,
            ),
            occurrence_index=annotation_occurrence_index,
            label=f"{annotation_label} ({suite_name} comprehensive)",
            color=annotation_color,
            note=f"{annotation_note} Applied after the mixed edit batch.",
        )

        suites[suite_name] = {
            **operation_summary(result),
            "output_path": str(saved_output_path),
            "output_filename": result.output_filename,
            "output_bytes": None if result.output_bytes is None else len(result.output_bytes),
            "expected_markers": expected_markers,
            "html_output": html_output,
            "reparsed_texts": reparsed_texts[:50],
            "updated_edit_target": updated_edit_target.model_dump(mode="json"),
            "updated_annotation_target": updated_annotation_target.model_dump(mode="json"),
            "annotation_suite": annotation_suite,
        }

    return {
        "operations": [operation.model_dump(mode="json") for operation in operations],
        "validation": validation.model_dump(mode="json"),
        "paragraph_target": paragraph_target.model_dump(mode="json"),
        "paragraph_candidate_count": len(paragraph_candidates),
        "cell_target": cell_target.model_dump(mode="json") if cell_target else None,
        "cell_candidate_count": len(cell_candidates),
        "cell_skip_reason": cell_skip_reason,
        "table_summary": table_summary,
        "native_file": suites["native_file"],
        "bytes": suites["bytes"],
    }


def run_style_edit_suite(
    *,
    source_path: Path,
    source_doc_type: str,
    document: DocumentInput,
    output_dir: Path,
) -> dict[str, Any]:
    style_edits, selected_targets, skipped = build_style_edits(
        document=document,
        source_doc_type=source_doc_type,
    )
    if not style_edits:
        return {
            "skipped": True,
            "reason": "No compatible style targets were found for this source document.",
            "skipped_capabilities": skipped,
            "operations": [],
            "selected_targets": [],
        }

    validation = validate_document_edits(document=document, edits=style_edits)
    require_validation_ok("validate_document_edits style", validation)

    suites: dict[str, Any] = {}
    output_specs = {
        "native_file": {
            "output_path": default_style_output_path(source_path, output_dir, source_doc_type),
            "output_filename": None,
            "source_document": document,
        },
        "bytes": {
            "output_path": None,
            "output_filename": default_style_bytes_output_filename(source_path, source_doc_type),
            "source_document": DocumentInput(
                source_bytes=source_path.read_bytes(),
                source_doc_type=source_doc_type,  # type: ignore[arg-type]
                source_name=source_path.name,
            ),
        },
    }

    for suite_name, spec in output_specs.items():
        result = apply_document_edits(
            document=spec["source_document"],
            edits=style_edits,
            output_path=None if spec["output_path"] is None else str(spec["output_path"]),
            output_filename=spec["output_filename"],
            return_doc_ir=True,
        )
        require_result_ok(f"apply_document_edits style {suite_name}", result)
        if result.styles_applied != len(style_edits):
            raise RuntimeError(
                f"Style {suite_name} applied {result.styles_applied} style edit(s), expected {len(style_edits)}."
            )
        if result.updated_doc_ir is None:
            raise RuntimeError(f"Style {suite_name} edit did not return updated_doc_ir.")

        if suite_name == "bytes":
            if result.output_bytes is None:
                raise RuntimeError("Style bytes edit did not return output_bytes.")
            saved_output_path = output_dir / (result.output_filename or spec["output_filename"])
            saved_output_path.write_bytes(result.output_bytes)
        else:
            saved_output_path = Path(result.output_path or spec["output_path"])
            if not saved_output_path.exists():
                raise RuntimeError(f"Style edited output file was not created: {saved_output_path}")

        reparsed = parse_output_doc_ir(
            result.output_bytes if suite_name == "bytes" else saved_output_path,
            output_name=result.output_filename or saved_output_path,
            source_doc_type=source_doc_type,
        )
        html_output = write_doc_html(
            doc=reparsed,
            output_path=output_dir / f"{source_path.stem}_style_edit{'_bytes' if suite_name == 'bytes' else ''}.html",
            title=f"Style Edit ({suite_name}): {saved_output_path.name}",
        )

        suites[suite_name] = {
            **operation_summary(result),
            "output_path": str(saved_output_path),
            "output_filename": result.output_filename,
            "output_bytes": None if result.output_bytes is None else len(result.output_bytes),
            "html_output": html_output,
            "preview_style_targets": [
                summarize_style_target_state(result.updated_doc_ir, edit)
                for edit in style_edits
            ],
            "reparsed_style_targets": [
                summarize_style_target_state(reparsed, edit)
                for edit in style_edits
            ],
        }

    return {
        "skipped": False,
        "operations": [edit.model_dump(mode="json") for edit in style_edits],
        "validation": validation.model_dump(mode="json"),
        "selected_targets": selected_targets,
        "skipped_capabilities": skipped,
        "native_file": suites["native_file"],
        "bytes": suites["bytes"],
    }


def run_manual_file_flow(
    *,
    source_path: Path,
    source_doc_type: str,
    output_dir: Path,
    target_kind: str,
    target_id: str | None,
    contains: str | None,
    target_index: int,
    replacement: str | None,
    append_text: str,
    cell_target_id: str | None,
    cell_contains: str | None,
    cell_target_index: int,
    annotation_selected_text: str | None,
    annotation_occurrence_index: int | None,
    annotation_label: str,
    annotation_color: str,
    annotation_note: str,
    structural_paragraph_text: str,
    structural_run_text: str,
    structural_cell_text: str,
) -> dict[str, Any]:
    if not source_path.exists():
        raise FileNotFoundError(f"Source file does not exist: {source_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"{source_path.stem}_summary.json"
    source_doc_ir = DocIR.from_file(source_path, doc_type=source_doc_type)  # type: ignore[arg-type]
    source_doc_ir.ensure_node_identity()
    resolved_source_doc_type = source_doc_ir.source_doc_type or source_doc_type
    cached_document = DocumentInput(
        doc_ir=source_doc_ir,
        source_path=str(source_path),
        source_doc_type=resolved_source_doc_type,  # type: ignore[arg-type]
    )
    edit_target, candidates = select_edit_target(
        document=cached_document,
        target_kind=target_kind,
        target_id=target_id,
        contains=contains,
        target_index=target_index,
    )
    initial_annotation_target = resolve_annotation_target(
        document=cached_document,
        edit_target=edit_target,
    )
    comprehensive_edit_suite = run_comprehensive_edit_suite(
        source_path=source_path,
        source_doc_type=source_doc_type,
        source_doc_ir=source_doc_ir,
        document=cached_document,
        output_dir=output_dir,
        edit_target=edit_target,
        annotation_selected_text=annotation_selected_text,
        annotation_occurrence_index=annotation_occurrence_index,
        annotation_label=annotation_label,
        annotation_color=annotation_color,
        annotation_note=annotation_note,
        replacement=replacement,
        append_text=append_text,
        cell_target_id=cell_target_id,
        cell_contains=cell_contains,
        cell_target_index=cell_target_index,
        structural_paragraph_text=structural_paragraph_text,
        structural_run_text=structural_run_text,
        structural_cell_text=structural_cell_text,
    )
    style_edit_suite = run_style_edit_suite(
        source_path=source_path,
        source_doc_type=resolved_source_doc_type,
        document=cached_document,
        output_dir=output_dir,
    )

    summary = {
        "ok": True,
        "source": {
            "path": str(source_path),
            "requested_doc_type": source_doc_type,
        },
        "paths": {
            "comprehensive_output": comprehensive_edit_suite["native_file"]["output_path"],
            "comprehensive_bytes_output": comprehensive_edit_suite["bytes"]["output_path"],
            "comprehensive_html": comprehensive_edit_suite["native_file"]["html_output"],
            "comprehensive_bytes_html": comprehensive_edit_suite["bytes"]["html_output"],
            "style_output": None if style_edit_suite["skipped"] else style_edit_suite["native_file"]["output_path"],
            "style_bytes_output": None if style_edit_suite["skipped"] else style_edit_suite["bytes"]["output_path"],
            "style_html": None if style_edit_suite["skipped"] else style_edit_suite["native_file"]["html_output"],
            "style_bytes_html": None if style_edit_suite["skipped"] else style_edit_suite["bytes"]["html_output"],
            "comprehensive_review_html_full": comprehensive_edit_suite["native_file"]["annotation_suite"]["full_target"][
                "review_html"
            ],
            "comprehensive_review_html_selected": comprehensive_edit_suite["native_file"]["annotation_suite"][
                "selected_text"
            ]["review_html"],
            "comprehensive_bytes_review_html_full": comprehensive_edit_suite["bytes"]["annotation_suite"][
                "full_target"
            ]["review_html"],
            "comprehensive_bytes_review_html_selected": comprehensive_edit_suite["bytes"]["annotation_suite"][
                "selected_text"
            ]["review_html"],
            "summary_json": str(summary_path),
        },
        "target_selection": {
            "target_kind_arg": target_kind,
            "target_id_arg": target_id,
            "contains_arg": contains,
            "target_index_arg": target_index,
            "candidate_count": len(candidates),
            "first_candidates": [
                {
                    "target_kind": target.target_kind,
                    "target_id": target.target_id,
                    "current_text": target.current_text,
                    "native_anchor": target.native_anchor.model_dump(mode="json") if target.native_anchor else None,
                }
                for target in candidates[:20]
            ],
        },
        "selected_targets": {
            "edit": edit_target.model_dump(mode="json"),
            "initial_annotation": initial_annotation_target.model_dump(mode="json"),
        },
        "comprehensive_edit_suite": comprehensive_edit_suite,
        "style_edit_suite": style_edit_suite,
    }
    write_json(summary_path, summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the comprehensive mixed edit, style edit, and annotation manual flow "
            "on one existing DOCX/HWPX/HWP file."
        ),
    )
    parser.add_argument(
        "source_path",
        type=Path,
        help="Existing .docx, .hwpx, or .hwp file to process.",
    )
    parser.add_argument(
        "--source-doc-type",
        choices=["auto", "docx", "hwpx", "hwp"],
        default="auto",
        help="Override source type inference.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "tests" / "results" / "manual_test_one",
        help="Directory for edited output, review HTML, and summary JSON.",
    )
    parser.add_argument(
        "--target-kind",
        choices=["auto", "run", "paragraph", "cell"],
        default="auto",
        help="Editable target kind to select. auto prefers runs, then paragraphs, then cells.",
    )
    parser.add_argument(
        "--target-id",
        default=None,
        help="Exact stable target id to edit.",
    )
    parser.add_argument(
        "--contains",
        default=None,
        help="Select the first writable target whose current text contains this substring.",
    )
    parser.add_argument(
        "--target-index",
        type=int,
        default=0,
        help="Zero-based index among matched writable targets.",
    )
    parser.add_argument(
        "--replacement",
        default=None,
        help="Replacement text for the selected target. Defaults to appending --append-text.",
    )
    parser.add_argument(
        "--append-text",
        default=" [manual edit]",
        help="Text appended when --replacement is omitted.",
    )
    parser.add_argument(
        "--cell-target-id",
        default=None,
        help="Exact stable cell target id for the comprehensive set_cell_text/row/column operations.",
    )
    parser.add_argument(
        "--cell-contains",
        default=None,
        help="Select the comprehensive cell target by substring.",
    )
    parser.add_argument(
        "--cell-target-index",
        type=int,
        default=0,
        help="Zero-based index among matched writable cell targets.",
    )
    parser.add_argument(
        "--annotation-selected-text",
        default=None,
        help="Substring to annotate in the selected annotation suite. Defaults to the first token in the annotation target.",
    )
    parser.add_argument(
        "--annotation-occurrence-index",
        type=int,
        default=None,
        help="Zero-based occurrence index for --annotation-selected-text when it appears multiple times.",
    )
    parser.add_argument(
        "--annotation-label",
        default="Manual note",
        help="Label shown in the generated review HTML.",
    )
    parser.add_argument(
        "--annotation-color",
        default="#FFE08A",
        help="Highlight color for the generated review HTML.",
    )
    parser.add_argument(
        "--annotation-note",
        default="Manual annotation before applying the edit.",
        help="Annotation note shown in the generated review HTML.",
    )
    parser.add_argument(
        "--structural-paragraph-text",
        default="Manual structural paragraph",
        help="Text inserted by the comprehensive paragraph operation.",
    )
    parser.add_argument(
        "--structural-run-text",
        default=" [manual structural run]",
        help="Text inserted by the comprehensive run operation.",
    )
    parser.add_argument(
        "--structural-cell-text",
        default="Manual structural cell",
        help="Text used by the comprehensive set_cell_text operation when a table cell exists.",
    )
    return parser.parse_args()


def main() -> int:
    configure_logging(level="INFO")
    args = parse_args()
    summary = run_manual_file_flow(
        source_path=args.source_path,
        source_doc_type=args.source_doc_type,
        output_dir=args.output_dir,
        target_kind=args.target_kind,
        target_id=args.target_id,
        contains=args.contains,
        target_index=args.target_index,
        replacement=args.replacement,
        append_text=args.append_text,
        cell_target_id=args.cell_target_id,
        cell_contains=args.cell_contains,
        cell_target_index=args.cell_target_index,
        annotation_selected_text=args.annotation_selected_text,
        annotation_occurrence_index=args.annotation_occurrence_index,
        annotation_label=args.annotation_label,
        annotation_color=args.annotation_color,
        annotation_note=args.annotation_note,
        structural_paragraph_text=args.structural_paragraph_text,
        structural_run_text=args.structural_run_text,
        structural_cell_text=args.structural_cell_text,
    )

    logger.info("Manual file flow completed.")
    logger.info("Source file is left unchanged; inspect the generated output paths below.")
    logger.info("Source: %s", summary["source"]["path"])
    logger.info("Comprehensive edited output: %s", summary["paths"]["comprehensive_output"])
    logger.info("Comprehensive bytes output: %s", summary["paths"]["comprehensive_bytes_output"])
    logger.info("Comprehensive HTML: %s", summary["paths"]["comprehensive_html"])
    logger.info("Comprehensive bytes HTML: %s", summary["paths"]["comprehensive_bytes_html"])
    if summary["style_edit_suite"]["skipped"]:
        logger.info("Style edit suite skipped: %s", summary["style_edit_suite"]["reason"])
    else:
        logger.info("Style edited output: %s", summary["paths"]["style_output"])
        logger.info("Style bytes output: %s", summary["paths"]["style_bytes_output"])
        logger.info("Style HTML: %s", summary["paths"]["style_html"])
        logger.info("Style bytes HTML: %s", summary["paths"]["style_bytes_html"])
    logger.info("Comprehensive review HTML full: %s", summary["paths"]["comprehensive_review_html_full"])
    logger.info("Comprehensive review HTML selected: %s", summary["paths"]["comprehensive_review_html_selected"])
    logger.info("Comprehensive bytes review HTML full: %s", summary["paths"]["comprehensive_bytes_review_html_full"])
    logger.info(
        "Comprehensive bytes review HTML selected: %s",
        summary["paths"]["comprehensive_bytes_review_html_selected"],
    )
    logger.info("Summary JSON: %s", summary["paths"]["summary_json"])
    logger.info(
        "Initial edit target: %s %s",
        summary["selected_targets"]["edit"]["target_kind"],
        summary["selected_targets"]["edit"]["target_id"],
    )
    logger.info("Initial annotation target: %s", summary["selected_targets"]["initial_annotation"]["target_id"])
    logger.info(
        "Comprehensive modified target ids: %s",
        ", ".join(summary["comprehensive_edit_suite"]["native_file"]["modified_target_ids"]),
    )
    if not summary["style_edit_suite"]["skipped"]:
        logger.info(
            "Style modified target ids: %s",
            ", ".join(summary["style_edit_suite"]["native_file"]["modified_target_ids"]),
        )
        logger.info(
            "Style edits applied: %s",
            summary["style_edit_suite"]["native_file"]["styles_applied"],
        )
    logger.info(
        "Suite: mixed text+structural comprehensive edit path, style edit path, native/bytes exports, "
        "HTML exports, and post-edit annotation exports"
    )
    warnings = summary["comprehensive_edit_suite"]["native_file"]["warnings"]
    if warnings:
        logger.warning("Warnings: %s", "; ".join(warnings))
    if not summary["style_edit_suite"]["skipped"]:
        style_warnings = summary["style_edit_suite"]["native_file"]["warnings"]
        if style_warnings:
            logger.warning("Style warnings: %s", "; ".join(style_warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
