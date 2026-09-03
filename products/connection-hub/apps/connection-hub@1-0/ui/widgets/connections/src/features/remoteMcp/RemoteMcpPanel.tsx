import { type FormEvent, useMemo, useState } from 'react';
import type { RemoteMcpConnector, RemoteMcpTool } from '../../api/types';
import { publicMcpUrl, publicOperationUrl } from '../../api/client';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { DoorRef } from '../../components/CopyControls';
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
import { formatSchema, summarizeSchema } from './toolSchema';

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

function shortDigest(value?: string): string {
  const text = String(value || '').trim();
  if (!text) return '';
  return text.length > 14 ? `${text.slice(0, 14)}…` : text;
}

/** A DOM-safe id fragment for `aria-controls`. */
function domToken(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, '-');
}

interface ToolDisclosureProps {
  tool: RemoteMcpTool;
  /** Disclosure key: connector id plus tool name (plus a pending marker), so
   *  equal tool names on two connectors never open each other. */
  stateKey: string;
  open: boolean;
  onToggle: (key: string) => void;
  /** A tool from the pending (not yet accepted) descriptor renders with the
   *  pending badge; it never reads as the accepted contract. */
  pending?: boolean;
}

/** One discovered tool as a compact disclosure row: closed, it is the name
 *  plus the first line of its description; open, it lists the parameters read
 *  from the input schema, the output schema when the server supplied one, and
 *  a secondary technical fold with the proxy name, digest, and raw schemas. */
function ToolDisclosure({ tool, stateKey, open, onToggle, pending }: ToolDisclosureProps) {
  const input = useMemo(() => summarizeSchema(tool.input_schema), [tool.input_schema]);
  const output = useMemo(() => summarizeSchema(tool.output_schema), [tool.output_schema]);
  const bodyId = `tool-${domToken(stateKey)}`;
  const description = (tool.description || '').trim();
  const hasInputSchema = tool.input_schema !== undefined && tool.input_schema !== null;
  const hasOutputSchema = tool.output_schema !== undefined && tool.output_schema !== null;
  const requiredCount = input.parameters.filter((parameter) => parameter.required).length;

  return (
    <div className={`tool-disclosure${open ? ' open' : ''}${pending ? ' tool-disclosure--pending' : ''}`}>
      <button
        type="button"
        className="tool-disclosure__toggle"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => onToggle(stateKey)}
      >
        <span className="tool-disclosure__chevron" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <code className="tool-disclosure__name">{tool.name}</code>
        {pending ? <span className="badge badge-warn">pending</span> : null}
        <span className="tool-disclosure__summary">
          {description || <span className="muted">no description</span>}
        </span>
        <span className="tool-disclosure__count muted">
          {input.parameters.length
            ? `${input.parameters.length} param${input.parameters.length === 1 ? '' : 's'}${requiredCount ? ` · ${requiredCount} required` : ''}`
            : hasInputSchema ? 'no params' : 'no schema'}
        </span>
      </button>
      {open ? (
        <div className="tool-disclosure__body" id={bodyId}>
          <div className="tool-detail">
            <span className="card-field-label">Description</span>
            <span className="card-field-value tool-description">
              {description || <span className="muted">The server supplied no description.</span>}
            </span>
          </div>
          <div className="tool-detail">
            <span className="card-field-label">Parameters</span>
            <div className="card-field-value">
              {input.parameters.length ? (
                <ul className="tool-params">
                  {input.parameters.map((parameter) => (
                    <li className="tool-param" key={parameter.name}>
                      <div className="tool-param__head">
                        <code className="tool-param__name">{parameter.name}</code>
                        <span className="tool-param__type">{parameter.type}</span>
                        {parameter.required
                          ? <span className="badge badge-ok tool-param__flag">required</span>
                          : <span className="badge tool-param__flag">optional</span>}
                      </div>
                      {parameter.description ? (
                        <div className="tool-param__desc">{parameter.description}</div>
                      ) : null}
                      {parameter.enumValues.length ? (
                        <div className="tool-param__meta">
                          <span className="tool-param__meta-label">one of</span>
                          <span className="chip-row">
                            {parameter.enumValues.map((value) => (
                              <code className="claim-chip tool-enum" key={value}>{value}</code>
                            ))}
                          </span>
                        </div>
                      ) : null}
                      {parameter.defaultValue ? (
                        <div className="tool-param__meta">
                          <span className="tool-param__meta-label">default</span>
                          <code className="tool-enum">{parameter.defaultValue}</code>
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <span className="muted">
                  {!hasInputSchema
                    ? 'The server supplied no input schema.'
                    : input.opaque
                      ? 'The input schema declares no named parameters. See the raw schema below.'
                      : 'This tool takes no parameters.'}
                </span>
              )}
              {input.additionalProperties && input.parameters.length ? (
                <div className="muted tool-note">The schema also allows properties beyond these.</div>
              ) : null}
            </div>
          </div>
          {hasOutputSchema ? (
            <div className="tool-detail">
              <span className="card-field-label">Output</span>
              <div className="card-field-value">
                {output.parameters.length ? (
                  <ul className="tool-params">
                    {output.parameters.map((parameter) => (
                      <li className="tool-param" key={parameter.name}>
                        <div className="tool-param__head">
                          <code className="tool-param__name">{parameter.name}</code>
                          <span className="tool-param__type">{parameter.type}</span>
                        </div>
                        {parameter.description ? (
                          <div className="tool-param__desc">{parameter.description}</div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <span className="muted">Structured output declared. See the raw schema below.</span>
                )}
              </div>
            </div>
          ) : null}
          <details className="tool-technical">
            <summary>Technical details</summary>
            <div className="card-fields tool-technical__fields">
              <span className="card-field-label">Proxy name</span>
              <span className="card-field-value"><code className="tool-code">{tool.proxy_name || '—'}</code></span>
              <span className="card-field-label">Digest</span>
              <span className="card-field-value">
                {tool.descriptor_digest
                  ? <code className="tool-code" title={tool.descriptor_digest}>{tool.descriptor_digest}</code>
                  : <span className="muted">none</span>}
              </span>
            </div>
            {hasInputSchema ? (
              <details className="tool-raw">
                <summary>Raw input schema</summary>
                <pre className="tool-raw__json">{formatSchema(tool.input_schema)}</pre>
              </details>
            ) : null}
            {hasOutputSchema ? (
              <details className="tool-raw">
                <summary>Raw output schema</summary>
                <pre className="tool-raw__json">{formatSchema(tool.output_schema)}</pre>
              </details>
            ) : null}
          </details>
        </div>
      ) : null}
    </div>
  );
}

export function RemoteMcpPanel() {
  const dispatch = useAppDispatch();
  const { items, busy } = useAppSelector((state) => state.remoteMcp);
  const [connectOpen, setConnectOpen] = useState(false);
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
  // Open tool disclosures, keyed by connector id + tool name (+ pending marker).
  const [openTools, setOpenTools] = useState<Record<string, boolean>>({});
  const controlsBusy = busy || oauthBusy;
  const oauthCallbackUrl = publicOperationUrl('remote_mcp_oauth_callback');
  // The address an external MCP client registers. It is derived from the
  // widget's active settings (origin, tenant, project, bundle), so the page
  // opened through an HTTPS tunnel yields that tunnel's origin here.
  const clientEndpoint = publicMcpUrl('remote_mcp_proxy');

  const rows = useMemo(
    () => items.slice().sort((a, b) => a.label.localeCompare(b.label)),
    [items],
  );

  const toggleTool = (key: string) => {
    setOpenTools((current) => ({ ...current, [key]: !current[key] }));
  };

  const refreshCardCatalog = async () => {
    await dispatch(loadDelegatedAccess()).unwrap().catch(() => undefined);
  };

  const resetConnectForm = () => {
    setLabel('');
    setEndpoint('');
    setCredentialMode('none');
    setCredentialHeader('X-API-Key');
    setCredentialValue('');
    setOAuthClientMode('automatic');
    setOAuthClientId('');
    setOAuthClientSecret('');
    setOAuthAuthMethod('client_secret_basic');
    setLocalError('');
  };

  const closeConnectForm = () => {
    // Closing discards the draft, never the connector list. A pending upstream
    // OAuth authorization keeps its "Open it again" link above the list.
    resetConnectForm();
    setConnectOpen(false);
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
        // The pane stays open while the browser flow is pending so the
        // operator keeps the fallback authorization link in reach.
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
      resetConnectForm();
      setConnectOpen(false);
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
        <span className="muted form-hint">
          The upstream server Connection Hub calls for you. Clients connect to the Client MCP endpoint above, never to this address.
        </span>
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
      <div className="form-actions">
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
          {credentialMode === 'oauth' ? 'Authorize MCP server' : 'Connect MCP server'}
        </button>
        <button className="btn btn-ghost" type="button" onClick={closeConnectForm} disabled={oauthBusy}>
          Cancel
        </button>
      </div>
    </form>
  );

  const renderTools = (
    connector: RemoteMcpConnector,
    tools: RemoteMcpTool[] | undefined,
    pending: boolean,
  ) => {
    if (!tools?.length) return <span className="muted">none</span>;
    return (
      <div className="tool-disclosures">
        {tools.map((tool) => {
          const key = `${pending ? 'pending:' : ''}${connector.connector_id}::${tool.name}`;
          return (
            <ToolDisclosure
              key={key}
              tool={tool}
              stateKey={key}
              open={Boolean(openTools[key])}
              onToggle={toggleTool}
              pending={pending}
            />
          );
        })}
      </div>
    );
  };

  const connectorList = (
    <div className="remote-mcp-list">
      {rows.length ? rows.map((connector) => {
        const drift = driftItems(connector);
        const editingCredential = credentialEditor === connector.connector_id;
        const editingOAuthClient = oauthClientEditor === connector.connector_id;
        const drifted = connector.descriptor_state === 'drifted';
        return (
          <article className="account remote-mcp-row" key={connector.connector_id}>
            <div className="account-info">
              <div className="account-title">
                {connector.label}
                <span className={`badge ${connector.state === 'active' ? 'badge-ok' : ''}`}>
                  {connector.state}
                </span>
                {drifted ? (
                  <span className="badge badge-warn">tools changed</span>
                ) : null}
              </div>
              <div className="card-fields">
                <span className="card-field-label">Upstream</span>
                <span className="card-field-value">
                  <span className="muted">Streamable HTTP endpoint Connection Hub calls</span>
                  <DoorRef value={connector.endpoint} copyLabel="Copy upstream endpoint" />
                </span>
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
                <span className="card-field-label">Descriptor</span>
                <span className="card-field-value">
                  {connector.descriptor_digest ? (
                    <>
                      {connector.descriptor_revision ? `rev ${connector.descriptor_revision} · ` : ''}
                      <code className="tool-code" title={connector.descriptor_digest}>{shortDigest(connector.descriptor_digest)}</code>
                      {drifted ? <span className="muted"> · accepted, pending changes below</span> : null}
                    </>
                  ) : <span className="muted">none</span>}
                </span>
                <span className="card-field-label">Tools</span>
                <span className="card-field-value">
                  {renderTools(connector, connector.tools, false)}
                </span>
                {drifted && connector.pending_tools?.length ? (
                  <>
                    <span className="card-field-label">Pending</span>
                    <span className="card-field-value">
                      <div className="muted tool-note">
                        Discovered on the last refresh, not accepted yet
                        {connector.pending_descriptor_digest ? (
                          <>{' · '}<code className="tool-code" title={connector.pending_descriptor_digest}>{shortDigest(connector.pending_descriptor_digest)}</code></>
                        ) : null}
                      </div>
                      {renderTools(connector, connector.pending_tools, true)}
                    </span>
                  </>
                ) : null}
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
              {drifted ? (
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

  // The creation surface is summoned, not resident (the same shape as Create
  // automation access): its trigger sits in the tab's action row next to the
  // always-visible client endpoint, and the pane exists only while it is open,
  // so the connector list spans the tab the rest of the time.
  return (
    <>
      <div className="remote-mcp-head">
        <div className="remote-mcp-client-endpoint">
          <span className="card-field-label">Client MCP endpoint</span>
          <DoorRef value={clientEndpoint} copyLabel="Copy client MCP endpoint" />
          <span className="muted remote-mcp-client-endpoint__hint">
            Register this address in Claude or another MCP client. It is one door for every connector below. Each connector's own upstream endpoint is what Connection Hub calls for you.
          </span>
        </div>
        {!connectOpen ? (
          <div className="tab-actions remote-mcp-head__actions">
            <button className="btn" type="button" onClick={() => setConnectOpen(true)}>
              Connect MCP server
            </button>
          </div>
        ) : null}
      </div>
      {pendingAuthorizeUrl ? (
        <div className="notice success remote-mcp-pending-oauth">
          <span>
            Authorization opened in another tab.{' '}
            <a href={pendingAuthorizeUrl} target="_blank" rel="noreferrer">Open it again</a>
          </span>
          <button className="btn btn-ghost" type="button" onClick={() => setPendingAuthorizeUrl('')}>
            Dismiss
          </button>
        </div>
      ) : null}
      <PaneGroup
        panes={[
          ...(connectOpen ? [{
            id: 'remote-mcp-connect', title: 'Connect MCP server', content: connectorForm, lead: true,
          }] : []),
          { id: 'remote-mcp-list', title: `External MCP servers · ${rows.length}`, content: connectorList },
        ]}
      />
    </>
  );
}
