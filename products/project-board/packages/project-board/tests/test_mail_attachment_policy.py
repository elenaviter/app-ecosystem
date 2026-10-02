"""The host-independent mail policy also runs without the platform SDK."""

import pytest

from project_board.contract.errors import DomainError
from project_board.contract.mail_attachments import (
    EXECUTABLE_BINARY_RULE, MAX_MAIL_ATTACHMENT_BYTES, MAX_MAIL_ATTACHMENTS,
    executable_binary_format, validate_mail_attachment,
)


@pytest.mark.parametrize("magic,format", [
    (b"\x7fELF", "ELF"),
    (b"\xfe\xed\xfa\xce", "Mach-O"),
    (b"\xce\xfa\xed\xfe", "Mach-O"),
    (b"\xfe\xed\xfa\xcf", "Mach-O"),
    (b"\xcf\xfa\xed\xfe", "Mach-O"),
    (b"\x00asm", "WebAssembly"),
])
def test_content_not_filename_identifies_executable_binary(magic, format):
    assert executable_binary_format(magic + b"\x00" * 64) == format
    with pytest.raises(DomainError) as refused:
        validate_mail_attachment(magic + b"\x00" * 64)
    assert refused.value.details["rule"] == EXECUTABLE_BINARY_RULE
    assert "Git" in str(refused.value)


@pytest.mark.parametrize("content", [
    b"#!/bin/sh\necho hi\n", b"#!/usr/bin/env python3\nprint('hi')\n",
    b"MZ is ordinary text, not a PE header", b"public class Example {}\n",
])
def test_source_and_scripts_are_allowed(content):
    assert not executable_binary_format(content)
    validate_mail_attachment(content)


def test_shared_limits_and_empty_content():
    assert MAX_MAIL_ATTACHMENT_BYTES == 25 * 1024 * 1024
    assert MAX_MAIL_ATTACHMENTS == 10
    with pytest.raises(DomainError) as refused:
        validate_mail_attachment(b"")
    assert refused.value.code == "field_attachment_empty"
