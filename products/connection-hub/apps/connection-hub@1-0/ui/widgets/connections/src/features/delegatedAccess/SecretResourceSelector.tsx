import { useMemo, useState } from 'react';

import {
  buildSecretResources,
  secretResourceLabel,
  type SecretSelectorCoverage,
  type SecretSelectorScope,
  type SecretSelectorTarget,
  type UserSecretArea,
} from './secretResourceSelection';

export interface SecretSelectorContext {
  tenant: string;
  project: string;
}

export function SecretResourceSelector({
  context,
  existing,
  disabled,
  onAdd,
}: {
  context: SecretSelectorContext;
  existing: string[];
  disabled?: boolean;
  onAdd: (resources: string[]) => void;
}) {
  const [scope, setScope] = useState<SecretSelectorScope>('platform');
  const [coverage, setCoverage] = useState<SecretSelectorCoverage>('exact');
  const [target, setTarget] = useState<SecretSelectorTarget>('one');
  const [key, setKey] = useState('');
  const [bundleId, setBundleId] = useState('');
  const [userId, setUserId] = useState('');
  const [userArea, setUserArea] = useState<UserSecretArea>('direct');
  const [userBundleId, setUserBundleId] = useState('');
  const [error, setError] = useState('');

  const preview = useMemo(() => {
    try {
      return buildSecretResources({
        ...context,
        scope,
        coverage,
        target,
        key,
        bundleId,
        userId,
        userArea,
        userBundleId,
      });
    } catch {
      return [];
    }
  }, [bundleId, context, coverage, key, scope, target, userArea, userBundleId, userId]);

  const add = () => {
    try {
      const selected = buildSecretResources({
        ...context,
        scope,
        coverage,
        target,
        key,
        bundleId,
        userId,
        userArea,
        userBundleId,
      });
      const additions = selected.filter((resource) => !existing.includes(resource));
      if (!additions.length) {
        setError('This secret authority is already on the card.');
        return;
      }
      setError('');
      onAdd(additions);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Secret authority is invalid.');
    }
  };

  const coverageField = scope !== 'deployment' ? (
    <label className="secret-selector__field">
      <span>Coverage</span>
      <select
        className="input"
        value={coverage}
        onChange={(event) => setCoverage(event.target.value as SecretSelectorCoverage)}
      >
        <option value="exact">Exact key</option>
        <option value="prefix">Namespace</option>
        <option value="all">All in scope</option>
      </select>
    </label>
  ) : null;

  return (
    <div className="secret-selector">
      <div className="secret-selector__grid">
        <label className="secret-selector__field">
          <span>Secret scope</span>
          <select
            className="input"
            value={scope}
            onChange={(event) => {
              setScope(event.target.value as SecretSelectorScope);
              setError('');
            }}
          >
            <option value="platform">Platform</option>
            <option value="bundle">Application bundle</option>
            <option value="user">User-owned</option>
            <option value="deployment">Entire deployment</option>
          </select>
        </label>

        {scope === 'bundle' || scope === 'user' ? (
          <label className="secret-selector__field">
            <span>{scope === 'bundle' ? 'Applications' : 'Users'}</span>
            <select
              className="input"
              value={target}
              onChange={(event) => {
                const value = event.target.value as SecretSelectorTarget;
                setTarget(value);
                if (scope === 'user' && value === 'all') setUserArea('direct');
              }}
            >
              <option value="one">One {scope === 'bundle' ? 'application' : 'user'}</option>
              <option value="all">All {scope === 'bundle' ? 'applications' : 'users'}</option>
            </select>
          </label>
        ) : null}

        {scope === 'bundle' && target === 'one' ? (
          <label className="secret-selector__field secret-selector__field--wide">
            <span>Application ID</span>
            <input className="input" value={bundleId} onChange={(event) => setBundleId(event.target.value)} />
          </label>
        ) : null}

        {scope === 'user' && target === 'one' ? (
          <>
            <label className="secret-selector__field">
              <span>User ID</span>
              <input className="input" value={userId} onChange={(event) => setUserId(event.target.value)} />
            </label>
            <label className="secret-selector__field">
              <span>Ownership</span>
              <select
                className="input"
                value={userArea}
                onChange={(event) => setUserArea(event.target.value as UserSecretArea)}
              >
                <option value="direct">User</option>
                <option value="bundle">User application</option>
              </select>
            </label>
            {userArea === 'bundle' ? (
              <label className="secret-selector__field secret-selector__field--wide">
                <span>Application ID</span>
                <input className="input" value={userBundleId} onChange={(event) => setUserBundleId(event.target.value)} />
              </label>
            ) : null}
          </>
        ) : null}

        {coverageField}
        {scope !== 'deployment' && coverage !== 'all' ? (
          <label className="secret-selector__field secret-selector__field--wide">
            <span>{coverage === 'prefix' ? 'Namespace' : 'Key'}</span>
            <input
              className="input"
              value={key}
              placeholder={scope === 'platform' ? 'services.provider.api_key' : 'provider.api_key'}
              onChange={(event) => setKey(event.target.value)}
            />
          </label>
        ) : null}
      </div>
      {preview.length ? (
        <div className="secret-selector__preview">
          {preview.map((resource) => <code key={resource}>{secretResourceLabel(resource)}</code>)}
        </div>
      ) : null}
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      <button className="btn btn-ghost" type="button" disabled={disabled} onClick={add}>
        Add secret authority
      </button>
    </div>
  );
}
