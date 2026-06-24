from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "cctv_memory"
_PROJECT_ROOT = _PACKAGE_ROOT.parent
_FORBIDDEN_DIRS = ("application", "domain", "workers")
_POSTGRES_SQLITE_INHERITANCE_ALLOWLIST = {
    # Read-only adapter; PostgreSQL overrides FTS methods and does not write through SQLite mappers.
    "PostgresObservationReadRepository",
}


def _backend_conditional_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "backend":
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "database":
                found.append(node.lineno)
    return found


def test_backend_selection_stays_out_of_business_layers() -> None:
    violations: list[str] = []
    for rel_dir in _FORBIDDEN_DIRS:
        base = _PACKAGE_ROOT / rel_dir
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.name == "doctor.py":
                continue
            lines = _backend_conditional_lines(path)
            if lines:
                rel = path.relative_to(_PACKAGE_ROOT)
                violations.append(f"{rel}:{lines}")

    assert not violations, "database.backend must only be read in composition/infrastructure"


def test_postgres_write_repositories_do_not_passively_inherit_sqlite_adapters() -> None:
    source_path = _PROJECT_ROOT / "cctv_memory/infrastructure/db/repositories/postgres.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name in _POSTGRES_SQLITE_INHERITANCE_ALLOWLIST:
            continue
        inherits_sqlite = any(
            isinstance(base, ast.Name) and base.id.startswith("Sqlite") for base in node.bases
        )
        if not inherits_sqlite:
            continue
        non_docstring_body = [
            stmt
            for stmt in node.body
            if not (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            )
        ]
        if len(non_docstring_body) == 1 and isinstance(non_docstring_body[0], ast.Pass):
            offenders.append(node.name)
    assert offenders == []


def test_postgres_timestamp_comparison_methods_are_overridden_without_isoformat() -> None:
    source_path = _PROJECT_ROOT / "cctv_memory/infrastructure/db/repositories/postgres.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    required = {
        "PostgresAnalysisUnitRepository": {"list_stale_running"},
        "PostgresObservationReadRepository": {
            "search_authorized_candidates",
            "authorized_candidate_pool",
        },
    }
    missing: list[str] = []
    isoformat_users: list[str] = []
    for class_name, method_names in required.items():
        cls = classes[class_name]
        methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
        for method_name in method_names:
            method = methods.get(method_name)
            if method is None:
                missing.append(f"{class_name}.{method_name}")
                continue
            for node in ast.walk(method):
                if isinstance(node, ast.Attribute) and node.attr == "isoformat":
                    isoformat_users.append(f"{class_name}.{method_name}:{node.lineno}")

    assert missing == []
    assert isoformat_users == []


def test_mappers_do_not_use_model_validate_json() -> None:
    # mappers.py is backend-agnostic: SQLite returns JSON columns as TEXT while
    # PostgreSQL JSONB columns come back already-deserialized as dict/list.
    # ``model_validate_json`` only accepts str/bytes, so it raises on the
    # PostgreSQL dict and surfaces as a spurious HTTP 400. Mappers must use
    # ``model_validate`` over a str-or-obj normalizer instead.
    source_path = _PROJECT_ROOT / "cctv_memory/infrastructure/db/mappers.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "model_validate_json"
    ]
    assert offenders == [], (
        "mappers.py must not use model_validate_json (breaks on PostgreSQL JSONB dicts); "
        f"offending lines: {offenders}"
    )


_TIMESTAMP_JSON_NORMALIZERS = {
    "_iso",
    "_dt",
    "_dt_or_now",
    "_loads_obj",
    "_loads_list",
}


def test_mappers_normalize_native_typed_columns() -> None:
    # SQLite stores timestamp columns as TEXT and JSON columns as JSON strings,
    # while the PostgreSQL backend uses TIMESTAMPTZ (driver returns datetime)
    # and JSONB (driver returns dict/list). A ``*_to_dto`` mapper that passes a
    # raw ``row.<col>_at`` / ``row.<col>_json`` value straight into the DTO
    # works on SQLite but breaks on PostgreSQL: the native datetime/dict either
    # raises a Pydantic ``string_type`` error (crashing the worker claim path so
    # the VLM never runs) or silently bypasses validation. Every timestamp/JSON
    # column must flow through a str-or-native normalizer.
    source_path = _PROJECT_ROOT / "cctv_memory/infrastructure/db/mappers.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.endswith("_to_dto"):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.keyword) or node.arg is None:
                continue
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "row"
                and (value.attr.endswith("_at") or value.attr.endswith("_json"))
            ):
                offenders.append(f"{fn.name}: {node.arg}=row.{value.attr} (line {value.lineno})")

    assert offenders == [], (
        "native-typed (TIMESTAMPTZ/JSONB) columns must pass through a normalizer "
        f"({sorted(_TIMESTAMP_JSON_NORMALIZERS)}), not be assigned raw into a DTO; "
        f"offenders: {offenders}"
    )


def _is_native_col(name: str) -> bool:
    # TIMESTAMPTZ columns end with _at/_time; JSONB columns end with _json.
    return name.endswith("_at") or name.endswith("_time") or name.endswith("_json")


def test_postgres_repos_do_not_write_native_columns_through_orm() -> None:
    # The bug class behind the task-queue terminal-write failure (mark_succeeded
    # rendered lease_expires_at=$::VARCHAR against a TIMESTAMPTZ column): a
    # PostgreSQL repository writing a TIMESTAMPTZ/JSONB column through the ORM
    # (update(orm.X).values(col=...), session.execute(insert(...).values(...)),
    # or attribute assignment row.col = ...) relies on the SQLite-shaped ORM
    # String/Text annotation and breaks on PostgreSQL with DatatypeMismatch.
    # PostgreSQL repos must write these columns via explicit text() SQL with
    # typed binds. (Reads through *_to_dto are covered by the guard above; the
    # ORM with_variant change in tables.py is defense-in-depth, not a license to
    # write native columns through the ORM.)
    source_path = _PROJECT_ROOT / "cctv_memory/infrastructure/db/repositories/postgres.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offenders: list[str] = []

    # (a) update(orm.X)/insert(orm.X) ... .values(native_col=...)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "values":
            continue
        # Confirm the chain roots at an update()/insert() call.
        root = node.func.value
        roots_in_dml = False
        while isinstance(root, ast.Call):
            fn = root.func
            if isinstance(fn, ast.Name) and fn.id in ("update", "insert"):
                roots_in_dml = True
                break
            if isinstance(fn, ast.Attribute):
                root = fn.value
                continue
            break
        if not roots_in_dml:
            continue
        for kw in node.keywords:
            if kw.arg and _is_native_col(kw.arg):
                offenders.append(f"ORM .values({kw.arg}=...) at line {node.lineno}")

    # (b) attribute assignment: row.<native_col> = ...
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and _is_native_col(target.attr)
            ):
                offenders.append(f"attribute write .{target.attr} = ... at line {node.lineno}")

    assert offenders == [], (
        "PostgreSQL repositories must not write TIMESTAMPTZ/JSONB columns through the "
        "ORM (renders ::VARCHAR/::TEXT and fails with DatatypeMismatch). Use explicit "
        f"text() SQL with typed binds instead. Offenders: {offenders}"
    )
