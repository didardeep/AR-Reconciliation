"""Shared API dependencies."""
from __future__ import annotations
from app.db.session import get_session  # re-export for routers

__all__ = ["get_session"]
