from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Mapping, Sequence


SCHEMA_VERSION = "scoped-collection-v1"
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
CURSOR_VERSION = 1
_FILTER_NAMES = frozenset({"assignee", "depends_on", "status"})


class CollectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CollectionScope:
    tenant: str
    project: str
    namespace: str

    @property
    def values(self) -> tuple[str, str, str]:
        return (
            _required_text(self.tenant, "scope.tenant"),
            _required_text(self.project, "scope.project"),
            _required_text(self.namespace, "scope.namespace"),
        )


@dataclass(frozen=True)
class KeysetPage:
    items: list[dict[str, Any]]
    next_cursor: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "count": len(self.items),
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True)
class ScopedKeysetCursor:
    """Opaque keyset cursor bound to one authority scope and one query shape.

    This cursor does not own or copy rows. The authoritative store supplies a
    page ordered by ``key_fields``; the cursor only carries the last key and
    refuses reuse for another scope or query. ``ScopedCollectionStore`` is one
    consumer. Owners of existing rows, such as conversation storage, can use
    the same paging contract without moving their data into the collection
    table.
    """

    scope: Mapping[str, Any]
    query: Mapping[str, Any]
    key_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.scope:
            raise CollectionError(
                "collection_invalid_cursor_binding",
                "A keyset cursor requires a non-empty authority scope.",
            )
        if not self.key_fields or any(not str(value).strip() for value in self.key_fields):
            raise CollectionError(
                "collection_invalid_cursor_binding",
                "A keyset cursor requires named key fields.",
            )
        try:
            _json(
                {
                    "scope": dict(self.scope),
                    "query": dict(self.query),
                    "key_fields": list(self.key_fields),
                }
            )
        except (TypeError, ValueError) as exc:
            raise CollectionError(
                "collection_invalid_cursor_binding",
                "The cursor scope and query must be JSON-serializable.",
            ) from exc

    @property
    def fingerprint(self) -> str:
        payload = {
            "scope": dict(self.scope),
            "query": dict(self.query),
            "key_fields": list(self.key_fields),
        }
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def encode(self, values: Sequence[Any]) -> str:
        normalized = self._validated_key(values, code="collection_invalid_cursor_key")
        try:
            payload = _json(
                {
                    "v": CURSOR_VERSION,
                    "q": self.fingerprint,
                    "k": list(normalized),
                }
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CollectionError(
                "collection_invalid_cursor_key",
                "Cursor key values must be JSON-serializable scalars.",
            ) from exc
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    def decode(self, cursor: str) -> tuple[Any, ...]:
        try:
            encoded = _required_text(cursor, "cursor")
            padding = "=" * (-len(encoded) % 4)
            raw = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(raw.decode("utf-8"))
        except (
            CollectionError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as exc:
            raise CollectionError(
                "collection_invalid_cursor",
                "The collection cursor is invalid.",
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("v") != CURSOR_VERSION:
            raise CollectionError(
                "collection_invalid_cursor",
                "The collection cursor version is not supported.",
            )
        if payload.get("q") != self.fingerprint:
            raise CollectionError(
                "collection_cursor_mismatch",
                "The collection cursor belongs to a different scope or query.",
            )
        key = payload.get("k")
        if not isinstance(key, list):
            raise CollectionError(
                "collection_invalid_cursor",
                "The collection cursor does not contain a complete keyset.",
            )
        return self._validated_key(key, code="collection_invalid_cursor")

    def _validated_key(self, values: Sequence[Any], *, code: str) -> tuple[Any, ...]:
        normalized = tuple(values)
        if len(normalized) != len(self.key_fields):
            raise CollectionError(
                code,
                f"The cursor key requires {len(self.key_fields)} values.",
            )
        for field, value in zip(self.key_fields, normalized):
            if value is None or not isinstance(value, (str, int, float, bool)):
                raise CollectionError(
                    code,
                    f"Cursor key {field} must be a non-null JSON scalar.",
                )
            if isinstance(value, str) and not value:
                raise CollectionError(
                    code,
                    f"Cursor key {field} must not be empty.",
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise CollectionError(
                    code,
                    f"Cursor key {field} must be a finite JSON number.",
                )
        return normalized


def keyset_page(
    rows: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    cursor: ScopedKeysetCursor,
    key: Callable[[Mapping[str, Any]], Sequence[Any]],
) -> KeysetPage:
    """Trim a ``limit + 1`` authoritative read and cursor the last visible row."""

    visible = [dict(row) for row in rows[:limit]]
    next_cursor = cursor.encode(key(visible[-1])) if len(rows) > limit and visible else None
    return KeysetPage(items=visible, next_cursor=next_cursor)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CollectionError(
            "collection_invalid_argument",
            f"{field} must be a non-empty string.",
        )
    return text


def _normalized_values(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    candidates: Sequence[Any]
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, Sequence):
        candidates = value
    else:
        raise CollectionError(
            "collection_invalid_filter",
            f"Filter {field} must be a string or a sequence of strings.",
        )
    return tuple(sorted({str(item).strip() for item in candidates if str(item).strip()}))


def _normalized_filters(filters: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    provided = dict(filters or {})
    unknown = sorted(set(provided) - _FILTER_NAMES)
    if unknown:
        raise CollectionError(
            "collection_invalid_filter",
            f"Unsupported collection filters: {', '.join(unknown)}.",
        )
    return {
        "assignee": _normalized_values(provided.get("assignee"), "assignee"),
        "depends_on": _normalized_values(provided.get("depends_on"), "depends_on"),
        "status": _normalized_values(provided.get("status"), "status"),
    }


def _decoded(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _record(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    source = dict(row)
    result = {
        key: source[key]
        for key in (
            "item_id",
            "sort_key",
            "status",
            "assignee",
            "dependencies",
            "revision",
            "detail",
            "created_at",
            "updated_at",
        )
        if key in source
    }
    result["dependencies"] = _decoded(result.get("dependencies"), [])
    result["detail"] = _decoded(result.get("detail"), {})
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            result[key] = aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


class ScopedCollectionStore:
    """A Postgres row collection with bounded keyset pagination."""

    def __init__(self, *, pg_pool: Any, scope: CollectionScope) -> None:
        self.pg_pool = pg_pool
        self.scope = scope

    @property
    def available(self) -> bool:
        return self.pg_pool is not None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        if self.pg_pool is None:
            raise CollectionError(
                "collection_store_unavailable",
                "Collection storage requires a Postgres pool.",
            )
        acquire = getattr(self.pg_pool, "acquire", None)
        if callable(acquire):
            async with acquire() as connection:
                yield connection
            return
        yield self.pg_pool

    async def ensure_schema(self) -> None:
        if not self.available:
            return
        statements = (
            """
            CREATE TABLE IF NOT EXISTS app_scoped_collection_rows (
                tenant TEXT NOT NULL,
                project TEXT NOT NULL,
                namespace TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                sort_key TEXT NOT NULL,
                status TEXT NOT NULL,
                assignee TEXT NOT NULL DEFAULT '',
                dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
                revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
                detail JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tenant, project, namespace, collection_id, item_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS app_scoped_collection_rows_order
            ON app_scoped_collection_rows (
                tenant, project, namespace, collection_id, sort_key, item_id
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS app_scoped_collection_rows_status
            ON app_scoped_collection_rows (
                tenant, project, namespace, collection_id, status, sort_key, item_id
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS app_scoped_collection_rows_assignee
            ON app_scoped_collection_rows (
                tenant, project, namespace, collection_id, assignee, sort_key, item_id
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS app_scoped_collection_rows_dependencies
            ON app_scoped_collection_rows USING GIN (dependencies)
            """,
        )
        async with self.connection() as conn:
            for statement in statements:
                await conn.execute(statement)

    async def put_row(
        self,
        *,
        collection_id: str,
        item_id: str,
        sort_key: str,
        status: str,
        assignee: str = "",
        dependencies: Sequence[str] = (),
        detail: Mapping[str, Any] | None = None,
        expected_revision: int,
    ) -> dict[str, Any]:
        collection_id = _required_text(collection_id, "collection_id")
        item_id = _required_text(item_id, "item_id")
        sort_key = _required_text(sort_key, "sort_key")
        status = _required_text(status, "status")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise CollectionError(
                "collection_invalid_revision",
                "expected_revision must be a non-negative integer.",
            )
        normalized_dependencies = list(
            dict.fromkeys(
                _required_text(value, "dependencies[]") for value in dependencies
            )
        )
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """
                WITH updated AS (
                    UPDATE app_scoped_collection_rows
                    SET sort_key=$6,
                        status=$7,
                        assignee=$8,
                        dependencies=$9::jsonb,
                        detail=$10::jsonb,
                        revision=app_scoped_collection_rows.revision+1,
                        updated_at=NOW()
                    WHERE tenant=$1 AND project=$2 AND namespace=$3
                      AND collection_id=$4 AND item_id=$5
                      AND revision=$11 AND $11::bigint>0
                    RETURNING item_id, sort_key, status, assignee, dependencies,
                              revision, detail, created_at, updated_at
                ), inserted AS (
                    INSERT INTO app_scoped_collection_rows (
                        tenant, project, namespace, collection_id, item_id,
                        sort_key, status, assignee, dependencies, detail, revision
                    )
                    SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,1
                    WHERE $11::bigint=0
                    ON CONFLICT (tenant, project, namespace, collection_id, item_id)
                    DO NOTHING
                    RETURNING item_id, sort_key, status, assignee, dependencies,
                              revision, detail, created_at, updated_at
                )
                SELECT * FROM updated
                UNION ALL
                SELECT * FROM inserted
                """,
                *self.scope.values,
                collection_id,
                item_id,
                sort_key,
                status,
                str(assignee or "").strip(),
                _json(normalized_dependencies),
                _json(dict(detail or {})),
                expected_revision,
            )
        record = _record(row)
        if record is None:
            raise CollectionError(
                "collection_revision_conflict",
                f"Row {item_id} is absent or no longer has revision {expected_revision}.",
            )
        return record

    async def get_row(self, *, collection_id: str, item_id: str) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT item_id, sort_key, status, assignee, dependencies,
                       revision, detail, created_at, updated_at
                FROM app_scoped_collection_rows
                WHERE tenant=$1 AND project=$2 AND namespace=$3
                  AND collection_id=$4 AND item_id=$5
                """,
                *self.scope.values,
                _required_text(collection_id, "collection_id"),
                _required_text(item_id, "item_id"),
            )
        return _record(row)

    async def delete_row(
        self,
        *,
        collection_id: str,
        item_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise CollectionError(
                "collection_invalid_revision",
                "A delete requires a positive expected_revision.",
            )
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM app_scoped_collection_rows
                WHERE tenant=$1 AND project=$2 AND namespace=$3
                  AND collection_id=$4 AND item_id=$5 AND revision=$6
                RETURNING item_id, sort_key, status, assignee, dependencies,
                          revision, detail, created_at, updated_at
                """,
                *self.scope.values,
                _required_text(collection_id, "collection_id"),
                _required_text(item_id, "item_id"),
                expected_revision,
            )
        record = _record(row)
        if record is None:
            raise CollectionError(
                "collection_revision_conflict",
                f"Row {item_id} is absent or no longer has revision {expected_revision}.",
            )
        return record

    async def list_rows(
        self,
        *,
        collection_id: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        order: str = "asc",
    ) -> KeysetPage:
        collection_id = _required_text(collection_id, "collection_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_PAGE_LIMIT
        ):
            raise CollectionError(
                "collection_invalid_limit",
                f"limit must be between 1 and {MAX_PAGE_LIMIT}.",
            )
        order = str(order or "").strip().lower()
        if order not in {"asc", "desc"}:
            raise CollectionError(
                "collection_invalid_order",
                "order must be asc or desc.",
            )
        normalized_filters = _normalized_filters(filters)
        cursor_codec = ScopedKeysetCursor(
            scope={
                "tenant": self.scope.values[0],
                "project": self.scope.values[1],
                "namespace": self.scope.values[2],
            },
            query={
                "collection_id": collection_id,
                "filters": {
                    key: list(value) for key, value in sorted(normalized_filters.items())
                },
                "order": order,
            },
            key_fields=("sort_key", "item_id"),
        )
        after_sort_key: str | None = None
        after_item_id: str | None = None
        if cursor:
            decoded = cursor_codec.decode(cursor)
            after_sort_key = _required_text(decoded[0], "cursor.sort_key")
            after_item_id = _required_text(decoded[1], "cursor.item_id")
        comparator = ">" if order == "asc" else "<"
        direction = "ASC" if order == "asc" else "DESC"
        query = f"""
            SELECT item_id, sort_key, status, assignee, dependencies,
                   revision, detail, created_at, updated_at
            FROM app_scoped_collection_rows
            WHERE tenant=$1 AND project=$2 AND namespace=$3
              AND collection_id=$4
              AND (CARDINALITY($5::text[])=0 OR status=ANY($5::text[]))
              AND (CARDINALITY($6::text[])=0 OR assignee=ANY($6::text[]))
              AND (CARDINALITY($7::text[])=0 OR dependencies ?| $7::text[])
              AND ($8::text IS NULL OR (sort_key, item_id) {comparator} ($8::text, $9::text))
            ORDER BY sort_key {direction}, item_id {direction}
            LIMIT $10
        """
        async with self.connection() as conn:
            rows = list(
                await conn.fetch(
                    query,
                    *self.scope.values,
                    collection_id,
                    list(normalized_filters["status"]),
                    list(normalized_filters["assignee"]),
                    list(normalized_filters["depends_on"]),
                    after_sort_key,
                    after_item_id,
                    limit + 1,
                )
            )
        page = keyset_page(
            [_record(row) or {} for row in rows],
            limit=limit,
            cursor=cursor_codec,
            key=lambda row: (
                _required_text(row.get("sort_key"), "row.sort_key"),
                _required_text(row.get("item_id"), "row.item_id"),
            ),
        )
        return page


__all__ = [
    "CollectionError",
    "CollectionScope",
    "CURSOR_VERSION",
    "DEFAULT_PAGE_LIMIT",
    "KeysetPage",
    "MAX_PAGE_LIMIT",
    "SCHEMA_VERSION",
    "ScopedCollectionStore",
    "ScopedKeysetCursor",
    "keyset_page",
]
