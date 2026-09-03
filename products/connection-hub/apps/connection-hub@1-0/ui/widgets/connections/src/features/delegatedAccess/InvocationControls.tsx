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
  onSet,
}: {
  operation: string;
  policy?: DelegatedInvocationPolicy;
  busy: boolean;
  onSet: (mode: InvocationMode, expectedRevision: number) => void;
}) {
  const mode = policy?.mode || 'always';
  const onceAvailable = mode === 'once' && policy?.remaining === 1;
  return (
    <span className="operation-policy" data-operation={operation}>
      <span className="operation-policy__label">
        <code>{operation}</code> runs
      </span>
      <span className="invocation-policy-control" role="group" aria-label={`Invocation policy for ${operation}`}>
        <button
          type="button"
          className={mode === 'always' ? 'active' : ''}
          aria-pressed={mode === 'always'}
          aria-label={`${operation}: always`}
          title={`Allow every ${operation} invocation while the card remains active`}
          disabled={busy || mode === 'always'}
          onClick={() => onSet('always', policy?.revision || 0)}
        >
          Always
        </button>
        <button
          type="button"
          className={mode === 'once' ? 'active' : ''}
          aria-pressed={mode === 'once'}
          aria-label={`${operation}: once`}
          title={onceAvailable ? `One ${operation} invocation remains` : `Allow the next ${operation} invocation once`}
          disabled={busy || onceAvailable}
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
  onChoose,
}: {
  operation: string;
  mode: InvocationMode | null;
  busy: boolean;
  onChoose: (mode: InvocationMode) => void;
}) {
  return (
    <span className="operation-policy operation-policy--choice" data-operation={operation}>
      <span className="operation-policy__label">
        How may <code>{operation}</code> run?
      </span>
      <span className="invocation-policy-control" role="group" aria-label={`Invocation policy for ${operation}`}>
        <button
          type="button"
          className={mode === 'once' ? 'active' : ''}
          aria-pressed={mode === 'once'}
          aria-label={`${operation}: once`}
          title={`Allow ${operation} for ${INVOCATION_MODE_TEXT.once}`}
          disabled={busy}
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
          Always
        </button>
      </span>
      <span className="operation-policy__status" aria-live="polite">
        {mode
          ? <><code>{operation}</code>: {mode} ({INVOCATION_MODE_TEXT[mode]})</>
          : 'not chosen yet'}
      </span>
    </span>
  );
}
