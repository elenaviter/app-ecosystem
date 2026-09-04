/**
 * Presentational pieces of the compatible-resource editor. They render what
 * the server said about a card's resources and record the grantor's choices;
 * the rules live in resourceEditing.ts and every save decision is the
 * server's.
 */
import {
  driftNeedsReview,
  offerReasonText,
  type ResourceDriftState,
  type ResourceOffer,
} from './resourceEditing';

/** The head of one resource section in edit mode: name, address hint, and
 *  the per-resource remove action. */
export function ResourceSectionHead({
  title,
  isNew,
  onRemove,
  removeLabel,
}: {
  title: string;
  isNew?: boolean;
  onRemove?: () => void;
  removeLabel?: string;
}) {
  return (
    <div className="resource-section-head">
      <div className="account-title">
        {title}
        {isNew ? <span className="badge badge-warn">adding</span> : null}
      </div>
      {onRemove ? (
        <button type="button" className="btn btn-ghost resource-section-head__remove" onClick={onRemove}>
          {removeLabel || (isNew ? 'Do not add' : 'Remove from card')}
        </button>
      ) : null}
    </div>
  );
}

/** A resource marked for removal: the section collapses to one line so the
 *  other resources keep their room, and the decision can be undone until Save. */
export function RemovedResourceStub({ title, onUndo }: { title: string; onUndo: () => void }) {
  return (
    <div className="resource-removed-stub">
      <span>
        <b>{title}</b> is removed from this card when you save. Its other resources, their policies,
        and the credential are not affected.
      </span>
      <button type="button" className="inline-more" onClick={onUndo}>Undo</button>
    </div>
  );
}

/** Per-resource descriptor review: what changed on THIS resource's own
 *  authority since the card accepted it, and the checkbox that accepts a
 *  changed selected operation. Unticked changed operations stay suspended. */
export function ResourceDriftReview({
  resource,
  state,
  accepted,
  onToggleAccept,
}: {
  resource: string;
  state?: ResourceDriftState;
  accepted: string[];
  onToggleAccept: (operation: string, on: boolean) => void;
}) {
  if (!state || !driftNeedsReview(state)) return null;
  const changed = state.changed_operations || [];
  const removed = state.removed_operations || [];
  const added = state.added_operations || [];
  const removedClaims = state.removed_claims || [];
  const addedClaims = state.added_claims || [];
  const kindLabel = state.kind === 'remote_mcp' ? 'connector descriptor' : 'catalog row';
  return (
    <div className="resource-drift-review" data-resource={resource}>
      <div className="resource-drift-review__head">
        <strong>
          {state.status === 'removed'
            ? 'This resource is no longer offered'
            : `This resource's ${kindLabel} changed since this card accepted it`}
        </strong>
        {state.accepted_revision || state.current_revision ? (
          <small className="muted">
            accepted <code>{state.accepted_revision || 'none'}</code>
            {' · '}
            current <code>{state.current_revision || 'none'}</code>
          </small>
        ) : null}
      </div>
      {changed.length ? (
        <div className="resource-drift-review__group">
          <div className="card-field-label">Changed, suspended until you accept</div>
          <ul className="resource-drift-review__list">
            {changed.map((operation) => {
              const on = accepted.includes(operation);
              return (
                <li key={`changed-${operation}`}>
                  <label className="descriptor-accept">
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={(event) => onToggleAccept(operation, event.target.checked)}
                    />
                    <span>
                      Accept the new descriptor of <code>{operation}</code>
                    </span>
                  </label>
                  <small className="muted">
                    Until accepted, this operation stays granted on the card but is not run.
                  </small>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      {removed.length || removedClaims.length ? (
        <div className="resource-drift-review__group">
          <div className="card-field-label">No longer offered</div>
          <ul className="resource-drift-review__list">
            {removed.map((operation) => (
              <li key={`removed-${operation}`}>
                <code>{operation}</code> — already ineffective, removed when you save
              </li>
            ))}
            {removedClaims.map((claim) => (
              <li key={`removed-claim-${claim}`}>
                <code>{claim}</code> — already ineffective, removed when you save
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {added.length || addedClaims.length ? (
        <div className="resource-drift-review__group">
          <div className="card-field-label">Newly advertised, not granted</div>
          <ul className="resource-drift-review__list">
            {added.map((operation) => (
              <li key={`added-${operation}`}>
                <code>{operation}</code> — select it above to grant it
              </li>
            ))}
            {addedClaims.map((claim) => (
              <li key={`added-claim-${claim}`}>
                <code>{claim}</code> — select it above to grant it
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/** The owner-scoped picker of resources that may join this card, with the
 *  reason for every resource that may not. */
export function ResourceOfferPicker({
  offers,
  added,
  onAdd,
}: {
  offers: ResourceOffer[];
  added: string[];
  onAdd: (resource: string) => void;
}) {
  const candidates = offers.filter((offer) => offer.reason !== 'already_on_card' && !added.includes(offer.resource));
  if (!candidates.length) return null;
  const compatible = candidates.filter((offer) => offer.compatible);
  const blocked = candidates.filter((offer) => !offer.compatible);
  return (
    <div className="resource-offer-picker">
      <div className="account-title">Add a resource to this card</div>
      {compatible.length ? (
        <div className="resource-offer-picker__row">
          {compatible.map((offer) => (
            <button
              type="button"
              className="btn btn-ghost resource-offer"
              key={offer.resource}
              title={offer.resource}
              onClick={() => onAdd(offer.resource)}
            >
              + {offer.label}
            </button>
          ))}
        </div>
      ) : (
        <p className="muted resource-offer-picker__empty">Nothing else compatible is delegable to this card.</p>
      )}
      {blocked.length ? (
        <ul className="resource-offer-picker__blocked">
          {blocked.map((offer) => (
            <li key={offer.resource} className="resource-offer--blocked" title={offer.resource}>
              <b>{offer.label}</b>
              <span className="muted"> {offerReasonText(offer)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
