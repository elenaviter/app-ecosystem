"""Host-independent limits and content policy for Problem Board mail files."""

from __future__ import annotations

from .errors import DomainError


# The platform staging limit, shared by operator and worker attachment lanes.
MAX_MAIL_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_MAIL_ATTACHMENTS = 10
EXECUTABLE_BINARY_RULE = "no-executable-binary"

# Per-kind ceilings the platform applies to an uploaded attachment (KDCube
# infra/gateway/safe_preflight.py, PreflightConfig): the service is the
# authority. A file the host would queue and the platform then refuses is
# refused here at send instead (W475). The service decides the type it hands
# to that check itself (the Problem Board attachment service's _preflight),
# so the host can apply the same rule exactly; attachment_kind mirrors it.
TEXT_MAX_BYTES = 10 * 1024 * 1024
SVG_MAX_BYTES = 2 * 1024 * 1024
_TEXT_LIKE_MIME = frozenset({
    "text/plain", "text/markdown", "text/csv", "text/html", "text/css",
    "text/xml", "text/yaml", "text/x-yaml", "text/javascript",
    "application/json", "application/x-ndjson", "application/xml",
    "application/yaml", "application/x-yaml", "application/toml",
    "application/javascript",
})


def _service_mime(data: bytes, mime: str) -> str:
    """The type the service hands to the platform check for these bytes.

    A declared text, image, PDF, ZIP or Office type, or PDF or ZIP content,
    is kept. Anything else (the generic binary type, a script type) becomes
    text/plain when the whole file is UTF-8 without control characters.
    """

    candidate = str(mime or "").split(";", 1)[0].strip().lower() or "application/octet-stream"
    if (not candidate.startswith(("text/", "image/"))
            and candidate not in {"application/pdf", "application/zip"}
            and "officedocument" not in candidate
            and not data.startswith((b"%PDF", b"PK"))):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return candidate
        if not any(ord(char) < 32 and char not in "\t\n\r" for char in text):
            return "text/plain"
    return candidate


def attachment_kind(data: bytes, *, mime: str = "") -> str:
    """``svg``, ``text`` or ``other``: which ceiling the platform applies.

    The service decides the type from the declared one and, for a generic or
    script type, from the content; a name alone never makes binary bytes text.
    """

    # The platform routes PDF and ZIP content to its document checks before
    # it looks at the declared type, and those carry no byte ceiling of
    # their own: a file with such bytes is never held to the text or SVG one.
    if data.startswith((b"%PDF", b"PK")):
        return "other"
    routed = _service_mime(data, mime)
    if routed == "image/svg+xml":
        return "svg"
    if routed in _TEXT_LIKE_MIME or routed.startswith("text/"):
        return "text"
    return "other"


def kind_limit_bytes(kind: str) -> int:
    return {"svg": SVG_MAX_BYTES, "text": TEXT_MAX_BYTES}.get(kind, MAX_MAIL_ATTACHMENT_BYTES)


def executable_binary_format(data: bytes) -> str:
    """Recognize native executable containers, not extensions or mode bits.

    A PE needs its actual signature, not merely text beginning with ``MZ``.
    Fat Mach-O's architecture count distinguishes it from Java's class magic.
    Scripts (including executable shebang files) are not binary containers.
    """
    magic = data[:4]
    if magic == b"\x7fELF":
        return "ELF"
    if magic in {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                 b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"}:
        return "Mach-O"
    if magic in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                 b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}:
        endian = "little" if magic[0] in {0xbe, 0xbf} else "big"
        count = int.from_bytes(data[4:8], endian)
        width = 32 if magic in {b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"} else 20
        if 0 < count <= 128 and len(data) >= 8 + count * width:
            return "Mach-O universal"
    if data[:2] == b"MZ" and len(data) >= 64:
        offset = int.from_bytes(data[60:64], "little")
        if offset >= 64 and data[offset:offset + 4] == b"PE\x00\x00":
            return "PE"
    if magic == b"\x00asm":
        return "WebAssembly"
    return ""


def validate_mail_attachment(
    data: bytes,
    *,
    error_namespace: str = "field",
    filename: str = "",
    mime: str = "",
) -> None:
    """Apply the same bounded binary refusal on the host and service.

    With ``filename`` or ``mime`` the host also applies the platform's
    per-kind ceilings (text 10 MiB, SVG 2 MiB), so such a file is refused
    when it is sent, not after it was queued.
    """
    if not data:
        raise DomainError(f"{error_namespace}_attachment_empty", "An attachment is empty.")
    if len(data) > MAX_MAIL_ATTACHMENT_BYTES:
        raise DomainError(
            f"{error_namespace}_attachment_too_large",
            f"Attachments are limited to {MAX_MAIL_ATTACHMENT_BYTES} bytes.",
            details={"maximum_bytes": MAX_MAIL_ATTACHMENT_BYTES},
        )
    if filename or mime:
        kind = attachment_kind(data, mime=mime)
        limit = kind_limit_bytes(kind)
        if len(data) > limit:
            raise DomainError(
                f"{error_namespace}_attachment_too_large",
                f"The platform accepts {kind} attachments up to {limit} bytes; this one is {len(data)}.",
                details={"maximum_bytes": limit, "kind": kind, "size": len(data)},
            )
    binary = executable_binary_format(data)
    if binary:
        raise DomainError(
            f"{error_namespace}_attachment_executable_binary_refused",
            f"Rule {EXECUTABLE_BINARY_RULE} refuses {binary} executable binary files; use Git when one is needed.",
            status=422,
            details={"rule": EXECUTABLE_BINARY_RULE, "format": binary},
        )
