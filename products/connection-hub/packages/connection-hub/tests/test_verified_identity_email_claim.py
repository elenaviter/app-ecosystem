"""An upstream's email_verified claim keeps "not said" apart from "not verified" (W260)."""

from __future__ import annotations

import pytest

from connection_hub.server_side_login.model import VerifiedIdentity, email_verified_claim


@pytest.mark.parametrize(
    ("claim", "expected"),
    [(True, True), (False, False), ("true", True), ("FALSE", False), (None, None), ("maybe", None), (1, None)],
)
def test_the_claim_reads_the_provider_shapes_and_keeps_absence_unknown(claim, expected):
    assert email_verified_claim(claim) is expected


def test_an_identity_without_a_verdict_is_unknown_not_unverified():
    assert VerifiedIdentity(provider="cognito", subject="s").email_verified is None
