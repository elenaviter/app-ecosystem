/**
 * The two invocation-policy controls of a card, each naming the operation it
 * belongs to so no control can be read as its neighbour's.
 *
 * `InvocationPolicyControl` switches the policy of an operation the card
 * already grants in the edit draft; Save writes it (W872: no call on a click).
 * `OperationInvocationChoice` records the policy for an operation that is NOT
 * granted yet; the grant and the policy are then committed together by the
 * focused grant transaction. The two never render for the same operation.
 */
import type { DelegatedInvocationPolicy } from '../../api/types';
import { INVOCATION_MODE_TEXT, policySwitchView, type InvocationMode } from './invocationChoice';

/** W872: a click only changes the draft; the mode is written when the person presses Save. */
export const POLICY_SAVED_ON_SAVE = 'Changes when you press Save.';

export function InvocationPolicyControl({
  operation,
  policy,
  draftMode,
  busy,
  onceDisabled = false,
  onSet,
}: {
  operation: string;
  policy?: DelegatedInvocationPolicy;
  /** W872: the mode the person switched to in this edit, not saved yet. */
  draftMode?: InvocationMode;
  busy: boolean;
  onceDisabled?: boolean;
  /** Records the choice in the edit draft. It makes no request (operator, 2026-10-15). */
  onSet: (mode: InvocationMode) => void;
}) {
  const { shown, changed, alwaysDisabled, onceDisabled: onceOff, used } = policySwitchView(
    policy, draftMode, busy, onceDisabled,
  );
  const onceAvailable = !draftMode && policy?.mode === 'once' && policy?.remaining === 1;
  return (
    <span className="operation-policy" data-operation={operation}>
      <span
        className={`invocation-policy-control${changed ? ' invocation-policy-control--changed' : ''}`}
        role="group"
        aria-label={`Invocation policy for ${operation}`}
      >
        <button
          type="button"
          className={shown === 'always' ? 'active' : ''}
          aria-pressed={shown === 'always'}
          aria-label={`${operation}: always`}
          title={`Allow every ${operation} call while the card is active. ${POLICY_SAVED_ON_SAVE}`}
          disabled={alwaysDisabled}
          onClick={() => onSet('always')}
        >
          Every time
        </button>
        <button
          type="button"
          className={shown === 'once' ? 'active' : ''}
          aria-pressed={shown === 'once'}
          aria-label={`${operation}: once`}
          title={onceDisabled
            ? 'Namespace and all-scope secret authority is reusable; choose Always or grant one exact key'
            : `${onceAvailable ? `One ${operation} invocation remains` : `Allow the next ${operation} invocation once`}. ${POLICY_SAVED_ON_SAVE}`}
          disabled={onceOff}
          onClick={() => onSet('once')}
        >
          Once
        </button>
        {changed ? (
          <small className="invocation-policy-control__changed">changed, not saved</small>
        ) : used ? (
          <small>used</small>
        ) : null}
      </span>
    </span>
  );
}

export function OperationInvocationChoice({
  operation,
  mode,
  busy,
  onceDisabled = false,
  onChoose,
}: {
  operation: string;
  mode: InvocationMode | null;
  busy: boolean;
  onceDisabled?: boolean;
  onChoose: (mode: InvocationMode) => void;
}) {
  return (
    <span className="operation-policy operation-policy--choice" data-operation={operation}>
      <span className="invocation-policy-control" role="group" aria-label={`Invocation policy for ${operation}`}>
        <button
          type="button"
          className={mode === 'once' ? 'active' : ''}
          aria-pressed={mode === 'once'}
          aria-label={`${operation}: once`}
          title={onceDisabled
            ? 'Namespace and all-scope secret authority is reusable; choose Always or grant one exact key'
            : `Allow ${operation} for ${INVOCATION_MODE_TEXT.once}`}
          disabled={busy || onceDisabled}
          onClick={() => onChoose('once')}
        >
          Once
        </button>
        <button
          type="button"
          className={mode === 'always' ? 'active' : ''}
          aria-pressed={mode === 'always'}
          aria-label={`${operation}: always`}
          title={`Allow ${operation} for ${INVOCATION_MODE_TEXT.always}`}
          disabled={busy}
          onClick={() => onChoose('always')}
        >
          Every time
        </button>
      </span>
      {!mode ? (
        <span className="operation-policy__status operation-policy__status--attention" aria-live="polite">
          choose one
        </span>
      ) : null}
    </span>
  );
}
