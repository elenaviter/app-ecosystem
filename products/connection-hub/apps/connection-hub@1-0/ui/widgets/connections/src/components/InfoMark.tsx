/**
 * A small information mark that explains the control beside it in one
 * sentence. It opens on tap, click, Enter or Space, and closes on a second
 * activation, Escape, or a click elsewhere; hover shows the same text as a
 * native tooltip on a pointer device. Help lives here, never as a paragraph
 * above a form: the form stays bare and the explanation is one gesture away.
 */
import { useEffect, useId, useRef, useState } from 'react';

export function InfoMark({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const ref = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (event: MouseEvent | TouchEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('touchstart', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('touchstart', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return (
    <span className="info-mark-wrap" ref={ref}>
      <button
        type="button"
        className={`info-mark${open ? ' info-mark--open' : ''}`}
        aria-label="More about this"
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        title={open ? undefined : text}
        onClick={(event) => { event.preventDefault(); event.stopPropagation(); setOpen((value) => !value); }}
      >
        i
      </button>
      {open ? (
        <span className="info-mark__bubble" role="note" id={id} onClick={(event) => event.stopPropagation()}>
          {text}
        </span>
      ) : null}
    </span>
  );
}
