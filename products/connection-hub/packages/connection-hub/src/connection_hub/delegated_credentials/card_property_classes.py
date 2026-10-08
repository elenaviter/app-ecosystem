"""W502: every known Card property key, classified as authorization or personal.

A Card's ``properties`` mixes authorization metadata (identity markers,
snapshot and composition rules, application-operation and agent-capability
policy) with person-owned settings (a GitHub link, a commit email). Code that
hands Card content to another application (``card_census_read``) sends only
``AUTHORIZATION`` keys: a key in neither set is withheld, so a new setting
fails closed, and the classification test forces every new key in source
through this decision (EMain #618).
"""

from __future__ import annotations

from .agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY, AGENT_CAPABILITY_DEFAULTS_PROPERTY, AGENT_CAPABILITY_METADATA_PROPERTY,
    AGENT_CAPABILITY_PROJECTION_PROPERTY, AGENT_CAPABILITY_SELECTION_PROPERTY, AGENT_DESCRIPTOR_CONTROL_PROPERTY,
)
from .application_operation_policy import APPLICATION_OPERATIONS_PROPERTY
from .controls.effective import SERVICE_COMPOSITION_MODES_PROPERTY
from .controls.project_invitation import PROJECT_INVITATION_CONTROL_PROPERTY
from .controls.project_person import PROJECT_PERSON_CONTROL_PROPERTY
from .controls.snapshot import CONTROL_SNAPSHOT_PROPERTY
from .conversation_target_policy import CONVERSATION_TARGETS_PROPERTY
from .project_identity_lifecycle import MY_CARD_COMMIT_EMAIL_PROPERTY, MY_CARD_GITHUB_PROPERTY

AUTHORIZATION = frozenset({
    PROJECT_PERSON_CONTROL_PROPERTY, CONTROL_SNAPSHOT_PROPERTY, SERVICE_COMPOSITION_MODES_PROPERTY,
    PROJECT_INVITATION_CONTROL_PROPERTY, APPLICATION_OPERATIONS_PROPERTY, CONVERSATION_TARGETS_PROPERTY,
    AGENT_CAPABILITY_AUTHORITY_PROPERTY, AGENT_CAPABILITY_DEFAULTS_PROPERTY, AGENT_CAPABILITY_METADATA_PROPERTY,
    AGENT_CAPABILITY_PROJECTION_PROPERTY, AGENT_CAPABILITY_SELECTION_PROPERTY, AGENT_DESCRIPTOR_CONTROL_PROPERTY,
})
PERSONAL = frozenset({MY_CARD_GITHUB_PROPERTY, MY_CARD_COMMIT_EMAIL_PROPERTY})


def authorization_properties(properties) -> dict:
    """Only the classified authorization keys; personal and unclassified keys never leave."""
    return {name: value for name, value in dict(properties or {}).items() if name in AUTHORIZATION}


__all__ = ["AUTHORIZATION", "PERSONAL", "authorization_properties"]
