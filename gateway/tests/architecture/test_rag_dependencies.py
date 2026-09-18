"""Keep the RAG layering from collapsing back into app.rag."""

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2] / "app"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_domain_has_no_transport_persistence_or_adapter_dependencies():
    forbidden = ("fastapi", "sqlalchemy", "redis", "app.api", "app.integrations")
    for path in (ROOT / "domain" / "rag").glob("*.py"):
        assert not any(item.startswith(forbidden) for item in imported_modules(path)), (
            path
        )


def test_worker_does_not_depend_on_public_routes():
    for path in (ROOT / "workers" / "rag").glob("*.py"):
        assert not any(item.startswith("app.api") for item in imported_modules(path)), (
            path
        )


def test_application_code_has_no_legacy_rag_imports():
    for path in ROOT.rglob("*.py"):
        if path == ROOT / "rag" / "models.py":
            continue
        assert "app.rag" not in imported_modules(path), path
