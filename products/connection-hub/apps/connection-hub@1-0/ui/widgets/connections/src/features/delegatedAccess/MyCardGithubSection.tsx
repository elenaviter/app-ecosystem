import { useCallback, useEffect, useRef, useState } from 'react';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { postOp } from '../../api/client';
import { armOAuthReturn } from '../delegatedToKdcube/oauthReturn';
import { startDelegatedToKdcubeOAuth } from '../delegatedToKdcube/delegatedToKdcubeSlice';
import {
  CONNECTION_HUB_CHANNEL,
  autoLinkAccount,
  commitEmailRequest,
  githubKeyChanged,
  githubLinkRequest,
  githubStatusLine,
  githubStatusRequest,
  githubUnlinkRequest,
  linkPendingKey,
  linkableAccounts,
  openedRepositories,
  opensGithubSection,
  ownerAction,
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

function readFlag(key: string): boolean {
  try {
    return sessionStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function writeFlag(key: string, on: boolean): void {
  try {
    if (on) sessionStorage.setItem(key, '1');
    else sessionStorage.removeItem(key);
  } catch {
    // Storage unavailable: the in-memory flag still carries this page's connect.
  }
}

// Tell the board (another frame of the same origin) that this project's key changed.
function announce(projectRef: string, connectionState: string): void {
  const message = githubKeyChanged(projectRef, connectionState);
  try {
    const channel = new BroadcastChannel(CONNECTION_HUB_CHANNEL);
    channel.postMessage(message);
    channel.close();
  } catch {
    // No BroadcastChannel: the host message below and the board's own re-read cover it.
  }
  if (window.parent !== window) window.parent.postMessage(message, '*');
}

// The GitHub key and commit email on a person's project My Card (W371).
export function MyCardGithubSection({
  projectRef,
  openParams,
}: {
  projectRef: string;
  openParams?: Record<string, string>;
}) {
  const dispatch = useAppDispatch();
  const accounts = useAppSelector((s) => s.delegatedToKdcube.accounts);
  const [repositories] = useState(() => openedRepositories(openParams, window.location.search));
  const sectionRef = useRef<HTMLDivElement | null>(null);
  const [scrolled, setScrolled] = useState(false);
  const [status, setStatus] = useState<GithubKeyStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [email, setEmail] = useState('');
  // A Connect started here links this project on return (point 2 of the
  // operator's first link); kept in session storage across a re-mount.
  const pendingKey = linkPendingKey(projectRef);
  const linkPending = useRef(readFlag(pendingKey));
  const lastAnnounced = useRef('');

  const refresh = useCallback(async () => {
    const result = await run(githubStatusRequest(projectRef, repositories), 'GitHub status failed');
    setStatus(result);
    setEmail(result.commit_email || '');
    const state = String(result.connection_state || '');
    if (lastAnnounced.current && lastAnnounced.current !== state) announce(projectRef, state);
    lastAnnounced.current = state;
    return result;
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
      const result = await refresh();
      // Link, unlink and email all change what the board card shows.
      announce(projectRef, String(result.connection_state || ''));
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
    // A new connection links this project when it lands; a reconnect keeps its link.
    if (!accountId) {
      linkPending.current = true;
      writeFlag(pendingKey, true);
    }
    armOAuthReturn();
    window.open(result.authorize_url, '_blank', 'noopener,noreferrer');
  });

  // Point 2: the account a Connect made here brought back is linked for this
  // project without a second press, when it is the only GitHub account.
  useEffect(() => {
    const accountId = autoLinkAccount(status, accounts || [], linkPending.current);
    if (!accountId || busy) return;
    linkPending.current = false;
    writeFlag(pendingKey, false);
    void act(() => run(githubLinkRequest(projectRef, accountId), 'GitHub was not linked'));
  }, [status, accounts]); // eslint-disable-line react-hooks/exhaustive-deps

  // Opened for this section (the board's "Connect GitHub"): bring it into view once.
  useEffect(() => {
    if (scrolled || loading || !sectionRef.current) return;
    if (opensGithubSection(openParams, window.location.search)) {
      sectionRef.current.scrollIntoView({ block: 'start' });
    }
    setScrolled(true);
  }, [loading, openParams, scrolled]);

  if (loading && !status) return <p className="muted">Checking GitHub…</p>;

  const line = githubStatusLine(status);
  const state = status?.connection_state || 'not_connected';
  const candidates = state === 'not_connected' ? linkableAccounts(accounts || [], status) : [];

  return (
    <div className="my-card-github" ref={sectionRef}>
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
