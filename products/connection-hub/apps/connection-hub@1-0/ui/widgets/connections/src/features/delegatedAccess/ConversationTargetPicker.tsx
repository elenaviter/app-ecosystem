import type { ApplicationApiCatalogModel } from './ApplicationApiCatalog';

interface ConversationTargetPickerProps {
  catalog: ApplicationApiCatalogModel;
  selected: string[];
  onChange: (targets: string[]) => void;
}

export function ConversationTargetPicker({ catalog, selected, onChange }: ConversationTargetPickerProps) {
  const known = new Set(catalog.applications.map((app) => app.id));
  const stale = selected.filter((id) => !known.has(id));
  const toggle = (id: string, checked: boolean) => onChange(
    checked ? [...new Set([...selected, id])].sort() : selected.filter((value) => value !== id),
  );
  return (
    <fieldset className="conversation-targets">
      <legend>Conversation applications</legend>
      <small>Additional applications whose conversation history this Card permits.</small>
      {catalog.status === 'loading' || catalog.status === 'idle'
        ? <p className="muted">Loading applications...</p>
        : null}
      {catalog.status === 'error' ? (
        <p className="notice warning" role="alert">Application catalog unavailable. Existing selections remain unchanged.
          <button className="btn" type="button" onClick={catalog.retry}>Retry</button>
        </p>
      ) : null}
      {catalog.status === 'ready' ? (
        <div className="conversation-targets__list">
          {catalog.applications.map((app) => (
            <label key={app.id} className="conversation-targets__row">
              <input
                type="checkbox"
                checked={selected.includes(app.id)}
                onChange={(event) => toggle(app.id, event.target.checked)}
              />
              <span>{app.label}<code>{app.id}</code></span>
            </label>
          ))}
          {stale.map((id) => (
            <label key={id} className="conversation-targets__row conversation-targets__row--stale">
              <input type="checkbox" checked onChange={() => toggle(id, false)} />
              <span>{id}<small>No longer in the application catalog</small></span>
            </label>
          ))}
        </div>
      ) : null}
    </fieldset>
  );
}
