/**
 * The deployment's sign-in authorities, read as a console: the platform's
 * own sign-in first and marked, then every provider of every authority in
 * the registry with the pools it trusts (mixed mode), then the authority
 * providers apps registered at load. Descriptor-owned rows are read-only
 * here and say where they live; a change is a descriptor edit and a refresh.
 */
import { useEffect, useState } from 'react';
import type { AuthorityPoolRow, AuthorityProviderRow } from '../../api/types';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { InfoMark } from '../../components/InfoMark';
import { CopyButton } from '../../components/CopyControls';
import { AUTH_CONSTRUCTS, constructFacts } from './authConstructs';
import { loadAuthorities, setPlatformSignIn } from './authenticatorsSlice';
import { ProviderEditor } from './ProviderEditor';

// Two questions hide in one `type`: who identifies the user (an authority:
// Cognito pools, an OIDC issuer, the development IDP) and where the sign-in
// runs (a lane: in the browser with tokens presented, or on the server with
// one platform session). The pane reads them apart.
const LANE_TYPES = new Set(['bundle_session_login', 'bundle-session-login', 'bundle_session', 'bundle-session', 'session']);
const isLane = (row: AuthorityProviderRow) => LANE_TYPES.has((row.type || '').toLowerCase());
const laneUpstream = (row: AuthorityProviderRow) => row.session?.authenticator_ref?.provider_id || '';

interface Editing {
  authorityId: string;
  providerId: string;
  yaml: string;
  create: boolean;
  where: string;
}

/** A piece of YAML the admin pastes, with where it goes and what follows. */
function Fragment({ yaml, where, then }: { yaml: string; where: string; then: string }) {
  return (
    <div className="yaml-fragment">
      <div className="yaml-fragment__head">
        <span className="authority-platform__k">edit</span>
        <span className="yaml-fragment__where">{where}</span>
        <CopyButton value={yaml} label="Copy the YAML" />
      </div>
      <pre className="yaml-fragment__code">{yaml}</pre>
      <div className="yaml-fragment__then">{then}</div>
    </div>
  );
}

function Pool({ pool }: { pool: AuthorityPoolRow }) {
  return (
    <div className="authority-pool">
      <span className="authority-pool__alias">
        <span>{pool.alias}</span>
        {pool.primary ? <span className="badge badge-app">platform pool</span> : null}
      </span>
      <span className="authority-pool__cell"><span className="authority-pool__k">pool</span><code className="authority-pool__id">{pool.user_pool_id || '?'}</code></span>
      <span className="authority-pool__cell"><span className="authority-pool__k">client</span><code className="authority-pool__id">{pool.app_client_id || '?'}</code></span>
      <span className="authority-pool__meta">{pool.region || ''}{pool.hosted_ui_domain ? ` · ${pool.hosted_ui_domain.replace(/^https?:\/\//, '')}` : ''}</span>
    </div>
  );
}

function Provider({ row, onEdit }: { row: AuthorityProviderRow; onEdit?: (row: AuthorityProviderRow) => void }) {
  const auth = row.authenticator || {};
  const isSession = Boolean(row.session);
  return (
    <div className={`authority-provider${row.platform ? ' authority-provider--platform' : ''}`}>
      <div className="authority-provider__head">
        <span className="authority-provider__name">
          <strong>{row.provider_id}</strong>
          <span className="account-sub">{row.type || ''}</span>
          {row.platform ? <span className="badge badge-app">{isLane(row) ? 'current lane' : 'platform authority'}</span> : null}
          {row.enabled === false ? <span className="badge badge-neutral">disabled</span> : null}
        </span>
        {onEdit && row.yaml ? (
          <button className="btn btn-ghost" type="button" onClick={() => onEdit(row)} title="Open this block in the editing buffer">
            Edit
          </button>
        ) : null}
      </div>
      {row.label ? <div className="account-sub">{row.label}</div> : null}
      {row.where ? (
        <div className="authority-provider__where">
          <span className="authority-platform__k">where</span>
          <code>{row.where}</code>
          <CopyButton value={row.where} label="Copy the descriptor path" />
        </div>
      ) : null}
      {!isSession && (auth.user_pool_id || auth.issuer) ? (
        <div className="authority-provider__facts">
          {auth.issuer ? <span>issuer <code>{auth.issuer}</code></span> : null}
          {auth.user_pool_id ? <span>pool <code>{auth.user_pool_id}</code></span> : null}
          {auth.app_client_id ? <span>client <code>{auth.app_client_id}</code></span> : null}
          {auth.region ? <span>{auth.region}</span> : null}
          {auth.hosted_ui_domain ? <span>{auth.hosted_ui_domain.replace(/^https?:\/\//, '')}</span> : null}
        </div>
      ) : null}
      {isSession ? (
        <div className="authority-provider__facts">
          <span>
            upstream <code>{row.session?.authenticator_ref?.provider_id || '?'}</code>
          </span>
          {row.session?.scopes?.length ? <span>scopes <code>{row.session.scopes.join(' ')}</code></span> : null}
          {row.session?.groups_claim ? <span>groups <code>{row.session.groups_claim}</code></span> : null}
          {row.session?.return_origins?.length ? (
            <span>return origins {row.session.return_origins.length}</span>
          ) : null}
        </div>
      ) : null}
      {row.yaml ? (
        <details className="edit-section edit-section--tight">
          <summary>
            <span className="edit-section__name">As YAML</span>
            <InfoMark text="This provider's block as it stands in the descriptor, to copy. Keys that carry a secret reference or a cookie name read <unchanged>: keep them as they are in the file. Edit opens the same block in the editing buffer." />
          </summary>
          <div className="edit-section__body">
            <Fragment yaml={row.yaml} where={row.where || ''} then="Then refresh the runtime. Pools and providers take effect after the refresh." />
          </div>
        </details>
      ) : null}
      {row.trusted_providers?.length ? (
        <div className="authority-provider__pools">
          <div className="authority-provider__pools-head">
            trusts {row.trusted_providers.length} pool{row.trusted_providers.length === 1 ? '' : 's'}
            <InfoMark text="Mixed mode: tokens from any of these pools are accepted by this provider. The platform pool is the one its own sign-in uses." />
          </div>
          {row.trusted_providers.map((pool) => <Pool key={`${pool.user_pool_id}:${pool.app_client_id}`} pool={pool} />)}
        </div>
      ) : null}
    </div>
  );
}

export function AuthoritiesPane() {
  const dispatch = useAppDispatch();
  const { authorities, authoritiesError } = useAppSelector((s) => s.authenticators);
  useEffect(() => { void dispatch(loadAuthorities()); }, [dispatch]);
  const { authorityEdit, authorityEditBusy, authorityEditError } = useAppSelector((s) => s.authenticators);
  const platform = authorities?.platform;
  const platformAuthority = authorities?.authorities?.find((row) => row.authority_id === platform?.authority_id)
    || authorities?.authorities?.find((row) => row.platform);
  const providerRows = platformAuthority?.providers || [];
  const selectedRow = providerRows.find((row) => row.provider_id === platform?.provider_id);
  const selectedLane = selectedRow && isLane(selectedRow) ? selectedRow : null;
  const selectedAuthorityId = selectedLane ? laneUpstream(selectedLane) : (selectedRow?.provider_id || '');
  const selectedAuthorityRow = providerRows.find((row) => row.provider_id === selectedAuthorityId);
  const options = platform?.switch_options || [];
  const [target, setTarget] = useState('');
  const chosen = options.find((option) => option.provider_id === (target || options.find((o) => !o.current)?.provider_id || ''));
  const [confirmSwitch, setConfirmSwitch] = useState(false);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [constructId, setConstructId] = useState(AUTH_CONSTRUCTS[0].id);
  const [newProviderId, setNewProviderId] = useState('');
  const facts = constructFacts(authorities);
  const bundlePath = (authorityId: string, providerId: string) => (
    `bundles.yaml: <this app>.authority_registry.authorities.${authorityId}.providers.${providerId}`
  );
  const openEdit = (row: AuthorityProviderRow) => setEditing({
    authorityId: row.authority_id,
    providerId: row.provider_id,
    yaml: row.yaml || '',
    create: false,
    where: row.where || bundlePath(row.authority_id, row.provider_id),
  });
  const openConstruct = () => {
    const construct = AUTH_CONSTRUCTS.find((item) => item.id === constructId) || AUTH_CONSTRUCTS[0];
    const providerId = newProviderId.trim() || construct.defaultProviderId;
    setEditing({
      authorityId: facts.authorityId,
      providerId,
      yaml: construct.yaml(facts),
      create: true,
      where: bundlePath(facts.authorityId, providerId),
    });
  };
  const switchApplied = authorityEdit?.edit?.scope === 'lane' ? authorityEdit.edit : null;
  return (
    <section className="card">
      {authoritiesError ? <div className="error" role="alert">{authoritiesError}</div> : null}
      {platform ? (
        <div className="authority-platform">
          <div className="rail-group__head">
            <strong>Platform sign-in</strong>
            <InfoMark text="The authority every browser and API caller of this deployment signs in against. Selected in assembly.yaml; a change there needs a refresh." />
          </div>
          <div className="authority-platform__grid">
            <span className="authority-platform__k">authority</span>
            <span className="authority-platform__v">
              <strong>{selectedAuthorityId || '?'}</strong>
              <span className="account-sub">{selectedAuthorityRow?.type || ''}</span>
              <InfoMark text="Who identifies the user: the Cognito pool or pools, an OIDC issuer, or the development IDP. Mixed mode is a property of this authority, its trusted pools." />
            </span>
            <span className="authority-platform__k">login lane</span>
            <span className="authority-platform__v">
              <strong>{selectedLane ? 'server-side' : 'browser-side'}</strong>
              <span className="account-sub">
                {selectedLane
                  ? `the platform runs the sign-in through ${selectedAuthorityId} and keeps one session; lane provider ${selectedLane.provider_id}`
                  : `the browser signs in with ${selectedAuthorityId} and presents tokens; the platform verifies each request`}
              </span>
              <InfoMark text={selectedLane
                ? 'Server-side: sites and clients hold no tokens; the platform holds the session (auth.type bundle). The lane provider names its upstream authority.'
                : 'Browser-side: the site or the client runs the OIDC sign-in and presents the tokens it received; the platform verifies them on every request (auth.type cognito).'} />
            </span>
            <span className="authority-platform__k">runtime authenticator</span>
            <span className="authority-platform__v"><code>{platform.authenticator || '?'}</code> <span className="account-sub">selected by <code>{platform.selected_by || '?'}</code> as provider <code>{platform.provider_id || '?'}</code></span></span>
            {platform.upstream?.issuer_url ? (
              <>
                <span className="authority-platform__k">upstream</span>
                <span className="authority-platform__v">
                  <code>{platform.upstream.type || ''}</code> <code>{platform.upstream.issuer_url}</code>
                  {platform.upstream.client_id ? <> client <code>{platform.upstream.client_id}</code></> : null}
                </span>
              </>
            ) : null}
            {platform.where ? (
              <>
                <span className="authority-platform__k">where</span>
                <span className="authority-platform__v"><code>{platform.where}</code> <CopyButton value={platform.where} label="Copy the descriptor path" /></span>
              </>
            ) : null}
            {options.length > 1 ? (
              <>
                <span className="authority-platform__k">run the sign-in</span>
                <span className="authority-platform__v">
                  <select
                    className="input input-inline"
                    aria-label="Switch the platform sign-in to"
                    value={chosen?.provider_id || ''}
                    onChange={(event) => setTarget(event.target.value)}
                  >
                    {options.map((option) => {
                      const row = providerRows.find((item) => item.provider_id === option.provider_id);
                      const lane = row && isLane(row);
                      const label = lane
                        ? `server-side, through ${laneUpstream(row) || 'its upstream'} (lane ${option.provider_id})`
                        : `browser-side, with ${option.provider_id}`;
                      return (
                        <option key={option.provider_id} value={option.provider_id}>
                          {label}{option.current ? ' (current)' : ''}
                        </option>
                      );
                    })}
                  </select>
                  <InfoMark text="Where the sign-in runs. Browser-side: the site or client signs in with the authority and presents tokens. Server-side: the platform signs in through the authority and keeps one session. Apply writes the two assembly lines; the lane changes on the next refresh. Before going server-side, the identity provider client must carry each origin's two session URLs." />
                </span>
              </>
            ) : null}
          </div>
          {chosen && !chosen.current ? (
            <>
              <Fragment
                yaml={chosen.yaml}
                where={platform.switch_where || 'assembly.yaml'}
                then="Apply writes these two lines into the staged assembly.yaml, with the previous file kept beside it. The lane changes on the next runtime refresh, never live."
              />
              <div className="authority-provider__actions">
                <button className="btn" type="button" disabled={authorityEditBusy} onClick={() => setConfirmSwitch(true)}>
                  Apply
                </button>
              </div>
            </>
          ) : null}
          {authorityEditError ? <div className="error" role="alert">{authorityEditError}</div> : null}
          {switchApplied ? (
            <div className="provider-editor__result">
              <strong>Written</strong> to <code>{switchApplied.path}</code>
              {switchApplied.backup ? <>, previous file kept as <code>{switchApplied.backup}</code></> : null}
              {switchApplied.changed?.length ? <>: {switchApplied.changed.join(', ')}. Refresh the runtime to activate the new lane.</> : '. Nothing to change.'}
            </div>
          ) : null}
          <ConfirmDialog
            open={confirmSwitch}
            title={`Sign in through ${chosen?.provider_id || ''}?`}
            body={`auth.type becomes ${chosen?.auth_type || ''} and auth.connection_hub.provider_id becomes ${chosen?.provider_id || ''}, in the staged assembly.yaml. Nothing changes until the runtime is refreshed; before turning server-side login on, the identity provider client must carry each origin's two session URLs.`}
            confirmLabel="Write"
            onCancel={() => setConfirmSwitch(false)}
            onConfirm={() => { setConfirmSwitch(false); if (chosen) void dispatch(setPlatformSignIn({ providerId: chosen.provider_id })); }}
          />
        </div>
      ) : null}
      <div className="constructs">
        <span>New provider from a construct</span>
        <InfoMark text="The shapes the platform can sign in with, as templates: one Cognito pool; several pools (mixed mode); server-side login on Cognito; server-side login on an OIDC issuer; the development IDP. The editor opens with the block, values known from the current platform filled in, placeholders in angle brackets for the rest. Validate, then Apply writes it as a new provider of the platform authority; make it the sign-in with the switch above." />
        <select className="input input-inline" aria-label="Construct" value={constructId} onChange={(event) => setConstructId(event.target.value)}>
          {AUTH_CONSTRUCTS.map((construct) => <option key={construct.id} value={construct.id}>{construct.title}</option>)}
        </select>
        <input
          className="input input-inline"
          aria-label="Provider id"
          placeholder={(AUTH_CONSTRUCTS.find((item) => item.id === constructId) || AUTH_CONSTRUCTS[0]).defaultProviderId}
          value={newProviderId}
          onChange={(event) => setNewProviderId(event.target.value)}
        />
        <button className="btn btn-ghost" type="button" onClick={openConstruct}>Open in editor</button>
        <span className="account-sub">{(AUTH_CONSTRUCTS.find((item) => item.id === constructId) || AUTH_CONSTRUCTS[0]).summary}</span>
      </div>
      {editing ? (
        <ProviderEditor
          key={`${editing.authorityId}:${editing.providerId}:${editing.create ? 'new' : 'edit'}`}
          authorityId={editing.authorityId}
          providerId={editing.providerId}
          initialYaml={editing.yaml}
          create={editing.create}
          where={editing.where}
          renderPreview={(row) => <Provider row={row} />}
          onClose={() => setEditing(null)}
        />
      ) : null}
      {(authorities?.authorities || []).map((authority) => {
        const rows = authority.providers;
        const who = rows.filter((row) => !isLane(row));
        const lanes = rows.filter(isLane);
        return (
          <div className="authority" key={authority.authority_id}>
            <div className="rail-group__head">
              <strong>{authority.label || authority.authority_id}</strong>
              <span className="account-sub">{authority.authority_id}</span>
              {authority.platform ? <span className="badge badge-app">platform authority</span> : null}
            </div>
            <div className="authority__axis">
              <span className="edit-section__name">Who identifies users</span>
              <InfoMark text="The authorities: a Cognito pool or several (mixed mode is the trusted pools of one authority), an OIDC issuer, or the development IDP. The one the platform signs in with is marked." />
              <span className="account-sub">{who.length}</span>
            </div>
            {who.map((row) => (
              <Provider key={`${authority.authority_id}:${row.provider_id}`} row={{ ...row, platform: row.provider_id === selectedAuthorityId }} onEdit={openEdit} />
            ))}
            {lanes.length ? (
              <>
                <div className="authority__axis">
                  <span className="edit-section__name">Server-side login lanes</span>
                  <InfoMark text="A lane runs the sign-in on the server through one of the authorities above (its upstream) and hands the browser one platform session. The platform is on a lane when server-side login is on." />
                  <span className="account-sub">{lanes.length}</span>
                </div>
                {lanes.map((row) => (
                  <Provider key={`${authority.authority_id}:${row.provider_id}`} row={{ ...row, platform: Boolean(selectedLane && row.provider_id === selectedLane.provider_id) }} onEdit={openEdit} />
                ))}
              </>
            ) : null}
          </div>
        );
      })}
      {authorities?.discovered?.length ? (
        <div className="authority">
          <div className="rail-group__head">
            <strong>Registered by apps</strong>
            <InfoMark text="Authority providers that apps declared in their interface and registered when they loaded." />
            <span className="account-sub">{authorities.discovered.length}</span>
          </div>
          {authorities.discovered.map((row) => (
            <div className="authority-provider" key={`${row.authority_id}:${row.provider_id}`}>
              <div className="authority-provider__head">
                <span className="authority-provider__name">
                  <strong>{row.provider_id || row.authority_id}</strong>
                  <span className="account-sub">{row.authority_id}</span>
                  {row.bundle_id ? <span className="badge badge-neutral">{row.bundle_id}</span> : null}
                </span>
              </div>
              {row.label ? <div className="account-sub">{row.label}</div> : null}
              <div className="authority-provider__facts">
                {row.credential_kinds?.length ? <span>credentials <code>{row.credential_kinds.join(', ')}</code></span> : null}
                {row.authenticators?.length ? <span>authenticators <code>{row.authenticators.join(', ')}</code></span> : null}
                {row.transports?.length ? <span>transports <code>{row.transports.join(', ')}</code></span> : null}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
