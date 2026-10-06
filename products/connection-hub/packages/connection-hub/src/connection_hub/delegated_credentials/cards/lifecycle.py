"""Strict routing hints for an issuer-owned, two-Card lifecycle.

These values are not authorization. The host supplies the authenticated actor
separately and must obtain/revalidate its configured issuer decisions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..issuer_gate import change_digest
from .model import CARD_STATE_ACTIVE, CardAuthority


class LifecycleRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 1024:
        raise LifecycleRefused("issuer_lifecycle_request_invalid")
    return value


@dataclass(frozen=True)
class LifecycleTarget:
    owner_subject: str
    access_id: str
    expected_card_revision: int
    expected_authority_fingerprint: str
    issuer_kind: str
    issuer_ref: str

    @classmethod
    def from_mapping(cls, value: object) -> LifecycleTarget:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise LifecycleRefused("issuer_lifecycle_target_invalid")
        raw = dict(value)
        revision = raw["expected_card_revision"]
        if type(revision) is not int or revision < 1:
            raise LifecycleRefused("issuer_lifecycle_revision_invalid")
        for field in ("owner_subject", "access_id", "expected_authority_fingerprint", "issuer_kind", "issuer_ref"):
            raw[field] = _text(raw[field])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw["access_id"]):
            raise LifecycleRefused("issuer_lifecycle_target_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", raw["expected_authority_fingerprint"]):
            raise LifecycleRefused("issuer_lifecycle_fingerprint_invalid")
        return cls(**raw)

    @property
    def subject_hash(self) -> str:
        return hashlib.sha256(self.owner_subject.encode("utf-8")).hexdigest()

    def assert_authority(self, authority: CardAuthority | None) -> None:
        if authority is None:
            raise LifecycleRefused("issuer_lifecycle_target_missing")
        if (authority.grantor_subject, authority.access_id, authority.issuer_kind, authority.issuer_ref) != (
            self.owner_subject, self.access_id, self.issuer_kind, self.issuer_ref,
        ):
            raise LifecycleRefused("issuer_lifecycle_target_binding_mismatch")
        if authority.state != CARD_STATE_ACTIVE or authority.card_revision != self.expected_card_revision:
            raise LifecycleRefused("issuer_lifecycle_revision_moved")
        if authority.content_hash() != self.expected_authority_fingerprint:
            raise LifecycleRefused("issuer_lifecycle_fingerprint_moved")


@dataclass(frozen=True)
class LifecycleRequest:
    context_ref: str
    request_id: str
    action: str
    change_digest: str
    targets: tuple[LifecycleTarget, LifecycleTarget]

    @classmethod
    def from_mapping(cls, value: object) -> LifecycleRequest:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise LifecycleRefused("issuer_lifecycle_request_invalid")
        if value["action"] != "revoke":
            raise LifecycleRefused("issuer_lifecycle_action_invalid")
        raw_targets = value["targets"]
        if type(raw_targets) is not list or len(raw_targets) != 2:
            raise LifecycleRefused("issuer_lifecycle_participant_count_invalid")
        targets = tuple(sorted((LifecycleTarget.from_mapping(item) for item in raw_targets),
                               key=lambda target: (target.owner_subject, target.access_id)))
        if targets[0].access_id == targets[1].access_id:
            raise LifecycleRefused("issuer_lifecycle_duplicate_target")
        candidate = {"action": "revoke", "targets": [asdict(target) for target in targets]}
        digest = _text(value["change_digest"])
        if digest != change_digest(candidate):
            raise LifecycleRefused("issuer_lifecycle_digest_mismatch")
        return cls(_text(value["context_ref"]), _text(value["request_id"]), "revoke", digest, targets)

    def to_dict(self) -> dict[str, Any]:
        return {"context_ref": self.context_ref, "request_id": self.request_id,
                "action": self.action, "change_digest": self.change_digest,
                "targets": [asdict(target) for target in self.targets]}

    def binding(self, actor_subject: str) -> dict[str, Any]:
        return {"actor_subject": _text(actor_subject), "request": self.to_dict()}

    def transaction_id(self, actor_subject: str) -> str:
        # A changed body for the same actor/context/request reaches the SAME
        # receipt key and is rejected against the complete stored binding.
        return change_digest({"actor_subject": _text(actor_subject),
                              "context_ref": self.context_ref, "request_id": self.request_id})
