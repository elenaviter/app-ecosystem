/**
 * W34: the optional "Give this agent more access" section of the Agent Card
 * consent review. Everything the agent's Control allows, on every resource and
 * service, and every connected account's provider permissions; held entries
 * shown ticked and fixed, entries outside the Control shown disabled. It only
 * adds; the Card's Edit narrows. See consentExtras.ts for the decisions.
 */
import type { ConsentAccount } from './agentCardConsent';
import type {
  ExtraPicks,
  ExtraResourceOffer,
  ExtraRow,
  NeededProviderClaims,
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
}

function toggled(values: string[], value: string, checked: boolean): string[] {
  return checked ? Array.from(new Set([...values, value])) : values.filter((item) => item !== value);
}

function OUTSIDE() {
  return <span className="badge badge-error" style={{ marginLeft: 8 }}>not in this agent&apos;s Control</span>;
}

export function AgentCardConsentExtras({
  offer, picks, onPicks, accounts, baseScope, bindable, providerLabel, accountLabel, disabled,
}: Props) {
  const providers = Array.from(new Set(accounts.map((account) => account.provider_id))).sort();
  const pickedCount = Object.values(picks.grants).flat().length + Object.values(picks.outer).flat().length
    + Object.values(picks.named).flatMap((byNamespace) => Object.values(byNamespace).flat()).length
    + Object.values(picks.accounts).flatMap((byAccount) => Object.values(byAccount).flat()).length;

  const row = (item: ExtraRow, checked: boolean, onChange: (value: boolean) => void, testId: string) => (
    <label
      className={`namespace-operation${item.inControl ? '' : ' namespace-operation-unavailable'}`}
      key={item.key}
      data-testid={testId}
    >
      <input
        type="checkbox"
        checked={item.held || checked}
        disabled={disabled || item.held || !item.inControl}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        {item.label !== item.key ? <>{item.label} <code>{item.key}</code></> : <code>{item.key}</code>}
        {item.held ? <span className="badge badge-ok" style={{ marginLeft: 8 }}>already allowed</span> : null}
        {item.inControl ? null : OUTSIDE()}
      </span>
    </label>
  );

  return (
    <details className="request-review-section agent-card-consent-extras" data-testid="agent-card-consent-extras">
      <summary className="account-title" style={{ cursor: 'pointer' }}>
        Give this agent more access (optional)
        {pickedCount ? <span className="badge badge-warn" style={{ marginLeft: 8 }}>{pickedCount} selected</span> : null}
      </summary>
      <small className="muted">
        Anything this agent&apos;s Control allows. Nothing here is selected for you, and nothing the Card holds is
        removed; to take access away, edit the Card.
      </small>
      {offer.map((item) => (
        <div className="agent-card-consent-extra-resource" data-testid="agent-card-consent-extra-resource" key={item.resource}>
          <div className="account-title">{item.label}</div>
          {item.grants.some((grant) => !grant.held) ? (
            <div className="resource-grants">
              {item.grants.map((grant) => row(
                grant,
                (picks.grants[item.resource] || []).includes(grant.key),
                (checked) => onPicks({ ...picks, grants: { ...picks.grants, [item.resource]: toggled(picks.grants[item.resource] || [], grant.key, checked) } }),
                'agent-card-consent-extra-grant',
              ))}
            </div>
          ) : null}
          {item.outer.some((operation) => !operation.held) ? (
            <div className="namespace-operation-list">
              {item.outer.map((operation) => row(
                operation,
                (picks.outer[item.resource] || []).includes(operation.key),
                (checked) => onPicks({ ...picks, outer: { ...picks.outer, [item.resource]: toggled(picks.outer[item.resource] || [], operation.key, checked) } }),
                'agent-card-consent-extra-tool',
              ))}
            </div>
          ) : null}
          {item.namespaces.map((namespace) => {
            const selected = picks.named[item.resource]?.[namespace.namespace] || [];
            const addable = namespace.rows.filter((entry) => entry.inControl && !entry.held).length;
            return (
              <details className="namespace-boundary" key={namespace.namespace} data-testid="agent-card-consent-extra-namespace">
                <summary>
                  <span><strong>{namespace.label}</strong> <small>{namespace.namespace}</small></span>
                  <span className="badge">{selected.length ? `${selected.length} of ${addable} to add` : `${addable} can be added`}</span>
                </summary>
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
                  ))}
                </div>
              </details>
            );
          })}
        </div>
      ))}
      {providers.map((providerId) => (
        <div className="agent-card-consent-extra-accounts" data-testid="agent-card-consent-extra-accounts" key={providerId}>
          <div className="account-title">{providerLabel(providerId) || providerId} accounts this agent may use</div>
          {accounts.filter((account) => account.provider_id === providerId).map((account) => {
            const held = new Set(baseScope[providerId]?.[account.account_id] || []);
            const chosen = picks.accounts[providerId]?.[account.account_id] || [];
            return (
              <div className="account-provider-account" key={account.account_id}>
                <div className="account-sub">{accountLabel(account.account_id)}</div>
                <div className="resource-grants">
                  {(account.claims || []).map((claim) => (
                    <label className="grant-chip" key={claim} data-testid="agent-card-consent-extra-account-claim">
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
                        : (bindable[providerId] || []).includes(claim) ? <span className="badge badge-warn">needed</span> : null}
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      ))}
    </details>
  );
}

/** Provider permissions the Card's operations need that no connected account
 *  holds. The button saves what is selected first, then opens that provider's
 *  consent with the agent hand-off (the colleague, 2026-11-11: "так, варіант 2"). */
export function MissingProviderPermissions({ missing, accounts, providerLabel, disabled, onSaveAndRequest }: {
  missing: NeededProviderClaims[];
  accounts: ConsentAccount[];
  providerLabel: (providerId: string) => string;
  disabled: boolean;
  onSaveAndRequest: (needed: NeededProviderClaims) => void;
}) {
  return (
    <>
      {missing.map((needed) => {
      const label = providerLabel(needed.providerId) || needed.providerId;
      const connected = accounts.some((account) => account.provider_id === needed.providerId);
      return (
        <div className="notice" style={{ marginTop: 8 }} data-testid="agent-card-consent-extra-missing" key={`${needed.providerId}:${needed.connectorAppId}`}>
          <div>
            {connected
              ? <>None of your {label} accounts can do <code>{needed.claims.join(', ')}</code> yet.</>
              : <>No {label} account is connected for <code>{needed.claims.join(', ')}</code>.</>}
            {' '}What you selected is saved on the Card first; then {label} asks you to approve.
          </div>
          <button className="btn" type="button" disabled={disabled} onClick={() => onSaveAndRequest(needed)}>
            {connected ? `Save and request in ${label}` : `Save and connect a ${label} account`}
          </button>
        </div>
      );
    })}
    </>
  );
}
