"""Supplier purchase-response (PR) module.

This package is intentionally self-contained so it can be reviewed before its
routers and workflow hook are enabled in the existing application.
"""

from .routes import internal_router, public_router
from .service import create_and_send_pr

__all__ = ["create_and_send_pr", "internal_router", "public_router"]
