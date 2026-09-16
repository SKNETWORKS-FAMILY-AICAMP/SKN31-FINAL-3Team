"""Select the quotation extraction provider from process configuration."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .runpod import RunPodQuotationParser


_RUNPOD_PARSER: RunPodQuotationParser | None = None


def get_configured_quotation_parser(
    local_factory: Callable[[], Any],
) -> Any:
    """Return the configured parser while keeping the local parser injectable.

    Importing the local Hugging Face parser here would create a dependency
    cycle with ``quotation_extractor``.  A factory callback keeps this adapter
    layer independent and also makes provider selection easy to test.
    """

    provider = os.getenv("QUOTATION_EXTRACTOR_PROVIDER", "runpod").strip().lower()
    if provider in {"", "local", "huggingface", "hf"}:
        return local_factory()
    if provider == "runpod":
        global _RUNPOD_PARSER
        if _RUNPOD_PARSER is None:
            _RUNPOD_PARSER = RunPodQuotationParser.from_env()
        return _RUNPOD_PARSER
    raise ValueError(
        "QUOTATION_EXTRACTOR_PROVIDER must be one of: local, runpod"
    )


def reset_parser_cache() -> None:
    """Discard cached network clients after configuration changes or in tests."""

    global _RUNPOD_PARSER
    _RUNPOD_PARSER = None
