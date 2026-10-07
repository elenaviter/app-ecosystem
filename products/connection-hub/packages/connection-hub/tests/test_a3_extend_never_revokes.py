"""Audit A3: an edit of an OAuth client's Card that leaves nothing selected is REFUSED, never a revoke.

The operator: "changing what this token points into should not invalidate the
actual token the parties possess". ``update_access`` already refuses such a
save (the 2026-09-12 incident); ``extend_client_access`` revoked the Card and
with it the client's delivered tokens. Real PostgreSQL authority, decision
store and Redis handles (the W603 world); the client's tokens are issued
through the original-issuance path.
"""

from __future__ import annotations

import pytest

from test_w603_original_issuance import GRANTOR, RESOURCE, _begin, _card, _reserve, _usable, _world

USER = {"user_id": GRANTOR, "roles": ["kdcube:role:registered"], "permissions": []}


@pytest.mark.asyncio
async def test_an_extend_that_would_leave_nothing_selected_refuses_and_the_tokens_keep_working(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        tokens = await _reserve(w, plan)
        assert (await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)).state == "committed"
        before = await _card(w, plan.access_id)
        assert await _usable(w, tokens) == {"access": True, "refresh": True}
        # As in the 2026-09-12 incident: the Card's only service is withdrawn from the catalog.
        from test_resident_profile_cards import _connections
        withdrawn = _connections()
        withdrawn["delegated_credentials"]["oauth"]["resources"] = [
            row for row in withdrawn["delegated_credentials"]["oauth"]["resources"]
            if not str(row.get("resource", "")).startswith("https://host/api/mcp/memories")]
        w.service._catalog_resolver.publish(withdrawn)
        result = await w.service.extend_client_access(
            USER, client_id=plan.client_id, access_id=plan.access_id, resource=RESOURCE, claims=["memories:read"],
            replace=True)
        assert result["ok"] is False and result.get("revoked") is not True, result
        assert result["error"] == "delegated_access_requires_resource_grants"
        after = await _card(w, plan.access_id)
        assert after == before  # the Card is exactly as it was: same revision, still active
        assert await _usable(w, tokens) == {"access": True, "refresh": True}  # the delivered tokens keep working
