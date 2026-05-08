"""OpenDataLoader local JAR runner."""

from __future__ import annotations

import json
import locale
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

_ENV_ODL_JAR = "DOCUMENT_PROCESSOR_ODL_JAR"
_VENDORED_ODL_JAR = "opendataloader-pdf-cli-2.2.1.jar"


def resolve_odl_jar_path() -> Path:
    """Resolve the ODL CLI JAR path for local execution."""
    if configured_jar := os.environ.get(_ENV_ODL_JAR):
        jar_path = Path(configured_jar).expanduser()
        if not jar_path.exists():
            raise FileNotFoundError(f"Configured ODL jar not found: {jar_path}")
        return jar_path

    vendored_jar = Path(__file__).resolve().parent / "vendor" / "odl" / _VENDORED_ODL_JAR
    if vendored_jar.exists():
        return vendored_jar

    raise FileNotFoundError(
        "OpenDataLoader jar not found. Vendor the jar in document_processor/pdf/odl/vendor/odl "
        f"or set {_ENV_ODL_JAR}."
    )


def convert_pdf_local(
    path: str | Path,
    *,
    output_dir: str | Path,
    formats: str | Iterable[str] = "json",
    config: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Run the vendored ODL CLI in local mode and return expected output paths."""
    source_path = Path(path)
    resolved_output_dir = Path(output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    resolved_formats = _normalize_formats(formats)
    command = _build_odl_command(
        source_path,
        output_dir=resolved_output_dir,
        formats=resolved_formats,
        config=config or {},
    )
    _run_command(command)

    outputs = {
        output_format: resolved_output_dir / f"{source_path.stem}{_format_suffix(output_format)}"
        for output_format in resolved_formats
    }
    missing = [str(path) for path in outputs.values() if not path.exists()]
    if missing:
        raise RuntimeError(f"OpenDataLoader outputs not found: {', '.join(missing)}")
    return outputs


def run_odl_json(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Run ODL against a PDF and return parsed JSON output."""

    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = convert_pdf_local(
            path,
            output_dir=temp_dir,
            formats="json",
            config=config,
        )["json"]
        return json.loads(output_path.read_text(encoding="utf-8"))


def _build_odl_command(
    path: Path,
    *,
    output_dir: Path,
    formats: list[str],
    config: dict[str, Any],
) -> list[str]:
    pages = config.get("pages")
    if isinstance(pages, (list, tuple)):
        pages = ",".join(str(page) for page in pages)
    elif pages is not None:
        pages = str(pages)

    command = [
        "java",
        # ODL image export renders page regions through Java2D/PDFBox. On macOS,
        # headful AWT can abort in CLI contexts, so force headless rendering.
        "-Djava.awt.headless=true",
        "-jar",
        str(resolve_odl_jar_path()),
        str(path),
        "--output-dir",
        str(output_dir),
        "--format",
        ",".join(formats),
        "--quiet",
    ]

    _append_option(command, "--table-method", "cluster")
    _append_option(command, "--reading-order", "xycut")
    _append_option(command, "--image-output", config.get("image_output"))
    _append_option(command, "--image-pixel-size", _image_pixel_size_from_config(config))
    _append_option(command, "--pages", pages)
    _append_flag(
        command,
        "--include-header-footer",
        config.get("include_header_footer", False),
    )
    return command


def _run_command(command: list[str]) -> None:
    try:
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            encoding=locale.getpreferredencoding(False),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Java runtime not found. Install Java and ensure `java` is on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr or exc.stdout or ""
        raise RuntimeError(
            f"OpenDataLoader CLI failed with exit code {exc.returncode}: {details.strip()}"
        ) from exc


def _append_option(command: list[str], name: str, value: Any) -> None:
    if value is None:
        return
    command.extend([name, str(value)])


def _append_flag(command: list[str], name: str, enabled: bool) -> None:
    if enabled:
        command.append(name)


def _normalize_formats(formats: str | Iterable[str]) -> list[str]:
    if isinstance(formats, str):
        resolved = [formats]
    else:
        resolved = [value for value in formats]
    if not resolved:
        raise ValueError("At least one ODL output format is required.")
    deduped = list(dict.fromkeys(resolved))
    markdown_variants = {
        value for value in deduped
        if value in {"markdown", "markdown-with-html", "markdown-with-images"}
    }
    if len(markdown_variants) > 1:
        raise ValueError("Use only one markdown output variant at a time.")
    return deduped


def _image_pixel_size_from_config(config: dict[str, Any]) -> int | float | None:
    return {
        "standard": None,
        "high": 2400,
        "max": 4000,
    }.get(config.get("image_quality"))


def _format_suffix(output_format: str) -> str:
    return {
        "json": ".json",
        "text": ".txt",
        "html": ".html",
        "pdf": ".pdf",
        "markdown": ".md",
        "markdown-with-html": ".md",
        "markdown-with-images": ".md",
    }.get(output_format, f".{output_format}")


__all__ = [
    "convert_pdf_local",
    "resolve_odl_jar_path",
    "run_odl_json",
]
