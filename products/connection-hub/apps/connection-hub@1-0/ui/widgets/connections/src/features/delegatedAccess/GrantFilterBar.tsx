/**
 * The filter for granted-access cards: the input row that sits in the tab's
 * action row (text, settings toggle with an active-setting count, the ⓘ that
 * opens the explainer, the match tally), the settings panel that drops below
 * that row, and the explainer dialog. The three are separate components so the
 * panel can place the row beside "Create automation access" and the settings
 * across the full tab width underneath.
 *
 * Every rule these controls expose is implemented in `grantFilter.ts`; the
 * explainer describes that module and nothing more.
 */
import { useEffect } from 'react';
import {
  ALL_SEARCH_FIELDS,
  activeSettingCount,
  DEFAULT_GRANT_FILTER,
  EXPIRING_SOON_SECONDS,
  type GrantFilter,
  type GrantKind,
  type GrantSearchField,
  type GrantSort,
  type GrantState,
} from './grantFilter';

const FIELD_LABELS: Record<GrantSearchField, string> = {
  name: 'Name',
  app: 'App',
  client: 'Client id',
  door: 'Door',
};

const KINDS: Array<{ id: GrantKind; label: string }> = [
  { id: 'any', label: 'any' },
  { id: 'agent', label: 'agent' },
  { id: 'oauth', label: 'connected app' },
  { id: 'manual', label: 'manual token' },
];

const EXPIRING_DAYS = Math.round(EXPIRING_SOON_SECONDS / 86400);

const STATES: Array<{ id: GrantState; label: string }> = [
  { id: 'any', label: 'any' },
  { id: 'active', label: 'active' },
  { id: 'expiring', label: `expiring ≤ ${EXPIRING_DAYS} d` },
  { id: 'expired', label: 'expired' },
];

const SORTS: Array<{ id: GrantSort; label: string }> = [
  { id: 'newest', label: 'newest granted first' },
  { id: 'expiring', label: 'expiring soonest' },
  { id: 'name', label: 'name' },
];

export interface GrantFilterProps {
  filter: GrantFilter;
  onChange: (patch: Partial<GrantFilter>) => void;
}

/** The input row: placed by the panel inside the tab's action row. */
export function GrantFilterControls({
  filter,
  onChange,
  matched,
  total,
  settingsOpen,
  onToggleSettings,
  onOpenInfo,
}: GrantFilterProps & {
  matched: number;
  total: number;
  settingsOpen: boolean;
  onToggleSettings: () => void;
  onOpenInfo: () => void;
}) {
  const count = activeSettingCount(filter);
  return (
    <div className="grant-filter" role="search" aria-label="Filter granted access">
      <input
        type="search"
        className="input grant-filter__input"
        value={filter.query}
        placeholder="Filter cards by name, app, client id, or door"
        aria-label="Filter cards by name, app, client id, or door"
        onChange={(event) => onChange({ query: event.target.value })}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && filter.query) {
            event.stopPropagation();
            onChange({ query: '' });
          }
        }}
      />
      <button
        type="button"
        className={settingsOpen ? 'icon-btn grant-filter__toggle open' : 'icon-btn grant-filter__toggle'}
        onClick={onToggleSettings}
        title="Filter settings"
        aria-label="Filter settings"
        aria-pressed={settingsOpen}
        aria-expanded={settingsOpen}
        aria-controls="grant-filter-settings"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M2 4.5h6.2M11.8 4.5H14M2 11.5h2.2M7.8 11.5H14" fill="none" stroke="currentColor"
                strokeWidth="1.4" strokeLinecap="round" />
          <circle cx="10" cy="4.5" r="1.7" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <circle cx="6" cy="11.5" r="1.7" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
        {count ? <span className="grant-filter__count" aria-label={`${count} settings active`}>{count}</span> : null}
      </button>
      <button
        type="button"
        className="icon-btn grant-filter__info"
        onClick={onOpenInfo}
        title="How card filtering works"
        aria-label="How card filtering works"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <circle cx="8" cy="8" r="6.4" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d="M8 7.2v4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <circle cx="8" cy="4.9" r="0.85" fill="currentColor" />
        </svg>
      </button>
      <span className="grant-filter__tally account-sub" aria-live="polite">
        {matched} of {total}
      </span>
    </div>
  );
}

function Chip({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={on ? 'grant-filter__chip on' : 'grant-filter__chip'}
      aria-pressed={on}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

function DateRange({
  from,
  to,
  onFrom,
  onTo,
  name,
}: {
  from: string;
  to: string;
  onFrom: (value: string) => void;
  onTo: (value: string) => void;
  name: string;
}) {
  return (
    <span className="grant-filter__dates">
      <label className="grant-filter__date">
        <span className="grant-filter__date-label">from</span>
        <input
          type="date"
          className="input input-inline"
          value={from}
          aria-label={`${name} from`}
          onChange={(event) => onFrom(event.target.value)}
        />
      </label>
      <label className="grant-filter__date">
        <span className="grant-filter__date-label">to</span>
        <input
          type="date"
          className="input input-inline"
          value={to}
          aria-label={`${name} to`}
          onChange={(event) => onTo(event.target.value)}
        />
      </label>
    </span>
  );
}

/** The settings panel: placed by the panel under the action row. */
export function GrantFilterSettings({ filter, onChange }: GrantFilterProps) {
  const fields = filter.fields.length ? filter.fields : ALL_SEARCH_FIELDS;
  const toggleField = (field: GrantSearchField) => {
    const next = fields.includes(field) ? fields.filter((item) => item !== field) : [...fields, field];
    // The last ticked field cannot be unticked: text with nowhere to match
    // would silently hide every card.
    onChange({ fields: next.length ? ALL_SEARCH_FIELDS.filter((item) => next.includes(item)) : fields });
  };
  const reset = () => onChange({ ...DEFAULT_GRANT_FILTER, query: filter.query });
  return (
    <div className="grant-filter__settings" id="grant-filter-settings">
      <div className="grant-filter__settings-head">
        <span className="grant-filter__settings-title">Filter settings</span>
        <button type="button" className="inline-more" onClick={reset}>
          reset to defaults
        </button>
      </div>
      <div className="grant-filter__row">
        <span className="grant-filter__label">where</span>
        <div className="grant-filter__controls">
          {ALL_SEARCH_FIELDS.map((field) => (
            <Chip key={field} on={fields.includes(field)} onClick={() => toggleField(field)}>
              {FIELD_LABELS[field]}
            </Chip>
          ))}
        </div>
      </div>
      <div className="grant-filter__row">
        <span className="grant-filter__label">kind</span>
        <div className="grant-filter__controls">
          {KINDS.map((kind) => (
            <Chip key={kind.id} on={filter.kind === kind.id} onClick={() => onChange({ kind: kind.id })}>
              {kind.label}
            </Chip>
          ))}
        </div>
      </div>
      <div className="grant-filter__row">
        <span className="grant-filter__label">state</span>
        <div className="grant-filter__controls">
          {STATES.map((state) => (
            <Chip key={state.id} on={filter.state === state.id} onClick={() => onChange({ state: state.id })}>
              {state.label}
            </Chip>
          ))}
        </div>
      </div>
      <div className="grant-filter__row">
        <span className="grant-filter__label">granted</span>
        <div className="grant-filter__controls">
          <DateRange
            name="Granted"
            from={filter.grantedFrom}
            to={filter.grantedTo}
            onFrom={(value) => onChange({ grantedFrom: value })}
            onTo={(value) => onChange({ grantedTo: value })}
          />
        </div>
      </div>
      <div className="grant-filter__row">
        <span className="grant-filter__label">expires</span>
        <div className="grant-filter__controls">
          <DateRange
            name="Expires"
            from={filter.expiresFrom}
            to={filter.expiresTo}
            onFrom={(value) => onChange({ expiresFrom: value })}
            onTo={(value) => onChange({ expiresTo: value })}
          />
        </div>
      </div>
      <div className="grant-filter__row">
        <span className="grant-filter__label">order</span>
        <div className="grant-filter__controls">
          <select
            className="input input-inline"
            value={filter.sort}
            aria-label="Order"
            onChange={(event) => onChange({ sort: event.target.value as GrantSort })}
          >
            {SORTS.map((sort) => (
              <option key={sort.id} value={sort.id}>{sort.label}</option>
            ))}
          </select>
        </div>
      </div>
      <p className="grant-filter__hint">
        Filters apply as you type. Text is matched plainly over the fields ticked under where. Nothing is ranked.
      </p>
    </div>
  );
}

/** The explainer dialog opened from the ⓘ. Same shell as the widget's other
 *  dialogs; closes on ✕, backdrop click, or Escape. */
export function GrantFilterInfo({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div
      className="script-modal"
      role="dialog"
      aria-modal="true"
      aria-label="How card filtering works"
      onClick={onClose}
    >
      <div className="script-dialog grant-filter__dialog" onClick={(event) => event.stopPropagation()}>
        <div className="script-dialog-head">
          <div>
            <div className="script-dialog-title">How card filtering works</div>
            <div className="script-dialog-sub">
              Each card is one caller's authority. The filter only hides cards and never changes one.
            </div>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} title="Close" aria-label="Close">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="grant-filter__step">
          <div className="grant-filter__step-title">Step 1: every setting must pass</div>
          <p>A card stays visible only when it passes all of them. There is no scoring, so a card is either shown or hidden.</p>
          <pre className="grant-filter__formula">visible = your cards ∩ where (text) ∩ kind ∩ state ∩ granted window ∩ expires window</pre>
        </div>

        <div className="grant-filter__step">
          <div className="grant-filter__step-title">Step 2: text is matched plainly</div>
          <p>
            The text you type is looked for, case-insensitively, inside the fields ticked under <b>where</b>.
            Nothing is ranked or guessed: <code>prod</code> finds <code>productivity</code>, <code>dcr-</code> finds every
            client the platform registered for a connecting app.
          </p>
          <dl className="grant-filter__terms">
            <dt>Name</dt>
            <dd>The label you gave a card, or the name a connected app registered when it connected.</dd>
            <dt>App</dt>
            <dd>
              For a hosted agent, the agent and the app it runs in, read from its client id
              <code>kdcube-agent:&lt;app&gt;:&lt;agent&gt;</code>.
            </dd>
            <dt>Client id</dt>
            <dd>
              The id the platform authorizes and logs, such as <code>dcr-…</code> or <code>kdcube-agent:…</code>, and an
              automation's access id, the one a revoke takes.
            </dd>
            <dt>Door</dt>
            <dd>
              The protected resource a card opens. Its short alias counts, so <code>…/mcp/productivity</code> reads as
              <code>productivity</code>, and so do its full address and its catalog label.
            </dd>
          </dl>
        </div>

        <div className="grant-filter__step">
          <div className="grant-filter__step-title">Step 3: kind and state are exact</div>
          <p>
            <b>Kind</b> is who holds the credential, the same badge the card shows:{' '}
            <span className="badge badge-ok">agent</span> a hosted agent's grant,{' '}
            <span className="badge badge-ok">connected app</span> an OAuth client such as Claude,{' '}
            <span className="badge badge-warn">manual token</span> a token issued here for your own script or job.
          </p>
          <p>
            <b>State</b> reads the card's expiry against now. <b>Active</b>: the expiry is ahead, or none is recorded.{' '}
            <b>Expiring</b>: it ends within {EXPIRING_DAYS} days. <b>Expired</b>: it has passed and the credential no longer
            works. Active includes expiring, since that credential still works.
          </p>
        </div>

        <div className="grant-filter__step">
          <div className="grant-filter__step-title">Step 4: two date windows</div>
          <p>
            <b>Granted</b> is the moment the card came to exist: the consent you gave, or the create you did here.{' '}
            <b>Expires</b> is when its credential stops working. From and to are whole days in your time zone,
            both inclusive. A card without a recorded date matches only while that window is empty, since nothing
            can be said about it.
          </p>
        </div>

        <div className="grant-filter__step">
          <div className="grant-filter__step-title">Agent cards</div>
          <p>
            An agent card groups every permission row of one agent. It stays visible when any of its rows matches,
            and it shows all of them, so what an agent can do keeps reading in one place.
          </p>
        </div>

        <div className="grant-filter__step">
          <div className="grant-filter__step-title">Order</div>
          <p>
            Agent cards are listed before the other cards, and the order applies within each group. Newest granted
            first by default. Expiring soonest puts cards without a recorded expiry last. Name orders by label, and
            an agent card by agent and app.
          </p>
        </div>

        <p className="grant-filter__why">
          Why hide instead of rank: a list you filter shows exactly what matches, so a card that should be revoked
          cannot sit unnoticed at rank twelve.
        </p>
      </div>
    </div>
  );
}
