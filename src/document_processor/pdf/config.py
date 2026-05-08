"""Public PDF parsing configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ImageOutput = Literal["embedded", "external", "off"]
ImageQuality = Literal["standard", "high", "max"]


class PdfParseConfig(BaseModel):
    """Small public PDF parsing configuration.

    ODL-specific extraction knobs are intentionally kept internal so PDF parsing
    has the same high-level shape as DOCX/HWP/HWPX parsing.
    """

    model_config = {"extra": "forbid"}

    pages: str | list[int] | None = Field(
        default=None,
        description='Pages to parse, e.g. "1,3,5-7". Defaults to all pages.',
    )
    include_header_footer: bool = False
    image_quality: ImageQuality = "standard"
    image_output: ImageOutput | None = None

    @field_validator("pages")
    @classmethod
    def _validate_pages(cls, value: str | list[int] | None) -> str | list[int] | None:
        if value is None:
            return None
        if isinstance(value, list):
            if any(page < 1 for page in value):
                raise ValueError("pages must contain positive 1-based page numbers")
            return value
        if not value.strip():
            raise ValueError("pages cannot be empty")
        return value

    def to_odl_config(
        self,
        *,
        pages: list[int] | None = None,
        for_doc_ir: bool = False,
    ) -> dict[str, Any]:
        image_output = self.image_output
        if for_doc_ir and image_output != "off":
            image_output = "embedded"

        config: dict[str, Any] = {
            "reading_order": "xycut",
            "table_method": "cluster",
            "pages": pages if pages is not None else self.pages,
            "include_header_footer": self.include_header_footer,
            "image_output": image_output,
            "image_quality": self.image_quality,
        }
        return {key: value for key, value in config.items() if value is not None}


__all__ = [
    "ImageOutput",
    "ImageQuality",
    "PdfParseConfig",
]
