"""PDF diagnostics emitted during parsing."""

from .table_warnings import PdfTableWarning, detect_pdf_table_warnings, log_pdf_table_warnings

__all__ = [
    "PdfTableWarning",
    "detect_pdf_table_warnings",
    "log_pdf_table_warnings",
]
