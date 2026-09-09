"""Freshness boundary for the assistant.

The default implementation intentionally does not call ERPNext.  It exists so
an ERP-specific reconcile adapter can be injected without coupling the chat
application service to ERP DocTypes or REST endpoints.
"""

from __future__ import annotations


class ProjectionOnlyFreshness:
    def refresh_reference(self, reference: str, *, actor: str) -> bool:
        del reference, actor
        return False
