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

W848 (operator, 2026-10-14, on the project-file view of a repository with
no GitHub: "B: the client reads local repositories directly << of course
this! this is special type of project. where we do not have any github"):
a repository whose URL is a path on this machine is read in place by
``read_local``, with no clone, no fetch and no key, at its default branch's
newest commit (the branch its HEAD names), never from its working tree.
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

    async def read_local(self, *, repository_path: str, path: str, max_bytes: int | None = None) -> tuple[str, str]:
        """W848: a listed file of a repository on this machine, at its default branch's newest commit.

        The caller has already confined ``repository_path`` to a root this host's own repository map binds.
        """

        relative = listed_path(path)
        root = Path(repository_path).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise DomainError("repository_path_missing", f"{repository_path} is not a folder on this machine.")
        root = root.resolve()
        try:
            git_dir = Path((await self._local_git(["rev-parse", "--absolute-git-dir"], cwd=root)).strip()).resolve()
        except DomainError as exc:
            raise DomainError("repository_not_git", f"{repository_path} is not a git repository.") from exc
        # The folder itself must be the repository (or a bare one), not a folder inside another repository.
        if git_dir not in (root / ".git", root):
            raise DomainError("repository_not_git", f"{repository_path} is not the top of a git repository.")
        head = (await self._local_git(["symbolic-ref", "--quiet", "HEAD"], cwd=root, check=False)).strip()
        if not head.startswith("refs/heads/"):
            raise DomainError("repository_read_failed", f"{repository_path} has no default branch (its HEAD is detached).")
        branch = head[len("refs/heads/"):]
        try:
            commit = (await self._local_git(["rev-parse", "--verify", "--quiet", f"{head}^{{commit}}"], cwd=root)).strip()
        except DomainError as exc:
            raise DomainError("file_missing", f"{branch} of {repository_path} has no commit yet.") from exc
        entry = (await self._local_git(["ls-tree", commit, "--", relative], cwd=root)).strip()
        if entry.split(None, 2)[1:2] != ["blob"]:
            raise DomainError("file_missing", f"{relative} is not a file on {branch} of {repository_path}.")
        size = int((await self._local_git(["cat-file", "-s", f"{commit}:{relative}"], cwd=root)).strip() or 0)
        if max_bytes is not None and size > max_bytes:
            raise DomainError("file_too_large", f"{relative} is {size} bytes; a file view carries at most {max_bytes}.")
        # The committed blob, raw: no working tree, no textconv, no filter.
        content = await self._local_git(["cat-file", "blob", f"{commit}:{relative}"], cwd=root)
        return content, commit

    async def _local_git(self, args: list[str], *, cwd: Path, check: bool = True) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        # Read-only on the folder: no lazy fetch from a promisor remote, no fsmonitor hook, no index refresh.
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
                    "GIT_OPTIONAL_LOCKS": "0"})
        process = await asyncio.create_subprocess_exec(
            "git", "-c", "core.fsmonitor=false", "-c", "protocol.allow=never", *args,
            cwd=str(cwd), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DomainError("repository_read_failed", f"git {args[0]} did not finish within {GIT_TIMEOUT_SECONDS} s.") from exc
        if check and process.returncode != 0:
            detail = err.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
            raise DomainError("repository_read_failed", f"git {args[0]} failed: {detail[0][:300]}")
        return out.decode("utf-8", errors="replace") if process.returncode == 0 else ""

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
