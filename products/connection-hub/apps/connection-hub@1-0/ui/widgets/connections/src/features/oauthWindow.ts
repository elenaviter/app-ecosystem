/**
 * The provider's sign-in tab, opened inside the click (W435, operator 2026-10-05).
 *
 * Every Connect first asks Connection Hub to start the sign-in (a CSRF token,
 * then the start call) and only then knows the provider's address. Safari
 * blocks a window opened after those awaits: the click no longer counts as
 * the user's gesture, so "Connect GitHub" did nothing, silently, while the
 * server logged each start as ok. The tab is opened blank at once, in the
 * click, and pointed at the provider when the address arrives; it is closed
 * when the start fails, so no empty tab is left behind.
 */
export interface PendingAuthorizationWindow {
  /** Send the opened tab to the provider; true when a tab shows it. */
  go(url: string): boolean;
  /** Close the blank tab when there is nowhere to send it. */
  cancel(): void;
}

export function openPendingAuthorizationWindow(): PendingAuthorizationWindow {
  const pending = window.open('about:blank', '_blank');
  // The provider page must not reach back into Connection Hub.
  if (pending) pending.opener = null;
  return {
    go(url: string): boolean {
      if (pending && !pending.closed) {
        pending.location.replace(url);
        return true;
      }
      // The blank tab was blocked or closed: one direct attempt, which
      // browsers that allow it still honour.
      return Boolean(window.open(url, '_blank', 'noopener,noreferrer'));
    },
    cancel(): void {
      if (pending && !pending.closed) pending.close();
    },
  };
}
