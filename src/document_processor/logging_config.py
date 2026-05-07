"""Logging configuration for document_processor."""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "document_processor"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_HANDLER_KIND_ATTR = "_document_processor_handler_kind"
_HANDLER_PATH_ATTR = "_document_processor_handler_path"
_configured = False


def _coerce_level(level: int | str) -> int:
    if isinstance(level, str):
        resolved = logging.getLevelName(level.upper())
        if isinstance(resolved, str):
            raise ValueError(f"Unknown logging level: {level!r}")
        return resolved
    return level


def _managed_handlers(logger: logging.Logger, kind: str) -> list[logging.Handler]:
    return [
        handler
        for handler in logger.handlers
        if getattr(handler, _HANDLER_KIND_ATTR, None) == kind
    ]


def _remove_managed_handlers(logger: logging.Logger, kind: str) -> None:
    for handler in _managed_handlers(logger, kind):
        logger.removeHandler(handler)
        handler.close()


def _formatter(log_format: str, date_format: str) -> logging.Formatter:
    return logging.Formatter(log_format, datefmt=date_format)


def _set_handler_common(
    handler: logging.Handler,
    *,
    kind: str,
    level: int,
    formatter: logging.Formatter,
) -> None:
    setattr(handler, _HANDLER_KIND_ATTR, kind)
    handler.setLevel(level)
    handler.setFormatter(formatter)


def configure_logging(
    level: int | str = logging.WARNING,
    *,
    log_file: str | Path | None = None,
    console: bool = True,
    file_mode: str = "a",
    log_format: str = DEFAULT_LOG_FORMAT,
    date_format: str = DEFAULT_DATE_FORMAT,
    propagate: bool = False,
) -> logging.Logger:
    """Configure the package-wide logger used by DocIR and helpers.

    The default is a WARNING-level console logger. Passing ``log_file`` adds a
    file handler; calling this function again updates or replaces managed
    package handlers instead of stacking duplicates.
    """
    global _configured

    resolved_level = _coerce_level(level)
    formatter = _formatter(log_format, date_format)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(resolved_level)
    logger.propagate = propagate

    if console:
        console_handlers = _managed_handlers(logger, "console")
        if console_handlers:
            console_handler = console_handlers[0]
            for extra_handler in console_handlers[1:]:
                logger.removeHandler(extra_handler)
                extra_handler.close()
        else:
            console_handler = logging.StreamHandler()
            logger.addHandler(console_handler)
        _set_handler_common(
            console_handler,
            kind="console",
            level=resolved_level,
            formatter=formatter,
        )
    else:
        _remove_managed_handlers(logger, "console")

    if log_file is None:
        _remove_managed_handlers(logger, "file")
    else:
        file_path = Path(log_file).expanduser()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_file_path = str(file_path.resolve())
        file_handlers = _managed_handlers(logger, "file")
        reusable_handler: logging.Handler | None = None
        for handler in file_handlers:
            if getattr(handler, _HANDLER_PATH_ATTR, None) == resolved_file_path and reusable_handler is None:
                reusable_handler = handler
                continue
            logger.removeHandler(handler)
            handler.close()
        if reusable_handler is None:
            reusable_handler = logging.FileHandler(file_path, mode=file_mode, encoding="utf-8")
            setattr(reusable_handler, _HANDLER_PATH_ATTR, resolved_file_path)
            logger.addHandler(reusable_handler)
        _set_handler_common(
            reusable_handler,
            kind="file",
            level=resolved_level,
            formatter=formatter,
        )

    _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the package logger.

    Use ``get_logger(__name__)`` inside modules. Names outside the package are
    prefixed under ``document_processor`` so all helper logs share one
    configuration.
    """
    if not _configured:
        configure_logging()

    if name is None or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


__all__ = [
    "DEFAULT_DATE_FORMAT",
    "DEFAULT_LOG_FORMAT",
    "LOGGER_NAME",
    "configure_logging",
    "get_logger",
]
