/**
 * W1018 A: the consent for a hosted agent opens on its own Agent Card.
 * Operator, 2026-11-07 (relayed by the coordinator): "W1018 - A".
 *
 * The review shows the Card as it is (revision, what it holds, its Control),
 * what the agent asked for, and the connected accounts that can do it, with the
 * requested account and claim highlighted. One Approve sends one
 * `delegated_agent_grant_create` on that Card at the shown revision: the
 * requested capability and the exact account scope, in the existing Card
 * transaction. There is no second Save.
 */
import { useMemo, useState } from 'react';

import { postOp } from '../../api/client';
import type {
  DelegatedAccessRecord,
  DelegatedAccessResourceOption,
  DelegatedControlCardView,
} from '../../api/types';
import { agentGrantWirePayload } from './agentGrantPayload';
import {
  agentCardConsentGrant,
  agentCardOuterGrant,
  consentProviderFor,
  controlNamedOperations,
  operationInControl,
  outerOperationInControl,
  outerPolicyState,
  outsideControlText,
  proposeConsentAccount,
  withProposedAccountClaim,
  type AgentCardConsentRequest,
  type ConsentAccount,
} from './agentCardConsent';
import { resolvePendingServiceCapability } from './pendingGrantProjection';
import { operationRows } from './DelegatedResourceCatalog';
import { OperationInvocationChoice } from './InvocationControls';
import { INVOCATION_MODE_TEXT, type InvocationMode } from './invocationChoice';

type Scope = Record<string, Record<string, string[]>>;

interface Props {
  request: AgentCardConsentRequest;
  card: DelegatedAccessRecord;
  resources: DelegatedAccessResourceOption[];
  accounts: ConsentAccount[];
  providerLabel: (providerId: string) => string;
  accountLabel: (accountId: string) => string;
  resourceLabel: (resource: string) => string;
  controlLabel: string;
  onApproved: () => void;
  onDismiss: () => void;
}

function agentName(clientId: string): { agent: string; app: string } | null {
  const parts = clientId.split(':');
  if (parts[0] !== 'kdcube-agent' || parts.length < 3) return null;
  return { app: parts[1].replace(/@.+$/, ''), agent: parts.slice(2).join(':') };
}

export function AgentCardConsentReview({
  request, card, resources, accounts, providerLabel, accountLabel, resourceLabel, controlLabel,
  onApproved, onDismiss,
}: Props) {
  const capability = useMemo(
    () => resolvePendingServiceCapability(request, resources, operationRows),
    [request, resources],
  );
  const control = (card.control_card || card.project_control) as DelegatedControlCardView | undefined;
  const ceiling = controlNamedOperations(control, request.resource);
  const operationAllowed = operationInControl(ceiling, request.namespace, request.operation);
  const claim = request.accountClaim
    || capability?.accountRequirements.flatMap((requirement) => requirement.claims)[0]
    || '';
  const providerId = claim
    ? consentProviderFor(claim, capability?.accountRequirements || [], accounts, request.accountProviderId)
    : '';
  const proposal = useMemo(
    () => (providerId && claim ? proposeConsentAccount(accounts, providerId, claim, request.accountId) : { kind: 'none' as const }),
    [accounts, providerId, claim, request.accountId],
  );
  const capable = accounts.filter((account) => account.provider_id === providerId
    && (!claim || (account.claims || []).includes(claim)));
  const heldScope: Scope = card.account_scope || {};
  const [scope, setScope] = useState<Scope>(() => (
    proposal.kind === 'named' || proposal.kind === 'sole'
      ? withProposedAccountClaim(heldScope, providerId, proposal.accountId, claim)
      : heldScope
  ));
  const [operationSelected, setOperationSelected] = useState(operationAllowed && Boolean(request.operation));
  // An outer (MCP/REST) operation keeps the pending page's once/always contract:
  // chosen beside that one operation, or fixed by the request, and sent in the
  // same approval with its change id and any request-bound proof.
  const outerAllowed = outerOperationInControl(control, request.resource, request.outerOperation);
  const policy = outerPolicyState(request);
  const [outerSelected, setOuterSelected] = useState(outerAllowed && Boolean(request.outerOperation));
  const [mode, setMode] = useState<InvocationMode | null>(() => (
    policy === 'preset' && (request.invocationPolicy === 'once' || request.invocationPolicy === 'always')
      ? request.invocationPolicy
      : null
  ));
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState('');

  const toggle = (accountId: string, value: string, checked: boolean) => {
    setScope((current) => {
      const accountsMap = { ...(current[providerId] || {}) };
      const held = new Set(accountsMap[accountId] || []);
      if (checked) held.add(value); else held.delete(value);
      if (held.size) accountsMap[accountId] = Array.from(held); else delete accountsMap[accountId];
      const next = { ...current };
      if (Object.keys(accountsMap).length) next[providerId] = accountsMap; else delete next[providerId];
      return next;
    });
  };
  const bound = (accountId: string) => (scope[providerId]?.[accountId] || []).includes(claim);
  const heldBound = (accountId: string) => (heldScope[providerId]?.[accountId] || []).includes(claim);
  const accountChosen = !claim || capable.some((account) => bound(account.account_id));
  const addsSomething = JSON.stringify(scope) !== JSON.stringify(heldScope) || operationSelected || outerSelected;
  const outerReady = !outerSelected || policy === 'none' || (policy !== 'missing' && Boolean(mode));
  const canApprove = !busy && !done && accountChosen && addsSomething && outerReady;

  const approve = async () => {
    setBusy(true);
    setError('');
    try {
      const args = request.outerOperation
        ? agentCardOuterGrant({
          card: card as Parameters<typeof agentCardOuterGrant>[0]['card'],
          request, requiredDoorGrants: [], operationSelected: outerSelected, accountScope: scope, mode,
        })
        : agentCardConsentGrant({
          card, request, requiredDoorGrants: capability?.requiredDoorGrants || [],
          operationSelected, accountScope: scope,
        });
      const answer = await postOp<Record<string, unknown>>('delegated_agent_grant_create', agentGrantWirePayload(args));
      if (answer?.ok === false) {
        const outside = answer.error === 'agent_capability_selection_outside_control'
          ? outsideControlText(answer.details, resourceLabel)
          : '';
        setError(outside || `Connection Hub refused the change: ${String(answer.error || 'unknown reason')}.`);
        return;
      }
      setDone(true);
      onApproved();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'The change could not be sent.');
    } finally {
      setBusy(false);
    }
  };

  const who = agentName(request.clientId);
  const heldClaims = card.resource_grants?.[request.resource] || [];
  return (
    <section className="card card-attention agent-card-consent" data-testid="agent-card-consent">
      <div className="card-head">
        <div className="form-title">An agent is asking for your permission, on its Card</div>
      </div>
      <p style={{ marginTop: 0 }}>
        {who ? <>The agent <strong>{who.agent}</strong> of the app <strong>{who.app}</strong></> : <>The agent <strong>{request.clientId}</strong></>}
        {' '}asks to use more on <strong>{resourceLabel(request.resource)}</strong>. Approving changes its Card below, once.
      </p>
      <div className="agent-card-consent-card account-sub" data-testid="agent-card-consent-card">
        Card <code>{card.access_id}</code> · revision {card.card_revision ?? '—'}
        {' · '}Control: {control?.state === 'active' ? controlLabel : control?.state === 'not_controlled' || !control ? 'none' : 'unavailable, nothing new can be added'}
        {heldClaims.length ? <> · holds <code>{heldClaims.join(', ')}</code></> : null}
      </div>
      {request.namespace && request.operation ? (
        <div className="request-review-section" data-testid="agent-card-consent-operation">
          <div className="account-title">Service operation</div>
          <label className={`namespace-operation${operationAllowed ? '' : ' namespace-operation-unavailable'}`}>
            <input
              type="checkbox"
              checked={operationSelected}
              disabled={!operationAllowed || done}
              onChange={(event) => setOperationSelected(event.target.checked)}
            />
            <span>
              <code>{request.namespace}</code>{' · '}<code>{request.operation}</code>
              {operationAllowed ? null : (
                <span className="badge badge-error" style={{ marginLeft: 8 }}>not in this agent&apos;s Control</span>
              )}
            </span>
          </label>
          {operationAllowed ? null : (
            <small className="muted">The agent&apos;s Control does not allow this operation, so it cannot be granted here.</small>
          )}
        </div>
      ) : null}
      {request.outerOperation ? (
        <div className="request-review-section" data-testid="agent-card-consent-outer">
          <div className="account-title">Tool</div>
          <label className={`namespace-operation${outerAllowed ? '' : ' namespace-operation-unavailable'}`}>
            <input
              type="checkbox"
              checked={outerSelected}
              disabled={!outerAllowed || done}
              onChange={(event) => setOuterSelected(event.target.checked)}
            />
            <span>
              <code>{request.outerOperation}</code>
              {outerAllowed ? null : (
                <span className="badge badge-error" style={{ marginLeft: 8 }}>not in this agent&apos;s Control</span>
              )}
            </span>
          </label>
          {outerAllowed && policy === 'choose' ? (
            <div className="pending-operation-policy">
              <OperationInvocationChoice
                operation={request.outerOperation}
                mode={mode}
                busy={busy || done || !outerSelected}
                onChoose={setMode}
              />
            </div>
          ) : null}
          {outerAllowed && policy === 'preset' && mode ? (
            <small className="muted">Allowed for {INVOCATION_MODE_TEXT[mode]}, as the request asks.</small>
          ) : null}
          {policy === 'missing' ? (
            <div className="notice" style={{ marginTop: 6 }}>
              This request asks for a once/always choice but carries no change to commit it with, so it cannot be approved here.
            </div>
          ) : null}
          {request.requestBound ? (
            <small className="muted">This approval applies to the exact request the agent made.</small>
          ) : null}
        </div>
      ) : null}
      {claim ? (
        <div className="request-review-section agent-card-consent-accounts" data-testid="agent-card-consent-accounts">
          <div className="account-title">
            Which {providerLabel(providerId) || 'connected'} account may this agent use for <code>{claim}</code>?
          </div>
          {proposal.kind === 'choose' ? (
            <div className="notice" style={{ marginTop: 6 }}>Several of your accounts can do this. Choose the one this agent may use.</div>
          ) : null}
          {!capable.length ? (
            <div className="notice" style={{ marginTop: 6 }}>
              None of your connected {providerLabel(providerId) || ''} accounts can do <code>{claim}</code>. Connect one first.
            </div>
          ) : (
            <ul className="accounts">
              {capable.map((account) => {
                const requested = request.accountId === account.account_id
                  || ((proposal.kind === 'sole' || proposal.kind === 'named') && proposal.accountId === account.account_id);
                return (
                  <li className={`account${requested ? ' agent-card-consent-requested' : ''}`} key={account.account_id}>
                    <label style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
                      <input
                        type="checkbox"
                        checked={bound(account.account_id)}
                        disabled={done || heldBound(account.account_id)}
                        onChange={(event) => toggle(account.account_id, claim, event.target.checked)}
                      />
                      <span>
                        <span className="account-title">{accountLabel(account.account_id)}</span>{' '}
                        <code>{claim}</code>
                        {heldBound(account.account_id) ? <span className="badge badge-ok" style={{ marginLeft: 8 }}>already allowed</span> : null}
                        {requested && !heldBound(account.account_id) ? <span className="badge badge-warn" style={{ marginLeft: 8 }}>requested</span> : null}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
      {error ? <div className="error" role="alert">{error}</div> : null}
      {done ? (
        <div className="notice" role="status">Approved. The agent&apos;s Card now allows this; go back and retry the request.</div>
      ) : null}
      <div className="row pending-actions">
        <button className="btn" type="button" disabled={!canApprove} onClick={() => void approve()}>
          {busy ? 'Approving…' : 'Approve on this Card'}
        </button>
        <button className="btn" type="button" onClick={onDismiss}>{done ? 'Close' : 'Not now'}</button>
      </div>
    </section>
  );
}
