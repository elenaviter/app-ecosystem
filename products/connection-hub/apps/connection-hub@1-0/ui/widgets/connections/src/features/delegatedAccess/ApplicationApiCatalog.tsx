import { useCallback, useEffect, useMemo, useState } from 'react';
import { getBundlesCatalog } from '../../api/client';
import { InfoMark } from '../../components/InfoMark';
import {
  applicationApiInventory,
  filterApplicationApiInventory,
  type ApplicationApiInventoryEntry,
} from './applicationApiInventory';
import {
  applicationOperationRoleFor,
  platformRoleLabel,
  projectedRoleAllows,
  type ApplicationOperationRolePolicy,
} from './applicationOperationRoles';
import {
  ApplicationDefaultRoleControl,
  ApplicationEffectiveRole,
  ApplicationOperationRoleControl,
} from './ApplicationOperationRoleControls';

type CatalogState = {
  status: 'idle' | 'loading' | 'ready' | 'error';
  applications: ApplicationApiInventoryEntry[];
  error: string;
};

export interface ApplicationApiCatalogModel extends CatalogState {
  retry: () => void;
}

export function useApplicationApiCatalog(enabled: boolean): ApplicationApiCatalogModel {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<CatalogState>({
    status: 'idle',
    applications: [],
    error: '',
  });

  useEffect(() => {
    if (!enabled) return undefined;
    let current = true;
    setState((previous) => ({ ...previous, status: 'loading', error: '' }));
    void getBundlesCatalog()
      .then((payload) => {
        if (!current) return;
        setState({
          status: 'ready',
          applications: applicationApiInventory(payload),
          error: '',
        });
      })
      .catch((error: unknown) => {
        if (!current) return;
        setState((previous) => ({
          ...previous,
          status: 'error',
          error: error instanceof Error ? error.message : 'Application APIs could not be loaded.',
        }));
      });
    return () => {
      current = false;
    };
  }, [enabled, attempt]);

  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  return { ...state, retry };
}

export interface ApplicationApiCatalogProps {
  model: ApplicationApiCatalogModel;
  selectedOperations: string[];
  availableRoles: string[];
  policy: ApplicationOperationRolePolicy;
  effectiveSelection?: {
    operations: string[];
    policy: ApplicationOperationRolePolicy;
    controlLabel: string;
    mode: 'and' | 'or';
  };
  disabled?: boolean;
  onOperationChange: (operationRef: string, checked: boolean) => void;
  onDefaultRoleChange: (role: string) => void;
  onOperationRoleChange: (operationRef: string, role: string) => void;
}

const USER_TYPE_RANK: Record<string, number> = {
  anonymous: 0,
  registered: 1,
  paid: 2,
  privileged: 3,
};

function selectedUserType(role: string): string {
  if ([
    'kdcube:role:super-admin',
    'kdcube:role:admin',
    'kdcube:role:privileged',
  ].includes(role)) return 'privileged';
  if (role === 'kdcube:role:paid') return 'paid';
  return role ? 'registered' : '';
}

function visibilityWarning(
  userTypes: string[],
  requiredRoles: string[],
  projectedRole: string,
): string {
  if (!projectedRole) return 'Choose the role this Card uses.';
  if (requiredRoles.length && !requiredRoles.some((role) => (
    projectedRoleAllows(projectedRole, role)
  ))) {
    return `Requires ${requiredRoles.join(' or ')}.`;
  }
  if (!userTypes.length) return '';
  const current = selectedUserType(projectedRole);
  const currentRank = USER_TYPE_RANK[current];
  const requiredRank = Math.min(...userTypes
    .map((userType) => USER_TYPE_RANK[userType])
    .filter((rank) => rank !== undefined));
  if (Number.isFinite(requiredRank) && currentRank < requiredRank) {
    return `Requires ${userTypes.join(' or ')} user access.`;
  }
  return '';
}

export function ApplicationApiCatalog({
  model,
  selectedOperations,
  availableRoles,
  policy,
  effectiveSelection,
  disabled = false,
  onOperationChange,
  onDefaultRoleChange,
  onOperationRoleChange,
}: ApplicationApiCatalogProps) {
  const [query, setQuery] = useState('');
  const selected = useMemo(() => new Set(selectedOperations), [selectedOperations]);
  const effective = useMemo(
    () => new Set(effectiveSelection?.operations || []),
    [effectiveSelection?.operations],
  );
  const visibleApplications = useMemo(
    () => filterApplicationApiInventory(model.applications, query),
    [model.applications, query],
  );
  const filtered = Boolean(query.trim());
  const knownOperations = useMemo(() => new Set(
    model.applications.flatMap((app) => app.apis.map((api) => api.operationRef).filter(Boolean)),
  ), [model.applications]);
  const staleOperations = Array.from(new Set([
    ...selectedOperations,
    ...(effectiveSelection?.operations || []),
    ...Object.keys(policy.operationRoles),
    ...Object.keys(effectiveSelection?.policy.operationRoles || {}),
  ])).filter((operation) => !knownOperations.has(operation));
  if (model.status === 'idle') return null;
  const apiCount = model.applications.reduce((total, app) => total + app.apis.length, 0);

  return (
    <section className="application-api-catalog" aria-label="Application APIs">
      <div className="application-api-catalog__head">
        <span className="edit-section__name">Application APIs</span>
        <InfoMark text="APIs declared by each app in the current platform bundle catalog." />
        {model.status === 'ready' ? (
          <span className="edit-section__count">
            {model.applications.length} app{model.applications.length === 1 ? '' : 's'} · {' '}
            {effectiveSelection
              ? `${effectiveSelection.operations.length} effective · ${selectedOperations.length} on Caller Card · ${apiCount} available`
              : `${selectedOperations.length} of ${apiCount} selected`}
          </span>
        ) : null}
      </div>

      <ApplicationDefaultRoleControl
        availableRoles={availableRoles}
        policy={policy}
        disabled={disabled}
        onChange={onDefaultRoleChange}
      />
      {effectiveSelection ? (
        <div className="application-role-effective" role="status">
          <strong>Effective default</strong>
          <span className="badge badge-ok">
            {platformRoleLabel(effectiveSelection.policy.defaultRole)}
          </span>
          <small>
            {effectiveSelection.mode.toUpperCase()} with {effectiveSelection.controlLabel}; rows below name the effective invocation role.
          </small>
        </div>
      ) : null}

      {model.status === 'ready' && model.applications.length ? (
        <div className="application-api-catalog__search">
          <input
            type="search"
            value={query}
            aria-label="Search applications and APIs"
            placeholder="Search applications and APIs"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      ) : null}

      {model.status === 'loading' ? (
        <p className="application-api-catalog__state">Loading application APIs...</p>
      ) : null}
      {model.status === 'error' ? (
        <div className="application-api-catalog__error" role="alert">
          <span>{model.error}</span>
          <button type="button" className="btn btn-ghost" onClick={model.retry}>Retry</button>
        </div>
      ) : null}
      {model.status === 'ready' && !model.applications.length ? (
        <p className="application-api-catalog__state">No applications are available.</p>
      ) : null}
      {model.status === 'ready' && model.applications.length && !visibleApplications.length ? (
        <p className="application-api-catalog__state">No applications or APIs match this search.</p>
      ) : null}
      {model.status === 'ready' ? (
        <div className="application-api-catalog__apps">
          {staleOperations.length ? (
            <div className="application-api-stale" role="status">
              <strong>No longer in the active catalog</strong>
              {staleOperations.map((operation) => {
                const callerSelected = selected.has(operation);
                const effectiveSelected = effective.has(operation);
                const callerOverride = Boolean(policy.operationRoles[operation]);
                return (
                  <div className="application-api-stale__row" key={operation}>
                    <input
                      type="checkbox"
                      checked={callerSelected}
                      disabled={disabled || !callerSelected}
                      onChange={() => onOperationChange(operation, false)}
                    />
                    <code>{operation}</code>
                    <span className="badge badge-warn">stale</span>
                    {callerSelected || callerOverride ? (
                      <span className="badge badge-neutral">
                        Caller: {platformRoleLabel(applicationOperationRoleFor(policy, operation))}
                      </span>
                    ) : null}
                    {effectiveSelection && effectiveSelected ? (
                      <ApplicationEffectiveRole
                        operationRef={operation}
                        policy={effectiveSelection.policy}
                      />
                    ) : null}
                    {callerOverride && !callerSelected ? (
                      <ApplicationOperationRoleControl
                        operationRef={operation}
                        availableRoles={availableRoles}
                        policy={policy}
                        disabled={disabled}
                        onChange={(role) => onOperationRoleChange(operation, role)}
                      />
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}
          {visibleApplications.map((app) => {
            const operationRefs = app.apis
              .map((api) => api.operationRef)
              .filter(Boolean);
            const selectedCount = operationRefs
              .filter((operationRef) => selected.has(operationRef)).length;
            const effectiveCount = operationRefs
              .filter((operationRef) => effective.has(operationRef)).length;
            const everyOperationSelected = Boolean(operationRefs.length)
              && selectedCount === operationRefs.length;

            return app.apis.length ? (
              <details
                className="application-api-app"
                key={app.id}
              >
                <summary>
                  <span>
                    <strong>{app.label}</strong>
                    {app.label !== app.id ? <small><code>{app.id}</code></small> : null}
                  </span>
                  <span className="badge badge-neutral">
                    {effectiveSelection
                      ? `${effectiveCount} effective · ${selectedCount} caller`
                      : `${selectedCount} of ${operationRefs.length} selected`}
                  </span>
                  <span className="edit-section__quick" aria-label={`Select displayed APIs for ${app.label}`}>
                    <button
                      type="button"
                      disabled={disabled || everyOperationSelected || !operationRefs.length}
                      onClick={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        operationRefs.forEach((operationRef) => {
                          if (!selected.has(operationRef)) onOperationChange(operationRef, true);
                        });
                      }}
                    >
                      {filtered ? 'All shown' : 'All'}
                    </button>
                    <button
                      type="button"
                      disabled={disabled || selectedCount === 0}
                      onClick={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        operationRefs.forEach((operationRef) => {
                          if (selected.has(operationRef)) onOperationChange(operationRef, false);
                        });
                      }}
                    >
                      {filtered ? 'None shown' : 'None'}
                    </button>
                  </span>
                </summary>
                {app.description ? <p className="application-api-app__description">{app.description}</p> : null}
                <div className="application-api-list">
                  {app.apis.map((api) => {
                    const callerSelected = selected.has(api.operationRef);
                    const callerOverride = Boolean(policy.operationRoles[api.operationRef]);
                    const effectiveSelected = effective.has(api.operationRef);
                    const projectedRole = effectiveSelection && effectiveSelected
                      ? applicationOperationRoleFor(effectiveSelection.policy, api.operationRef)
                      : applicationOperationRoleFor(policy, api.operationRef);
                    const removedByControl = Boolean(effectiveSelection && callerSelected && !effectiveSelected);
                    const addedByControl = Boolean(effectiveSelection && !callerSelected && effectiveSelected);
                    return (
                      <div
                        className={[
                          'application-api-row',
                          callerSelected || effectiveSelected ? 'application-api-row--selected' : '',
                          removedByControl ? 'authority-diff-removed' : '',
                          addedByControl ? 'authority-diff-added' : '',
                        ].filter(Boolean).join(' ')}
                        key={`${app.id}:${api.operationRef || `${api.alias}:${api.method}:${api.route}`}`}
                      >
                        <label className="application-api-choice">
                          <input
                            type="checkbox"
                            checked={Boolean(api.operationRef) && callerSelected}
                            disabled={disabled || !api.operationRef}
                            onChange={(event) => onOperationChange(api.operationRef, event.target.checked)}
                          />
                          <span>
                            <strong>{api.alias}</strong>
                            {!api.operationRef ? (
                              <small className="application-api-warning">Operation identity unavailable</small>
                            ) : null}
                            {api.operationRef && (callerSelected || effectiveSelected) ? (() => {
                              const warning = visibilityWarning(api.userTypes, api.roles, projectedRole);
                              return warning ? <small className="application-api-warning">{warning}</small> : null;
                            })() : null}
                          </span>
                        </label>
                        {callerSelected || callerOverride ? (
                          <ApplicationOperationRoleControl
                            operationRef={api.operationRef}
                            availableRoles={availableRoles}
                            policy={policy}
                            disabled={disabled}
                            onChange={(role) => onOperationRoleChange(api.operationRef, role)}
                          />
                        ) : null}
                        {callerOverride && !callerSelected ? (
                          <small className="application-api-warning" role="alert">
                            This role override is inactive because the operation is not selected. Return it to Default before saving.
                          </small>
                        ) : null}
                        {effectiveSelection && effectiveSelected ? (
                          <div className="application-operation-effective">
                            <ApplicationEffectiveRole
                              operationRef={api.operationRef}
                              policy={effectiveSelection.policy}
                            />
                            {addedByControl ? <span className="badge badge-ok">added by {effectiveSelection.controlLabel}</span> : null}
                          </div>
                        ) : null}
                        {removedByControl ? (
                          <small className="control-cap-note">
                            Selected on the Caller Card, but inactive because {effectiveSelection?.controlLabel} does not select it.
                          </small>
                        ) : null}
                        <dl>
                          <dt>Alias</dt>
                          <dd><code className="application-api-alias">{api.alias}</code></dd>
                          <dt>Operation</dt>
                          <dd>
                            {api.operationId ? <code>{api.operationId}</code> : <span>Not declared</span>}
                            {api.operationIdExplicit ? <span className="badge badge-neutral">shared</span> : null}
                          </dd>
                          <dt>Method</dt>
                          <dd>{api.method ? <code>{api.method}</code> : <span>Not declared</span>}</dd>
                          <dt>Route</dt>
                          <dd>{api.route ? <code>{api.route}</code> : <span>Not declared</span>}</dd>
                          <dt>User types</dt>
                          <dd>
                            {api.userTypes.length
                              ? api.userTypes.map((userType) => <code key={userType}>{userType}</code>)
                              : <span>Any</span>}
                          </dd>
                          <dt>Roles</dt>
                          <dd>
                            {api.roles.length
                              ? api.roles.map((role) => <code key={role}>{role}</code>)
                              : <span>Any</span>}
                          </dd>
                        </dl>
                      </div>
                    );
                  })}
                </div>
              </details>
            ) : (
              <div className="application-api-app application-api-app--empty" key={app.id}>
                <span>
                  <strong>{app.label}</strong>
                  {app.label !== app.id ? <small><code>{app.id}</code></small> : null}
                </span>
                <span>No APIs declared</span>
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
