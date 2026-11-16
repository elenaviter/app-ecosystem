/** The hand-off from the provider consent step back to the agent's Card review. */

export interface ConsentPlanAgentHandoff {
  clientId: string;
  resource: string;
  accountId: string;
  claim: string;
  /** The DOOR claims the agent grant must cover — the hand-off card submits
   *  these. Without them a FRESH flow (no recorded pending demand) grants
   *  nothing and the create fails with requires_resource_grants. */
  claims?: string[];
}

const AUTOMATION_CLIENT_PREFIX = 'automation:';

// The agent-grant card URL: this same widget, on the Access cards tab,
// with the pending agent grant pre-filled (account + claim focused).
//
// A manual automation takes `manual_access_id` instead: the pending pane's only
// save is delegated_agent_grant_create, which resolves a card by a deterministic
// key. A manual record is keyed by a random access_id, so that save answers
// "this client has no existing grant to extend" for a card in the same list.
// The backend denial builder applies the same rule; this is the second entry
// point into that pane.
export function agentCardHref(handoff: ConsentPlanAgentHandoff): string {
  const url = new URL(window.location.href);
  url.searchParams.set('tab', 'delegated_by_kdcube');
  if (handoff.clientId.startsWith(AUTOMATION_CLIENT_PREFIX)) {
    url.searchParams.set('manual_access_id', handoff.clientId.split(':')[1] || '');
    url.searchParams.delete('pending_agent_grant');
  } else {
    url.searchParams.set('pending_agent_grant', '1');
  }
  url.searchParams.set('agent_client_id', handoff.clientId);
  url.searchParams.set('resource', handoff.resource);
  if (handoff.accountId) url.searchParams.set('account_id', handoff.accountId);
  if (handoff.claim) url.searchParams.set('account_claim', handoff.claim);
  // Drop the connect step's own params so the agent card reads a clean state.
  ['provider_id', 'connector_app_id', 'claims', 'agent_resource', 'agent_claims', 'tool_name'].forEach(
    (key) => url.searchParams.delete(key),
  );
  if (handoff.claims && handoff.claims.length) {
    // After the cleanup above: these are the AGENT-GRANT claims the card
    // submits, not the connect step's provider claims.
    url.searchParams.set('claims', handoff.claims.join(','));
  }
  return url.toString();
}
