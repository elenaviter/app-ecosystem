"""`pb setup` checks that the endpoint is the board's MCP before it writes (W304).

W304 finding 14 (2026-09-27): a person setting up a second machine grepped the
first machine's relay.json for "endpoint", found the worker-stream address
first, and almost gave that to `pb setup`. Nothing would have said so until the
relay failed. The operator's ruling: the setup values come from the board's
"Connect a machine" panel, and `pb setup` checks the endpoint answers as the
board's MCP before it writes anything.

The check sends no credential. An unauthenticated MCP `initialize` to the
board is refused with 401 and a `WWW-Authenticate: Bearer resource_metadata=`
header (RFC 9728); the metadata it names must list this exact endpoint as its
`resource`. Any other answer is a named refusal.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ..contract.errors import DomainError

EXPECTED_SHAPE = (
    "<server>/api/integrations/bundles/<tenant>/<project>/problem-board@1-0/public/mcp/problem_board"
)
WHERE_TO_COPY = "Copy the setup line from 'Connect a machine' in the board's top bar."
_RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]+)"')
# The deployment's tenant and project are part of the board's own address.
_BUNDLE_PATH = re.compile(
    r"^/api/integrations/bundles/(?P<tenant>[^/]+)/(?P<project>[^/]+)/[^/]+/public/mcp/problem_board/?$"
)
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pb-setup-endpoint-check", "version": "1"},
    },
}

# (status, headers) of an unauthenticated initialize; raises OSError when unreachable.
Post = Callable[[str], tuple[int, Mapping[str, str]]]
# The JSON document at a URL; raises OSError or ValueError when unreadable.
FetchJson = Callable[[str], Any]


def post_initialize(endpoint: str) -> tuple[int, Mapping[str, str]]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(_INITIALIZE).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            return reply.status, {key.lower(): value for key, value in reply.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {key.lower(): value for key, value in (exc.headers or {}).items()}


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as reply:
        return json.load(reply)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _not_board(endpoint: str, found: str, **details: Any) -> DomainError:
    return DomainError(
        "work_setup_endpoint_not_board",
        f"{endpoint} does not answer as the Problem Board MCP endpoint ({found}). "
        f"The endpoint has the shape {EXPECTED_SHAPE}. {WHERE_TO_COPY}",
        status=400,
        details={"endpoint": endpoint, "expected_shape": EXPECTED_SHAPE, "found": found, **details},
    )


def setup_scope(endpoint: str, *, tenant: str = "", platform_project: str = "") -> tuple[str, str]:
    """The tenant and platform project `pb setup` uses, read from the endpoint (W304 finding 18).

    Operator, mid-walk 2026-09-27: asking for the tenant and the platform
    project is frustrating, since the endpoint already names them. They are
    parsed from its path; a value the person also gives must agree, or setup
    refuses before writing anything. An endpoint that is not a board bundle
    address is refused by name.
    """

    path = urlsplit(str(endpoint or "").strip()).path
    match = _BUNDLE_PATH.match(path)
    if match is None:
        raise DomainError(
            "work_setup_endpoint_not_bundle",
            f"The endpoint is not a board address: it has the shape {EXPECTED_SHAPE}. {WHERE_TO_COPY}",
            status=400,
            details={"endpoint": endpoint, "expected_shape": EXPECTED_SHAPE},
        )
    parsed = {"tenant": match.group("tenant"), "platform_project": match.group("project")}
    given = {"tenant": str(tenant or "").strip(), "platform_project": str(platform_project or "").strip()}
    mismatched = {key: {"given": value, "endpoint": parsed[key]} for key, value in given.items() if value and value != parsed[key]}
    if mismatched:
        raise DomainError(
            "work_setup_scope_mismatch",
            "The endpoint names another "
            + " and ".join(key.replace("_", " ") for key in mismatched)
            + " than you gave. Leave --tenant and --platform-project out; the endpoint names them.",
            status=400,
            details={"endpoint": endpoint, **mismatched},
        )
    return parsed["tenant"], parsed["platform_project"]


def verify_board_endpoint(
    endpoint: str,
    *,
    post: Post = post_initialize,
    fetch: FetchJson = fetch_json,
) -> dict[str, Any]:
    """The board's MCP answered at `endpoint`, or a named refusal."""

    endpoint = endpoint.rstrip("/")
    try:
        status, headers = post(endpoint)
    except (OSError, ValueError) as exc:
        raise DomainError(
            "work_setup_endpoint_unreachable",
            f"{endpoint} did not answer from this machine ({type(exc).__name__}). Use the "
            f"address as this machine reaches the server, not a local address of another "
            f"machine. {WHERE_TO_COPY}",
            status=503,
            details={"endpoint": endpoint, "reason": type(exc).__name__},
        ) from exc
    if status != 401:
        raise _not_board(endpoint, f"HTTP {status} to an unauthenticated initialize, not 401")
    match = _RESOURCE_METADATA.search(str(headers.get("www-authenticate") or ""))
    if match is None:
        raise _not_board(endpoint, "a 401 without a resource_metadata challenge")
    metadata_url = match.group(1)
    if _origin(metadata_url) != _origin(endpoint):
        raise _not_board(endpoint, "resource metadata on another server", resource_metadata=metadata_url)
    try:
        metadata = fetch(metadata_url)
    except (OSError, ValueError) as exc:
        raise _not_board(endpoint, f"unreadable resource metadata ({type(exc).__name__})", resource_metadata=metadata_url) from exc
    resource = str((metadata or {}).get("resource") or "").rstrip("/") if isinstance(metadata, Mapping) else ""
    if resource != endpoint:
        raise _not_board(
            endpoint,
            "resource metadata for another resource",
            resource_metadata=metadata_url,
            resource=resource,
        )
    return {
        "state": "verified",
        "endpoint": endpoint,
        "resource_metadata": metadata_url,
        "authorization_servers": list(metadata.get("authorization_servers") or []),
    }
