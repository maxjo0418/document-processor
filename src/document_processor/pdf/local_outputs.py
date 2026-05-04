"""Typed access to ODL local output artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from ..io_utils import TemporarySourcePath, get_source_name
from .config import PdfParseConfig
from .odl import convert_pdf_local

PdfLocalFormat = str
DEFAULT_LOCAL_FORMATS: tuple[PdfLocalFormat, ...] = ("json", "html", "markdown")


@dataclass(frozen=True, slots=True)
class PdfLocalOutputs:
    """Local ODL artifacts materialized on disk."""

    source_path: str | None
    output_dir: Path
    format_paths: dict[PdfLocalFormat, Path]

    @property
    def json_path(self) -> Path | None:
        return self.format_paths.get("json")

    @property
    def html_path(self) -> Path | None:
        return self.format_paths.get("html")

    @property
    def markdown_path(self) -> Path | None:
        for fmt in ("markdown", "markdown-with-html", "markdown-with-images"):
            if fmt in self.format_paths:
                return self.format_paths[fmt]
        return None

    def path_for(self, output_format: PdfLocalFormat) -> Path | None:
        return self.format_paths.get(output_format)

    def read_text(self, output_format: PdfLocalFormat, *, encoding: str = "utf-8") -> str:
        path = self.path_for(output_format)
        if path is None:
            raise KeyError(f"Output format not available: {output_format}")
        return path.read_text(encoding=encoding)

    def read_json(self) -> dict[str, Any]:
        path = self.json_path
        if path is None:
            raise KeyError("JSON output not available")
        return json.loads(path.read_text(encoding="utf-8"))


def export_pdf_local_outputs(
    source: str | Path | bytes | BinaryIO,
    *,
    output_dir: str | Path,
    formats: Iterable[PdfLocalFormat] = DEFAULT_LOCAL_FORMATS,
    config: PdfParseConfig | dict[str, Any] | None = None,
) -> PdfLocalOutputs:
    """Export local ODL artifacts to a directory and return typed handles."""
    resolved_config = (
        config
        if isinstance(config, PdfParseConfig)
        else PdfParseConfig.model_validate(config or {})
    )
    resolved_output_dir = Path(output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    with TemporarySourcePath(source, suffix=".pdf") as source_path:
        format_paths = convert_pdf_local(
            source_path,
            output_dir=resolved_output_dir,
            formats=list(formats),
            config=resolved_config.to_odl_config(for_doc_ir=False),
        )

    return PdfLocalOutputs(
        source_path=get_source_name(source) or str(source_path),
        output_dir=resolved_output_dir,
        format_paths=format_paths,
    )


__all__ = [
    "DEFAULT_LOCAL_FORMATS",
    "PdfLocalFormat",
    "PdfLocalOutputs",
    "export_pdf_local_outputs",
]
