import { useCallback, useEffect, useMemo, useState } from 'react';
import { getBundlesCatalog } from '../../api/client';
import { InfoMark } from '../../components/InfoMark';
import {
  applicationApiInventory,
  filterApplicationApiInventory,
  type ApplicationApiInventoryEntry,
} from './applicationApiInventory';

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
  selectedRoles: string[];
  disabled?: boolean;
  onOperationChange: (operationRef: string, checked: boolean) => void;
}

const USER_TYPE_RANK: Record<string, number> = {
  anonymous: 0,
  registered: 1,
  paid: 2,
  privileged: 3,
};

function selectedUserType(roles: string[]): string {
  if (roles.some((role) => (
    role === 'kdcube:role:super-admin'
    || role === 'kdcube:role:admin'
    || role === 'kdcube:role:privileged'
  ))) return 'privileged';
  if (roles.includes('kdcube:role:paid')) return 'paid';
  return roles.length ? 'registered' : '';
}

function visibilityWarning(
  userTypes: string[],
  requiredRoles: string[],
  selectedRoles: string[],
): string {
  if (!selectedRoles.length) return 'Choose the role this Card uses.';
  if (requiredRoles.length && !requiredRoles.some((role) => selectedRoles.includes(role))) {
    return `Requires ${requiredRoles.join(' or ')}.`;
  }
  if (!userTypes.length) return '';
  const current = selectedUserType(selectedRoles);
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
  selectedRoles,
  disabled = false,
  onOperationChange,
}: ApplicationApiCatalogProps) {
  const [query, setQuery] = useState('');
  const selected = useMemo(() => new Set(selectedOperations), [selectedOperations]);
  const visibleApplications = useMemo(
    () => filterApplicationApiInventory(model.applications, query),
    [model.applications, query],
  );
  const filtered = Boolean(query.trim());
  const knownOperations = useMemo(() => new Set(
    model.applications.flatMap((app) => app.apis.map((api) => api.operationRef).filter(Boolean)),
  ), [model.applications]);
  const staleOperations = selectedOperations.filter((operation) => !knownOperations.has(operation));
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
            {selectedOperations.length} of {apiCount} selected
          </span>
        ) : null}
      </div>

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
              {staleOperations.map((operation) => (
                <label className="application-api-stale__row" key={operation}>
                  <input
                    type="checkbox"
                    checked
                    disabled={disabled}
                    onChange={() => onOperationChange(operation, false)}
                  />
                  <code>{operation}</code>
                  <span className="badge badge-warn">stale</span>
                </label>
              ))}
            </div>
          ) : null}
          {visibleApplications.map((app) => {
            const operationRefs = app.apis
              .map((api) => api.operationRef)
              .filter(Boolean);
            const selectedCount = operationRefs
              .filter((operationRef) => selected.has(operationRef)).length;
            const everyOperationSelected = Boolean(operationRefs.length)
              && selectedCount === operationRefs.length;

            return app.apis.length ? (
              <details className="application-api-app" key={app.id}>
                <summary>
                  <span>
                    <strong>{app.label}</strong>
                    {app.label !== app.id ? <small><code>{app.id}</code></small> : null}
                  </span>
                  <span className="badge badge-neutral">
                    {selectedCount} of {operationRefs.length} selected
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
                  {app.apis.map((api) => (
                    <div
                      className={`application-api-row${selected.has(api.operationRef) ? ' application-api-row--selected' : ''}`}
                      key={`${app.id}:${api.operationRef || `${api.alias}:${api.method}:${api.route}`}`}
                    >
                      <label className="application-api-choice">
                        <input
                          type="checkbox"
                          checked={Boolean(api.operationRef) && selected.has(api.operationRef)}
                          disabled={disabled || !api.operationRef}
                          onChange={(event) => onOperationChange(api.operationRef, event.target.checked)}
                        />
                        <span>
                          <strong>{api.alias}</strong>
                          {!api.operationRef ? (
                            <small className="application-api-warning">Operation identity unavailable</small>
                          ) : null}
                          {api.operationRef && selected.has(api.operationRef) ? (() => {
                            const warning = visibilityWarning(api.userTypes, api.roles, selectedRoles);
                            return warning ? <small className="application-api-warning">{warning}</small> : null;
                          })() : null}
                        </span>
                      </label>
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
                  ))}
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
