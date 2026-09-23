from __future__ import annotations

import hashlib
import math
import os
import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RepositoryMap:
    roots: Mapping[str, Path]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RepositoryMap":
        roots: dict[str, Path] = {}
        for alias, raw in values.items():
            if not REPOSITORY_ALIAS_RE.fullmatch(str(alias)):
                raise DomainError(
                    "journal_repository_alias_invalid",
                    "Repository aliases use letters, digits, dot, underscore, or hyphen.",
                    details={"alias": str(alias)},
                )
            value = raw.get("root") if isinstance(raw, Mapping) else raw
            root = Path(str(value or "")).expanduser()
            if not root.is_absolute():
                raise DomainError(
                    "journal_repository_root_invalid",
                    "LOCAL repository roots must be absolute paths.",
                    details={"alias": str(alias)},
                )
            resolved = root.resolve()
            if not resolved.is_dir():
                raise DomainError(
                    "journal_repository_root_missing",
                    "A mapped LOCAL repository checkout does not exist.",
                    details={"alias": str(alias)},
                )
            roots[str(alias)] = resolved
        return cls(roots=roots)

    def resolve(
        self, value: str, *, create: bool = False, require_directory: bool = True
    ) -> tuple[RepositoryRef, Path]:
        reference = parse_repository_ref(value)
        root = self.roots.get(reference.repository)
        if root is None:
            raise DomainError(
                "journal_repository_unmapped",
                "The portable ref has no LOCAL repository checkout mapping.",
                details={"repository": reference.repository},
            )
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
        return reference, candidate


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
        _, target = self.repositories.resolve(
            binding["journal_home_ref"], create=create_home
        )
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

    def context(self, project_ref: str) -> dict[str, Any]:
        binding, home_ref, journal_home = self._bound(project_ref)
        project_artifact = ""
        if binding.get("project_artifact_ref"):
            _, artifact = self.repositories.resolve(
                str(binding["project_artifact_ref"])
            )
            project_artifact = str(artifact)
        project_id = parse_ref(project_ref).object_id
        journal_directory = journal_home / "journal"
        return {
            **binding,
            "local_journal_home": str(journal_home),
            "local_journal_directory": str(journal_directory),
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
            home_ref, home = self.repositories.resolve(str(binding["journal_home_ref"]))
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
]
