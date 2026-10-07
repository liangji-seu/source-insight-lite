from __future__ import annotations

import ast
import fnmatch
import os
import tokenize
from pathlib import Path
from typing import Callable

from .models import FileUnit

DEFAULT_EXCLUDES = {
    ".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__",
    "site-packages", "third_party", "vendor",
}


def _module_name(root: Path, path: Path) -> str:
    if root.is_file():
        return path.stem
    rel = path.relative_to(root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = Path(parts[-1]).stem
    return ".".join(p for p in parts if p) or root.name


def scan(root: str, excludes: list[str] | None = None,
         progress: Callable[[int, int, str], None] | None = None) -> tuple[list[FileUnit], list[dict[str, str]]]:
    """Read Python files without importing or executing them."""
    root_path = Path(root).expanduser().resolve()
    excluded = set(DEFAULT_EXCLUDES)
    patterns = [x.strip().strip("/\\") for x in (excludes or []) if x.strip()]
    files: list[Path] = []
    if root_path.is_file() and root_path.suffix == ".py":
        files = [root_path]
    elif root_path.is_dir():
        for base, dirs, names in os.walk(root_path, topdown=True, followlinks=False):
            base_path = Path(base)
            kept_dirs: list[str] = []
            for directory in sorted(dirs):
                candidate = base_path / directory
                relative = candidate.relative_to(root_path).as_posix()
                skip = directory in excluded or any(
                    fnmatch.fnmatch(directory, pattern) or fnmatch.fnmatch(relative, pattern)
                    for pattern in patterns
                )
                if not skip and not candidate.is_symlink():
                    kept_dirs.append(directory)
            dirs[:] = kept_dirs
            for name in sorted(names):
                path = Path(base) / name
                relative = path.relative_to(root_path).as_posix()
                excluded_file = any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern)
                                    for pattern in patterns)
                if path.suffix == ".py" and not excluded_file and not path.is_symlink():
                    files.append(path)
    files.sort(key=lambda p: str(p).lower())
    total = len(files)
    units: list[FileUnit] = []
    errors: list[dict[str, str]] = []
    for completed, path in enumerate(files, 1):
        absolute = str(path.resolve())
        rel = str(path.relative_to(root_path)) if root_path.is_dir() else path.name
        try:
            with tokenize.open(str(path)) as stream:
                source = stream.read()
        except (OSError, UnicodeError, SyntaxError) as exc:
            msg = f"unable to read file: {exc}"
            errors.append({"file": absolute, "message": msg})
            units.append(FileUnit(absolute, rel, _module_name(root_path, path), "", None, msg))
            if progress:
                progress(completed, total, absolute)
            continue
        try:
            tree = ast.parse(source, filename=absolute)
        except (SyntaxError, ValueError, TypeError) as exc:
            msg = f"syntax error: {exc}"
            errors.append({"file": absolute, "message": msg})
            tree = None
        units.append(FileUnit(absolute, rel, _module_name(root_path, path), source, tree,
                              errors[-1]["message"] if errors and errors[-1]["file"] == absolute else None))
        if progress:
            progress(completed, total, absolute)
    return units, errors

