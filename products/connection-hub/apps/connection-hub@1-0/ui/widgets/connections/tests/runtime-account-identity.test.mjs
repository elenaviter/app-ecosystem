import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = (relativePath) =>
  readFileSync(new URL(`../${relativePath}`, import.meta.url), 'utf8');

test('Card identity shows owner and host-reported provider account separately', () => {
  const identity = source('src/features/delegatedAccess/CardRuntimeIdentity.tsx');
  assert.match(identity, /function runtimeAccountIdentity/);
  assert.match(identity, /export function RuntimeIdentityFields/);
  assert.match(identity, /layout === 'facts'/);
  assert.match(identity, />Owner</);
  assert.match(identity, />Provider account</);
  assert.match(identity, /kdcube_agent_account/);
  assert.match(identity, /kdcube_agent_provider/);
  assert.match(identity, /Not reported/);
  assert.match(identity, /No provider account was reported by this machine/);
  assert.match(identity, /Reported by host · identification metadata/);

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx');
  assert.match(
    panel,
    /<RuntimeIdentityFields\s+clientMetadata=\{draft\.client\.client_metadata\}\s+owner=\{draft\.grantor\.label \|\| draft\.grantor\.subject\}\s+ownerTitle=\{draft\.grantor\.subject\}\s+layout="facts"\s*\/>/,
  );
  assert.equal((panel.match(/<CardRuntimeIdentityFields item=\{item\}/g) || []).length, 2);
  assert.match(panel, /owner=\{item\.grantor_subject \|\| platformUserId\}/);
});
