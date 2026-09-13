import { useCallback, useEffect, useState } from 'react';
import { getBundlesCatalog } from '../../api/client';
import { InfoMark } from '../../components/InfoMark';
import {
  applicationApiInventory,
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

export function ApplicationApiCatalog({ model }: { model: ApplicationApiCatalogModel }) {
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
            {apiCount} API{apiCount === 1 ? '' : 's'}
          </span>
        ) : null}
      </div>

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
      {model.status === 'ready' ? (
        <div className="application-api-catalog__apps">
          {model.applications.map((app) => (
            app.apis.length ? (
              <details className="application-api-app" key={app.id}>
                <summary>
                  <span>
                    <strong>{app.label}</strong>
                    {app.label !== app.id ? <small><code>{app.id}</code></small> : null}
                  </span>
                  <span className="badge badge-neutral">
                    {app.apis.length} API{app.apis.length === 1 ? '' : 's'}
                  </span>
                </summary>
                {app.description ? <p className="application-api-app__description">{app.description}</p> : null}
                <div className="application-api-list">
                  {app.apis.map((api) => (
                    <div className="application-api-row" key={`${app.id}:${api.alias}:${api.method}:${api.route}`}>
                      <dl>
                        <dt>Alias</dt>
                        <dd><code className="application-api-alias">{api.alias}</code></dd>
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
            )
          ))}
        </div>
      ) : null}
    </section>
  );
}
