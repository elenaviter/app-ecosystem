"""A failed peer call names its cause in the log before it becomes one reason.

On 2026-09-26 a second project admin could not open any Control Card:
Connection Hub answered ``project_membership_provider_unavailable``, which every
exception of the peer call becomes, and nothing in any log named the exception.
Each of the three project-host authorizers now logs the exception type, an HTTP
status and detail when present, and the message, and never the payload.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)

BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def _module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "services" / "project_membership.py")
    return module


class _Refused(Exception):
    def __init__(self) -> None:
        super().__init__("403: Bundle operation project_membership_resolve is not visible to this user")
        self.status_code = 403
        self.detail = "Bundle operation project_membership_resolve is not visible to this user"


async def _refusing(**_kwargs):
    raise _Refused()


@pytest.mark.parametrize(
    ("build", "call", "reason"),
    [
        (
            lambda m: m.BundleOperationProjectMembershipResolver(
                bundle_id="problem-board@1-0", operation="project_membership_resolve", caller=_refusing
            ),
            lambda port: port.resolve_project_membership(project_ref="work:project:secret-one", subject="user:secret"),
            "project_membership_provider_unavailable",
        ),
        (
            lambda m: m.BundleOperationAgentCardAuthorizer(bundle_id="problem-board@1-0", caller=_refusing),
            lambda port: port.authorize_agent_card(access_id="aut_x", project_ref="work:project:secret-one", action="read"),
            "project_agent_card_provider_unavailable",
        ),
        (
            lambda m: m.BundleOperationControlCardAuthorizer(bundle_id="problem-board@1-0", caller=_refusing),
            lambda port: port.authorize_project_control_card(control_id="aut_x", project_ref="work:project:secret-one", action="read"),
            "project_control_card_provider_unavailable",
        ),
    ],
)
def test_a_failed_peer_call_logs_its_cause_without_the_payload(caplog, build, call, reason):
    module = _module()
    port = build(module)
    with caplog.at_level(logging.WARNING, logger="kdcube.connection_hub.project_membership"):
        with pytest.raises(Exception) as raised:
            asyncio.run(call(port))
    assert getattr(raised.value, "reason", None) == reason
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1, lines
    line = lines[0]
    assert reason in line
    assert "exception=_Refused" in line
    assert "status=403" in line
    assert "is not visible to this user" in line
    assert "bundle=problem-board@1-0" in line
    assert "secret" not in line
