from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .errors import DomainError


REPOSITORY_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class RepositoryRef:
    repository: str
    path: str

    def __str__(self) -> str:
        return f"repo:{self.repository}/{self.path}"


def parse_repository_ref(value: str, *, field: str = "repository_ref") -> RepositoryRef:
    text = str(value or "").strip()
    if not text.startswith("repo:"):
        raise DomainError(
            "work_repository_ref_invalid",
            f"{field} must use repo:<alias>/<relative-path>.",
            details={"field": field},
        )
    repository, separator, raw_path = text[5:].partition("/")
    path = PurePosixPath(raw_path)
    if (
        not separator
        or not REPOSITORY_ALIAS_RE.fullmatch(repository)
        or not raw_path
        or raw_path.startswith("/")
        or "\\" in raw_path
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DomainError(
            "work_repository_ref_invalid",
            f"{field} must use repo:<alias>/<relative-path> without traversal.",
            details={"field": field},
        )
    return RepositoryRef(repository=repository, path=path.as_posix())


def normalize_repository_ref(value: str, *, field: str = "repository_ref") -> str:
    return str(parse_repository_ref(value, field=field))


__all__ = [
    "REPOSITORY_ALIAS_RE",
    "RepositoryRef",
    "normalize_repository_ref",
    "parse_repository_ref",
]
