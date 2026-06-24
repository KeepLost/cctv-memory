"""Shared helpers for SQLite adapters (authorized-scope filtering, errors)."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.domain.enums import SecurityLevel
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.repositories.types import ConflictError, IdempotencyConflictError


def authorized_observation_filter(scope: AuthorizedScope) -> ColumnElement[bool]:
    """Build a fail-closed AND filter for observation_records (auth §4.1).

    Empty allowed_* lists deny the dimension (``IN ()`` is always false), which
    is exactly the required fail-closed behavior. The security-level predicate
    enumerates the allowed levels at or below ``max_security_level``.
    """
    allowed_levels = [
        level.value
        for level in SecurityLevel
        if scope.max_security_level.allows(level)
    ]
    return and_(
        orm.ObservationRecord.tenant_id == scope.tenant_id,
        orm.ObservationRecord.camera_id.in_(scope.allowed_camera_ids),
        orm.ObservationRecord.location_id.in_(scope.allowed_location_ids),
        orm.ObservationRecord.access_policy_id.in_(scope.allowed_access_policy_ids),
        orm.ObservationRecord.security_level.in_(allowed_levels),
    )


def authorized_video_filter(scope: AuthorizedScope) -> ColumnElement[bool]:
    """Build a fail-closed AND filter for video_sources by camera/policy/tenant."""
    return and_(
        orm.VideoSource.tenant_id == scope.tenant_id,
        orm.VideoSource.camera_id.in_(scope.allowed_camera_ids),
        orm.VideoSource.access_policy_id.in_(scope.allowed_access_policy_ids),
    )


def map_integrity_error(exc: IntegrityError, *, idempotency: bool = False) -> Exception:
    """Map a SQLite IntegrityError to a contract repository error."""
    if idempotency:
        return IdempotencyConflictError(str(exc.orig))
    return ConflictError(str(exc.orig))


def upsert_by_pk[Row](
    session: Session,
    model: type[Row],
    pk_predicate: ColumnElement[bool],
    *,
    build_new: Callable[[], Row],
    apply_update: Callable[[Row], None],
) -> Row:
    """Concurrency-safe write-first upsert by primary key (task 20260617-1118).

    The cold-start race: two workers concurrently first-provision the same row
    (e.g. the shared ``loc_auto_unregistered`` placeholder or one ``camera_id``).
    The previous ``get()``-then-``merge()`` was unsafe on TWO counts:

    1. The SELECT-then-INSERT was not atomic, so the loser hit a unique/PK
       violation that escaped as a raw IntegrityError and failed the whole job.
    2. Reading BEFORE writing in the same transaction opens a deferred read lock;
       the subsequent write-upgrade returns ``SQLITE_BUSY`` *immediately* and is
       NOT covered by ``busy_timeout`` (SQLite avoids deadlock on lock upgrades).
       Under real concurrency this surfaced as ``OperationalError: database is
       locked`` on the provisioning write.

    Fix: write FIRST (INSERT inside a SAVEPOINT, no preceding read in the same
    transaction, so ``busy_timeout`` serializes writers cleanly). On a PK/unique
    conflict the SAVEPOINT rolls back ONLY the nested unit (the outer session/
    transaction stays usable, never poisoned), the failed pending instance is
    expunged, and we read the existing row back and apply the caller's update —
    yielding idempotent provisioning AND preserved update-on-exists semantics.
    This realizes the upsert idempotency of database-capability-contract §3.2 and
    the unique_violation mapping of database-adapter-contract §8.
    """
    new_row = build_new()
    try:
        with session.begin_nested():
            session.add(new_row)
            session.flush()
        return new_row
    except IntegrityError:
        if new_row in session:
            session.expunge(new_row)
        existing = session.scalar(select(model).where(pk_predicate))
        if existing is None:
            # The conflict was not the expected PK race (or the row vanished):
            # surface it as a contract ConflictError rather than fabricating success.
            raise
        apply_update(existing)
        session.flush()
        return existing
