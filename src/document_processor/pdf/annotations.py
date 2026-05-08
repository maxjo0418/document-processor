from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Text
from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

from ..api_types import AnnotationValidationIssue, DocAnnotation
from ..models import BoundingBox, DocIR, ImageIR, ParagraphIR, RunIR, TableCellIR, TableIR


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_PDF_TEXT_ANNOTATION_ICONS = {
    "/Comment",
    "/Help",
    "/Insert",
    "/Key",
    "/NewParagraph",
    "/Note",
    "/Paragraph",
}


@dataclass(frozen=True)
class _PdfTargetLocation:
    target_id: str
    page_number: int
    bbox: BoundingBox
    text: str
    run_locations: tuple["_PdfRunLocation", ...] = ()

    @property
    def page_index(self) -> int:
        return self.page_number - 1


@dataclass(frozen=True)
class _ResolvedPdfAnnotation:
    annotation: DocAnnotation
    location: _PdfTargetLocation
    rects: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True)
class _PdfRunLocation:
    run: RunIR
    start: int
    end: int
    bbox: BoundingBox


@dataclass(frozen=True)
class _WritePdfAnnotationsResult:
    output_path: str | None
    output_filename: str | None
    output_bytes: bytes | None
    annotations_applied: int


def validate_pdf_annotations_for_doc(
    doc: DocIR,
    annotations: list[DocAnnotation],
) -> list[AnnotationValidationIssue]:
    _resolved, issues = resolve_pdf_annotations_for_doc(doc, annotations)
    return issues


def resolve_pdf_annotations_for_doc(
    doc: DocIR,
    annotations: list[DocAnnotation],
) -> tuple[list[_ResolvedPdfAnnotation], list[AnnotationValidationIssue]]:
    doc.ensure_node_identity()
    locations = _build_pdf_target_location_index(doc)
    page_count = len(doc.pages)
    resolved: list[_ResolvedPdfAnnotation] = []
    issues: list[AnnotationValidationIssue] = []

    for annotation in annotations:
        if not _HEX_COLOR_RE.match(annotation.color):
            issues.append(
                AnnotationValidationIssue(
                    code="invalid_operation",
                    target_id=annotation.target_id,
                    message="color must be a #RRGGBB value.",
                )
            )
            continue

        location = locations.get(annotation.target_id)
        if location is None:
            issues.append(
                AnnotationValidationIssue(
                    code="target_not_found",
                    target_id=annotation.target_id,
                    message=f"PDF annotation target does not exist or has no PDF location: {annotation.target_id}.",
                )
            )
            continue

        if location.page_number < 1 or (page_count and location.page_number > page_count):
            issues.append(
                AnnotationValidationIssue(
                    code="missing_page_number",
                    target_id=annotation.target_id,
                    message=f"PDF annotation target has invalid page_number: {location.page_number}.",
                )
            )
            continue

        rects, issue = _resolve_annotation_rects(annotation, location)
        if issue is not None:
            issues.append(issue)
            continue

        resolved.append(_ResolvedPdfAnnotation(annotation=annotation, location=location, rects=rects))

    return resolved, issues


def write_pdf_annotations(
    *,
    source_path: str | None,
    source_bytes: bytes | None,
    doc: DocIR,
    annotations: list[DocAnnotation],
    output_path: str | None,
    output_filename: str | None,
) -> _WritePdfAnnotationsResult:
    resolved, issues = resolve_pdf_annotations_for_doc(doc, annotations)
    if issues:
        raise ValueError("Cannot write invalid PDF annotations.")

    if source_path is not None:
        reader = PdfReader(source_path)
    elif source_bytes is not None:
        reader = PdfReader(BytesIO(source_bytes))
    else:
        raise ValueError("PDF annotation write-back requires source_path or source_bytes.")

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    for item in resolved:
        _add_pdf_annotation(writer, item)

    final_output_path = _resolve_output_path(
        source_path=source_path,
        output_path=output_path,
        output_filename=output_filename,
    )
    if final_output_path is not None:
        writer.write(final_output_path)
        return _WritePdfAnnotationsResult(
            output_path=str(final_output_path),
            output_filename=final_output_path.name,
            output_bytes=None,
            annotations_applied=len(resolved),
        )

    buffer = BytesIO()
    writer.write(buffer)
    return _WritePdfAnnotationsResult(
        output_path=None,
        output_filename=output_filename,
        output_bytes=buffer.getvalue(),
        annotations_applied=len(resolved),
    )


def _build_pdf_target_location_index(doc: DocIR) -> dict[str, _PdfTargetLocation]:
    locations: dict[str, _PdfTargetLocation] = {}

    def register(
        node_id: str | None,
        page_number: int | None,
        bbox: BoundingBox | None,
        *,
        text: str = "",
        run_locations: tuple[_PdfRunLocation, ...] = (),
    ) -> None:
        if node_id is None or page_number is None or bbox is None:
            return
        locations[node_id] = _PdfTargetLocation(
            target_id=node_id,
            page_number=page_number,
            bbox=bbox,
            text=text,
            run_locations=run_locations,
        )

    def register_paragraph(paragraph: ParagraphIR, inherited_page_number: int | None = None) -> None:
        page_number = paragraph.page_number or inherited_page_number
        run_text = "".join(run.text for run in paragraph.runs)
        register(
            paragraph.node_id,
            page_number,
            paragraph.bbox,
            text=paragraph.text or run_text,
            run_locations=_run_locations_for_paragraph(paragraph),
        )
        for run in paragraph.runs:
            register_run(run, page_number)
        for image in paragraph.images:
            register_image(image, page_number)
        for table in paragraph.tables:
            register_table(table, page_number)

    def register_run(run: RunIR, page_number: int | None) -> None:
        run_locations = ()
        if run.bbox is not None:
            run_locations = (_PdfRunLocation(run=run, start=0, end=len(run.text), bbox=run.bbox),)
        register(run.node_id, page_number, run.bbox, text=run.text, run_locations=run_locations)

    def register_image(image: ImageIR, page_number: int | None) -> None:
        register(image.node_id, page_number, image.bbox)

    def register_table(table: TableIR, page_number: int | None) -> None:
        register(table.node_id, page_number, table.bbox)
        for cell in table.cells:
            register_cell(cell, page_number)

    def register_cell(cell: TableCellIR, page_number: int | None) -> None:
        register(cell.node_id, page_number, cell.bbox, text=cell.text)
        for paragraph in cell.paragraphs:
            register_paragraph(paragraph, page_number)

    for paragraph in doc.paragraphs:
        register_paragraph(paragraph)

    return locations


def _run_locations_for_paragraph(paragraph: ParagraphIR) -> tuple[_PdfRunLocation, ...]:
    locations: list[_PdfRunLocation] = []
    cursor = 0
    for run in paragraph.runs:
        start = cursor
        end = start + len(run.text)
        if run.bbox is not None:
            locations.append(_PdfRunLocation(run=run, start=start, end=end, bbox=run.bbox))
        cursor = end
    return tuple(locations)


def _resolve_annotation_rects(
    annotation: DocAnnotation,
    location: _PdfTargetLocation,
) -> tuple[tuple[tuple[float, float, float, float], ...], AnnotationValidationIssue | None]:
    if annotation.selected_text is None:
        return (_rect_from_bbox(location.bbox),), None

    matches = _find_text_occurrences(location.text, annotation.selected_text)
    if not matches:
        return (), AnnotationValidationIssue(
            code="selected_text_not_found",
            target_id=annotation.target_id,
            message=f"Selected text does not occur in target {annotation.target_id}: {annotation.selected_text!r}.",
        )

    if annotation.occurrence_index is None:
        if len(matches) > 1:
            return (), AnnotationValidationIssue(
                code="selected_text_ambiguous",
                target_id=annotation.target_id,
                message=f"Selected text is ambiguous in target {annotation.target_id}; specify occurrence_index.",
            )
        occurrence_index = 0
    elif annotation.occurrence_index >= len(matches):
        return (), AnnotationValidationIssue(
            code="occurrence_index_out_of_bounds",
            target_id=annotation.target_id,
            message=(
                f"occurrence_index {annotation.occurrence_index} is out of bounds for "
                f"{annotation.target_id}; found {len(matches)} match(es)."
            ),
        )
    else:
        occurrence_index = annotation.occurrence_index

    start = matches[occurrence_index]
    end = start + len(annotation.selected_text)
    selected_runs = [run for run in location.run_locations if run.start >= start and run.end <= end]
    covers_selected_range = bool(selected_runs) and selected_runs[0].start == start and selected_runs[-1].end == end
    if covers_selected_range:
        cursor = start
        for run in selected_runs:
            if run.start != cursor:
                covers_selected_range = False
                break
            cursor = run.end

    if not covers_selected_range:
        return (), AnnotationValidationIssue(
            code="missing_bbox",
            target_id=annotation.target_id,
            message=(
                "Selected text cannot be mapped to exact PDF run bounding boxes yet; "
                "select complete run text or annotate the full target."
            ),
        )

    return tuple(_rect_from_bbox(run.bbox) for run in selected_runs), None


def _add_pdf_annotation(writer: PdfWriter, item: _ResolvedPdfAnnotation) -> None:
    annotation = item.annotation
    rects = item.rects
    rect = _union_rects(rects)
    page_index = item.location.page_index

    if annotation.selected_text is not None or not (annotation.note or "").strip():
        highlight = Highlight(
            rect=rect,
            quad_points=ArrayObject([FloatObject(value) for value in _quad_points_from_rects(rects)]),
            highlight_color=annotation.color.lstrip("#"),
            printing=True,
        )
        _apply_common_annotation_style(
            highlight,
            annotation=annotation,
            default_title="Study note",
            default_subject="Highlight",
            default_opacity=0.38,
        )
        writer.add_annotation(page_number=page_index, annotation=highlight)
        return

    if annotation.note and annotation.note.strip():
        note_rect = _note_rect_from_target_rect(rect)
        note = Text(
            text=annotation.note,
            rect=note_rect,
            open=False,
            flags=4,
        )
        note[NameObject("/Name")] = NameObject(_metadata_name(annotation, "icon", default="/Key"))
        note[NameObject("/C")] = _color_array(annotation.color)
        _apply_common_annotation_style(
            note,
            annotation=annotation,
            default_title="Study note",
            default_subject="Note",
            default_opacity=0.95,
        )
        writer.add_annotation(page_number=page_index, annotation=note)
        return

    raise ValueError("Unsupported PDF annotation state.")


def _rect_from_bbox(bbox: BoundingBox) -> tuple[float, float, float, float]:
    return (bbox.left_pt, bbox.bottom_pt, bbox.right_pt, bbox.top_pt)


def _quad_points_from_rects(rects: tuple[tuple[float, float, float, float], ...]) -> list[float]:
    values: list[float] = []
    for rect in rects:
        values.extend(_quad_points_from_rect(rect))
    return values


def _quad_points_from_rect(rect: tuple[float, float, float, float]) -> list[float]:
    left, bottom, right, top = rect
    return [left, bottom, right, bottom, left, top, right, top]


def _union_rects(rects: tuple[tuple[float, float, float, float], ...]) -> tuple[float, float, float, float]:
    left = min(rect[0] for rect in rects)
    bottom = min(rect[1] for rect in rects)
    right = max(rect[2] for rect in rects)
    top = max(rect[3] for rect in rects)
    return (left, bottom, right, top)


def _apply_common_annotation_style(
    pdf_annotation: dict,
    *,
    annotation: DocAnnotation,
    default_title: str,
    default_subject: str,
    default_opacity: float | None,
) -> None:
    contents = annotation.note or ""
    if contents:
        pdf_annotation[NameObject("/Contents")] = TextStringObject(contents)

    title = _metadata_str(annotation, "title", default=default_title)
    if title:
        pdf_annotation[NameObject("/T")] = TextStringObject(title)

    subject = _metadata_str(annotation, "subject", default=default_subject)
    if subject:
        pdf_annotation[NameObject("/Subj")] = TextStringObject(subject)

    opacity = _metadata_float(annotation, "opacity", default=default_opacity)
    if opacity is not None:
        pdf_annotation[NameObject("/CA")] = FloatObject(opacity)


def _metadata_str(annotation: DocAnnotation, key: str, *, default: str | None) -> str | None:
    value = annotation.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _metadata_float(annotation: DocAnnotation, key: str, *, default: float | None) -> float | None:
    value = annotation.metadata.get(key)
    if isinstance(value, int | float) and 0 <= value <= 1:
        return float(value)
    return default


def _metadata_name(annotation: DocAnnotation, key: str, *, default: str) -> str:
    value = annotation.metadata.get(key)
    if isinstance(value, str):
        name = value.strip()
        if not name.startswith("/"):
            name = f"/{name}"
        if name in _PDF_TEXT_ANNOTATION_ICONS:
            return name
    return default


def _note_rect_from_target_rect(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    left, _bottom, _right, top = rect
    icon_size = 18.0
    gap = 8.0
    x2 = max(0.0, left - gap)
    x1 = max(0.0, x2 - icon_size)
    y2 = top
    y1 = max(0.0, y2 - icon_size)
    return (x1, y1, x2, y2)


def _color_array(color: str) -> ArrayObject:
    hex_value = color.lstrip("#")
    return ArrayObject(
        [
            FloatObject(int(hex_value[0:2], 16) / 255),
            FloatObject(int(hex_value[2:4], 16) / 255),
            FloatObject(int(hex_value[4:6], 16) / 255),
        ]
    )


def _find_text_occurrences(text: str, selected_text: str) -> list[int]:
    matches: list[int] = []
    search_from = 0
    while True:
        index = text.find(selected_text, search_from)
        if index < 0:
            return matches
        matches.append(index)
        search_from = index + 1


def _resolve_output_path(
    *,
    source_path: str | None,
    output_path: str | None,
    output_filename: str | None,
) -> Path | None:
    if output_path is not None:
        return Path(output_path)
    if source_path is not None:
        source = Path(source_path)
        if output_filename is not None:
            return source.with_name(output_filename)
        return source.with_name(f"{source.stem}_annotated.pdf")
    return None
