"""Quotation extraction provider adapters.

The workflow depends on the small callable contract exposed by
``quotation_extractor.QuotationParser``.  Provider-specific transport and
credentials stay in this package so ERP/workflow code never talks to RunPod
directly.
"""

from .factory import get_configured_quotation_parser, reset_parser_cache
from .runpod import RunPodQuotationParser, RunPodQuotationParserError

__all__ = [
    "RunPodQuotationParser",
    "RunPodQuotationParserError",
    "get_configured_quotation_parser",
    "reset_parser_cache",
]
