"""Architecture dependency tests (testing-contract §10, ARCHITECTURE_CONSTITUTION §3).

Enforced statically by scanning import statements with the ``ast`` module —
no runtime import tricks.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "cctv_memory"


def _iter_python_files(*relative_dirs: str) -> list[Path]:
    files: list[Path] = []
    for rel in relative_dirs:
        base = _PACKAGE_ROOT / rel
        if base.exists():
            files.extend(sorted(base.rglob("*.py")))
    return files


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # relative import; resolve only the leaf module name loosely
                if node.module:
                    modules.add(node.module)
            elif node.module:
                modules.add(node.module)
    return modules


def _violations(files: list[Path], forbidden_prefixes: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for path in files:
        for module in _imported_modules(path):
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(prefix + "."):
                    found.append(f"{path.relative_to(_PACKAGE_ROOT)} imports {module}")
    return found


def test_domain_does_not_import_framework_or_sdk() -> None:
    files = _iter_python_files("domain")
    assert files, "expected domain modules to exist"
    forbidden = (
        "fastapi",
        "sqlalchemy",
        "aiosqlite",
        "alembic",
        "openai",
        "google",
        "anthropic",
        "cctv_memory.infrastructure",
        "cctv_memory.api",
        "cctv_memory.application",
    )
    violations = _violations(files, forbidden)
    assert not violations, f"domain layer violations: {violations}"


def test_application_does_not_import_db_drivers_or_infrastructure() -> None:
    files = _iter_python_files("application")
    assert files, "expected application modules to exist"
    forbidden = (
        "sqlalchemy",
        "aiosqlite",
        "sqlite3",
        "alembic",
        "fastapi",
        "cctv_memory.infrastructure",
    )
    violations = _violations(files, forbidden)
    assert not violations, f"application layer violations: {violations}"


def test_api_does_not_import_infrastructure_concrete() -> None:
    files = _iter_python_files("api")
    assert files, "expected api modules to exist"
    forbidden = ("cctv_memory.infrastructure", "sqlalchemy", "aiosqlite")
    violations = _violations(files, forbidden)
    assert not violations, f"api layer violations: {violations}"


def test_contracts_do_not_import_infrastructure_or_framework() -> None:
    files = _iter_python_files("contracts")
    assert files, "expected contract modules to exist"
    forbidden = (
        "fastapi",
        "sqlalchemy",
        "cctv_memory.infrastructure",
        "cctv_memory.api",
        "cctv_memory.application",
    )
    violations = _violations(files, forbidden)
    assert not violations, f"contracts layer violations: {violations}"


def test_infrastructure_does_not_import_api_router() -> None:
    # Infrastructure must NOT import the API layer. The api<->infra wiring lives in
    # the top-level cctv_memory.bootstrap composition root instead.
    files = _iter_python_files("infrastructure")
    assert files, "expected infrastructure modules to exist"
    forbidden = ("cctv_memory.api",)
    violations = _violations(files, forbidden)
    assert not violations, f"infrastructure layer violations: {violations}"


def test_domain_does_not_import_vlm_sdk_or_drivers() -> None:
    # Explicit hardening check for VLM SDKs and DB drivers in the domain layer.
    files = _iter_python_files("domain")
    forbidden = ("openai", "google", "anthropic", "sqlite3", "sqlalchemy", "fastapi")
    violations = _violations(files, forbidden)
    assert not violations, f"domain layer violations: {violations}"


def test_no_client_component_lives_in_server_repo() -> None:
    # This repository is the SERVER (+ in-process ops CLI). The designed client
    # (SDK / tool proxy / client CLI) is a SEPARATE deliverable and must NOT be
    # smuggled into the server package. Guard against a client component appearing
    # here (api-and-service-runtime-design §6; task server-client-seam).
    forbidden_dirs = ("client", "sdk", "tool_proxy", "toolproxy")
    present = [d for d in forbidden_dirs if (_PACKAGE_ROOT / d).exists()]
    assert not present, (
        "server repo must not contain a client component package: "
        f"{present}. The client is a separate deliverable (HTTP /api/v1 only)."
    )


def test_application_and_domain_do_not_import_auth_verifier_impl() -> None:
    # The auth verifier is an API-boundary identity seam. application/domain must
    # not depend on the concrete dev/prod verifier; they only consume the resolved
    # ``principal`` via AuthorizationService (runtime-design §2.2).
    files = _iter_python_files("application", "domain")
    forbidden = ("cctv_memory.infrastructure.auth",)
    violations = _violations(files, forbidden)
    assert not violations, f"auth-verifier seam violations: {violations}"

