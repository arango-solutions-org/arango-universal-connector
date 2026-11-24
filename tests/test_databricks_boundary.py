"""Backend isolation: only local_delta.py may import deltalake or pyarrow."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[1] / "packages"
ALLOWED = PACKAGES / "core/src/arango_connector/sources/databricks/local_delta.py"
FORBIDDEN = ("deltalake", "pyarrow")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_only_the_local_client_imports_the_delta_backend() -> None:
    offenders = [
        f"{path.relative_to(PACKAGES)} imports {name}"
        for path in sorted(PACKAGES.rglob("*.py"))
        if path != ALLOWED
        for name in sorted(_imports(path) & set(FORBIDDEN))
    ]
    assert offenders == []
    assert {"deltalake", "pyarrow"} <= _imports(ALLOWED)  # the check can see imports
