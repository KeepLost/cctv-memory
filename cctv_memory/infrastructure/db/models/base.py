"""SQLAlchemy declarative base (infrastructure-internal).

ORM models live behind repository adapters and must never leak to
application/domain (repository-port-contract §0, database-adapter-contract §2).
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all CCTV Memory ORM models."""
