from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .plan_nodes import plan_node_identity_ref
from .work_lifecycle import canonical_work_status


PLAN_INDEX_SCHEMA = "problem-board.plan-index.v2"
PLAN_NODES_SCHEMA = "problem-board.plan-nodes.v1"
SUMMARY_LIMIT = 1200


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates: Sequence[Any] = (value,)
    elif isinstance(value, Sequence):
        candidates = value
    else:
        candidates = ()
    return [text for item in candidates if (text := _text(item))]


def attachment_refs(row: Mapping[str, Any]) -> list[str]:
    """Durable platform URIs only; attachment bytes stay in platform storage."""

    refs: list[str] = []
    for value in row.get("attachment_refs") or row.get("attachments") or []:
        if isinstance(value, Mapping):
            candidate = value.get("file_ref") or value.get("uri") or value.get("ref")
        else:
            candidate = value
        ref = _text(candidate)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def plan_item_source(row: Mapping[str, Any]) -> dict[str, Any]:
    """The authoritative fields from which summary and retrieval text derive."""

    return {
        "item_ref": plan_node_identity_ref(
            row.get("identity_ref") or row.get("item_ref")
        ),
        "item_key": _text(row.get("item_key")),
        "title": _text(row.get("title")),
        "description": _text(row.get("description")),
        "acceptance": _strings(row.get("acceptance")),
        "tags": _strings(row.get("tags")),
        "keywords": _strings(row.get("keywords")),
        "attachment_refs": attachment_refs(row),
    }


def summarize_plan_item(source: Mapping[str, Any]) -> str:
    """A stable retrieval summary refreshed whenever its source hash changes."""

    title = _text(source.get("title"))
    description = _text(source.get("description"))
    acceptance = _strings(source.get("acceptance"))
    parts = [title]
    if description:
        parts.append(description)
    if acceptance:
        parts.append("Acceptance: " + "; ".join(acceptance))
    return " ".join(part for part in parts if part)[:SUMMARY_LIMIT].rstrip()


def search_text_for_plan_item(
    source: Mapping[str, Any], *, summary: str
) -> str:
    """One text value feeds both lexical indexing and semantic embedding."""

    lines = [
        f"Key: {_text(source.get('item_key'))}",
        f"Title: {_text(source.get('title'))}",
        f"Summary: {_text(summary)}",
        f"Description: {_text(source.get('description'))}",
    ]
    acceptance = _strings(source.get("acceptance"))
    tags = _strings(source.get("tags"))
    keywords = _strings(source.get("keywords"))
    attachments = _strings(source.get("attachment_refs"))
    if acceptance:
        lines.append("Acceptance: " + "; ".join(acceptance))
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    if keywords:
        lines.append("Keywords: " + ", ".join(keywords))
    if attachments:
        lines.append("Attachments: " + ", ".join(attachments))
    return "\n".join(line for line in lines if not line.endswith(": "))


def indexed_plan_item(
    row: Mapping[str, Any],
    *,
    ordinal: int,
    depends_on: Sequence[str] = (),
) -> dict[str, Any]:
    """The one plan-node representation published to Postgres and Git."""

    source = plan_item_source(row)
    source_hash = _hash(source)
    summary = summarize_plan_item(source)
    search_text = search_text_for_plan_item(source, summary=summary)
    return {
        **source,
        "item_id": _text(row.get("item_id")),
        "status": canonical_work_status(row.get("status") or "todo", strict=True),
        "revision": int(row.get("revision") or 0),
        "assignee": _text(row.get("assignee")),
        "depends_on": sorted(
            {
                plan_node_identity_ref(value)
                for value in depends_on
                if _text(value)
            }
        ),
        "ordinal": int(ordinal),
        "updated_at": _text(row.get("updated_at")),
        "note_count": len(
            [note for note in row.get("notes") or [] if isinstance(note, Mapping)]
        ),
        "summary": summary,
        "source_content_hash": source_hash,
        "summary_source_hash": source_hash,
        "search_text": search_text,
        "search_content_hash": hashlib.sha256(search_text.encode("utf-8")).hexdigest(),
    }


def plan_page_window(
    *,
    page: int,
    page_size: int,
    matched_count: int,
) -> dict[str, Any]:
    """Return the explicit direct-page coordinates shared by plan reads."""

    page_count = (
        (matched_count + page_size - 1) // page_size
        if matched_count
        else 0
    )
    return {
        "page": page,
        "page_count": page_count,
        "page_size": page_size,
        "offset": (page - 1) * page_size,
        "previous_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < page_count else None,
    }


def annotate_page_dependencies(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Mark full-plan dependency facts with membership in this one page."""

    page_refs = {
        _text(row.get("identity_ref") or row.get("item_ref"))
        for row in rows
        if _text(row.get("identity_ref") or row.get("item_ref"))
    }
    annotated: list[dict[str, Any]] = []
    for row in rows:
        facts: list[dict[str, Any]] = []
        for raw in row.get("dependency_facts") or []:
            if not isinstance(raw, Mapping):
                continue
            item_ref = _text(raw.get("item_ref"))
            if not item_ref:
                continue
            state = _text(raw.get("state")).lower()
            if state not in {"complete", "unfinished", "missing"}:
                state = "unfinished"
            facts.append(
                {
                    "item_ref": item_ref,
                    "state": state,
                    "on_page": item_ref in page_refs,
                }
            )
        annotated.append(
            {
                **dict(row),
                "dependency_facts": facts,
                "unshown_dependency_count": sum(
                    1 for fact in facts if not fact["on_page"]
                ),
            }
        )
    return annotated


def embedding_model_id(model_service: Any) -> str:
    config = getattr(model_service, "config", None)
    embedder = getattr(config, "embedder_config", None) or {}
    provider = _text(embedder.get("provider") or "openai")
    model = _text(
        embedder.get("model_name")
        or getattr(config, "embedding_model", "")
        or "openai-text-embedding-3-small"
    )
    return f"{provider}:{model}" if provider else model


__all__ = [
    "PLAN_INDEX_SCHEMA",
    "PLAN_NODES_SCHEMA",
    "attachment_refs",
    "annotate_page_dependencies",
    "embedding_model_id",
    "indexed_plan_item",
    "plan_page_window",
    "plan_item_source",
    "search_text_for_plan_item",
    "summarize_plan_item",
]
