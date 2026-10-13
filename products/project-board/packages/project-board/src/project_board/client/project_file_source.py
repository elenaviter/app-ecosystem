"""The coordinator machine's own copy of a project's listed files (W794).

Operator, 2026-10-13: "agents has nothing to do with this. this is per
project.", "we need the machine with coordinator and its credential", and
the file shown is the "last one" (answer "A": the newest commit of the
repository's default branch, fetched just before the read).

So a project file view is served by the relay process on the coordinator's
machine from a per-project clone it owns, never from an agent's workspace:
a blobless, depth-1 clone whose blobs are fetched only when a listed file is
read. Before each read the default branch is resolved again (a renamed
default branch must not keep serving the old one) and fetched; the file is
read with ``git show <commit>:<path>``, never from a working tree.

The GitHub token reaches git only through the environment of the git child
and a ``GIT_ASKPASS`` script that reads it from there: never in argv, the
remote URL, ``.git/config``, a file or a log. One lock per project clone.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from ..contract.errors import DomainError

TOKEN_ENV = "PB_PROJECT_FILE_TOKEN"
GIT_TIMEOUT_SECONDS = 60
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ASKPASS = (
    "#!/bin/sh\n"
    "# W794: answers git's prompts from the git child's environment; holds no secret.\n"
    'case "$1" in\n'
    '  Username*) printf "%s\\n" x-access-token ;;\n'
    f'  *) printf "%s\\n" "${TOKEN_ENV}" ;;\n'
    "esac\n"
)

TokenProvider = Callable[[str], Awaitable[str]]


def listed_path(path: str) -> str:
    """A repository-relative path, or a refusal: no absolute path, no ``..``, no ``.git``."""

    raw = str(path or "").strip()
    pure = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or pure.is_absolute()
        or any(part in ("", ".", "..") for part in raw.split("/"))
        or pure.parts[0] == ".git"
    ):
        raise DomainError("file_not_listed", "A project file path must be a plain path inside its repository.")
    return str(pure)


class ProjectFileSource:
    """Per-project blobless clones under ``root``; ``read`` returns the default branch's newest content."""

    def __init__(self, root: Path | str, *, remote_base: str = "https://github.com/") -> None:
        self.root = Path(root)
        self.remote_base = remote_base
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock(self, project_id: str, alias: str) -> asyncio.Lock:
        return self._locks.setdefault((project_id, alias), asyncio.Lock())

    def _askpass(self) -> Path:
        path = self.root / ".askpass.sh"
        if not path.exists() or path.read_text(encoding="utf-8") != _ASKPASS:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(_ASKPASS, encoding="utf-8")
            temporary.chmod(stat.S_IRWXU)
            os.replace(temporary, path)
        return path

    async def _git(self, args: list[str], *, cwd: Path, token: str) -> str:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_") and key not in ("GH_TOKEN", "GITHUB_TOKEN")
        }
        env.update({
            "GIT_ASKPASS": str(self._askpass()),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            TOKEN_ENV: token,
        })
        process = await asyncio.create_subprocess_exec(
            # No other credential helper answers for this clone.
            "git", "-c", "credential.helper=", "-c", "core.askPass=", *args,
            cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DomainError("fetch_failed", f"git {args[0]} did not finish within {GIT_TIMEOUT_SECONDS} s.") from exc
        if process.returncode != 0:
            # git's message names the remote, never the token (it is not in the URL or argv).
            detail = err.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
            raise DomainError("fetch_failed", f"git {args[0]} failed: {detail[0][:300]}")
        return out.decode("utf-8", errors="replace")

    async def read(
        self, *, project_id: str, alias: str, repository: str, path: str, token: TokenProvider,
    ) -> tuple[str, str]:
        """The file's content and the commit it was read at: the default branch's newest commit."""

        if not _ALIAS.fullmatch(alias or "") or not _REPOSITORY.fullmatch(repository or ""):
            raise DomainError("file_not_listed", "The project file names no GitHub repository this machine can read.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project_id or ""):
            raise DomainError("work_journal_view_project_mismatch", "The project id is not a plain identifier.")
        relative = listed_path(path)
        async with self._lock(project_id, alias):
            secret = await token(repository)
            clone = self.root / project_id / alias
            url = f"{self.remote_base}{repository}.git"
            if not (clone / ".git").is_dir():
                clone.parent.mkdir(parents=True, exist_ok=True)
                await self._git(
                    ["clone", "--quiet", "--filter=blob:none", "--no-checkout", "--depth=1", url, str(clone)],
                    cwd=clone.parent, token=secret,
                )
            elif (await self._git(["remote", "get-url", "origin"], cwd=clone, token=secret)).strip() != url:
                await self._git(["remote", "set-url", "origin", url], cwd=clone, token=secret)
            head = await self._git(["ls-remote", "--symref", "origin", "HEAD"], cwd=clone, token=secret)
            match = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", head, re.M)
            if match is None:
                raise DomainError("fetch_failed", "The repository names no default branch.")
            branch = match.group(1)
            await self._git(
                ["fetch", "--quiet", "--filter=blob:none", "--depth=1", "origin",
                 f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                cwd=clone, token=secret,
            )
            commit = (await self._git(["rev-parse", f"refs/remotes/origin/{branch}"], cwd=clone, token=secret)).strip()
            # The tree is local in a blobless clone: a missing path is told apart from a failed blob fetch.
            if not (await self._git(["ls-tree", "--name-only", commit, "--", relative], cwd=clone, token=secret)).strip():
                raise DomainError("file_missing", f"{relative} is not on {branch} of {repository}.")
            content = await self._git(["show", f"{commit}:{relative}"], cwd=clone, token=secret)
            return content, commit


__all__ = ["ProjectFileSource", "TOKEN_ENV", "listed_path"]
