# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W918 H1: the owner's admission-time authority proof."""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.grantor_authority_proof import (
    GrantorProofError, INVENTORY_UNAVAILABLE, PROOF_INVALID, issue_grantor_proof, verify_grantor_proof,
)

SECRET = "s" * 40
OWNER = {"user_id": "user:owner", "roles": ["kdcube:role:user", "docs:editor"], "permissions": ["docs.write"]}
SELECTION = {"resource_grants": {"https://docs.example.test/mcp": ["docs:read"]}}
NOW = 1_800_000_000


def issue(**over):
    args = dict(secret=SECRET, user=OWNER, grantor_subject="user:owner", access_id="agent-1", original_revision=4,
                request_id="agent-card-save:abc", selection=SELECTION, now=NOW, ttl_seconds=300)
    args.update(over)
    return issue_grantor_proof(**args)


def verify(token, **over):
    args = dict(secret=SECRET, now=NOW + 10, grantor_subject="user:owner", access_id="agent-1",
                original_revision=4, request_id="agent-card-save:abc", selection=SELECTION)
    args.update(over)
    return verify_grantor_proof(token, **args)


def test_a_valid_proof_carries_the_owners_session_authority():
    authority = verify(issue())
    assert authority.identity() == {"user_id": "user:owner", "roles": ["docs:editor", "kdcube:role:user"],
                                    "permissions": ["docs.write"]}
    assert authority.expires_at == NOW + 300


def test_only_the_owners_own_session_gets_a_proof():
    with pytest.raises(GrantorProofError, match="card_grantor_proof_not_owner"):
        issue(user={**OWNER, "user_id": "user:admin"})


def test_no_secret_means_no_proof():
    for secret in ("", "short"):
        with pytest.raises(GrantorProofError, match=INVENTORY_UNAVAILABLE):
            issue(secret=secret)
        with pytest.raises(GrantorProofError, match=INVENTORY_UNAVAILABLE):
            verify(issue(), secret=secret)


@pytest.mark.parametrize("change", [
    dict(grantor_subject="user:other"), dict(access_id="agent-2"), dict(original_revision=5),
    dict(request_id="agent-card-save:other"), dict(selection={"resource_grants": {}}),
    dict(now=NOW + 300), dict(now=NOW - 1), dict(secret="t" * 40),
])
def test_another_scope_an_expired_proof_or_another_key_is_refused(change):
    with pytest.raises(GrantorProofError, match=PROOF_INVALID):
        verify(issue(), **change)


@pytest.mark.parametrize("tamper", [
    lambda t: t[:-2] + ("AA" if t[-2:] != "AA" else "BB"),
    lambda t: "x" + t,
    lambda t: t.replace(".", ""),
    lambda t: "",
    lambda t: None,
])
def test_a_tampered_proof_is_refused(tamper):
    with pytest.raises(GrantorProofError, match=PROOF_INVALID):
        verify(tamper(issue()))


def test_a_proof_cannot_be_widened_by_editing_its_claims():
    import base64
    import json

    body, tag = issue().split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["roles"].append("kdcube:role:admin")
    forged = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
                                      ).rstrip(b"=").decode()
    with pytest.raises(GrantorProofError, match=PROOF_INVALID):
        verify(f"{forged}.{tag}")


def test_the_lifetime_is_bounded():
    for ttl in (0, 3601):
        with pytest.raises(GrantorProofError):
            issue(ttl_seconds=ttl)
