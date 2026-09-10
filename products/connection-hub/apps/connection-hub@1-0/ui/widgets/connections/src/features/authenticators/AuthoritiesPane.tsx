/**
 * The deployment's sign-in authorities, read as a console: the platform's
 * own sign-in first and marked, then every provider of every authority in
 * the registry with the pools it trusts (mixed mode), then the authority
 * providers apps registered at load. Descriptor-owned rows are read-only
 * here and say where they live; a change is a descriptor edit and a refresh.
 */
import { useEffect } from 'react';
import type { AuthorityPoolRow, AuthorityProviderRow } from '../../api/types';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { InfoMark } from '../../components/InfoMark';
import { CopyButton } from '../../components/CopyControls';
import { loadAuthorities } from './authenticatorsSlice';

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

function Provider({ row }: { row: AuthorityProviderRow }) {
  const auth = row.authenticator || {};
  const isSession = Boolean(row.session);
  return (
    <div className={`authority-provider${row.platform ? ' authority-provider--platform' : ''}`}>
      <div className="authority-provider__head">
        <span className="authority-provider__name">
          <strong>{row.provider_id}</strong>
          <span className="account-sub">{row.type || ''}</span>
          {row.platform ? <span className="badge badge-app">platform sign-in</span> : null}
          {row.enabled === false ? <span className="badge badge-neutral">disabled</span> : null}
        </span>
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
  const platform = authorities?.platform;
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
            <span className="authority-platform__k">provider</span>
            <span className="authority-platform__v">
              <strong>{platform.provider_id || '?'}</strong>
              <span className="account-sub">{platform.provider_type || ''}</span>
              <span className="badge badge-app">platform</span>
            </span>
            <span className="authority-platform__k">runtime authenticator</span>
            <span className="authority-platform__v"><code>{platform.authenticator || '?'}</code> <span className="account-sub">selected by <code>{platform.selected_by || '?'}</code></span></span>
            <span className="authority-platform__k">server-side login</span>
            <span className="authority-platform__v">
              {platform.hosted_sign_in ? 'on' : 'off'}
              <InfoMark text={platform.hosted_sign_in
                ? 'On: the platform runs the sign-in itself and keeps the session server-side (provider browser_session, auth.type bundle). Sites and clients hold no tokens.'
                : 'Off: a site or a client runs the sign-in on its own and presents the tokens it received; the platform verifies them (provider cognito, auth.type cognito). Switch by changing both keys in assembly.yaml and refreshing.'} />
            </span>
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
          </div>
        </div>
      ) : null}
      {(authorities?.authorities || []).map((authority) => (
        <div className="authority" key={authority.authority_id}>
          <div className="rail-group__head">
            <strong>{authority.label || authority.authority_id}</strong>
            <span className="account-sub">{authority.authority_id}</span>
            {authority.platform ? <span className="badge badge-app">platform authority</span> : null}
            <span className="account-sub">{authority.providers.length} provider{authority.providers.length === 1 ? '' : 's'}</span>
          </div>
          {authority.providers.map((row) => <Provider key={`${authority.authority_id}:${row.provider_id}`} row={row} />)}
        </div>
      ))}
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
