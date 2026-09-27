/**
 * Who the page is signed in as (W304 finding 26).
 *
 * An approver saw only "your user id <uuid>" on the consent page and could not
 * tell which platform account would own the Card, nor switch to another one.
 * The label is the platform's own answer for the signed-in session: the email,
 * else the name, else the user name, the same order the consent uses for the
 * Card's Owner.
 */

export interface AccountFields {
  email?: unknown;
  name?: unknown;
  username?: unknown;
  preferred_username?: unknown;
}

const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '');

export function accountLabel(fields: AccountFields | null | undefined): string {
  if (!fields) return '';
  return text(fields.email) || text(fields.name) || text(fields.username) || text(fields.preferred_username);
}

export const SWITCH_ACCOUNT_HINT =
  'Sign out and sign in as another account; you come back to this page, and nothing is approved on the way.';
