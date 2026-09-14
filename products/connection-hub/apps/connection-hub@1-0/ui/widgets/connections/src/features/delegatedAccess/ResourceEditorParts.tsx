/**
 * Presentational pieces of the compatible-resource editor. They render what
 * the server said about a card's resources and record the grantor's choices;
 * the rules live in resourceEditing.ts and every save decision is the
 * server's.
 */
import type { ReactNode } from 'react';
import {
  doorName,
  driftNeedsReview,
  offerReasonText,
  operationDisplayLabel,
  pickerOffers,
  type ResourceDriftState,
  type ResourceOffer,
} from './resourceEditing';
import { InfoMark } from '../../components/InfoMark';
import { OperationInvocationChoice } from './InvocationControls';
import type {
  DelegatedAccessGrantOption,
  DelegatedAccessOperationOption,
} from '../../api/types';
import type { InvocationMode } from './invocationChoice';

/** The disclosure head of one resource section in edit mode: name, pending
 *  state, and the per-resource remove action. */
export function ResourceSectionHead({
  title,
  isNew,
  onRemove,
  removeLabel,
  compositionState,
  controlLabel,
}: {
  title: string;
  isNew?: boolean;
  onRemove?: () => void;
  removeLabel?: string;
  compositionState?: 'added' | 'removed';
  controlLabel?: string;
}) {
  return (
    <summary className="resource-section-head">
      <span className="resource-section-head__title">
        <strong>{title}</strong>
        {isNew ? <span className="badge badge-warn">adding</span> : null}
        {compositionState === 'added' ? (
          <span className="badge badge-ok">added by {controlLabel || 'Control Card'}</span>
        ) : null}
        {compositionState === 'removed' ? (
          <span className="badge badge-error">removed by {controlLabel || 'Control Card'}</span>
        ) : null}
      </span>
      {onRemove ? (
        <button
          type="button"
          className="btn btn-ghost resource-section-head__remove"
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onRemove();
          }}
        >
          {removeLabel || (isNew ? 'Do not add' : 'Remove from card')}
        </button>
      ) : null}
    </summary>
  );
}

/** A resource marked for removal: the section collapses to one line so the
 *  other resources keep their room, and the decision can be undone until Save. */
export function RemovedResourceStub({
  title,
  onUndo,
  remainingEffect,
}: {
  title: string;
  onUndo: () => void;
  remainingEffect?: string;
}) {
  return (
    <div className="resource-removed-stub">
      <span>
        Removed from the Caller Card when you save: <b>{title}</b>.{' '}
        {remainingEffect || "The card's other resources and its credential stay as they are."}
      </span>
      <button type="button" className="inline-more" onClick={onUndo}>Undo</button>
    </div>
  );
}

function DriftTable({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="resource-drift-table" role="table" aria-label={label}>
      <div className="resource-drift-table__head" role="row">
        <span role="columnheader">Item</span>
        <span role="columnheader">Current effect</span>
        <span role="columnheader">Decision</span>
      </div>
      {children}
    </div>
  );
}

function DriftIdentity({
  identifier,
  label,
  description,
  kind,
}: {
  identifier: string;
  label?: string;
  description?: string;
  kind: 'Tool' | 'Permission';
}) {
  return (
    <span className="resource-drift-identity">
      <small>{kind}</small>
      <span className="resource-drift-identity__name">
        <strong>{operationDisplayLabel(identifier, [{ name: identifier, label }])}</strong>
        <code>{identifier}</code>
      </span>
      {description ? <InfoMark text={description} /> : null}
    </span>
  );
}

/** Per-resource descriptor review: what changed on THIS resource's own
 *  authority since the card accepted it, and the checkbox that accepts a
 *  changed selected operation. Unticked changed operations stay suspended. */
export function ResourceDriftReview({
  resource,
  state,
  accepted,
  operationOptions,
  grantOptions,
  selectedOperations,
  selectedClaims,
  invocationModeFor,
  busy,
  onceDisabled,
  onToggleAccept,
  onToggleOperation,
  onToggleClaim,
  onChooseInvocation,
}: {
  resource: string;
  state?: ResourceDriftState;
  accepted: string[];
  operationOptions: DelegatedAccessOperationOption[];
  grantOptions: DelegatedAccessGrantOption[];
  selectedOperations: string[];
  selectedClaims: string[];
  invocationModeFor: (operation: string) => InvocationMode | null;
  busy: boolean;
  onceDisabled?: boolean;
  onToggleAccept: (operation: string, on: boolean) => void;
  onToggleOperation: (operation: string, grants: string[], on: boolean) => void;
  onToggleClaim: (claim: string, on: boolean) => void;
  onChooseInvocation: (operation: string, mode: InvocationMode) => void;
}) {
  if (!state || !driftNeedsReview(state)) return null;
  const changed = state.changed_operations || [];
  const removed = state.removed_operations || [];
  const added = state.added_operations || [];
  const removedClaims = state.removed_claims || [];
  const addedClaims = state.added_claims || [];
  const kindLabel = state.kind === 'remote_mcp' ? 'connector descriptor' : 'catalog row';
  const operationOption = (operation: string) => (
    operationOptions.find((candidate) => candidate.name === operation)
  );
  const grantOption = (claim: string) => (
    grantOptions.find((candidate) => candidate.grant === claim)
  );
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
          <DriftTable label="Changed tools">
            {changed.map((operation) => {
              const on = accepted.includes(operation);
              const option = operationOption(operation);
              return (
                <div className="resource-drift-table__row" role="row" key={`changed-${operation}`}>
                  <span className="resource-drift-table__item" role="cell">
                    <DriftIdentity
                      identifier={operation}
                      label={option?.label}
                      description={option?.description}
                      kind="Tool"
                    />
                  </span>
                  <span className="resource-drift-table__effect" role="cell">
                    <span className="badge badge-warn">Suspended</span>
                    <small>Not run until its updated descriptor is accepted.</small>
                  </span>
                  <span className="resource-drift-table__decision" role="cell">
                    <label className="descriptor-accept">
                      <input
                        type="checkbox"
                        checked={on}
                        disabled={busy}
                        onChange={(event) => onToggleAccept(operation, event.target.checked)}
                      />
                      <span>{on ? 'Update accepted' : 'Accept update'}</span>
                    </label>
                  </span>
                </div>
              );
            })}
          </DriftTable>
        </div>
      ) : null}
      {removed.length || removedClaims.length ? (
        <div className="resource-drift-review__group">
          <div className="card-field-label">No longer offered</div>
          <DriftTable label="Items no longer offered">
            {removed.map((operation) => {
              const option = operationOption(operation);
              return (
                <div className="resource-drift-table__row" role="row" key={`removed-${operation}`}>
                  <span className="resource-drift-table__item" role="cell">
                    <DriftIdentity
                      identifier={operation}
                      label={option?.label}
                      description={option?.description}
                      kind="Tool"
                    />
                  </span>
                  <span className="resource-drift-table__effect" role="cell">
                    <span className="badge badge-neutral">Unavailable</span>
                    <small>Already ineffective.</small>
                  </span>
                  <small className="resource-drift-table__decision muted" role="cell">
                    No choice: the service no longer offers it. Save removes it from the card.
                  </small>
                </div>
              );
            })}
            {removedClaims.map((claim) => {
              const option = grantOption(claim);
              return (
                <div className="resource-drift-table__row" role="row" key={`removed-claim-${claim}`}>
                  <span className="resource-drift-table__item" role="cell">
                    <DriftIdentity
                      identifier={claim}
                      label={option?.label}
                      description={option?.description}
                      kind="Permission"
                    />
                  </span>
                  <span className="resource-drift-table__effect" role="cell">
                    <span className="badge badge-neutral">Unavailable</span>
                    <small>Already ineffective.</small>
                  </span>
                  <small className="resource-drift-table__decision muted" role="cell">
                    No choice: the service no longer offers it. Save removes it from the card.
                  </small>
                </div>
              );
            })}
          </DriftTable>
        </div>
      ) : null}
      {added.length || addedClaims.length ? (
        <div className="resource-drift-review__group">
          <div className="card-field-label">Newly advertised, not granted</div>
          <DriftTable label="Newly advertised tools and permissions">
            {added.map((operation) => {
              const option = operationOption(operation);
              const selected = selectedOperations.includes(operation);
              return (
                <div className="resource-drift-table__row" role="row" key={`added-${operation}`}>
                  <span className="resource-drift-table__item" role="cell">
                    <DriftIdentity
                      identifier={operation}
                      label={option?.label}
                      description={option?.description}
                      kind="Tool"
                    />
                  </span>
                  <span className="resource-drift-table__effect" role="cell">
                    <span className={selected ? 'badge badge-ok' : 'badge badge-neutral'}>
                      {selected ? 'Selected' : 'Not granted'}
                    </span>
                    <small>{selected ? 'Added when you save.' : 'The card cannot run it.'}</small>
                  </span>
                  <span className="resource-drift-table__decision" role="cell">
                    <label className="descriptor-accept" title={option?.description || operation}>
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={busy}
                        onChange={(event) => onToggleOperation(
                          operation,
                          option?.grants || [],
                          event.target.checked,
                        )}
                      />
                      <span>{selected ? 'Selected' : 'Add to card'}</span>
                    </label>
                    {selected ? (
                      <OperationInvocationChoice
                        operation={operation}
                        mode={invocationModeFor(operation)}
                        busy={busy}
                        onceDisabled={onceDisabled}
                        onChoose={(mode) => onChooseInvocation(operation, mode)}
                      />
                    ) : null}
                  </span>
                </div>
              );
            })}
            {addedClaims.map((claim) => {
              const option = grantOption(claim);
              const selected = selectedClaims.includes(claim);
              return (
                <div className="resource-drift-table__row" role="row" key={`added-claim-${claim}`}>
                  <span className="resource-drift-table__item" role="cell">
                    <DriftIdentity
                      identifier={claim}
                      label={option?.label}
                      description={option?.description}
                      kind="Permission"
                    />
                  </span>
                  <span className="resource-drift-table__effect" role="cell">
                    <span className={selected ? 'badge badge-ok' : 'badge badge-neutral'}>
                      {selected ? 'Selected' : 'Not granted'}
                    </span>
                    <small>{selected ? 'Added when you save.' : 'The card does not hold it.'}</small>
                  </span>
                  <span className="resource-drift-table__decision" role="cell">
                    <label className="descriptor-accept" title={option?.description || claim}>
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={busy}
                        onChange={(event) => onToggleClaim(claim, event.target.checked)}
                      />
                      <span>{selected ? 'Selected' : 'Add to card'}</span>
                    </label>
                  </span>
                </div>
              );
            })}
          </DriftTable>
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
  title = 'Add to this card',
  help,
}: {
  offers: ResourceOffer[];
  added: string[];
  onAdd: (resource: string) => void;
  title?: string;
  help?: string;
}) {
  const { compatible, blocked, clientDoor } = pickerOffers(offers, added);
  // Nothing to add and nothing to explain: no section at all.
  if (!compatible.length && !blocked.length) return null;
  return (
    <div className="resource-offer-picker">
      <div className="edit-section__head">
        <span className="edit-section__name">{title}</span>
        <InfoMark
          text={help || (clientDoor
            ? `This client connects to ${doorName(clientDoor)}, so only services reachable through that endpoint can be added.`
            : 'Resources this card may take in addition to what it holds.')}
        />
      </div>
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
        <p className="muted resource-offer-picker__empty">Nothing more can be added.</p>
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
