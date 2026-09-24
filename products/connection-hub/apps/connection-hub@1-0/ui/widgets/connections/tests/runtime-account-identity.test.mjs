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
  assert.match(identity, /label: 'Owner'/);
  assert.match(identity, /label: 'Provider account'/);
  assert.match(identity, /label: 'Account ID'/);
  assert.match(identity, /label: 'Organization ID'/);
  assert.match(identity, /kdcube_agent_account/);
  assert.match(identity, /kdcube_agent_provider/);
  assert.match(identity, /Not reported/);
  assert.match(identity, /Read from the \{providerLabel\} login on \{host\}/);
  assert.match(identity, /Used to identify the agent, not to grant access/);

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx');
  assert.match(
    panel,
    /<RuntimeIdentityFields\s+clientMetadata=\{draft\.client\.client_metadata\}\s+owner=\{draft\.grantor\.label \|\| draft\.grantor\.subject\}\s+ownerTitle=\{draft\.grantor\.subject\}\s+layout="facts"\s*\/>/,
  );
  assert.equal((panel.match(/<CardRuntimeIdentityFields/g) || []).length, 2);
  assert.equal((panel.match(/owner=\{cardOwner\(item\)\.label\}/g) || []).length, 2);
  assert.equal((panel.match(/ownerTitle=\{cardOwner\(item\)\.title\}/g) || []).length, 2);
  assert.match(panel, /ownerId && ownerId === platformUserId \? 'You'/);
});
