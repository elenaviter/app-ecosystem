import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { accountLabel } from '../src/components/signedInAccount.ts';

const source = (relativePath) =>
  readFileSync(new URL(`../${relativePath}`, import.meta.url), 'utf8');

test('W304 finding 26: the account label is the email, else the name, else the user name', () => {
  assert.equal(accountLabel({ email: 'ana@example.com', name: 'Ana', username: 'ana' }), 'ana@example.com');
  assert.equal(accountLabel({ email: '', name: 'Ana', username: 'ana' }), 'Ana');
  assert.equal(accountLabel({ username: 'ana' }), 'ana');
  assert.equal(accountLabel({ preferred_username: 'ana-oidc' }), 'ana-oidc');
  assert.equal(accountLabel(null), '');
  assert.equal(accountLabel({ email: 42 }), '');
});

test('the page head names the signed-in account and offers the switch before anything is approved', () => {
  const shell = source('src/components/AppShell.tsx');
  assert.match(shell, /signed in as<\/span>/);
  assert.match(shell, /\{signedInAs\}/);
  assert.match(shell, /Not you\? Switch account/);
  // Shown before the user id, in the same head.
  assert.ok(shell.indexOf('signed in as</span>') < shell.indexOf('your user id</span>'));

  const auth = source('src/api/platformAuth.ts');
  // The platform's own profile answers who is signed in; no token is read here.
  assert.match(auth, /client\.probe\(\)/);
  // The switch signs out through the server with this page as `next`, then signs in back to it.
  assert.match(auth, /client\.signOut\(\{ next: returnTo, followUpstream: true \}\)/);
  assert.match(auth, /startPlatformSignIn\(returnTo\)/);

  const app = source('src/App.tsx');
  assert.match(app, /switchPlatformAccount\(window\.location\.href\)/);
  // A profile of another user than the one the hub answers for is never shown.
  assert.match(app, /account\.userId === platformUserId/);
});
