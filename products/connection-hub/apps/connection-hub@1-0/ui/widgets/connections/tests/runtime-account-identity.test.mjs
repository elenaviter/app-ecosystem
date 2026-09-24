import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = (relativePath) =>
  readFileSync(new URL(`../${relativePath}`, import.meta.url), 'utf8');

test('Card identity shows owner and host-reported provider account separately', () => {
  const identity = source('src/features/delegatedAccess/CardRuntimeIdentity.tsx');
  assert.match(identity, />Owner</);
  assert.match(identity, />Provider account</);
  assert.match(identity, /kdcube_agent_account/);
  assert.match(identity, /kdcube_agent_provider/);
  assert.match(identity, /Reported by host · identification metadata/);

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx');
  assert.equal((panel.match(/<CardRuntimeIdentityFields item=\{item\}/g) || []).length, 2);
  assert.match(panel, /owner=\{item\.grantor_subject \|\| platformUserId\}/);
});
