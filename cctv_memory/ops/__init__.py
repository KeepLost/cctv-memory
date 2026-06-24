"""Offline operations tooling (not part of the layered request/runtime path).

Modules here are run by standalone scripts/CLI for capacity planning and other
ops analysis. They are offline, read-only utilities, not application-layer
request services. They read directly from the database (a SQLite file in
read-only mode, or a PostgreSQL instance via a read-only SQLAlchemy connection)
rather than going through the repository/runtime layer, and must work against
whichever backend is configured.
"""
