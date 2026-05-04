"""PDF parsing pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import DocIR, PageInfo
from .config import PdfParseConfig
from .odl import build_doc_ir_from_odl_result, preprocess_dotted_rule_splits, run_odl_json
from .parsing import PageClass, PdfProfile, decide_page, probe_pdf
from .preview.context import build_pdf_preview_context, collect_pdfium_visual_block_candidates
from .preview.models import PdfPreviewContext


def parse_pdf_to_doc_ir(
    path: str | Path,
    *,
    config: PdfParseConfig | dict[str, Any] | None = None,
    doc_id: str | None = None,
    doc_cls: type[DocIR] | None = None,
    **doc_kwargs: Any,
) -> DocIR:
    doc_ir, preview_context = _parse_pdf_to_doc_ir_with_preview(
        path,
        config=config,
        doc_id=doc_id,
        doc_cls=doc_cls,
        **doc_kwargs,
    )
    from .preview.normalize import enrich_pdf_doc_ir

    return enrich_pdf_doc_ir(doc_ir, preview_context=preview_context)


def _parse_pdf_to_doc_ir_with_preview(
    path: str | Path,
    *,
    config: PdfParseConfig | dict[str, Any] | None = None,
    doc_id: str | None = None,
    doc_cls: type[DocIR] | None = None,
    **doc_kwargs: Any,
) -> tuple[DocIR, PdfPreviewContext]:
    resolved_config = (
        config
        if isinstance(config, PdfParseConfig)
        else PdfParseConfig.model_validate(config or {})
    )
    source_path = Path(path)
    profile = probe_pdf(source_path)
    if profile is None:
        raise RuntimeError("PDF probe failed before ODL parsing.")

    selected_pages = _selected_pages(resolved_config.pages, page_count=profile.page_count)
    page_decisions = [decide_page(page_profile) for page_profile in profile.page_profiles]
    structured_pages = [
        decision.page_number
        for decision in page_decisions
        if decision.page_class == PageClass.STRUCTURED
        and (selected_pages is None or decision.page_number in selected_pages)
    ]

    resolved_doc_cls = doc_cls or DocIR
    preview_context = PdfPreviewContext()
    if structured_pages:
        raw_document = run_odl_json(
            source_path,
            resolved_config.to_odl_config(pages=structured_pages, for_doc_ir=True),
        )
        preprocess_dotted_rule_splits(
            raw_document,
            pdf_path=source_path,
            page_numbers=structured_pages,
        )
        # The dotted-rule pass mutates raw table structure. Build preview context
        # after it so table grid hints match the final DocIR TableIR shape.
        preview_context = build_pdf_preview_context(raw_document)
        preview_context.visual_block_candidates.extend(
            collect_pdfium_visual_block_candidates(
                pdf_path=source_path,
                page_numbers=structured_pages,
            )
        )
        doc_ir = build_doc_ir_from_odl_result(
            raw_document,
            source_path=str(source_path),
            doc_id=doc_id,
            doc_cls=resolved_doc_cls,
            **doc_kwargs,
        )
    else:
        resolved_doc_id = doc_id or source_path.stem
        doc_ir = resolved_doc_cls(
            doc_id=resolved_doc_id,
            source_path=str(source_path),
            source_doc_type="pdf",
            pages=[],
            paragraphs=[],
            assets={},
            **doc_kwargs,
        )

    _apply_probe_page_sizes(doc_ir, profile=profile, selected_pages=selected_pages)
    return doc_ir, preview_context


def _selected_pages(pages: str | list[int] | None, *, page_count: int) -> set[int] | None:
    if pages is None:
        return None
    if isinstance(pages, list):
        selected = set(pages)
    else:
        selected = set()
        for part in pages.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start = int(start_text.strip())
                end = int(end_text.strip())
                if start > end:
                    raise ValueError(f"Invalid page range: {part}")
                selected.update(range(start, end + 1))
            else:
                selected.add(int(part))
    if any(page < 1 or page > page_count for page in selected):
        raise ValueError(f"pages must be within 1-{page_count}")
    return selected


def _apply_probe_page_sizes(doc_ir: DocIR, *, profile: PdfProfile, selected_pages: set[int] | None = None) -> None:
    page_map = {page.page_number: page for page in doc_ir.pages}
    for page_profile in profile.page_profiles:
        if selected_pages is not None and page_profile.page_number not in selected_pages:
            continue
        page = page_map.get(page_profile.page_number)
        if page is None:
            page = PageInfo(page_number=page_profile.page_number)
            doc_ir.pages.append(page)
            page_map[page.page_number] = page
        if page.width_pt is None:
            page.width_pt = page_profile.page_width_pt
        if page.height_pt is None:
            page.height_pt = page_profile.page_height_pt
    doc_ir.pages.sort(key=lambda page: page.page_number)


__all__ = ["parse_pdf_to_doc_ir"]
