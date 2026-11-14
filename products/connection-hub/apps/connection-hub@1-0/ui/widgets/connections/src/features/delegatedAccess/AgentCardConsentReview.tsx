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
  accountSatisfying,
  agentCardConsentGrant,
  agentCardOuterGrant,
  capableAccounts,
  consentRequirements,
  controlNamedOperations,
  controlUnavailable,
  newRequestedClaims,
  operationInControl,
  outerOperationInControl,
  outerPolicyState,
  outsideControlText,
  proposeConsentAccount,
  withAccountClaims,
  type AgentCardConsentRequest,
  type ConsentAccount,
} from './agentCardConsent';
import { accountClaimsForOperation, resolvePendingServiceCapability } from './pendingGrantProjection';
import { operationRows } from './DelegatedResourceCatalog';
import { OperationInvocationChoice } from './InvocationControls';
import { AgentCardConsentExtras, MissingProviderPermissions, FixedChoice } from './AgentCardConsentExtras';
import {
  allowedPicks,
  consentExtraGrants,
  consentExtrasOffer,
  hasExtraPicks,
  neededProviderClaims,
  NO_EXTRA_PICKS,
  unboundPickedClaims,
  providerUpgradeHref,
  withExtraAccounts,
  type ExtraPicks,
  type NeededProviderClaims,
} from './consentExtras';
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
  /** Opens a provider consent link; the browser location by default (a test can record it). */
  navigate?: (href: string) => void;
}

function agentName(clientId: string): { agent: string; app: string } | null {
  const parts = clientId.split(':');
  if (parts[0] !== 'kdcube-agent' || parts.length < 3) return null;
  return { app: parts[1].replace(/@.+$/, ''), agent: parts.slice(2).join(':') };
}

export function AgentCardConsentReview({
  request, card, resources, accounts, providerLabel, accountLabel, resourceLabel, controlLabel,
  onApproved, onDismiss, navigate = (href) => window.location.assign(href),
}: Props) {
  const capability = useMemo(
    () => resolvePendingServiceCapability(request, resources, operationRows),
    [request, resources],
  );
  const control = (card.control_card || card.project_control) as DelegatedControlCardView | undefined;
  const ceiling = controlNamedOperations(control, request.resource);
  const operationAllowed = operationInControl(ceiling, request.namespace, request.operation);
  // Every account requirement of the request: the explicit account demand
  // (provider, claim, exact account) or each requirement of the requested
  // operation with all its claims. One account per requirement holds them all.
  const requirements = useMemo(
    () => consentRequirements(request, capability?.accountRequirements || [], accounts),
    [request, capability, accounts],
  );
  const heldScope: Scope = card.account_scope || {};
  const proposals = useMemo(
    () => requirements.map((requirement) => proposeConsentAccount(accounts, requirement)),
    [requirements, accounts],
  );
  // The account chosen per requirement ('' = none yet). A requirement the Card
  // already satisfies keeps that account; a named or sole capable account is proposed.
  const [chosen, setChosen] = useState<string[]>(() => requirements.map((requirement, index) => {
    const held = accountSatisfying(heldScope, requirement);
    if (held) return held;
    const proposal = proposals[index];
    return proposal.kind === 'named' || proposal.kind === 'sole' ? proposal.accountId : '';
  }));
  const scope: Scope = requirements.reduce((next, requirement, index) => (
    chosen[index] ? withAccountClaims(next, requirement.providerId, chosen[index], requirement.claims) : next
  ), heldScope);
  // What the agent asked for is fixed: shown selected and not removable here.
  // A request is answered by choosing its account (and an asked once/always),
  // never by unticking what was asked (W34 live check).
  const operationSelected = operationAllowed && Boolean(request.operation);
  // An outer (MCP/REST) operation keeps the pending page's once/always contract:
  // chosen beside that one operation, or fixed by the request, and sent in the
  // same approval with its change id and any request-bound proof.
  const outerAllowed = outerOperationInControl(control, request.resource, request.outerOperation);
  const policy = outerPolicyState(request);
  const outerSelected = outerAllowed && Boolean(request.outerOperation);
  const [mode, setMode] = useState<InvocationMode | null>(() => (
    policy === 'preset' && (request.invocationPolicy === 'once' || request.invocationPolicy === 'always')
      ? request.invocationPolicy
      : null
  ));
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState('');

  const satisfied = requirements.every((requirement, index) => {
    const accountId = chosen[index];
    if (!accountId) return false;
    if (requirement.accountId && accountId !== requirement.accountId) return false;
    return capableAccounts(accounts, requirement).some((account) => account.account_id === accountId)
      || Boolean(accountSatisfying(heldScope, { ...requirement, accountId }));
  });
  const heldClaims = card.resource_grants?.[request.resource] || [];
  const addsClaims = newRequestedClaims(heldClaims, request.claims).length > 0;
  const operationHeld = (() => {
    const stored = card.named_service_operations;
    if (!request.namespace || !request.operation) return false;
    return stored === '*' || Boolean(stored?.[request.resource]?.[request.namespace]?.includes(request.operation));
  })();
  const requestAdds = JSON.stringify(scope) !== JSON.stringify(heldScope)
    || (operationSelected && !operationHeld) || outerSelected || addsClaims;
  const outerReady = !outerSelected || policy === 'none' || (policy !== 'missing' && Boolean(mode));
  const blockedByControl = controlUnavailable(control);
  // W34: more access beyond the request, only added (merge), within the Control.
  const [picks, setPicks] = useState<ExtraPicks>(NO_EXTRA_PICKS);
  const offer = useMemo(() => consentExtrasOffer(card, control, resources, operationRows), [card, control, resources]);
  const extras = allowedPicks(offer, picks);
  const fullScope = withExtraAccounts(scope, extras.accounts, accounts);
  const needed = neededProviderClaims(card, resources, extras, accounts, fullScope, operationRows);
  // The request's own account requirement that no connected account can meet:
  // its provider is asked first, and nothing is saved before it is answered.
  const requestMissing: NeededProviderClaims[] = requirements.flatMap((requirement, index) => (
    proposals[index].kind === 'none' && !accountSatisfying(heldScope, requirement)
      ? [{ providerId: requirement.providerId, connectorAppId: '', resource: request.resource, claims: requirement.claims }]
      : []
  ));
  const missing = [...requestMissing, ...needed.missing.filter((item) => !requestMissing
    .some((first) => first.providerId === item.providerId && item.claims.every((claim) => first.claims.includes(claim))))];
  const extrasAdd = hasExtraPicks(extras) && (
    JSON.stringify(fullScope) !== JSON.stringify(scope)
    || Object.keys(extras.grants).length + Object.keys(extras.outer).length + Object.keys(extras.named).length > 0
  );
  // The agent's request is answered first: every account it asks for is chosen
  // and an asked once/always choice is made. Until then nothing is approved,
  // the extras included; they are saved only together with the request.
  const requestAnswered = satisfied && outerReady;
  // An operation picked here whose provider permission no account binds would
  // be refused by Connection Hub; it is named, and nothing is sent until bound.
  const unbound = unboundPickedClaims(card, resources, offer, extras, fullScope);
  const canApprove = !busy && !done && !blockedByControl && requestAnswered && !unbound.length
    && (requestAdds || extrasAdd);

  /** The writes, in order: the request's own grant (when answerable), then one
   *  merge per resource the extras touch. The Card-wide account scope travels
   *  with the first write. Each write is made against the revision the
   *  previous one produced. */
  const writes = (chosen = extras) => {
    const out: Array<Parameters<typeof agentGrantWirePayload>[0]> = [];
    if (requestAdds) {
      out.push(request.outerOperation
        ? agentCardOuterGrant({
          card: card as Parameters<typeof agentCardOuterGrant>[0]['card'],
          request, requiredDoorGrants: [], operationSelected: outerSelected, accountScope: fullScope, mode,
        })
        : agentCardConsentGrant({
          card, request, requiredDoorGrants: capability?.requiredDoorGrants || [],
          operationSelected, accountScope: fullScope,
        }));
    }
    let extraGrants = consentExtraGrants(card, request, offer, chosen);
    // The extras on the requested resource travel in the request's own write, so
    // the request and what was added beside it on that resource commit together.
    // A focused once/always grant stays exactly that operation and is not merged.
    const base = out[0];
    const same = extraGrants.find((grant) => grant.resource === request.resource);
    if (base && same && !base.invocationMode) {
      out[0] = {
        ...base,
        claims: Array.from(new Set([...base.claims, ...same.claims])),
        ...(same.resourceOperations ? { resourceOperations: Array.from(new Set([...(base.resourceOperations || []), ...same.resourceOperations])) } : {}),
        ...(same.namedServiceOperations ? {
          namedServiceOperations: Object.fromEntries(Array.from(new Set([
            ...Object.keys(base.namedServiceOperations || {}), ...Object.keys(same.namedServiceOperations),
          ])).map((namespace) => [namespace, Array.from(new Set([
            ...(base.namedServiceOperations?.[namespace] || []), ...(same.namedServiceOperations?.[namespace] || []),
          ]))])),
        } : {}),
      };
      extraGrants = extraGrants.filter((grant) => grant !== same);
    }
    extraGrants.forEach((grant, index) => {
      out.push(!out.length && index === 0 ? { ...grant, accountScope: fullScope } : grant);
    });
    if (!out.length && JSON.stringify(fullScope) !== JSON.stringify(heldScope)) {
      out.push({ clientId: request.clientId, accessId: card.access_id, resource: request.resource, claims: heldClaims, accountScope: fullScope });
    }
    return out;
  };

  const approve = async (chosen = extras): Promise<boolean> => {
    setBusy(true);
    setError('');
    try {
      let revision: number | undefined = typeof card.card_revision === 'number' ? card.card_revision : undefined;
      for (const args of writes(chosen)) {
        const answer = await postOp<Record<string, unknown>>('delegated_agent_grant_create', agentGrantWirePayload({
          ...args, expectedCardRevision: revision,
        }));
        if (answer?.ok === false) {
          const outside = answer.error === 'agent_capability_selection_outside_control'
            ? outsideControlText(answer.details, resourceLabel)
            : '';
          setError(outside || `Connection Hub refused the change: ${String(answer.error || 'unknown reason')}.`);
          return false;
        }
        const next = (answer?.access as { card_revision?: unknown } | undefined)?.card_revision;
        revision = typeof next === 'number' ? next : undefined;
      }
      setDone(true);
      onApproved();
      return true;
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'The change could not be sent.');
      return false;
    } finally {
      setBusy(false);
    }
  };

  // Once the request is answered: save what is selected first, then go to the
  // provider; a refusal stays here. Before that, only go to the provider.
  // Operations that need the permission being requested cannot be saved before
  // the provider grants it; they are left out of this save and named.
  const withoutDependents = (target: NeededProviderClaims): ExtraPicks => {
    const needsTarget = (resource: string, namespace: string, operation: string) => {
      const option = resources.find((item) => item.resource === resource)?.named_services
        ?.find((item) => item.namespace === namespace);
      return Boolean(option) && accountClaimsForOperation(option!, operation).some((need) => (
        need.providerId === target.providerId && need.claims.some((claim) => target.claims.includes(claim))));
    };
    const named: ExtraPicks['named'] = {};
    Object.entries(extras.named).forEach(([resource, byNamespace]) => {
      Object.entries(byNamespace).forEach(([namespace, operations]) => {
        const kept = operations.filter((operation) => !needsTarget(resource, namespace, operation));
        if (kept.length) named[resource] = { ...(named[resource] || {}), [namespace]: kept };
      });
    });
    return { ...extras, named };
  };
  const canSaveBefore = (target: NeededProviderClaims): boolean => {
    const chosen = withoutDependents(target);
    const scopeFor = withExtraAccounts(scope, chosen.accounts, accounts);
    return !busy && !done && !blockedByControl && requestAnswered
      && !unboundPickedClaims(card, resources, offer, chosen, scopeFor).length
      && (requestAdds || hasExtraPicks(chosen));
  };
  const saveAndRequest = async (target: NeededProviderClaims) => {
    const href = providerUpgradeHref(window.location.href, target, request.clientId, accounts);
    if (!href) return;
    if (canSaveBefore(target) && !(await approve(withoutDependents(target)))) return;
    navigate(href);
  };

  const who = agentName(request.clientId);
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
          <label className={`namespace-operation agent-card-consent-row${operationAllowed ? ' agent-card-consent-picked' : ' agent-card-consent-outside'}`}>
            {operationSelected ? <FixedChoice /> : <input type="checkbox" checked={false} disabled readOnly />}
            <span>
              <code>{request.namespace}</code>{' · '}<code>{request.operation}</code>
              {operationAllowed ? (
                <span className="badge badge-warn" style={{ marginLeft: 8 }}>{operationHeld ? 'already allowed' : 'requested'}</span>
              ) : null}
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
            {outerSelected ? <FixedChoice /> : <input type="checkbox" checked={false} disabled readOnly />}
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
      {newRequestedClaims(heldClaims, request.claims).length ? (
        <div className="request-review-section" data-testid="agent-card-consent-claims">
          <div className="account-title">Permissions to add</div>
          <code>{newRequestedClaims(heldClaims, request.claims).join(', ')}</code>
        </div>
      ) : null}
      {requirements.map((requirement, index) => {
        const capable = capableAccounts(accounts, requirement);
        const proposal = proposals[index];
        const heldAccount = accountSatisfying(heldScope, requirement);
        const claimsText = requirement.claims.join(' and ');
        return (
          <div
            className="request-review-section agent-card-consent-accounts"
            data-testid="agent-card-consent-accounts"
            key={`${requirement.providerId}:${requirement.claims.join(',')}`}
          >
            <div className="account-title">
              Which {providerLabel(requirement.providerId) || 'connected'} account may this agent use for <code>{claimsText}</code>?
            </div>
            {proposal.kind === 'named_unavailable' && !heldAccount ? (
              <div className="notice" style={{ marginTop: 6 }} data-testid="agent-card-consent-named-unavailable">
                The agent asked for the account <strong>{accountLabel(proposal.accountId)}</strong>, which is not connected
                or cannot do <code>{claimsText}</code>. Connect or upgrade that account first; another account does not
                answer this request.
              </div>
            ) : null}
            {proposal.kind === 'choose' && !heldAccount ? (
              <div className="notice" style={{ marginTop: 6 }}>Several of your accounts can do this. Choose the one this agent may use.</div>
            ) : null}
            {proposal.kind === 'none' && !heldAccount ? (
              <div className="notice" style={{ marginTop: 6 }}>
                None of your connected {providerLabel(requirement.providerId) || ''} accounts can do <code>{claimsText}</code>. Connect one first.
              </div>
            ) : null}
            {capable.length && !(requirement.accountId && proposal.kind === 'named_unavailable') ? (
              <ul className="accounts">
                {capable
                  .filter((account) => !requirement.accountId || account.account_id === requirement.accountId)
                  .map((account) => {
                    const held = heldAccount === account.account_id;
                    const requested = requirement.accountId === account.account_id
                      || (proposal.kind === 'sole' && proposal.accountId === account.account_id);
                    return (
                      <li className={`account${requested ? ' agent-card-consent-requested' : ''}`} key={account.account_id}>
                        <label style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
                          {held ? <FixedChoice type="radio" name={`agent-card-consent-${index}`} /> : (
                            <input
                              type="radio"
                              name={`agent-card-consent-${index}`}
                              checked={chosen[index] === account.account_id}
                              disabled={done || Boolean(heldAccount)}
                              onChange={() => setChosen((current) => current.map((value, at) => (at === index ? account.account_id : value)))}
                            />
                          )}
                          <span>
                            <span className="account-title">{accountLabel(account.account_id)}</span>{' '}
                            <code>{claimsText}</code>
                            {held ? <span className="badge badge-ok" style={{ marginLeft: 8 }}>already allowed</span> : null}
                            {requested && !held ? <span className="badge badge-warn" style={{ marginLeft: 8 }}>requested</span> : null}
                          </span>
                        </label>
                      </li>
                    );
                  })}
              </ul>
            ) : null}
          </div>
        );
      })}
      {blockedByControl ? null : (
        <AgentCardConsentExtras
          offer={offer}
          picks={picks}
          onPicks={setPicks}
          accounts={accounts}
          baseScope={scope}
          requested={{
            resource: request.resource,
            namespace: operationAllowed ? request.namespace : undefined,
            operation: operationAllowed ? request.operation : undefined,
            outerOperation: outerAllowed ? request.outerOperation : undefined,
            grants: Array.from(new Set([
              ...request.claims,
              ...(operationAllowed && request.operation ? capability?.requiredDoorGrants || [] : []),
            ])),
            accountClaims: requirements.reduce<Record<string, string[]>>((out, requirement) => ({
              ...out,
              [requirement.providerId]: Array.from(new Set([...(out[requirement.providerId] || []), ...requirement.claims])),
            }), {}),
          }}
          bindable={needed.bindable}
          providerLabel={providerLabel}
          accountLabel={accountLabel}
          disabled={busy || done}
        />
      )}
      {blockedByControl ? null : (
        <MissingProviderPermissions
          missing={missing}
          accounts={accounts}
          providerLabel={providerLabel}
          disabled={busy}
          canSave={canSaveBefore}
          onRequest={(target) => void saveAndRequest(target)}
        />
      )}
      {requestAnswered && !requestAdds && !done ? (
        <div className="notice" role="status" data-testid="agent-card-consent-request-held">
          What the agent asked for is already allowed on this Card. You can still add more below.
        </div>
      ) : null}
      {unbound.length ? (
        <div className="notice" role="status" data-testid="agent-card-consent-unbound">
          Choose an account for these permissions before approving; the operations you added need them:
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {unbound.map((item) => (
              <li key={`${item.providerId}:${item.claim}`}>
                <code>{item.claim}</code> ({providerLabel(item.providerId) || item.providerId}) for {item.operations.map((op) => <code key={op} style={{ marginRight: 4 }}>{op}</code>)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {blockedByControl ? (
        <div className="notice" role="status">This agent&apos;s Control cannot be read now, so nothing can be added to its Card. Try again later.</div>
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
