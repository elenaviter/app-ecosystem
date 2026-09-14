/**
 * The two invocation-policy controls of a card, each naming the operation it
 * belongs to so no control can be read as its neighbour's.
 *
 * `InvocationPolicyControl` changes the LIVE policy of an operation the card
 * already grants (one server call per click, with the policy's revision).
 * `OperationInvocationChoice` records the policy for an operation that is NOT
 * granted yet; the grant and the policy are then committed together by the
 * focused grant transaction. The two never render for the same operation.
 */
import type { DelegatedInvocationPolicy } from '../../api/types';
import { INVOCATION_MODE_TEXT, type InvocationMode } from './invocationChoice';

export function InvocationPolicyControl({
  operation,
  policy,
  busy,
  onceDisabled = false,
  onSet,
}: {
  operation: string;
  policy?: DelegatedInvocationPolicy;
  busy: boolean;
  onceDisabled?: boolean;
  onSet: (mode: InvocationMode, expectedRevision: number) => void;
}) {
  const mode = policy?.mode || null;
  const effectiveMode = mode || 'always';
  const onceAvailable = mode === 'once' && policy?.remaining === 1;
  return (
    <span className="operation-policy" data-operation={operation}>
      <span className="invocation-policy-control" role="group" aria-label={`Invocation policy for ${operation}`}>
        <button
          type="button"
          className={effectiveMode === 'always' ? 'active' : ''}
          aria-pressed={effectiveMode === 'always'}
          aria-label={`${operation}: always`}
          title={`Allow every ${operation} call while the card is active`}
          disabled={busy || effectiveMode === 'always'}
          onClick={() => onSet('always', policy?.revision || 0)}
        >
          Every time
        </button>
        <button
          type="button"
          className={mode === 'once' ? 'active' : ''}
          aria-pressed={mode === 'once'}
          aria-label={`${operation}: once`}
          title={onceDisabled
            ? 'Namespace and all-scope secret authority is reusable; choose Always or grant one exact key'
            : (onceAvailable ? `One ${operation} invocation remains` : `Allow the next ${operation} invocation once`)}
          disabled={busy || onceAvailable || onceDisabled}
          onClick={() => onSet('once', policy?.revision || 0)}
        >
          Once
        </button>
        {mode === 'once' && policy?.remaining === 0 ? (
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
