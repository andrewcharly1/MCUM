from __future__ import annotations

import logging
import os

from MCUM.logging_utils import configure_logging


def test_configure_logging_sets_hf_hub_env_defaults(monkeypatch) -> None:
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)
    monkeypatch.delenv("HF_HUB_VERBOSITY", raising=False)
    monkeypatch.delenv("TRANSFORMERS_VERBOSITY", raising=False)

    configure_logging(force=True)

    assert os.getenv("HF_HUB_DISABLE_PROGRESS_BARS") == "1"
    assert os.getenv("HF_HUB_VERBOSITY") == "error"
    assert os.getenv("TRANSFORMERS_VERBOSITY") == "error"


def test_configure_logging_forces_hf_hub_logger_to_error() -> None:
    from huggingface_hub.utils import logging as hf_logging

    hf_logging.set_verbosity_warning()
    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.WARNING)

    configure_logging(force=True)

    assert hf_logging.get_verbosity() == logging.ERROR
    assert logging.getLogger("huggingface_hub.utils._http").getEffectiveLevel() == logging.ERROR
