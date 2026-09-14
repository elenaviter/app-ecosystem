import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';

/** Keep dialogs outside pane, sticky, and overflow stacking contexts. */
export function ModalLayer({ children }: { children: ReactNode }) {
  if (typeof document === 'undefined') return null;
  return createPortal(children, document.body);
}
