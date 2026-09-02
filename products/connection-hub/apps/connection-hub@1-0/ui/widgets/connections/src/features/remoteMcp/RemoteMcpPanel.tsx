import { type FormEvent, useMemo, useState } from 'react';
import type { RemoteMcpConnector } from '../../api/types';
import { publicOperationUrl } from '../../api/client';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { PaneGroup } from '../../components/Pane';
import { loadDelegatedAccess } from '../delegatedAccess/delegatedAccessSlice';
import {
  acceptRemoteMcpDescriptor,
  createRemoteMcpConnector,
  deleteRemoteMcpConnector,
  refreshRemoteMcpConnector,
  requestRemoteMcpOAuth,
  setRemoteMcpConnectorEnabled,
  startRemoteMcpOAuth,
  updateRemoteMcpCredential,
} from './remoteMcpSlice';

type CredentialMode = 'none' | 'bearer' | 'header' | 'oauth';
type DirectCredentialMode = Exclude<CredentialMode, 'oauth'>;
type OAuthClientMode = 'automatic' | 'provisioned';
type OAuthTokenAuthMethod = 'none' | 'client_secret_basic' | 'client_secret_post';

interface OAuthClientFieldsProps {
  mode: OAuthClientMode;
  setMode: (mode: OAuthClientMode) => void;
  clientId: string;
  setClientId: (value: string) => void;
  clientSecret: string;
  setClientSecret: (value: string) => void;
  authMethod: OAuthTokenAuthMethod;
  setAuthMethod: (value: OAuthTokenAuthMethod) => void;
  callbackUrl: string;
}

function OAuthClientFields({
  mode,
  setMode,
  clientId,
  setClientId,
  clientSecret,
  setClientSecret,
  authMethod,
  setAuthMethod,
  callbackUrl,
}: OAuthClientFieldsProps) {
  return (
    <>
      <label>
        <span className="form-title">OAuth client registration</span>
        <select
          className="input"
          value={mode}
          onChange={(event) => {
            const next = event.target.value as OAuthClientMode;
            setMode(next);
            if (next === 'automatic') setClientSecret('');
          }}
        >
          <option value="automatic">Automatic registration</option>
          <option value="provisioned">Client created in provider console</option>
        </select>
      </label>
      {mode === 'provisioned' ? (
        <>
          <label>
            <span className="form-title">Redirect URI</span>
            <input
              className="input remote-mcp-readonly"
              value={callbackUrl}
              readOnly
              onFocus={(event) => event.currentTarget.select()}
            />
          </label>
          <label>
            <span className="form-title">Client ID</span>
            <input
              className="input"
              value={clientId}
              onChange={(event) => setClientId(event.target.value)}
              required
              autoComplete="off"
            />
          </label>
          <label>
            <span className="form-title">Token endpoint authentication</span>
            <select
              className="input"
              value={authMethod}
              onChange={(event) => {
                const next = event.target.value as OAuthTokenAuthMethod;
                setAuthMethod(next);
                if (next === 'none') setClientSecret('');
              }}
            >
              <option value="client_secret_basic">Client secret in Authorization header</option>
              <option value="client_secret_post">Client secret in request body</option>
              <option value="none">Public client without a secret</option>
            </select>
          </label>
          {authMethod !== 'none' ? (
            <label>
              <span className="form-title">Client secret</span>
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                value={clientSecret}
                onChange={(event) => setClientSecret(event.target.value)}
                required
              />
            </label>
          ) : null}
        </>
      ) : null}
    </>
  );
}

function oauthClientReady(
  mode: OAuthClientMode,
  clientId: string,
  clientSecret: string,
  authMethod: OAuthTokenAuthMethod,
): boolean {
  return mode === 'automatic'
    || Boolean(clientId.trim() && (authMethod === 'none' || clientSecret));
}

function formatTime(value?: number): string {
  if (!value) return 'never';
  return new Date(value * 1000).toLocaleString();
}

function driftItems(connector: RemoteMcpConnector): string[] {
  return Object.entries(connector.drift || {}).flatMap(([kind, names]) =>
    (names || []).map((name) => `${kind}: ${name}`),
  );
}

export function RemoteMcpPanel() {
  const dispatch = useAppDispatch();
  const { items, busy } = useAppSelector((state) => state.remoteMcp);
  const [label, setLabel] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [credentialMode, setCredentialMode] = useState<CredentialMode>('none');
  const [credentialHeader, setCredentialHeader] = useState('X-API-Key');
  const [credentialValue, setCredentialValue] = useState('');
  const [oauthClientMode, setOAuthClientMode] = useState<OAuthClientMode>('automatic');
  const [oauthClientId, setOAuthClientId] = useState('');
  const [oauthClientSecret, setOAuthClientSecret] = useState('');
  const [oauthAuthMethod, setOAuthAuthMethod] = useState<OAuthTokenAuthMethod>('client_secret_basic');
  const [oauthBusy, setOAuthBusy] = useState(false);
  const [localError, setLocalError] = useState('');
  const [credentialEditor, setCredentialEditor] = useState('');
  const [replacementMode, setReplacementMode] = useState<DirectCredentialMode>('none');
  const [replacementHeader, setReplacementHeader] = useState('X-API-Key');
  const [replacementValue, setReplacementValue] = useState('');
  const [oauthClientEditor, setOAuthClientEditor] = useState('');
  const [replacementOAuthMode, setReplacementOAuthMode] = useState<OAuthClientMode>('automatic');
  const [replacementOAuthClientId, setReplacementOAuthClientId] = useState('');
  const [replacementOAuthSecret, setReplacementOAuthSecret] = useState('');
  const [replacementOAuthAuthMethod, setReplacementOAuthAuthMethod] = useState<OAuthTokenAuthMethod>('client_secret_basic');
  const [deleteArmed, setDeleteArmed] = useState('');
  const [pendingAuthorizeUrl, setPendingAuthorizeUrl] = useState('');
  const controlsBusy = busy || oauthBusy;
  const oauthCallbackUrl = publicOperationUrl('remote_mcp_oauth_callback');

  const rows = useMemo(
    () => items.slice().sort((a, b) => a.label.localeCompare(b.label)),
    [items],
  );

  const refreshCardCatalog = async () => {
    await dispatch(loadDelegatedAccess()).unwrap().catch(() => undefined);
  };

  const launchOAuth = async (
    connector?: RemoteMcpConnector,
    clientMode?: OAuthClientMode,
    client?: {
      clientId: string;
      clientSecret?: string;
      tokenEndpointAuthMethod: OAuthTokenAuthMethod;
    },
    clearSecret?: () => void,
  ) => {
    const authorizationWindow = window.open('about:blank', '_blank');
    if (authorizationWindow) authorizationWindow.opener = null;
    try {
      const args = {
        label: connector?.label || label.trim(),
        endpoint: connector?.endpoint || endpoint.trim(),
        returnHint: window.location.href,
        connectorId: connector?.connector_id,
        expectedRevision: connector?.revision,
        oauthClientMode: clientMode,
        oauthClient: client,
      };
      clearSecret?.();
      let started;
      if (client) {
        setOAuthBusy(true);
        started = await requestRemoteMcpOAuth(args);
      } else {
        started = await dispatch(startRemoteMcpOAuth(args)).unwrap();
      }
      const authorizeUrl = String(started.authorize_url || '');
      setPendingAuthorizeUrl(authorizeUrl);
      try {
        sessionStorage.setItem('kdc-oauth-pending', '1');
      } catch {
        // The completion BroadcastChannel remains available.
      }
      if (authorizationWindow && !authorizationWindow.closed) {
        authorizationWindow.location.replace(authorizeUrl);
      }
    } catch (error) {
      authorizationWindow?.close();
      throw error;
    } finally {
      setOAuthBusy(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setLocalError('');
    setPendingAuthorizeUrl('');
    try {
      if (credentialMode === 'oauth') {
        await launchOAuth(
          undefined,
          oauthClientMode,
          oauthClientMode === 'provisioned'
            ? {
                clientId: oauthClientId.trim(),
                clientSecret: oauthClientSecret,
                tokenEndpointAuthMethod: oauthAuthMethod,
              }
            : undefined,
          () => setOAuthClientSecret(''),
        );
        return;
      }
      await dispatch(createRemoteMcpConnector({
        label: label.trim(),
        endpoint: endpoint.trim(),
        credentialMode,
        credentialHeader: credentialMode === 'header' ? credentialHeader.trim() : '',
        credentialValue: credentialMode === 'none' ? '' : credentialValue,
      })).unwrap();
      setLabel('');
      setEndpoint('');
      setCredentialMode('none');
      setCredentialValue('');
      await refreshCardCatalog();
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    }
  };

  const replaceCredential = async (connector: RemoteMcpConnector) => {
    try {
      await dispatch(updateRemoteMcpCredential({
        connectorId: connector.connector_id,
        expectedRevision: connector.revision,
        credentialMode: replacementMode,
        credentialHeader: replacementMode === 'header' ? replacementHeader.trim() : '',
        credentialValue: replacementMode === 'none' ? '' : replacementValue,
      })).unwrap();
      setCredentialEditor('');
      setReplacementValue('');
    } catch {
      // The slice reports the actionable backend error in the page banner.
    }
  };

  const connectorForm = (
    <form className="form form-flush" onSubmit={submit}>
      {localError ? <div className="error" role="alert">{localError}</div> : null}
      {pendingAuthorizeUrl ? (
        <div className="notice success">
          Authorization opened in another tab.{' '}
          <a href={pendingAuthorizeUrl} target="_blank" rel="noreferrer">Open it again</a>
        </div>
      ) : null}
      <label>
        <span className="form-title">Name</span>
        <input
          className="input"
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          required
          maxLength={160}
          placeholder="Customer records"
        />
      </label>
      <label>
        <span className="form-title">Streamable HTTP endpoint</span>
        <input
          className="input"
          type="url"
          value={endpoint}
          onChange={(event) => setEndpoint(event.target.value)}
          required
          placeholder="https://mcp.example.com/mcp"
        />
      </label>
      <label>
        <span className="form-title">Upstream authentication</span>
        <select
          className="input"
          value={credentialMode}
          onChange={(event) => setCredentialMode(event.target.value as CredentialMode)}
        >
          <option value="none">No credential</option>
          <option value="bearer">Bearer token</option>
          <option value="header">Custom header</option>
          <option value="oauth">OAuth browser login</option>
        </select>
      </label>
      {credentialMode === 'header' ? (
        <label>
          <span className="form-title">Header name</span>
          <input
            className="input"
            value={credentialHeader}
            onChange={(event) => setCredentialHeader(event.target.value)}
            required
          />
        </label>
      ) : null}
      {credentialMode !== 'none' && credentialMode !== 'oauth' ? (
        <label>
          <span className="form-title">Credential</span>
          <input
            className="input"
            type="password"
            autoComplete="off"
            value={credentialValue}
            onChange={(event) => setCredentialValue(event.target.value)}
            required
          />
        </label>
      ) : null}
      {credentialMode === 'oauth' ? (
        <OAuthClientFields
          mode={oauthClientMode}
          setMode={setOAuthClientMode}
          clientId={oauthClientId}
          setClientId={setOAuthClientId}
          clientSecret={oauthClientSecret}
          setClientSecret={setOAuthClientSecret}
          authMethod={oauthAuthMethod}
          setAuthMethod={setOAuthAuthMethod}
          callbackUrl={oauthCallbackUrl}
        />
      ) : null}
      <button
        className="btn"
        type="submit"
        disabled={
          controlsBusy
          || !label.trim()
          || !endpoint.trim()
          || (credentialMode === 'oauth' && !oauthClientReady(
            oauthClientMode,
            oauthClientId,
            oauthClientSecret,
            oauthAuthMethod,
          ))
        }
      >
        {credentialMode === 'oauth' ? 'Authorize MCP server' : '+ Connect MCP server'}
      </button>
    </form>
  );

  const connectorList = (
    <div className="remote-mcp-list">
      {rows.length ? rows.map((connector) => {
        const drift = driftItems(connector);
        const editingCredential = credentialEditor === connector.connector_id;
        const editingOAuthClient = oauthClientEditor === connector.connector_id;
        return (
          <article className="account remote-mcp-row" key={connector.connector_id}>
            <div className="account-info">
              <div className="account-title">
                {connector.label}
                <span className={`badge ${connector.state === 'active' ? 'badge-ok' : ''}`}>
                  {connector.state}
                </span>
                {connector.descriptor_state === 'drifted' ? (
                  <span className="badge badge-warn">tools changed</span>
                ) : null}
              </div>
              <div className="account-sub remote-mcp-endpoint">{connector.endpoint}</div>
              <div className="card-fields">
                <span className="card-field-label">Server</span>
                <span className="card-field-value">
                  {connector.server_name || 'Unnamed MCP server'}
                  {connector.server_version ? ` · ${connector.server_version}` : ''}
                </span>
                <span className="card-field-label">Checked</span>
                <span className="card-field-value">{formatTime(connector.last_checked_at)}</span>
                <span className="card-field-label">Credential</span>
                <span className="card-field-value">
                  {connector.credential_mode === 'none'
                    ? 'none'
                    : `${connector.credential_mode}${connector.credential_present ? ' · stored' : ' · missing'}`}
                </span>
                <span className="card-field-label">Tools</span>
                <span className="card-field-value chip-row">
                  {(connector.tools || []).map((tool) => (
                    <span className="claim-chip" key={tool.name}>{tool.name}</span>
                  ))}
                  {!connector.tools?.length ? <span className="muted">none</span> : null}
                </span>
              </div>
              {drift.length ? (
                <div className="notice warning remote-mcp-drift">
                  {drift.map((item) => <code key={item}>{item}</code>)}
                </div>
              ) : null}
              {connector.last_error ? <div className="error">{connector.last_error}</div> : null}

              {editingCredential ? (
                <div className="form remote-mcp-credential-form">
                  <label>
                    <span className="form-title">Upstream authentication</span>
                    <select
                      className="input"
                      value={replacementMode}
                      onChange={(event) => setReplacementMode(event.target.value as DirectCredentialMode)}
                    >
                      <option value="none">No credential</option>
                      <option value="bearer">Bearer token</option>
                      <option value="header">Custom header</option>
                    </select>
                  </label>
                  {replacementMode === 'header' ? (
                    <input
                      className="input"
                      value={replacementHeader}
                      onChange={(event) => setReplacementHeader(event.target.value)}
                      placeholder="Header name"
                    />
                  ) : null}
                  {replacementMode !== 'none' ? (
                    <input
                      className="input"
                      type="password"
                      autoComplete="off"
                      value={replacementValue}
                      onChange={(event) => setReplacementValue(event.target.value)}
                      placeholder="New credential"
                    />
                  ) : null}
                  <div className="button-row">
                    <button
                      className="btn"
                      type="button"
                      disabled={controlsBusy || (replacementMode !== 'none' && !replacementValue)}
                      onClick={() => void replaceCredential(connector)}
                    >
                      Save credential
                    </button>
                    <button className="btn btn-ghost" type="button" onClick={() => setCredentialEditor('')}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : null}
              {editingOAuthClient ? (
                <div className="form remote-mcp-credential-form">
                  <OAuthClientFields
                    mode={replacementOAuthMode}
                    setMode={setReplacementOAuthMode}
                    clientId={replacementOAuthClientId}
                    setClientId={setReplacementOAuthClientId}
                    clientSecret={replacementOAuthSecret}
                    setClientSecret={setReplacementOAuthSecret}
                    authMethod={replacementOAuthAuthMethod}
                    setAuthMethod={setReplacementOAuthAuthMethod}
                    callbackUrl={oauthCallbackUrl}
                  />
                  <div className="button-row">
                    <button
                      className="btn"
                      type="button"
                      disabled={
                        controlsBusy
                        || !oauthClientReady(
                          replacementOAuthMode,
                          replacementOAuthClientId,
                          replacementOAuthSecret,
                          replacementOAuthAuthMethod,
                        )
                      }
                      onClick={() => void launchOAuth(
                        connector,
                        replacementOAuthMode,
                        replacementOAuthMode === 'provisioned'
                          ? {
                              clientId: replacementOAuthClientId.trim(),
                              clientSecret: replacementOAuthSecret,
                              tokenEndpointAuthMethod: replacementOAuthAuthMethod,
                            }
                          : undefined,
                        () => setReplacementOAuthSecret(''),
                      ).then(() => setOAuthClientEditor('')).catch((error) => {
                        setLocalError(error instanceof Error ? error.message : String(error));
                      })}
                    >
                      Authorize with this client
                    </button>
                    <button className="btn btn-ghost" type="button" onClick={() => setOAuthClientEditor('')}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
            <div className="remote-mcp-actions">
              <button
                className="btn btn-ghost"
                type="button"
                title="Discover tools again"
                disabled={controlsBusy}
                onClick={() => void dispatch(refreshRemoteMcpConnector({
                  connectorId: connector.connector_id,
                  expectedRevision: connector.revision,
                })).unwrap().then(refreshCardCatalog).catch(() => undefined)}
              >
                ↻ Refresh tools
              </button>
              {connector.descriptor_state === 'drifted' ? (
                <button
                  className="btn"
                  type="button"
                  disabled={controlsBusy}
                  onClick={() => void dispatch(acceptRemoteMcpDescriptor({
                    connectorId: connector.connector_id,
                    expectedRevision: connector.revision,
                  })).unwrap().then(refreshCardCatalog).catch(() => undefined)}
                >
                  Accept tool changes
                </button>
              ) : null}
              <button
                className="btn btn-ghost"
                type="button"
                disabled={controlsBusy}
                onClick={() => void dispatch(setRemoteMcpConnectorEnabled({
                  connectorId: connector.connector_id,
                  expectedRevision: connector.revision,
                  enabled: connector.state !== 'active',
                })).unwrap().then(refreshCardCatalog).catch(() => undefined)}
              >
                {connector.state === 'active' ? 'Disable' : 'Enable'}
              </button>
              {connector.credential_mode !== 'oauth' ? (
                <button
                  className="btn btn-ghost"
                  type="button"
                  disabled={controlsBusy}
                  onClick={() => {
                    setOAuthClientEditor('');
                    setCredentialEditor(connector.connector_id);
                    setReplacementMode(connector.credential_mode as DirectCredentialMode);
                    setReplacementHeader(connector.credential_header || 'X-API-Key');
                    setReplacementValue('');
                  }}
                >
                  Credential
                </button>
              ) : (
                <>
                  <button
                    className="btn btn-ghost"
                    type="button"
                    disabled={controlsBusy}
                    onClick={() => void launchOAuth(connector).catch((error) => {
                      setLocalError(error instanceof Error ? error.message : String(error));
                    })}
                  >
                    Reconnect
                  </button>
                  <button
                    className="btn btn-ghost"
                    type="button"
                    disabled={controlsBusy}
                    onClick={() => {
                      setCredentialEditor('');
                      setOAuthClientEditor(connector.connector_id);
                      setReplacementOAuthMode('automatic');
                      setReplacementOAuthClientId('');
                      setReplacementOAuthSecret('');
                      setReplacementOAuthAuthMethod('client_secret_basic');
                    }}
                  >
                    OAuth client
                  </button>
                </>
              )}
              {deleteArmed === connector.connector_id ? (
                <div className="revoke-confirm">
                  <span className="revoke-confirm__q">Remove?</span>
                  <button
                    className="btn btn-danger"
                    type="button"
                    disabled={controlsBusy}
                    onClick={() => void dispatch(deleteRemoteMcpConnector({
                      connectorId: connector.connector_id,
                      expectedRevision: connector.revision,
                    })).unwrap().then(refreshCardCatalog).catch(() => undefined)}
                  >
                    Confirm
                  </button>
                  <button className="btn btn-ghost" type="button" onClick={() => setDeleteArmed('')}>
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  className="btn btn-danger"
                  type="button"
                  disabled={controlsBusy}
                  onClick={() => setDeleteArmed(connector.connector_id)}
                >
                  Remove
                </button>
              )}
            </div>
          </article>
        );
      }) : <p className="muted">No external MCP servers connected.</p>}
    </div>
  );

  return (
    <PaneGroup panes={[
      { id: 'remote-mcp-list', title: `External MCP servers · ${rows.length}`, content: connectorList, lead: true },
      { id: 'remote-mcp-connect', title: 'Connect MCP server', content: connectorForm },
    ]} />
  );
}
