from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

import yaml
from app_foundation.index.sqlite import FusionWeights

from ..contract.errors import DomainError
from ..contract.portable_refs import (
    REPOSITORY_ALIAS_RE,
    RepositoryRef,
    normalize_repository_ref,
    parse_repository_ref,
)
from ..contract.refs import parse_ref
from ..contract.scoped_collection import CollectionError, ScopedKeysetCursor
from .io import (
    atomic_write_json,
    exclusive_lock,
    read_json,
    utc_now,
)
from .journal_search import JournalDocument, JournalSearchIndex
from .project_setup import PROJECT_SETUP_FILE, read_project_setup
from .read_roots import DEFAULT_READ_REF, head_commit, read_root_lag
from .workspace_clone import clone_state


# The page holding a project's standing facts, at the root of its journal home.
PROJECT_FACTS_FILE = "project-facts.md"
# The page holding the project's reproducible development environment.
PROJECT_ENVIRONMENT_FILE = "project-environment.md"


WORKSPACE_SCHEMA = "problem-board.journal-workspace.v1"
JOURNAL_INDEX_STATUS_SCHEMA = "problem-board.journal-index-status.v1"
JOURNAL_INDEX_REQUIRED_FRONTMATTER = ("entry_ref", "project_ref")
JOURNAL_RETRIEVAL_FRONTMATTER = (
    "title",
    "summary",
    "status",
    "tags",
    "keywords",
    "see_also",
    "work_ref",
    "worker_name",
    "recorded_at",
)


@dataclass(frozen=True)
class JournalIndexExclusion:
    code: str
    message: str
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


def parse_source_repositories(values: Iterable[str]) -> dict[str, str]:
    repositories: dict[str, str] = {}
    for raw in values:
        alias, separator, root = str(raw or "").partition("=")
        alias = alias.strip()
        root = root.strip()
        if not separator or not REPOSITORY_ALIAS_RE.fullmatch(alias) or not root:
            raise DomainError(
                "journal_repository_map_invalid",
                "Source repositories use NAME=/absolute/checkout/path.",
                details={"value": str(raw or "")},
            )
        repositories[alias] = root
    return repositories


# Remote and branch each start with a letter or digit: a leading "-" would
# reach git fetch and checkout as an option.
_READ_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._/-]*$")


def repository_entry(alias: str, raw: Any) -> tuple[str, str, str]:
    """(root, read_root, read_ref) of one repository map entry.

    An entry is a checkout path, or ``{"root": ..., "read_root": ...,
    "read_ref": ...}`` where ``read_root`` is the dedicated, never-edited
    worktree the project's setup is read from (W262). Paths are returned as
    written; ``read_ref`` defaults to ``origin/main`` when a read root is set.
    """

    if not isinstance(raw, Mapping):
        return str(raw or "").strip(), "", ""
    root = str(raw.get("root") or "").strip()
    read_root = str(raw.get("read_root") or "").strip()
    read_ref = str(raw.get("read_ref") or "").strip()
    if read_ref and not read_root:
        raise DomainError(
            "journal_repository_map_invalid",
            "read_ref is set only beside a read_root.",
            details={"alias": str(alias)},
        )
    if read_root:
        read_ref = read_ref or DEFAULT_READ_REF
        if not _READ_REF_RE.fullmatch(read_ref) or ".." in read_ref:
            raise DomainError(
                "journal_repository_map_invalid",
                "read_ref names a remote-tracking ref such as origin/main.",
                details={"alias": str(alias)},
            )
    return root, read_root, read_ref


def repository_entries(
    roots: Mapping[str, str] | Iterable[tuple[str, str]],
    reads: Iterable[tuple[str, str, str]] = (),
) -> dict[str, Any]:
    """The mapping ``RepositoryMap.from_mapping`` takes, from a config's two tuples."""

    pairs = roots.items() if isinstance(roots, Mapping) else roots
    entries: dict[str, Any] = {str(alias): root for alias, root in pairs}
    for alias, read_root, read_ref in reads:
        if alias in entries:
            entries[alias] = {
                "root": entries[alias],
                "read_root": read_root,
                "read_ref": read_ref,
            }
    return entries


@dataclass(frozen=True)
class RepositoryMap:
    roots: Mapping[str, Path]
    # Aliases whose checkout is not on this machine yet. A relay records them
    # instead of refusing the whole map, so a worker's channel opens without
    # any journal checkout, and only a use of that alias is refused (W304 D13).
    missing: Mapping[str, Path] = field(default_factory=dict)
    # W262: per alias, the never-edited worktree a project's setup is read
    # from, and the ref it follows. A read root that is not there is kept and
    # checked on each use; reads then fall back to the root and the lag is
    # named, never fatal.
    read_roots: Mapping[str, Path] = field(default_factory=dict)
    read_refs: Mapping[str, str] = field(default_factory=dict)
    # W343: the calling worker's own workspace. When set, every alias resolves
    # to <workspace>/<alias>, the clone project-workspace.md tells the worker
    # to keep, and nothing else is consulted: no host checkout, no read root.
    workspace: Path | None = None

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "RepositoryMap":
        """Resolve every alias inside one worker's workspace (W343).

        A worker reads and writes project state only through its own clone of
        each repository. A clone that is not there is refused on use, naming
        the folder and the step that creates it; no other checkout stands in.
        """

        text = str(workspace or "").strip()
        if not text:
            raise DomainError(
                "journal_worker_workspace_missing",
                "This worker has no workspace, so it has no clone to read the "
                "project from: the host approves no work root. Ask the operator "
                "to add one (`pb host configure --add-allow-root <path>`).",
            )
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise DomainError(
                "journal_repository_root_invalid",
                "A worker workspace must be an absolute path.",
            )
        return cls(roots={}, workspace=path.resolve())

    def clone(self, alias: str) -> Path | None:
        """Where the alias lives for this map: the workspace clone, else the mapped root."""

        if self.workspace is not None:
            if not REPOSITORY_ALIAS_RE.fullmatch(alias) or alias in {".", ".."}:
                return None
            return self.workspace / alias
        return self.roots.get(alias) or self.missing.get(alias)

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, Any], *, require_existing: bool = True
    ) -> "RepositoryMap":
        """Map aliases to checkouts.

        With `require_existing` (configuration and the journal commands), a
        missing checkout is refused at once, so a typo never becomes a mapping.
        The relay passes False: a checkout that is not there yet is recorded and
        refused only when a journal actually needs it.
        """

        roots: dict[str, Path] = {}
        missing: dict[str, Path] = {}
        read_roots: dict[str, Path] = {}
        read_refs: dict[str, str] = {}
        for alias, raw in values.items():
            if not REPOSITORY_ALIAS_RE.fullmatch(str(alias)):
                raise DomainError(
                    "journal_repository_alias_invalid",
                    "Repository aliases use letters, digits, dot, underscore, or hyphen.",
                    details={"alias": str(alias)},
                )
            value, read_value, read_ref = repository_entry(str(alias), raw)
            if read_value:
                read_root = Path(read_value).expanduser()
                if not read_root.is_absolute():
                    raise DomainError(
                        "journal_repository_root_invalid",
                        "LOCAL repository read roots must be absolute paths.",
                        details={"alias": str(alias)},
                    )
                read_roots[str(alias)] = read_root.resolve()
                read_refs[str(alias)] = read_ref
            root = Path(str(value or "")).expanduser()
            if not root.is_absolute():
                raise DomainError(
                    "journal_repository_root_invalid",
                    "LOCAL repository roots must be absolute paths.",
                    details={"alias": str(alias)},
                )
            resolved = root.resolve()
            if not resolved.is_dir():
                if not require_existing:
                    missing[str(alias)] = resolved
                    continue
                raise DomainError(
                    "journal_repository_root_missing",
                    "A mapped LOCAL repository checkout does not exist.",
                    details={"alias": str(alias)},
                )
            roots[str(alias)] = resolved
        return cls(
            roots=roots, missing=missing, read_roots=read_roots, read_refs=read_refs
        )

    def read_root(self, alias: str) -> Path | None:
        """The alias's read root when it is set and present on this host."""

        read_root = self.read_roots.get(alias)
        return read_root if read_root is not None and read_root.is_dir() else None

    def resolve_read(
        self, value: str, *, require_directory: bool = True
    ) -> tuple[RepositoryRef, Path]:
        """Like ``resolve``, for reading a project's setup (W262).

        Resolves against the alias's read root when it has one on this host,
        else against its root. Never creates anything.
        """

        reference = parse_repository_ref(value)
        read_root = self.read_root(reference.repository)
        if read_root is None:
            return self.resolve(value, require_directory=require_directory)
        return reference, self._within(
            reference, read_root, create=False, require_directory=require_directory
        )

    def resolve(
        self, value: str, *, create: bool = False, require_directory: bool = True
    ) -> tuple[RepositoryRef, Path]:
        reference = parse_repository_ref(value)
        if self.workspace is not None:
            return reference, self._within(
                reference,
                self._workspace_clone(reference.repository),
                create=create,
                require_directory=require_directory,
            )
        root = self.roots.get(reference.repository)
        if root is None and reference.repository in self.missing:
            # Checked again on every use: a checkout cloned after the relay
            # started is used from its next cycle, without a restart.
            late = self.missing[reference.repository]
            if late.is_dir():
                root = late
        if root is None and reference.repository in self.missing:
            raise DomainError(
                "journal_repository_root_missing",
                "A mapped LOCAL repository checkout does not exist.",
                details={
                    "alias": reference.repository,
                    "repository": reference.repository,
                    "path": str(self.missing[reference.repository]),
                },
            )
        if root is None:
            raise DomainError(
                "journal_repository_unmapped",
                "The portable ref has no LOCAL repository checkout mapping.",
                details={"repository": reference.repository},
            )
        return reference, self._within(
            reference, root, create=create, require_directory=require_directory
        )

    def _workspace_clone(self, alias: str) -> Path:
        clone = self.clone(alias)
        if clone is None:
            raise DomainError(
                "journal_repository_unmapped",
                "The portable ref names no repository folder a workspace can hold.",
                details={"repository": alias},
            )
        if not (clone / ".git").exists():
            raise DomainError(
                "journal_repository_root_missing",
                f"{alias} is not cloned in this worker's workspace at {clone}. "
                "Clone it there as project-workspace.md step 2 says (the "
                "repository's URL is on the project card, `pb worker context` "
                "`repositories`), then run `pb worker workspace-report`. No "
                "other checkout is read in its place.",
                details={"alias": alias, "repository": alias, "path": str(clone)},
            )
        return clone.resolve()

    @staticmethod
    def _within(
        reference: RepositoryRef, root: Path, *, create: bool, require_directory: bool
    ) -> Path:
        candidate = (root / reference.path).resolve(strict=False)
        if candidate == root or root not in candidate.parents:
            raise DomainError(
                "journal_repository_ref_escape",
                "The portable ref resolves outside its mapped repository checkout.",
                details={"repository": reference.repository},
            )
        if create:
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
            candidate = candidate.resolve()
            if root not in candidate.parents:
                raise DomainError(
                    "journal_repository_ref_escape",
                    "The journal home traverses a link outside its mapped repository checkout.",
                )
        elif require_directory and not candidate.is_dir():
            raise DomainError(
                "journal_home_missing",
                "The bound journal home is absent from this LOCAL checkout.",
                details={"journal_home_ref": str(reference)},
            )
        return candidate


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, text
    loaded = yaml.safe_load(text[4:marker]) or {}
    if not isinstance(loaded, Mapping):
        return {}, text
    return dict(loaded), text[marker + 5 :]


def _list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    if str(value or "").strip():
        return (str(value),)
    return ()


WORKER_JOURNALS_DIRECTORY = "workers"
_WORKER_FOLDER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,199}")


def worker_journal_root(journal_workspace_root: str | Path, worker_name: str) -> Path:
    """One worker's journal catalog, links and index under the host's journal root (W343).

    Each worker indexes its own clone, so two workers on different commits
    never share an index built from one of them.
    """

    name = str(worker_name or "").strip().lower()
    if not _WORKER_FOLDER_RE.fullmatch(name):
        raise DomainError(
            "journal_worker_name_invalid",
            "A worker's journal folder is named by its stable worker name.",
            details={"worker_name": name},
        )
    return Path(journal_workspace_root).expanduser() / WORKER_JOURNALS_DIRECTORY / name


class JournalWorkspace:
    """LOCAL aggregate of Git-backed project journals and its disposable index."""

    def __init__(self, root: str | Path, repositories: RepositoryMap) -> None:
        self.root = Path(root).expanduser().resolve()
        self.repositories = repositories
        self.control = self.root / ".problem-board"
        self.catalog_path = self.control / "catalog.json"
        self.index = JournalSearchIndex(self.control / "index" / "journals.sqlite3")
        self.index_status_path = self.control / "index" / "journal-status.json"

    def initialize(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "projects").mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "workspaces").mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_lock(self.control / "locks" / "catalog.lock"):
            catalog = read_json(self.catalog_path, required=False)
            if catalog:
                if catalog.get("schema") != WORKSPACE_SCHEMA:
                    raise DomainError(
                        "journal_workspace_schema_invalid",
                        "The LOCAL journal workspace schema is unsupported.",
                    )
                return catalog
            catalog = {
                "schema": WORKSPACE_SCHEMA,
                "bindings": {},
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            atomic_write_json(self.catalog_path, catalog)
            return catalog

    def catalog(self) -> dict[str, Any]:
        return self.initialize()

    @staticmethod
    def _binding(value: Mapping[str, Any]) -> dict[str, Any]:
        project_ref = str(value.get("project_ref") or "")
        if parse_ref(project_ref).kind != "project":
            raise DomainError(
                "journal_project_ref_invalid", "Expected a work:project reference."
            )
        journal_home_ref = normalize_repository_ref(
            str(value.get("journal_home_ref") or ""), field="journal_home_ref"
        )
        artifact = str(value.get("project_artifact_ref") or "").strip()
        return {
            "project_ref": project_ref,
            "journal_home_ref": journal_home_ref,
            "project_artifact_ref": (
                normalize_repository_ref(artifact, field="project_artifact_ref")
                if artifact
                else ""
            ),
            "revision": int(value.get("revision") or 0),
        }

    def _link(self, binding: Mapping[str, Any], target: Path, *, group: str) -> Path:
        project_id = parse_ref(str(binding["project_ref"])).object_id
        destination = self.root / group / project_id
        if destination.exists() and not destination.is_symlink():
            raise DomainError(
                "journal_workspace_link_conflict",
                "A non-link already occupies the LOCAL project journal slot.",
                details={"project_ref": binding["project_ref"]},
            )
        if destination.is_symlink() and destination.resolve(strict=False) == target:
            return destination
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            os.symlink(target, temporary, target_is_directory=True)
            os.replace(temporary, destination)
        finally:
            if temporary.is_symlink():
                temporary.unlink()
        return destination

    def _remove_link(self, binding: Mapping[str, Any], *, group: str) -> None:
        project_id = parse_ref(str(binding["project_ref"])).object_id
        destination = self.root / group / project_id
        if destination.is_symlink():
            destination.unlink()
        elif destination.exists():
            raise DomainError(
                "journal_workspace_link_conflict",
                "A non-link occupies a LOCAL project slot managed by the journal workspace.",
                details={"project_ref": binding["project_ref"]},
            )

    def reconcile(
        self, value: Mapping[str, Any], *, create_home: bool = False, rebuild: bool = True
    ) -> dict[str, Any]:
        binding = self._binding(value)
        try:
            _, target = self.repositories.resolve(
                binding["journal_home_ref"], create=create_home
            )
        except DomainError as exc:
            if exc.code != "journal_repository_root_missing":
                raise
            # W343: the binding is kept while the worker's clone is missing, so
            # `pb worker context` names the missing clone and its step instead
            # of calling the project unbound.
            self._record_binding(binding)
            raise
        artifact_target: Path | None = None
        if binding.get("project_artifact_ref"):
            _, artifact_target = self.repositories.resolve(
                str(binding["project_artifact_ref"])
            )
        (target / "journal").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.initialize()
        with exclusive_lock(self.control / "locks" / "catalog.lock"):
            catalog = read_json(self.catalog_path)
            bindings = dict(catalog.get("bindings") or {})
            changed = bindings.get(binding["project_ref"]) != binding
            bindings[binding["project_ref"]] = binding
            catalog.update(bindings=bindings, updated_at=utc_now())
            atomic_write_json(self.catalog_path, catalog)
            link = self._link(binding, target, group="projects")
            artifact_link = (
                self._link(binding, artifact_target, group="workspaces")
                if artifact_target is not None
                else None
            )
            if artifact_link is None:
                self._remove_link(binding, group="workspaces")
        indexed = self.rebuild_index() if rebuild and (changed or not self.index.path.exists()) else None
        return {
            **binding,
            "local_home": str(target),
            "local_link": str(link),
            "local_project_artifact": str(artifact_target) if artifact_target else "",
            "local_workspace_link": str(artifact_link) if artifact_link else "",
            "changed": changed,
            "indexed_entries": indexed,
        }

    def _record_binding(self, binding: Mapping[str, Any]) -> None:
        self.initialize()
        with exclusive_lock(self.control / "locks" / "catalog.lock"):
            catalog = read_json(self.catalog_path)
            bindings = dict(catalog.get("bindings") or {})
            if bindings.get(binding["project_ref"]) == dict(binding):
                return
            bindings[binding["project_ref"]] = dict(binding)
            catalog.update(bindings=bindings, updated_at=utc_now())
            atomic_write_json(self.catalog_path, catalog)

    def clone_stamp(self, project_ref: str) -> dict[str, Any]:
        """The commit a view of this project's journal was read at, and its clone state (W343)."""

        catalog = read_json(self.catalog_path, required=False) or {}
        binding = dict((catalog.get("bindings") or {}).get(project_ref) or {})
        if not binding:
            return {}
        alias = parse_repository_ref(str(binding["journal_home_ref"])).repository
        clone = self.repositories.clone(alias)
        stamp: dict[str, Any] = {"journal_home_commit": head_commit(clone)}
        if self.repositories.workspace is not None:
            stamp["journal_clone"] = clone_state(alias, clone)
        return stamp

    def _bound(
        self,
        project_ref: str,
        *,
        initialize: bool = True,
    ) -> tuple[dict[str, Any], RepositoryRef, Path]:
        catalog = (
            self.catalog()
            if initialize
            else read_json(self.catalog_path, required=False)
        )
        binding = dict((catalog.get("bindings") or {}).get(project_ref) or {})
        if not binding:
            raise DomainError(
                "journal_project_unbound",
                "This project has no binding in the LOCAL journal workspace.",
                details={"project_ref": project_ref},
            )
        reference, path = self.repositories.resolve(str(binding["journal_home_ref"]))
        return binding, reference, path

    def _local_file(self, ref: str) -> str:
        _, path = self.repositories.resolve(ref, require_directory=False)
        return str(path)

    def _read_file(self, ref: str, used: set[str] | None = None) -> str:
        """A setup file on this host, from its alias's read root when set (W262)."""

        reference, path = self.repositories.resolve_read(ref, require_directory=False)
        if used is not None:
            used.add(reference.repository)
        return str(path)

    def _read_home(self, home_ref: RepositoryRef, journal_home: Path) -> tuple[Path, str, list[str]]:
        """Where the project's setup is read: (home, read root used, issues).

        The journal home inside the alias's read root when one is set and
        holds it; otherwise the journal home in the root checkout, which is
        where every write goes.
        """

        read_root = self.repositories.read_root(home_ref.repository)
        if read_root is None:
            return journal_home, "", []
        _, home = self.repositories.resolve_read(str(home_ref), require_directory=False)
        if not home.is_dir():
            return (
                journal_home,
                "",
                [
                    f"journal home {home_ref} is absent from the read root of "
                    f"{home_ref.repository} at {read_root}; the setup was read "
                    f"from its root checkout instead"
                ],
            )
        return home, str(read_root), []

    def context(self, project_ref: str, *, journal_branch: str = "") -> dict[str, Any]:
        binding, home_ref, journal_home = self._bound(project_ref)
        project_artifact = ""
        if binding.get("project_artifact_ref"):
            _, artifact = self.repositories.resolve(
                str(binding["project_artifact_ref"])
            )
            project_artifact = str(artifact)
        project_id = parse_ref(project_ref).object_id
        journal_directory = journal_home / "journal"
        # W262: the setup, facts, environment, instructions and profiles are
        # read from the alias's read root, a never-edited worktree the relay
        # keeps at its integration ref, when the host maps one. Writes (the
        # journal directory, indexing, reconcile) stay on the root checkout.
        read_home, read_root, read_issues = self._read_home(home_ref, journal_home)
        used: set[str] = {home_ref.repository}
        # The standing facts of a project (hosts, agents, release), kept at the
        # journal home's root. Named only when the page exists (W262).
        facts = read_home / PROJECT_FACTS_FILE
        has_facts = facts.is_file()
        environment = read_home / PROJECT_ENVIRONMENT_FILE
        has_environment = environment.is_file()
        # The project's declared setup: its instructions file and runtimes
        # (W262). By hand in the journal home until the Control Card holds it.
        setup = read_project_setup(
            read_home / PROJECT_SETUP_FILE,
            setup_ref=self._portable_child(home_ref, PurePosixPath(PROJECT_SETUP_FILE)),
            resolve=lambda ref: self._read_file(ref, used),
        )
        # A lag is named, never fatal: local refs only, no fetch here.
        issues = list(setup.get("project_setup_issues") or []) + read_issues
        for alias in sorted(used):
            configured = self.repositories.read_roots.get(alias)
            if configured is None:
                continue
            lag = read_root_lag(
                alias,
                configured,
                self.repositories.read_refs.get(alias) or DEFAULT_READ_REF,
            )
            if lag:
                issues.append(lag)
        commit_tree = (
            Path(read_root) if read_root else self.repositories.clone(home_ref.repository)
        )
        # W343: the worker's own clone, and how current it is. A lag is named
        # with the step that fixes it; no other checkout is read instead.
        clone = (
            clone_state(
                home_ref.repository,
                self.repositories.clone(home_ref.repository),
                branch=journal_branch,
            )
            if self.repositories.workspace is not None
            else {}
        )
        if clone.get("action"):
            issues.append(str(clone["action"]))
        setup["project_setup_issues"] = issues
        return {
            **binding,
            **setup,
            "journal_home_commit": head_commit(commit_tree),
            **({"journal_clone": clone} if clone else {}),
            "journal_home_read_root": read_root,
            "local_journal_home": str(journal_home),
            "local_journal_directory": str(journal_directory),
            "project_facts_ref": (
                self._portable_child(home_ref, PurePosixPath(PROJECT_FACTS_FILE))
                if has_facts
                else ""
            ),
            "local_project_facts": str(facts) if has_facts else "",
            "project_environment_ref": (
                self._portable_child(
                    home_ref, PurePosixPath(PROJECT_ENVIRONMENT_FILE)
                )
                if has_environment
                else ""
            ),
            "local_project_environment": (
                str(environment) if has_environment else ""
            ),
            "local_project_artifact": project_artifact,
            "local_journal_link": str(self.root / "projects" / project_id),
            "local_workspace_link": (
                str(self.root / "workspaces" / project_id)
                if project_artifact
                else ""
            ),
            "journal_authoring": {
                "author": "agent",
                "body": "Free-form Markdown shaped for the work and its future reader.",
                "directory_ref": self._portable_child(
                    home_ref, PurePosixPath("journal")
                ),
                "required_frontmatter": {
                    "entry_ref": (
                        "A unique work:journal:<created-at>:<entry-id>:"
                        "<semantic-name> reference."
                    ),
                    "project_ref": project_ref,
                },
                "retrieval_frontmatter": list(JOURNAL_RETRIEVAL_FRONTMATTER),
                "rule": (
                    "The agent writes the complete file, including front matter. "
                    "Problem Board does not generate its filename, tags, sections, or body."
                ),
            },
            "journal_index": self.index_status(),
        }

    @staticmethod
    def _portable_child(reference: RepositoryRef, relative: PurePosixPath) -> str:
        path = PurePosixPath(reference.path) / relative
        return f"repo:{reference.repository}/{path.as_posix()}"

    def index_entry(
        self, *, project_ref: str, repository_journal_ref: str
    ) -> dict[str, Any]:
        """Index one journal file the agent has already authored in Git."""

        entry = self.prepare_entry(
            project_ref=project_ref,
            repository_journal_ref=repository_journal_ref,
        )
        self.rebuild_index()
        return entry

    def prepare_entry(
        self,
        *,
        project_ref: str,
        repository_journal_ref: str,
        readonly: bool = False,
    ) -> dict[str, Any]:
        """Parse and validate one authored entry without changing the index."""

        _, home_ref, home = self._bound(project_ref, initialize=not readonly)
        reference, path = self.repositories.resolve(
            repository_journal_ref, require_directory=False
        )
        journal_directory = (home / "journal").resolve()
        resolved_path = path.resolve()
        if not resolved_path.is_file():
            raise DomainError(
                "journal_entry_not_found",
                f"Journal entry {reference} does not resolve to a LOCAL file.",
                status=404,
                details={"repository_journal_ref": str(reference)},
            )
        if not resolved_path.is_relative_to(journal_directory):
            raise DomainError(
                "journal_entry_outside_directory",
                "A project journal entry must be inside its bound journal directory.",
                details={"repository_journal_ref": str(reference)},
                status=403,
            )
        document = self._document_from_path(
            project_ref=project_ref,
            home_ref=home_ref,
            home=home,
            path=resolved_path,
            allow_legacy=False,
        )
        if isinstance(document, JournalIndexExclusion):
            raise DomainError(
                document.code,
                document.message,
                details=document.details,
            )
        return {**document.metadata(), "local_path": str(resolved_path)}

    def indexed_entry_state(
        self,
        *,
        entry_ref: str,
        content_hash: str,
    ) -> dict[str, Any]:
        """Observe whether the disposable index holds this exact entry version."""

        observed = self.index.inspect(entry_ref)
        metadata = dict(observed.get("metadata") or {})
        observed_hash = str(metadata.get("content_hash") or "")
        if observed.get("state") != "indexed":
            return {
                **observed,
                "matches": False,
                "expected_content_hash": str(content_hash or ""),
            }
        matches = observed_hash == str(content_hash or "")
        return {
            "state": "matched" if matches else "content_mismatch",
            "matches": matches,
            "entry_ref": str(entry_ref or ""),
            "content_hash": observed_hash,
            "expected_content_hash": str(content_hash or ""),
            "repository_journal_ref": str(
                metadata.get("repository_journal_ref") or ""
            ),
        }

    def _scan_documents(
        self,
    ) -> tuple[
        list[JournalDocument],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        documents: list[JournalDocument] = []
        issues: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        bindings = self.catalog().get("bindings") or {}
        for project_ref, raw_binding in sorted(bindings.items()):
            binding = dict(raw_binding or {})
            try:
                home_ref, home = self.repositories.resolve(
                    str(binding["journal_home_ref"])
                )
            except DomainError as exc:
                if exc.code != "journal_repository_root_missing":
                    raise
                issues.append(exc.to_dict())
                continue
            for path in sorted((home / "journal").glob("**/*.md")):
                try:
                    document = self._document_from_path(
                        project_ref=project_ref,
                        home_ref=home_ref,
                        home=home,
                        path=path,
                        allow_legacy=True,
                    )
                except DomainError as exc:
                    issues.append(exc.to_dict())
                    continue
                if isinstance(document, JournalIndexExclusion):
                    exclusions.append(document.to_dict())
                    continue
                documents.append(document)
                issues.extend(dict(issue) for issue in document.index_issues)
        return documents, issues, exclusions

    def index_status(self) -> dict[str, Any]:
        status = read_json(self.index_status_path, required=False)
        if status:
            return {
                **status,
                "excluded_count": int(status.get("excluded_count") or 0),
                "exclusions": list(status.get("exclusions") or []),
            }
        return {
            "schema": JOURNAL_INDEX_STATUS_SCHEMA,
            "state": "not_built",
            "indexed_entries": 0,
            "issue_count": 0,
            "issues": [],
            "excluded_count": 0,
            "exclusions": [],
            "recorded_at": "",
        }

    def _record_index_status(
        self,
        *,
        indexed_entries: int,
        issues: Sequence[Mapping[str, Any]],
        exclusions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        status = {
            "schema": JOURNAL_INDEX_STATUS_SCHEMA,
            "state": "ready_with_issues" if issues else "ready",
            "indexed_entries": int(indexed_entries),
            "issue_count": len(issues),
            "issues": [dict(issue) for issue in issues],
            "excluded_count": len(exclusions),
            "exclusions": [dict(exclusion) for exclusion in exclusions],
            "recorded_at": utc_now(),
        }
        atomic_write_json(self.index_status_path, status)
        return status

    def _document_from_path(
        self,
        *,
        project_ref: str,
        home_ref: RepositoryRef,
        home: Path,
        path: Path,
        allow_legacy: bool = False,
    ) -> JournalDocument | JournalIndexExclusion:
        text = path.read_text(encoding="utf-8")
        relative = PurePosixPath(path.relative_to(home).as_posix())
        repository_journal_ref = self._portable_child(home_ref, relative)
        issues: list[dict[str, Any]] = []
        try:
            metadata, body = _frontmatter(text)
        except yaml.YAMLError as exc:
            error = DomainError(
                "journal_entry_frontmatter_invalid",
                "The journal entry front matter is not valid YAML.",
                details={
                    "repository_journal_ref": repository_journal_ref,
                    "error": str(exc),
                },
            )
            if not allow_legacy:
                raise error from exc
            issues.append(error.to_dict())
            metadata, body = {}, text
        if path.name == "chronicles.md" and not any(
            metadata.get(field) for field in JOURNAL_INDEX_REQUIRED_FRONTMATTER
        ):
            return JournalIndexExclusion(
                code="journal_row_chronicle_excluded",
                message=(
                    f"{repository_journal_ref} is a row-only chronicle index, "
                    "not a journal entry."
                ),
                details={
                    "repository_journal_ref": repository_journal_ref,
                    "reason": "row_only_chronicle",
                },
            )
        missing = [
            field
            for field in JOURNAL_INDEX_REQUIRED_FRONTMATTER
            if not str(metadata.get(field) or "").strip()
        ]
        if missing and not allow_legacy:
            raise DomainError(
                "journal_entry_metadata_missing",
                "A journal entry needs entry_ref and project_ref front matter before it can be indexed.",
                details={
                    "repository_journal_ref": repository_journal_ref,
                    "missing": missing,
                },
            )
        if missing:
            issues.append(
                DomainError(
                    "journal_entry_metadata_missing",
                    "A legacy journal entry is indexed with identity derived from its bound path.",
                    details={
                        "repository_journal_ref": repository_journal_ref,
                        "missing": missing,
                    },
                ).to_dict()
            )

        entry_ref = str(metadata.get("entry_ref") or "")
        parsed_entry = None
        if entry_ref:
            try:
                candidate = parse_ref(entry_ref)
                if candidate.kind == "journal" and candidate.is_canonical:
                    parsed_entry = candidate
                elif candidate.kind == "journal" and allow_legacy:
                    parsed_entry = candidate
                    issues.append(
                        DomainError(
                            "journal_entry_ref_legacy",
                            "A legacy journal entry is indexed without rewriting its historical file.",
                            details={
                                "repository_journal_ref": repository_journal_ref,
                                "entry_ref": entry_ref,
                            },
                        ).to_dict()
                    )
            except DomainError:
                pass
        if parsed_entry is None and not allow_legacy:
            raise DomainError(
                "journal_entry_ref_invalid",
                (
                    "Journal entry_ref must use "
                    "work:journal:<created-at>:<entry-id>:<semantic-name>."
                ),
                details={"repository_journal_ref": repository_journal_ref},
            )
        if parsed_entry is None:
            entry_ref = (
                "work:journal:legacy-"
                + hashlib.sha256(repository_journal_ref.encode("utf-8")).hexdigest()[:32]
            )
            if "entry_ref" not in missing:
                issues.append(
                    DomainError(
                        "journal_entry_ref_invalid",
                        "A legacy journal entry is indexed with identity derived from its bound path.",
                        details={
                            "repository_journal_ref": repository_journal_ref,
                            "entry_ref": str(metadata.get("entry_ref") or ""),
                        },
                    ).to_dict()
                )

        entry_project_ref = str(metadata.get("project_ref") or project_ref)
        if entry_project_ref != project_ref:
            raise DomainError(
                "journal_entry_project_mismatch",
                "The journal entry front matter names a different project.",
                details={
                    "repository_journal_ref": repository_journal_ref,
                    "expected_project_ref": project_ref,
                    "entry_project_ref": entry_project_ref,
                },
            )
        return JournalDocument(
            entry_ref=str(parsed_entry) if parsed_entry is not None else entry_ref,
            project_ref=project_ref,
            work_ref=str(metadata.get("work_ref") or ""),
            worker_name=str(metadata.get("worker_name") or ""),
            title=str(metadata.get("title") or path.stem),
            summary=str(metadata.get("summary") or ""),
            status=str(metadata.get("status") or ""),
            tags=_list(metadata.get("tags")),
            keywords=_list(metadata.get("keywords")),
            see_also=_list(metadata.get("see_also")),
            repository_journal_ref=repository_journal_ref,
            source_path=str(path),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            recorded_at=str(metadata.get("recorded_at") or ""),
            frontmatter=yaml.safe_dump(
                metadata, sort_keys=False, allow_unicode=True
            ).rstrip(),
            body=body,
            index_issues=tuple(issues),
        )

    @staticmethod
    def _catalog_row(document: JournalDocument) -> dict[str, Any]:
        return {
            key: value
            for key, value in document.metadata().items()
            if key != "source_path"
        } | {"snippet": document.summary}

    @staticmethod
    def _matches_catalog_filters(
        document: JournalDocument,
        *,
        worker_name: str,
        date_from: str,
        date_to: str,
        statuses: Sequence[str] = (),
    ) -> bool:
        recorded = str(document.recorded_at or "")[:10]
        return (
            (not worker_name or document.worker_name == worker_name)
            and (not date_from or bool(recorded and recorded >= date_from))
            and (not date_to or bool(recorded and recorded <= date_to))
            and (not statuses or document.status.lower() in statuses)
        )

    @staticmethod
    def _decode_catalog_cursor(
        codec: ScopedKeysetCursor, cursor: str
    ) -> tuple[Any, ...]:
        try:
            return codec.decode(cursor)
        except CollectionError as exc:
            raise DomainError(exc.code, exc.message) from exc

    def rebuild_index(self) -> int:
        """Bring the index in line with what Git holds.

        Named rebuild for its callers, but incremental underneath: only entries
        whose text changed are re-indexed, and entries that left Git are
        deleted. Opening the panel no longer costs a full re-read of every
        journal the project has ever had.
        """

        documents, issues, exclusions = self._scan_documents()
        indexed_entries = self.index.sync(documents)
        self._record_index_status(
            indexed_entries=indexed_entries,
            issues=issues,
            exclusions=exclusions,
        )
        return indexed_entries

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        return self.index.search(query, **filters)

    def view_catalog(
        self, *, project_ref: str, query: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.view_catalog_page(
            project_ref=project_ref,
            query=query,
            limit=limit,
        )["entries"]

    def view_catalog_page(
        self,
        *,
        project_ref: str,
        query: str = "",
        cursor: str = "",
        limit: int = 25,
        worker_name: str = "",
        date_from: str = "",
        date_to: str = "",
        statuses: Sequence[str] = (),
        weights: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read one current page from Git, using the index only for a search."""

        page_limit = max(1, min(int(limit), 100))
        clean_query = str(query or "").strip()
        clean_worker = str(worker_name or "").strip().lower()
        clean_from = str(date_from or "").strip()
        clean_to = str(date_to or "").strip()
        clean_statuses = tuple(sorted({str(value or "").strip().lower() for value in statuses if str(value or "").strip()}))
        raw_weights = dict(weights or {})
        rank_values = {"semantic": 0.0, "lexical": 1.0, "recency": 0.5}
        for arm in rank_values:
            if arm not in raw_weights:
                continue
            try:
                value = float(raw_weights[arm])
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    "journal_search_weights_invalid",
                    "Journal rank weights must be finite numbers from 0 to 2.",
                ) from exc
            if not math.isfinite(value) or not 0.0 <= value <= 2.0:
                raise DomainError(
                    "journal_search_weights_invalid",
                    "Journal rank weights must be finite numbers from 0 to 2.",
                )
            rank_values[arm] = value
        if rank_values["semantic"] != 0.0:
            raise DomainError(
                "journal_semantic_search_unavailable",
                "Git-backed journals have lexical and recency indexes; semantic ranking is unavailable.",
            )
        if rank_values["lexical"] <= 0.0 and rank_values["recency"] <= 0.0:
            raise DomainError(
                "journal_search_weights_invalid",
                "Lexical or recency ranking must have a positive weight.",
            )
        rank_weights = FusionWeights(**rank_values)
        _, home_ref, home = self._bound(project_ref)
        cursor_query = {
            "query": clean_query,
            "worker_name": clean_worker,
            "date_from": clean_from,
            "date_to": clean_to,
            "statuses": list(clean_statuses),
            "weights": rank_values,
        }

        if not clean_query:
            codec = ScopedKeysetCursor(
                scope={"project_ref": project_ref, "journal_home_ref": str(home_ref)},
                query={**cursor_query, "mode": "browse"},
                key_fields=("repository_path",),
            )
            boundary = ""
            if cursor:
                decoded_boundary = self._decode_catalog_cursor(codec, cursor)[0]
                if not isinstance(decoded_boundary, str) or not decoded_boundary:
                    raise DomainError(
                        "collection_invalid_cursor",
                        "The journal cursor does not contain a repository path.",
                    )
                boundary = decoded_boundary
            paths = sorted(
                (home / "journal").glob("**/*.md"),
                key=lambda path: path.relative_to(home).as_posix(),
                reverse=True,
            )
            matches: list[tuple[str, JournalDocument]] = []
            page_issues: list[dict[str, Any]] = []
            page_exclusions: list[dict[str, Any]] = []
            for path in paths:
                relative = path.relative_to(home).as_posix()
                if boundary and relative >= boundary:
                    continue
                try:
                    document = self._document_from_path(
                        project_ref=project_ref,
                        home_ref=home_ref,
                        home=home,
                        path=path,
                        allow_legacy=True,
                    )
                except DomainError as exc:
                    page_issues.append(exc.to_dict())
                    continue
                if isinstance(document, JournalIndexExclusion):
                    page_exclusions.append(document.to_dict())
                    continue
                page_issues.extend(dict(issue) for issue in document.index_issues)
                if not self._matches_catalog_filters(
                    document,
                    worker_name=clean_worker,
                    date_from=clean_from,
                    date_to=clean_to,
                    statuses=clean_statuses,
                ):
                    continue
                matches.append((relative, document))
                if len(matches) > page_limit:
                    break
            visible = matches[:page_limit]
            next_cursor = (
                codec.encode((visible[-1][0],))
                if len(matches) > page_limit and visible
                else ""
            )
            return {
                "entries": [self._catalog_row(document) for _, document in visible],
                "next_cursor": next_cursor,
                "index_issues": page_issues,
                "index_exclusions": page_exclusions,
            }

        all_documents, index_issues, index_exclusions = self._scan_documents()
        indexed_entries = self.index.sync(all_documents)
        self._record_index_status(
            indexed_entries=indexed_entries,
            issues=index_issues,
            exclusions=index_exclusions,
        )
        documents = [
            document
            for document in all_documents
            if document.project_ref == project_ref
        ]
        generation = hashlib.sha256(
            "\n".join(
                f"{document.entry_ref}:{document.content_hash}"
                for document in sorted(documents, key=lambda value: value.entry_ref)
            ).encode("utf-8")
        ).hexdigest()
        codec = ScopedKeysetCursor(
            scope={"project_ref": project_ref, "journal_home_ref": str(home_ref)},
            query={**cursor_query, "mode": "search"},
            key_fields=("generation", "offset"),
        )
        offset = 0
        if cursor:
            cursor_generation, cursor_offset = self._decode_catalog_cursor(codec, cursor)
            if str(cursor_generation) != generation:
                raise DomainError(
                    "journal_catalog_cursor_stale",
                    "The project journal changed while these search results were open. Refresh the search.",
                    status=409,
                )
            if (
                isinstance(cursor_offset, bool)
                or not isinstance(cursor_offset, int)
                or cursor_offset < 0
            ):
                raise DomainError(
                    "collection_invalid_cursor",
                    "The journal search cursor does not contain a valid offset.",
                )
            offset = cursor_offset
        rows = self.index.search(
            clean_query,
            project_ref=project_ref,
            worker_name=clean_worker,
            limit=max(1, len(documents)),
            weights=rank_weights,
        )
        rows = [
            row
            for row in rows
            if (
                (not clean_from or str(row.get("recorded_at") or "")[:10] >= clean_from)
                and (not clean_to or str(row.get("recorded_at") or "")[:10] <= clean_to)
                and (not clean_statuses or str(row.get("status") or "").lower() in clean_statuses)
            )
        ]
        visible = rows[offset : offset + page_limit]
        next_offset = offset + len(visible)
        next_cursor = (
            codec.encode((generation, next_offset))
            if next_offset < len(rows) and visible
            else ""
        )
        return {
            "entries": [
                {key: value for key, value in row.items() if key not in {"source_path", "rank"}}
                for row in visible
            ],
            "next_cursor": next_cursor,
            "index_issues": index_issues,
            "index_exclusions": index_exclusions,
        }

    def read(self, repository_journal_ref: str) -> dict[str, Any]:
        reference, path = self.repositories.resolve(
            repository_journal_ref, require_directory=False
        )
        if not path.is_file():
            raise DomainError(
                "journal_entry_not_found",
                f"Journal entry {reference} does not resolve to a LOCAL file.",
                status=404,
                details={"repository_journal_ref": str(reference)},
            )
        text = path.read_text(encoding="utf-8")
        metadata, body = _frontmatter(text)
        return {
            "repository_journal_ref": str(reference),
            "metadata": metadata,
            "body": body,
            "content": text,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "local_path": str(path),
        }

    def read_for_project(
        self, *, project_ref: str, repository_journal_ref: str
    ) -> dict[str, Any]:
        _, _, home = self._bound(project_ref)
        result = self.read(repository_journal_ref)
        path = Path(str(result["local_path"])).resolve()
        if not path.is_relative_to(home.resolve()):
            raise DomainError(
                "journal_entry_outside_project",
                "The requested journal entry is outside this project's bound journal home.",
                status=403,
            )
        metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
        if str(metadata.get("project_ref") or "") != project_ref:
            raise DomainError(
                "journal_entry_project_mismatch",
                "The requested journal entry belongs to another project.",
                status=403,
            )
        result.pop("local_path", None)
        return result


__all__ = [
    "JOURNAL_INDEX_STATUS_SCHEMA",
    "JOURNAL_INDEX_REQUIRED_FRONTMATTER",
    "JOURNAL_RETRIEVAL_FRONTMATTER",
    "JournalWorkspace",
    "RepositoryMap",
    "WORKSPACE_SCHEMA",
    "parse_source_repositories",
    "repository_entries",
    "repository_entry",
    "worker_journal_root",
]
