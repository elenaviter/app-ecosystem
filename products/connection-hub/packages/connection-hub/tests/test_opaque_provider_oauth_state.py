"""The account-connect OAuth state is opaque to the provider.

The state carried the whole connection context (user, provider, claims,
account, the full return URL with request tickets) as readable base64 JSON;
the provider and every hop on the redirect saw it. It is now a random reference
and its signature; the context stays in the state store.
"""

import base64
import time

import pytest

from connection_hub.delegated_to_kdcube.oauth import (
    MemoryOAuthStateStore,
    consume_oauth_state,
    create_oauth_state,
    is_opaque_state,
    peek_state_payload,
    sign_state,
)

SECRET = "state-secret"
RETURN = "https://hub.example.test/widgets/connections?request_approval_ticket=T1&request_digest=D1"


async def _created(store):
    return await create_oauth_state(
        store, secret=SECRET, user_id="google:1165", provider_id="google",
        connector_app_id="kdcube_gmail", claims=("gmail:read",), account_id="acct-7",
        return_hint=RETURN,
    )


def _decodable_parts(state):
    out = []
    for part in state.split("."):
        try:
            out.append(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8", "replace"))
        except Exception:
            out.append(part)
    return " ".join(out)


@pytest.mark.asyncio
async def test_the_state_names_nothing_of_the_connection():
    store = MemoryOAuthStateStore()
    state = (await _created(store))["state"]
    assert is_opaque_state(state)
    readable = state + " " + _decodable_parts(state)
    for secret_value in ("google:1165", "acct-7", "gmail:read", "kdcube_gmail", "request_approval_ticket", "T1", "D1"):
        assert secret_value not in readable
    with pytest.raises(ValueError, match="no readable payload"):
        peek_state_payload(state)


@pytest.mark.asyncio
async def test_the_callback_reads_the_context_from_the_store_once():
    store = MemoryOAuthStateStore()
    state = (await _created(store))["state"]
    payload = await consume_oauth_state(store, state=state, secret=SECRET)
    assert payload["user_id"] == "google:1165"
    assert payload["claims"] == ["gmail:read"]
    assert payload["return_hint"] == RETURN
    with pytest.raises(ValueError, match="already used"):
        await consume_oauth_state(store, state=state, secret=SECRET)


@pytest.mark.asyncio
async def test_a_forged_or_expired_state_is_refused():
    store = MemoryOAuthStateStore()
    state = (await _created(store))["state"]
    with pytest.raises(ValueError, match="signature"):
        await consume_oauth_state(store, state=state[:-1] + ("0" if state[-1] != "0" else "1"), secret=SECRET)
    with pytest.raises(ValueError, match="signature"):
        await consume_oauth_state(store, state=state, secret="another-secret")

    expired_store = MemoryOAuthStateStore()
    expired = (await create_oauth_state(
        expired_store, secret=SECRET, user_id="u", provider_id="google",
        connector_app_id="kdcube_gmail", claims=(), ttl_seconds=900,
    ))["state"]
    for key in expired_store.items:
        expired_store.items[key]["exp"] = int(time.time()) - 1
    with pytest.raises(ValueError, match="expired"):
        await consume_oauth_state(expired_store, state=expired, secret=SECRET)


@pytest.mark.asyncio
async def test_a_readable_state_is_no_longer_accepted():
    """Only opaque states are issued; a readable one is refused and stays in the store."""
    store = MemoryOAuthStateStore()
    payload = {"v": 1, "user_id": "u", "provider_id": "google", "connector_app_id": "kdcube_gmail",
               "claims": [], "nonce": "n1", "iat": int(time.time()), "exp": int(time.time()) + 900}
    legacy = sign_state(payload, SECRET)
    await store.put(legacy, payload, ttl_seconds=900)
    assert not is_opaque_state(legacy)
    with pytest.raises(ValueError, match="invalid"):
        await consume_oauth_state(store, state=legacy, secret=SECRET)
