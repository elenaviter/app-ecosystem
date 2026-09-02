import { type FormEvent, useMemo, useState } from 'react';
import type { RemoteMcpConnector } from '../../api/types';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { PaneGroup } from '../../components/Pane';
import { loadDelegatedAccess } from '../delegatedAccess/delegatedAccessSlice';
import {
  acceptRemoteMcpDescriptor,
  createRemoteMcpConnector,
  deleteRemoteMcpConnector,
  refreshRemoteMcpConnector,
  setRemoteMcpConnectorEnabled,
  startRemoteMcpOAuth,
  updateRemoteMcpCredential,
} from './remoteMcpSlice';

type CredentialMode = 'none' | 'bearer' | 'header' | 'oauth';
type DirectCredentialMode = Exclude<CredentialMode, 'oauth'>;

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
  const [localError, setLocalError] = useState('');
  const [credentialEditor, setCredentialEditor] = useState('');
  const [replacementMode, setReplacementMode] = useState<DirectCredentialMode>('none');
  const [replacementHeader, setReplacementHeader] = useState('X-API-Key');
  const [replacementValue, setReplacementValue] = useState('');
  const [deleteArmed, setDeleteArmed] = useState('');
  const [pendingAuthorizeUrl, setPendingAuthorizeUrl] = useState('');

  const rows = useMemo(
    () => items.slice().sort((a, b) => a.label.localeCompare(b.label)),
    [items],
  );

  const refreshCardCatalog = async () => {
    await dispatch(loadDelegatedAccess()).unwrap().catch(() => undefined);
  };

  const launchOAuth = async (connector?: RemoteMcpConnector) => {
    const authorizationWindow = window.open('about:blank', '_blank');
    if (authorizationWindow) authorizationWindow.opener = null;
    try {
      const started = await dispatch(startRemoteMcpOAuth({
        label: connector?.label || label.trim(),
        endpoint: connector?.endpoint || endpoint.trim(),
        returnHint: window.location.href,
        connectorId: connector?.connector_id,
        expectedRevision: connector?.revision,
      })).unwrap();
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
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setLocalError('');
    setPendingAuthorizeUrl('');
    try {
      if (credentialMode === 'oauth') {
        await launchOAuth();
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
      <button className="btn" type="submit" disabled={busy || !label.trim() || !endpoint.trim()}>
        {credentialMode === 'oauth' ? 'Authorize MCP server' : '+ Connect MCP server'}
      </button>
    </form>
  );

  const connectorList = (
    <div className="remote-mcp-list">
      {rows.length ? rows.map((connector) => {
        const drift = driftItems(connector);
        const editingCredential = credentialEditor === connector.connector_id;
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
                      disabled={busy || (replacementMode !== 'none' && !replacementValue)}
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
            </div>
            <div className="remote-mcp-actions">
              <button
                className="btn btn-ghost"
                type="button"
                title="Discover tools again"
                disabled={busy}
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
                  disabled={busy}
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
                disabled={busy}
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
                  disabled={busy}
                  onClick={() => {
                    setCredentialEditor(connector.connector_id);
                    setReplacementMode(connector.credential_mode as DirectCredentialMode);
                    setReplacementHeader(connector.credential_header || 'X-API-Key');
                    setReplacementValue('');
                  }}
                >
                  Credential
                </button>
              ) : (
                <button
                  className="btn btn-ghost"
                  type="button"
                  disabled={busy}
                  onClick={() => void launchOAuth(connector).catch((error) => {
                    setLocalError(error instanceof Error ? error.message : String(error));
                  })}
                >
                  Reconnect
                </button>
              )}
              {deleteArmed === connector.connector_id ? (
                <div className="revoke-confirm">
                  <span className="revoke-confirm__q">Remove?</span>
                  <button
                    className="btn btn-danger"
                    type="button"
                    disabled={busy}
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
                  disabled={busy}
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
