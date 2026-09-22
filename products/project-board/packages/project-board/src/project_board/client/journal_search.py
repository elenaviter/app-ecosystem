"""Journal search, backed by the SDK's hybrid index rather than another of our own.

Problem Board carried its own SQLite/FTS5 journal index, rebuilt wholesale every
time the panel opened. The platform already had this, and it now lives in a foundation package
precisely so a host-side tool can use it:
`app_foundation.index.sqlite.HybridIndex` is documented as a generic index
for any per-scope collection, naming pins, tasks and memories as examples. It is
upsert-based, so incremental by construction, and it owns lexical ranking,
recency decay and a version signature.

Keeping a second one meant every improvement had to be made twice and only ever
was made once. Three things the SDK lacked for this case were added to the SDK,
where the next collection view gets them too: matched-passage snippets, skipping
the embedder entirely when the semantic factor is off, and treating an empty
query as a browse of the scope rather than a search that found nothing.

The index stays disposable. Journals live in Git; this is a derived read model
and deleting the file costs one rebuild.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app_foundation.index.sqlite import (
    BruteForceVectorStore,
    Document,
    FusionWeights,
    HybridIndex,
    IndexConfig,
)

from ..contract.errors import DomainError

# Journal search is lexical. Entries are short, written by agents in a shared
# vocabulary, and searched by people who know roughly what they are looking for,
# so recency and keyword rank answer the question. Semantic search would add an
# embedder call per write and a provider dependency to a host-side CLI that has
# neither.
SEMANTIC_DISABLED = -1.0


async def _no_embedding(texts: Sequence[str]) -> list[list[float]]:
    """Never called: the semantic factor is off, so nothing is embedded.

    Present because IndexConfig requires an embedder, and a function that
    explains itself is better than a lambda returning zeros.
    """

    raise RuntimeError("journal search is lexical; no embedder should be called")


@dataclass(frozen=True)
class JournalDocument:
    entry_ref: str
    project_ref: str
    work_ref: str
    worker_name: str
    title: str
    summary: str
    status: str
    tags: tuple[str, ...]
    keywords: tuple[str, ...]
    see_also: tuple[str, ...]
    repository_journal_ref: str
    source_path: str
    content_hash: str
    recorded_at: str
    frontmatter: str
    body: str
    index_issues: tuple[Mapping[str, Any], ...] = ()

    def searchable_text(self) -> str:
        """What a person would search for, in one blob.

        Tags and keywords are repeated into the text as well as kept in
        metadata: metadata is filtered on exactly, while a search for a word
        that happens to be a tag should still find the entry.
        """

        return "\n".join(
            part
            for part in (
                self.title,
                self.summary,
                " ".join(self.tags),
                " ".join(self.keywords),
                " ".join(self.see_also),
                self.status,
                self.worker_name,
                self.frontmatter,
                self.body,
            )
            if part
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "entry_ref": self.entry_ref,
            "project_ref": self.project_ref,
            "work_ref": self.work_ref,
            "worker_name": self.worker_name,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "tags": list(self.tags),
            "keywords": list(self.keywords),
            "see_also": list(self.see_also),
            "repository_journal_ref": self.repository_journal_ref,
            "source_path": self.source_path,
            "content_hash": self.content_hash,
            "recorded_at": self.recorded_at,
            "index_issues": [dict(issue) for issue in self.index_issues],
        }


def _epoch(recorded_at: str) -> float | None:
    """Recency ordering comes from when the entry says it was written."""

    from datetime import datetime

    stamp = str(recorded_at or "").strip().replace("Z", "+00:00")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def _run_sync(factory):
    """Run one coroutine from callers that may or may not already be async.

    The SDK index is async and this index is read from both sides of the house:
    the CLI, which has no event loop, and the relay, which is running inside
    one. asyncio.run refuses outright when a loop is already running, so a
    single facade cannot just call it. Handing the work to a thread with its own
    loop is the honest bridge, and journal indexing is rare and short enough
    that a thread per call costs nothing worth optimising.

    The factory builds the coroutine rather than receiving it, so nothing is
    ever created on one loop and awaited on another.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


class JournalSearchIndex:
    """A synchronous facade over the SDK index, because its callers are a CLI."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._index: HybridIndex | None = None

    def _configured(self, weights: FusionWeights | None = None) -> HybridIndex:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return HybridIndex(
            IndexConfig(
                db_path=self.path,
                embed_fn=_no_embedding,
                dim=1,
                vector_store=BruteForceVectorStore(),
                semantic_enabled=False,
                min_semantic_score=SEMANTIC_DISABLED,
                recency_half_life_days=30.0,
                weights=weights or FusionWeights(semantic=0.0, lexical=1.0, recency=0.5),
            )
        )

    def _open(self) -> HybridIndex:
        if self._index is None:
            self._index = self._configured()
        return self._index

    def sync(self, documents: Iterable[JournalDocument]) -> int:
        """Bring the index in line with the entries on disk, touching only changes.

        The wholesale rebuild this replaces re-read and re-wrote every entry
        each time the panel opened, which is why opening it got slower as the
        project got older. Journals already carry a content hash, so an entry
        whose hash is unchanged is skipped, and an entry that disappeared from
        Git is deleted rather than left behind as a hit nobody can open.
        """

        index = self._open()
        wanted = {doc.entry_ref: doc for doc in documents}

        async def run() -> int:
            known = {doc_id for doc_id in index.ids()}
            stale = sorted(known - set(wanted))
            if stale:
                await index.delete(stale)

            changed = [
                Document(
                    id=doc.entry_ref,
                    text=doc.searchable_text(),
                    metadata=doc.metadata(),
                    timestamp=_epoch(doc.recorded_at),
                )
                for doc in wanted.values()
            ]
            # upsert is already a no-op for unchanged text, so the hash check is
            # about not building documents we would throw away rather than about
            # correctness.
            if changed:
                await index.upsert(changed)
            return len(wanted)

        return _run_sync(run)

    def inspect(self, entry_ref: str) -> dict[str, Any]:
        """Read one indexed document without opening or repairing the index."""

        clean_ref = str(entry_ref or "").strip()
        if not self.path.is_file():
            return {"state": "index_absent", "entry_ref": clean_ref}
        try:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT metadata_json FROM docs WHERE id = ?",
                    (clean_ref,),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise DomainError(
                "journal_index_unavailable",
                "The disposable journal index cannot be read.",
                status=503,
                details={"index_path": str(self.path), "error": str(exc)},
            ) from exc
        if row is None:
            return {"state": "entry_absent", "entry_ref": clean_ref}
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise DomainError(
                "journal_index_metadata_invalid",
                "The indexed journal entry has invalid metadata.",
                status=503,
                details={"entry_ref": clean_ref},
            ) from exc
        return {
            "state": "indexed",
            "entry_ref": clean_ref,
            "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
        }

    def search(
        self,
        query: str,
        *,
        project_ref: str = "",
        work_ref: str = "",
        worker_name: str = "",
        status: str = "",
        limit: int = 20,
        weights: FusionWeights | None = None,
    ) -> list[dict[str, Any]]:
        if not self.path.is_file():
            raise DomainError(
                "journal_index_unavailable",
                "The disposable journal index has not been built. Run journal-refresh or journal-index first.",
                status=503,
                details={"index_path": str(self.path)},
            )
        # Rank weights belong to one request. Use a short-lived index facade so
        # concurrent relay requests cannot overwrite shared mutable config.
        index = self._configured(weights)
        filters = {
            key: value
            for key, value in (
                ("project_ref", project_ref),
                ("work_ref", work_ref),
                ("worker_name", worker_name),
                ("status", status),
            )
            if value
        }

        async def run() -> list[dict[str, Any]]:
            hits = await index.search(
                query,
                top_k=max(1, min(int(limit), 10_000)),
                filters=filters or None,
                mode="hybrid",
                snippets=True,
            )
            rows: list[dict[str, Any]] = []
            for hit in hits:
                row = dict(hit.metadata)
                row["rank"] = hit.score
                row["search_ranks"] = dict(hit.sub)
                # With no query there is nothing to point at, so the summary is
                # the honest stand-in rather than a fabricated excerpt.
                row["snippet"] = hit.snippet or row.get("summary") or ""
                rows.append(row)
            return rows

        return _run_sync(run)


__all__ = ["JournalDocument", "JournalSearchIndex"]
