"""Workspace-relative path contract for model-facing tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_DRIVE = re.compile(r"^[A-Za-z]:")


class WorkspacePathError(ValueError):
    """A model-supplied path is not a safe workspace-relative path."""


@dataclass(frozen=True)
class WorkspacePath:
    """A normalized relative path and its canonical path inside one workspace."""

    root: Path
    relative: PurePosixPath
    resolved: Path

    @classmethod
    def parse(
        cls,
        root: str | Path,
        value: str,
        *,
        allow_root: bool = False,
    ) -> "WorkspacePath":
        raw = value.strip().replace("\\", "/")
        if not raw and not allow_root:
            raise WorkspacePathError("path must not be empty")
        if raw.startswith("/") or raw.startswith("//") or _DRIVE.match(raw):
            raise WorkspacePathError(
                f"path must be relative to the workspace, got {value!r}"
            )

        relative = PurePosixPath(raw)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise WorkspacePathError(
                f"path must not contain '.', '..', or empty components: {value!r}"
            )

        root_path = Path(root).expanduser().resolve()
        try:
            resolved = (root_path / Path(*relative.parts)).resolve(strict=False)
        except OSError as exc:
            raise WorkspacePathError(f"invalid workspace path {value!r}: {exc}") from exc
        if not resolved.is_relative_to(root_path):
            raise WorkspacePathError(
                f"path resolves outside the workspace: {value!r}"
            )
        return cls(root_path, relative, resolved)


def workspace_path(
    root: str | Path,
    value: str,
    *,
    allow_root: bool = False,
) -> Path:
    return WorkspacePath.parse(root, value, allow_root=allow_root).resolved


def workspace_relative(root: str | Path, path: str | Path) -> str:
    root_path = Path(root).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    try:
        return target.relative_to(root_path).as_posix()
    except ValueError as exc:
        raise WorkspacePathError(f"path is outside the workspace: {target}") from exc

