"""Scan-like vs structured PDF page triage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .probe import (
    FULL_OCR_CONTENT_IMAGE_RATIO_THRESHOLD,
    FULL_OCR_IMAGE_RATIO_THRESHOLD,
    GIBBERISH_MIN_CHARS,
    GIBBERISH_NORMAL_TEXT_MAX_RATIO,
    PageProfile,
)


class PageClass(str, Enum):
    SCAN_LIKE = "scan_like"
    STRUCTURED = "structured"


@dataclass(frozen=True)
class PageDecision:
    page_number: int
    page_class: PageClass


def decide_page(page_profile: PageProfile) -> PageDecision:
    if page_profile.image_area_ratio >= FULL_OCR_IMAGE_RATIO_THRESHOLD:
        return PageDecision(
            page_number=page_profile.page_number,
            page_class=PageClass.SCAN_LIKE,
        )
    if page_profile.image_area_in_content_ratio >= FULL_OCR_CONTENT_IMAGE_RATIO_THRESHOLD:
        return PageDecision(
            page_number=page_profile.page_number,
            page_class=PageClass.SCAN_LIKE,
        )
    if (
        page_profile.char_count >= GIBBERISH_MIN_CHARS
        and page_profile.normal_text_ratio <= GIBBERISH_NORMAL_TEXT_MAX_RATIO
    ):
        return PageDecision(
            page_number=page_profile.page_number,
            page_class=PageClass.SCAN_LIKE,
        )
    return PageDecision(
        page_number=page_profile.page_number,
        page_class=PageClass.STRUCTURED,
    )


def summarize_page_decisions(page_decisions: list[PageDecision]) -> dict[str, int]:
    summary = {page_class.value: 0 for page_class in PageClass}
    for decision in page_decisions:
        summary[decision.page_class.value] += 1
    return summary


__all__ = [
    "PageClass",
    "PageDecision",
    "decide_page",
    "summarize_page_decisions",
]
