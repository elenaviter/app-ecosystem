/**
 * Readable Card titles and owners (W304 finding 22).
 *
 * A Problem Board worker Card registered before the client led with the alias
 * is titled "Connection Hub CLI · Problem Board worker · codex:alias:session".
 * A Card's title is fixed at registration, so the view rewrites it to lead
 * with who the Card is for. And a Card's owner is shown as a person, never as
 * a raw subject id; the id stays one hover away, labelled.
 */

const LEGACY_WORKER_TITLE = /^Connection Hub CLI · Problem Board (worker|coordinator) · (.+)$/;

export function readableCardLabel(label: string | undefined): string {
  const value = String(label || '');
  const match = LEGACY_WORKER_TITLE.exec(value);
  if (!match) return value;
  const [, role, identity] = match;
  const parts = identity.split(':');
  if (parts.length >= 3) {
    const [runtime, alias, ...session] = parts;
    return `${alias} · Problem Board ${role} · ${runtime}:${session.join(':')}`;
  }
  return `Problem Board ${role} · ${identity}`;
}

export function cardOwnerView(
  grantorSubject: string | undefined,
  viewerSubject: string | undefined,
): { owner: string; ownerTitle: string } {
  const subject = String(grantorSubject || viewerSubject || '');
  if (!subject) return { owner: '', ownerTitle: '' };
  const owner = viewerSubject && subject === viewerSubject ? 'You' : 'Another person';
  return { owner, ownerTitle: `Owner ID: ${subject}` };
}
