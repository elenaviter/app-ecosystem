"""Host-independent limits and content policy for Problem Board mail files."""

from __future__ import annotations

from .errors import DomainError


# The platform staging limit, shared by operator and worker attachment lanes.
MAX_MAIL_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_MAIL_ATTACHMENTS = 10
EXECUTABLE_BINARY_RULE = "no-executable-binary"


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


def validate_mail_attachment(data: bytes, *, error_namespace: str = "field") -> None:
    """Apply the same bounded binary refusal on the host and service."""
    if not data:
        raise DomainError(f"{error_namespace}_attachment_empty", "An attachment is empty.")
    if len(data) > MAX_MAIL_ATTACHMENT_BYTES:
        raise DomainError(
            f"{error_namespace}_attachment_too_large",
            f"Attachments are limited to {MAX_MAIL_ATTACHMENT_BYTES} bytes.",
            details={"maximum_bytes": MAX_MAIL_ATTACHMENT_BYTES},
        )
    binary = executable_binary_format(data)
    if binary:
        raise DomainError(
            f"{error_namespace}_attachment_executable_binary_refused",
            f"Rule {EXECUTABLE_BINARY_RULE} refuses {binary} executable binary files; use Git when one is needed.",
            status=422,
            details={"rule": EXECUTABLE_BINARY_RULE, "format": binary},
        )
