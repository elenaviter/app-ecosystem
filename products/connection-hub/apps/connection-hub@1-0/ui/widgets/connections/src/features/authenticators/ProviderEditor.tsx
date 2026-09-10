/**
 * The editing buffer of one sign-in provider: a piece of YAML mapped onto
 * the real descriptor. Validate checks it on the server (parsed, secret
 * keys merged where it says <unchanged>, resolvable) and renders it the way
 * the pane shows a provider; Apply writes it to the staged file through the
 * platform's editor, with a backup beside the file. Activation is a runtime
 * refresh until the live reload lands, and the result says so.
 */
import { useState } from 'react';
import type { AuthorityProviderRow, AuthorityProviderValidateResult } from '../../api/types';
import { useAppDispatch, useAppSelector } from '../../app/hooks';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { InfoMark } from '../../components/InfoMark';
import { setAuthorityProvider, validateAuthorityProvider } from './authenticatorsSlice';

export interface ProviderEditorProps {
  authorityId: string;
  providerId: string;
  initialYaml: string;
  create?: boolean;
  where: string;
  renderPreview: (row: AuthorityProviderRow) => React.ReactNode;
  onClose: () => void;
}

export function ProviderEditor({ authorityId, providerId, initialYaml, create, where, renderPreview, onClose }: ProviderEditorProps) {
  const dispatch = useAppDispatch();
  const { authorityEditBusy, authorityEditError, authorityEdit } = useAppSelector((s) => s.authenticators);
  const [yaml, setYaml] = useState(initialYaml);
  const [checked, setChecked] = useState<AuthorityProviderValidateResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [allowSecretRemoval, setAllowSecretRemoval] = useState(false);
  // The buffer that was validated: Apply is allowed only for exactly it.
  const [checkedYaml, setCheckedYaml] = useState(initialYaml);
  const stale = checked !== null && checked.valid === true && checkedYaml !== yaml;

  const validate = async () => {
    setChecking(true);
    const result = await dispatch(validateAuthorityProvider({ authorityId, providerId, yaml })).unwrap().catch((message: string) => ({ ok: false, problems: [message] } as AuthorityProviderValidateResult));
    setChecked(result);
    setCheckedYaml(yaml);
    setChecking(false);
  };
  const apply = async () => {
    setConfirm(false);
    await dispatch(setAuthorityProvider({ authorityId, providerId, yaml, allowSecretRemoval, create })).unwrap().catch(() => undefined);
  };
  const applied = authorityEdit?.edit && authorityEdit.edit.changed?.some((key) => key.endsWith(`.providers.${providerId}`));
  const canApply = checked?.valid === true && checkedYaml === yaml && !authorityEditBusy;
  return (
    <section className="card provider-editor">
      <div className="edit-section__head">
        <span className="edit-section__name">{create ? 'New provider' : 'Edit provider'} <code>{providerId}</code></span>
        <InfoMark text="This is the provider's block as it stands in the descriptor, in an editing buffer. Change it, Validate to see problems or the provider as it would appear, then Apply to write it into the staged file. Keys that read <unchanged> keep their value from the file." />
        <span className="edit-section__count">{where}</span>
      </div>
      <textarea
        className="input provider-editor__buffer"
        spellCheck={false}
        value={yaml}
        rows={Math.min(28, Math.max(8, yaml.split('\n').length + 1))}
        onChange={(event) => { setYaml(event.target.value); }}
        aria-label="Provider YAML"
      />
      <div className="provider-editor__actions">
        <button className="btn btn-ghost" type="button" disabled={checking || authorityEditBusy} onClick={() => void validate()}>
          {checking ? 'Checking…' : 'Validate'}
        </button>
        <button className="btn" type="button" disabled={!canApply} title={canApply ? undefined : 'Validate the buffer first'} onClick={() => setConfirm(true)}>
          Apply
        </button>
        <label className="checkbox-line provider-editor__allow">
          <input type="checkbox" checked={allowSecretRemoval} onChange={(event) => setAllowSecretRemoval(event.target.checked)} />
          <span>allow dropping a secret-bearing key</span>
        </label>
        <button className="btn btn-ghost" type="button" onClick={onClose}>Close</button>
      </div>
      {stale ? <div className="provider-editor__note">The buffer changed since it was validated. Validate again before applying.</div> : null}
      {checked && checked.problems?.length ? (
        <ul className="provider-editor__problems">
          {checked.problems.map((problem) => <li key={problem}>{problem}</li>)}
        </ul>
      ) : null}
      {checked?.valid && checked.rendered ? (
        <div className="provider-editor__preview">
          <div className="rail-group__head"><strong>As it would appear</strong></div>
          {renderPreview(checked.rendered)}
        </div>
      ) : null}
      {authorityEditError ? <div className="error" role="alert">{authorityEditError}</div> : null}
      {applied && authorityEdit?.edit ? (
        <div className="provider-editor__result">
          <strong>Written</strong> to <code>{authorityEdit.edit.path}</code>, previous file kept as <code>{authorityEdit.edit.backup}</code>.
          {authorityEdit.activation === 'refresh'
            ? ' Refresh the runtime to activate it.'
            : authorityEdit.activation === 'live' ? ' Active now.' : ''}
        </div>
      ) : null}
      <ConfirmDialog
        open={confirm}
        title={`Write ${providerId} into the staged descriptor?`}
        body="The block replaces the one in bundles.yaml; every other key and comment stays, and the previous file is kept beside it. It takes effect on the next runtime refresh."
        confirmLabel="Write"
        onCancel={() => setConfirm(false)}
        onConfirm={() => void apply()}
      />
    </section>
  );
}
