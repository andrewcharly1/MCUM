from __future__ import annotations

import logging
import os
import warnings
from typing import Final


LOG_FORMAT: Final[str] = "%(message)s"
LOGGER_NAMESPACE: Final[str] = "MCUM"


def _apply_library_env_defaults() -> None:
    """Set quiet defaults before model libraries configure their own loggers."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")


_apply_library_env_defaults()


def _coerce_log_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level

    resolved = str(level or os.getenv("MCUM_LOG_LEVEL", "INFO")).upper()
    return getattr(logging, resolved, logging.INFO)


def _quiet_third_party_loggers() -> None:
    # Keep third-party model loaders quiet unless they emit warnings/errors.
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
    logging.getLogger("transformers").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    # Hugging Face Hub configures its own root logger on import, so force it down
    # explicitly if the library is already loaded in this process.
    try:
        from huggingface_hub.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
    except Exception:
        pass


def configure_logging(level: str | int | None = None, force: bool = False) -> None:
    """Configure MCUM runtime logging without changing CLI output format."""
    resolved_level = _coerce_log_level(level)
    _apply_library_env_defaults()
    warnings.filterwarnings(
        "ignore",
        message="You are sending unauthenticated requests to the HF Hub.*",
    )
    logging.basicConfig(level=resolved_level, format=LOG_FORMAT, force=force)
    _quiet_third_party_loggers()


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(LOGGER_NAMESPACE)
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{name}")
