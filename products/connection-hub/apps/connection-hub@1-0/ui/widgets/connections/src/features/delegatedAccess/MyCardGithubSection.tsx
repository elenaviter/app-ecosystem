import { useCallback, useEffect, useState } from 'react';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { postOp } from '../../api/client';
import { startDelegatedToKdcubeOAuth } from '../delegatedToKdcube/delegatedToKdcubeSlice';
import {
  commitEmailRequest,
  githubLinkRequest,
  githubStatusLine,
  githubStatusRequest,
  githubUnlinkRequest,
  linkableAccounts,
  ownerAction,
  repositoriesFromSearch,
  repositoryStateLabel,
  type GithubConnectHint,
  type GithubKeyStatus,
  type OperationRequest,
} from './myCardGithub';

function textError(error: unknown): string {
  return error instanceof Error ? error.message : String(error || 'Request failed');
}

async function run(request: OperationRequest, fallback: string): Promise<GithubKeyStatus> {
  const result = await postOp<GithubKeyStatus>(request.operation, request.data);
  if (result?.ok === false) throw new Error(result.message || result.error || fallback);
  return result;
}

const BADGE = { ok: 'badge badge-ok', warn: 'badge badge-warn', off: 'badge badge-neutral' } as const;

// The GitHub key and commit email on a person's project My Card (W371).
export function MyCardGithubSection({ projectRef }: { projectRef: string }) {
  const dispatch = useAppDispatch();
  const accounts = useAppSelector((s) => s.delegatedToKdcube.accounts);
  const [repositories] = useState(() => repositoriesFromSearch(window.location.search));
  const [status, setStatus] = useState<GithubKeyStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [email, setEmail] = useState('');

  const refresh = useCallback(async () => {
    const result = await run(githubStatusRequest(projectRef, repositories), 'GitHub status failed');
    setStatus(result);
    setEmail(result.commit_email || '');
  }, [projectRef, repositories]);

  // Re-read when the person's connected accounts change: returning from the
  // GitHub sign-in reloads them (App's refresh), and the link may follow.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    refresh()
      .catch((err) => {
        if (!cancelled) setError(textError(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refresh, accounts]);

  const act = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await work();
      await refresh();
    } catch (err) {
      setError(textError(err));
    } finally {
      setBusy(false);
    }
  };

  const connect = (hint: GithubConnectHint, accountId?: string) => act(async () => {
    const result = await dispatch(startDelegatedToKdcubeOAuth({
      providerId: hint.provider_id,
      connectorAppId: hint.connector_app_id,
      claims: hint.claims,
      returnHint: window.location.href,
      accountId,
    })).unwrap();
    if (!result?.authorize_url) throw new Error('GitHub sign-in could not start');
    try {
      sessionStorage.setItem('kdc-oauth-pending', '1');
    } catch {
      // Storage unavailable: the BroadcastChannel push still refreshes.
    }
    window.open(result.authorize_url, '_blank', 'noopener,noreferrer');
  });

  if (loading && !status) return <p className="muted">Checking GitHub…</p>;

  const line = githubStatusLine(status);
  const state = status?.connection_state || 'not_connected';
  const candidates = state === 'not_connected' ? linkableAccounts(accounts || [], status) : [];

  return (
    <div className="my-card-github">
      <div className="card-fields">
        <span className="card-field-label">GitHub</span>
        <span className="card-field-value">
          <span className={BADGE[line.tone]}>{line.text}</span>
        </span>
        <span className="card-field-label">Commit email</span>
        <span className="card-field-value">
          <input
            type="email"
            value={email}
            placeholder="The email your agents' commits carry on this project"
            onChange={(event) => setEmail(event.target.value)}
            disabled={busy}
          />
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy || email.trim() === (status?.commit_email || '')}
            onClick={() => act(() => run(commitEmailRequest(projectRef, email), 'Commit email was not saved'))}
          >
            Save
          </button>
        </span>
      </div>
      {state === 'connected' && status?.owners?.length ? (
        <ul className="my-card-github-owners">
          {status.owners.map((owner) => {
            const fix = ownerAction(owner);
            return (
              <li key={owner.owner}>
                <strong>{owner.owner}</strong>
                <ul>
                  {owner.repositories.map((row) => (
                    <li key={row.repository}>
                      <code>{row.repository}</code> — {repositoryStateLabel(row.state)}
                    </li>
                  ))}
                </ul>
                {fix ? (
                  <a className="btn btn-ghost" href={fix.url} target="_blank" rel="noopener noreferrer">
                    {fix.label}
                  </a>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {status?.coverage_error ? <p className="muted">GitHub could not be reached to check the repositories.</p> : null}
      <div className="account-actions">
        {state === 'connected' ? (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={() => act(() => run(githubUnlinkRequest(projectRef), 'GitHub was not unlinked'))}
          >
            Unlink GitHub from this project
          </button>
        ) : null}
        {state === 'needs_reconnect' && status?.connect ? (
          <button type="button" className="btn" disabled={busy} onClick={() => connect(status.connect!, status.account_id)}>
            Reconnect GitHub
          </button>
        ) : null}
        {state === 'not_connected' && candidates.length ? (
          candidates.map((account) => (
            <button
              key={account.account_id}
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => act(() => run(githubLinkRequest(projectRef, account.account_id), 'GitHub was not linked'))}
            >
              Use {account.display_name || 'this GitHub account'} for this project
            </button>
          ))
        ) : null}
        {state === 'not_connected' && !candidates.length && status?.connect ? (
          <button type="button" className="btn" disabled={busy} onClick={() => connect(status.connect!)}>
            Connect GitHub
          </button>
        ) : null}
      </div>
      {error ? <div className="error" role="alert">{error}</div> : null}
    </div>
  );
}
