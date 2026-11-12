/**
 * W34: the optional "Give this agent more access" section of the Agent Card
 * consent review. Everything the agent's Control allows, on every resource and
 * service, and every connected account's provider permissions; held entries
 * shown ticked and fixed, entries outside the Control shown disabled. It only
 * adds; the Card's Edit narrows. See consentExtras.ts.
 *
 * Every group is its own disclosure: service operations per namespace,
 * permissions (claims) per service, tools, and accounts per provider, so the
 * owner opens only the service they mean.
 */
import type { ReactNode } from 'react';

import type { ConsentAccount } from './agentCardConsent';
import {
  grantGroups,
  type ExtraPicks,
  type ExtraResourceOffer,
  type ExtraRow,
  type NeededProviderClaims,
} from './consentExtras';

type Scope = Record<string, Record<string, string[]>>;

interface Props {
  offer: ExtraResourceOffer[];
  picks: ExtraPicks;
  onPicks: (next: ExtraPicks) => void;
  accounts: ConsentAccount[];
  /** The scope before the extras: what the Card binds plus the request's own choice. */
  baseScope: Scope;
  bindable: Record<string, string[]>;
  providerLabel: (providerId: string) => string;
  accountLabel: (accountId: string) => string;
  disabled: boolean;
  /** What the agent asked for: shown selected, fixed and marked "requested"
   *  wherever it appears in the groups, never counted as something to add. */
  requested?: {
    resource: string;
    namespace?: string;
    operation?: string;
    outerOperation?: string;
    grants: string[];
    accountClaims: Record<string, string[]>;
  };
}

function toggled(values: string[], value: string, checked: boolean): string[] {
  return checked ? Array.from(new Set([...values, value])) : values.filter((item) => item !== value);
}

function countText(rows: readonly ExtraRow[], picked: readonly string[], asked: readonly string[] = []): string {
  const open = rows.filter((row) => row.inControl && !row.held && !asked.includes(row.key));
  const held = rows.filter((row) => row.held).length;
  const requested = rows.filter((row) => !row.held && asked.includes(row.key)).length;
  const chosen = open.filter((row) => picked.includes(row.key)).length;
  return [
    requested ? `${requested} requested` : '',
    chosen ? `${chosen} selected` : '',
    `${open.length} can be added`,
    held ? `${held} already allowed` : '',
  ].filter(Boolean).join(' · ');
}

/** The keys All would select: inside the Control, not held, not requested. */
function addableKeys(rows: readonly ExtraRow[], asked: readonly string[] = []): string[] {
  return rows.filter((row) => row.inControl && !row.held && !asked.includes(row.key)).map((row) => row.key);
}

/** One collapsible group, its summary styled as a control the owner opens. */
function Group({ title, hint, count, testId, children, onAll, onNone, disabled }: {
  title: ReactNode; hint?: string; count: string; testId: string; children: ReactNode;
  /** All / None for the group's addable entries; absent for groups of groups. */
  onAll?: () => void; onNone?: () => void; disabled?: boolean;
}) {
  const press = (action?: () => void) => (event: { preventDefault: () => void; stopPropagation: () => void }) => {
    event.preventDefault();
    event.stopPropagation();
    action?.();
  };
  return (
    <details className="agent-card-consent-group" data-testid={testId}>
      <summary>
        <span className="agent-card-consent-group__title">
          <strong>{title}</strong>
          {hint ? <small>{hint}</small> : null}
        </span>
        {onAll || onNone ? (
          <span className="agent-card-consent-group__bulk">
            <button type="button" disabled={disabled} onClick={press(onAll)} data-testid="agent-card-consent-group-all">All</button>
            <button type="button" disabled={disabled} onClick={press(onNone)} data-testid="agent-card-consent-group-none">None</button>
          </span>
        ) : null}
        <span className="agent-card-consent-group__count">{count}</span>
      </summary>
      <div className="agent-card-consent-group__body">{children}</div>
    </details>
  );
}

export function AgentCardConsentExtras({
  offer, picks, onPicks, accounts, baseScope, bindable, providerLabel, accountLabel, disabled, requested,
}: Props) {
  const askedOperations = (resource: string, namespace: string) => (
    requested && requested.resource === resource && requested.namespace === namespace && requested.operation
      ? [requested.operation] : []);
  const askedGrants = (resource: string) => (requested && requested.resource === resource ? requested.grants : []);
  const askedOuter = (resource: string) => (
    requested && requested.resource === resource && requested.outerOperation ? [requested.outerOperation] : []);
  const providers = Array.from(new Set(accounts.map((account) => account.provider_id))).sort();
  const pickedCount = Object.values(picks.grants).flat().length + Object.values(picks.outer).flat().length
    + Object.values(picks.named).flatMap((byNamespace) => Object.values(byNamespace).flat()).length
    + Object.values(picks.accounts).flatMap((byAccount) => Object.values(byAccount).flat()).length;

  const row = (item: ExtraRow, checked: boolean, onChange: (value: boolean) => void, testId: string, chip = false, asked = false) => {
    if (asked && !item.held) {
      return (
        <label className={`${chip ? 'grant-chip agent-card-consent-chip' : 'namespace-operation agent-card-consent-row'} agent-card-consent-picked agent-card-consent-requested`} key={item.key} data-testid={testId}>
          <input type="checkbox" checked disabled readOnly />
          <span>
            {item.label !== item.key ? <>{item.label} <code>{item.key}</code></> : <code>{item.key}</code>}
            <span className="badge badge-warn" style={{ marginLeft: 8 }}>requested</span>
          </span>
        </label>
      );
    }
    const picked = checked && item.inControl && !item.held;
    const className = [
      chip ? 'grant-chip agent-card-consent-chip' : 'namespace-operation agent-card-consent-row',
      item.held ? 'agent-card-consent-held' : '',
      item.inControl ? '' : 'agent-card-consent-outside',
      picked ? 'agent-card-consent-picked' : '',
    ].filter(Boolean).join(' ');
    return (
      <label className={className} key={item.key} data-testid={testId}>
        <input
          type="checkbox"
          checked={item.held || checked}
          disabled={disabled || item.held || !item.inControl}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>
          {item.label !== item.key ? <>{item.label} <code>{item.key}</code></> : <code>{item.key}</code>}
          {item.held ? <span className="badge badge-ok" style={{ marginLeft: 8 }}>already allowed</span> : null}
          {item.inControl ? null : (
            <span className="badge badge-error" style={{ marginLeft: 8 }}>not in this agent&apos;s Control</span>
          )}
        </span>
      </label>
    );
  };

  return (
    <details className="request-review-section agent-card-consent-extras" data-testid="agent-card-consent-extras">
      <summary className="agent-card-consent-extras__toggle">
        <span>
          <strong>Give this agent more access (optional)</strong>
          <small>Open to choose more service operations, permissions, tools and accounts</small>
        </span>
        {pickedCount ? <span className="badge badge-warn">{pickedCount} selected</span> : null}
      </summary>
      <small className="muted agent-card-consent-extras__note">
        Anything this agent&apos;s Control allows. Nothing here is selected for you, and nothing the Card holds is
        removed; to take access away, edit the Card. What you select here is saved only together with the
        agent&apos;s request above.
      </small>
      {offer.map((item) => {
        const pickedGrants = picks.grants[item.resource] || [];
        const pickedOuter = picks.outer[item.resource] || [];
        return (
          <div className="agent-card-consent-extra-resource" data-testid="agent-card-consent-extra-resource" key={item.resource}>
            <div className="account-title">{item.label}</div>
            {item.namespaces.length ? (
              <Group
                title="Service operations"
                hint="Each service opens on its own; an operation brings the permissions it needs"
                count={`${item.namespaces.length} services`}
                testId="agent-card-consent-extra-operations"
              >
                {item.namespaces.map((namespace) => {
                  const selected = picks.named[item.resource]?.[namespace.namespace] || [];
                  const asked = askedOperations(item.resource, namespace.namespace);
                  const setNamespace = (operations: string[]) => onPicks({
                    ...picks,
                    named: {
                      ...picks.named,
                      [item.resource]: { ...(picks.named[item.resource] || {}), [namespace.namespace]: operations },
                    },
                  });
                  return (
                    <Group
                      key={namespace.namespace}
                      title={<>{namespace.label} <code>{namespace.namespace}</code></>}
                      count={countText(namespace.rows, selected, asked)}
                      testId="agent-card-consent-extra-namespace"
                      disabled={disabled}
                      onAll={() => setNamespace(addableKeys(namespace.rows, asked))}
                      onNone={() => setNamespace([])}
                    >
                      <div className="namespace-operation-list">
                        {namespace.rows.map((operation) => row(
                          operation,
                          selected.includes(operation.key),
                          (checked) => onPicks({
                            ...picks,
                            named: {
                              ...picks.named,
                              [item.resource]: {
                                ...(picks.named[item.resource] || {}),
                                [namespace.namespace]: toggled(selected, operation.key, checked),
                              },
                            },
                          }),
                          'agent-card-consent-extra-operation',
                          false,
                          asked.includes(operation.key),
                        ))}
                      </div>
                    </Group>
                  );
                })}
              </Group>
            ) : null}
            {item.grants.some((grant) => !grant.held) ? (
              <Group
                title="Permissions (claims)"
                hint="Grouped by service; each group opens on its own"
                count={countText(item.grants, pickedGrants, askedGrants(item.resource))}
                testId="agent-card-consent-extra-grants"
              >
                {grantGroups(item.grants).map((group) => (
                  <Group
                    key={group.service}
                    title={<code>{group.service}</code>}
                    count={countText(group.rows, pickedGrants, askedGrants(item.resource))}
                    testId="agent-card-consent-extra-grant-group"
                    disabled={disabled}
                    onAll={() => onPicks({ ...picks, grants: { ...picks.grants, [item.resource]: Array.from(new Set([
                      ...pickedGrants, ...addableKeys(group.rows, askedGrants(item.resource)),
                    ])) } })}
                    onNone={() => onPicks({ ...picks, grants: { ...picks.grants, [item.resource]: pickedGrants
                      .filter((key) => !group.rows.some((entry) => entry.key === key)) } })}
                  >
                    <div className="resource-grants">
                      {group.rows.map((grant) => row(
                        grant,
                        pickedGrants.includes(grant.key),
                        (checked) => onPicks({ ...picks, grants: { ...picks.grants, [item.resource]: toggled(pickedGrants, grant.key, checked) } }),
                        'agent-card-consent-extra-grant',
                        true,
                        askedGrants(item.resource).includes(grant.key),
                      ))}
                    </div>
                  </Group>
                ))}
              </Group>
            ) : null}
            {item.outer.some((operation) => !operation.held) ? (
              <Group
                title="Tools"
                count={countText(item.outer, pickedOuter, askedOuter(item.resource))}
                testId="agent-card-consent-extra-tools"
                disabled={disabled}
                onAll={() => onPicks({ ...picks, outer: { ...picks.outer, [item.resource]: addableKeys(item.outer, askedOuter(item.resource)) } })}
                onNone={() => onPicks({ ...picks, outer: { ...picks.outer, [item.resource]: [] } })}
              >
                <div className="namespace-operation-list">
                  {item.outer.map((operation) => row(
                    operation,
                    pickedOuter.includes(operation.key),
                    (checked) => onPicks({ ...picks, outer: { ...picks.outer, [item.resource]: toggled(pickedOuter, operation.key, checked) } }),
                    'agent-card-consent-extra-tool',
                    false,
                    askedOuter(item.resource).includes(operation.key),
                  ))}
                </div>
              </Group>
            ) : null}
          </div>
        );
      })}
      {providers.map((providerId) => {
        const providerAccounts = accounts.filter((account) => account.provider_id === providerId);
        const needed = bindable[providerId] || [];
        return (
          <Group
            key={providerId}
            title={`${providerLabel(providerId) || providerId} accounts this agent may use`}
            hint="Permissions each account holds; ones this agent's operations need are marked"
            count={needed.length ? `${needed.length} needed` : `${providerAccounts.length} accounts`}
            testId="agent-card-consent-extra-accounts"
          >
            {providerAccounts.map((account) => {
              const held = new Set(baseScope[providerId]?.[account.account_id] || []);
              const chosen = picks.accounts[providerId]?.[account.account_id] || [];
              return (
                <div className="account-provider-account" key={account.account_id}>
                  <div className="account-sub">{accountLabel(account.account_id)}</div>
                  <div className="resource-grants">
                    {(account.claims || []).map((claim) => (
                      <label
                        className={[
                          'grant-chip agent-card-consent-chip',
                          held.has(claim) ? 'agent-card-consent-held' : '',
                          !held.has(claim) && chosen.includes(claim) ? 'agent-card-consent-picked' : '',
                        ].filter(Boolean).join(' ')}
                        key={claim}
                        data-testid="agent-card-consent-extra-account-claim"
                      >
                        <input
                          type="checkbox"
                          checked={held.has(claim) || chosen.includes(claim)}
                          disabled={disabled || held.has(claim)}
                          onChange={(event) => onPicks({
                            ...picks,
                            accounts: {
                              ...picks.accounts,
                              [providerId]: {
                                ...(picks.accounts[providerId] || {}),
                                [account.account_id]: toggled(chosen, claim, event.target.checked),
                              },
                            },
                          })}
                        />
                        <span>{claim}</span>
                        {held.has(claim) ? <span className="badge badge-ok">already allowed</span>
                          : (requested?.accountClaims[providerId] || []).includes(claim)
                            ? <span className="badge badge-warn">requested above</span>
                            : needed.includes(claim) ? <span className="badge badge-warn">needed</span> : null}
                      </label>
                    ))}
                  </div>
                </div>
              );
            })}
          </Group>
        );
      })}
    </details>
  );
}

/** Provider permissions this consent needs that no connected account holds.
 *  Once the request is answered, the button saves what is selected first, then
 *  opens that provider's consent with the agent hand-off. While the request is
 *  not answered nothing can be saved, so the button only opens the provider. */
export function MissingProviderPermissions({ missing, accounts, providerLabel, disabled, canSave, onRequest }: {
  missing: NeededProviderClaims[];
  accounts: ConsentAccount[];
  providerLabel: (providerId: string) => string;
  disabled: boolean;
  canSave: (needed: NeededProviderClaims) => boolean;
  onRequest: (needed: NeededProviderClaims) => void;
}) {
  return (
    <>
      {missing.map((needed) => {
        const label = providerLabel(needed.providerId) || needed.providerId;
        const connected = accounts.some((account) => account.provider_id === needed.providerId);
        const action = connected ? `request in ${label}` : `connect a ${label} account`;
        const saves = canSave(needed);
        return (
          <div className="notice" style={{ marginTop: 8 }} data-testid="agent-card-consent-extra-missing" key={`${needed.providerId}:${needed.connectorAppId}`}>
            <div>
              {connected
                ? <>None of your {label} accounts can do <code>{needed.claims.join(', ')}</code> yet.</>
                : <>No {label} account is connected for <code>{needed.claims.join(', ')}</code>.</>}
              {' '}
              {saves
                ? <>What you selected is saved on the Card first; then {label} asks you to approve. Operations
                  that need <code>{needed.claims.join(', ')}</code> are saved after {label} grants it: choose them again
                  when you return here.</>
                : <>Nothing is saved before the agent&apos;s request above is answered.</>}
            </div>
            <button className="btn" type="button" disabled={disabled} onClick={() => onRequest(needed)}>
              {saves ? `Save and ${action}` : action.charAt(0).toUpperCase() + action.slice(1)}
            </button>
          </div>
        );
      })}
    </>
  );
}
