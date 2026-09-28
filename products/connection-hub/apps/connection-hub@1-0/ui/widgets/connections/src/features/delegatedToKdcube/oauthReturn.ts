// The return from a provider's approval tab (W371).
//
// The approval happens in another tab. The callback page broadcasts on the
// same-origin channel, and a returning focus refreshes once; neither reaches
// a widget embedded in the board when the callback lands on another origin
// (the public address) or the frame never gets focus. The operator's first
// GitHub link, 2026-09-28: the account was stored and the widget never read
// it. So an approval that is in flight also re-reads the accounts on a
// bounded clock until they change, then stops: nothing polls when no
// approval is pending.

export const OAUTH_PENDING_KEY = 'kdc-oauth-pending';
export const OAUTH_ARMED_EVENT = 'kdc-oauth-armed';
export const RETURN_POLL_MS = 3000;
// Three minutes: an approval screen, a sign-in and a GitHub App install fit.
export const RETURN_POLL_TRIES = 60;

/** Mark an approval in flight and tell the app to watch for its return. */
export function armOAuthReturn(): void {
  try {
    sessionStorage.setItem(OAUTH_PENDING_KEY, '1');
  } catch {
    // Storage unavailable: the event below still starts the watch.
  }
  window.dispatchEvent(new Event(OAUTH_ARMED_EVENT));
}

interface AccountLike {
  account_id: string;
  status?: string;
  credential_status?: string;
}

/** What changes when an approval lands: a new account, or a credential renewed or repaired. */
export function accountsSignature(accounts: AccountLike[] | undefined | null): string {
  return (accounts || [])
    .map((account) => `${account.account_id}:${account.status || ''}:${account.credential_status || ''}`)
    .sort()
    .join('|');
}
