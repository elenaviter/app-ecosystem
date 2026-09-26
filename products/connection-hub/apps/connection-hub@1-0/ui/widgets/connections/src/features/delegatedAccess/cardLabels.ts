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

const PERSON_SUBJECT_PREFIXES = ['cognito:', 'user:'];

function bareSubject(value: string | undefined): string {
  const text = String(value || '').trim();
  const prefix = PERSON_SUBJECT_PREFIXES.find((candidate) => text.startsWith(candidate));
  return prefix ? text.slice(prefix.length) : text;
}

export interface ControlIssuerFields {
  issuer_label?: string;
  issuer_ref?: string;
  issuer_kind?: string;
  control_id?: string;
}

export interface IssuerViewer {
  /** The signed-in viewer's own subject. */
  viewerSubject?: string;
  /** The person a Team > People link opened this Card for, and their name. */
  targetSubject?: string;
  targetLabel?: string;
}

/** Whether a Control Card's issuer is a person (their Control Card for a project). */
export function isPersonIssuer(fields: ControlIssuerFields): boolean {
  const ref = String(fields.issuer_ref || '');
  return fields.issuer_kind === 'operator' || PERSON_SUBJECT_PREFIXES.some((prefix) => ref.startsWith(prefix));
}

/**
 * What to call a Control Card by its issuer, never a person's raw id.
 *
 * A person's Control Card showed "Card composed with cognito:<id> (AND)" and a
 * button "Open cognito:<id>" (operator, 2026-09-26). A person is named: "your
 * Control Card" for the viewer's own, "<name>'s Control Card" when the board's
 * link named them, and "another person's Control Card" when this viewer is not
 * told the name (the same rule as the board, #188 and #191).
 */
export function controlIssuerLabel(fields: ControlIssuerFields, who: IssuerViewer = {}): string {
  const label = String(fields.issuer_label || '').trim();
  if (label) return label;
  if (isPersonIssuer(fields)) {
    const issuer = bareSubject(fields.issuer_ref);
    if (issuer && issuer === bareSubject(who.viewerSubject)) return 'your Control Card';
    const name = String(who.targetLabel || '').trim();
    if (issuer && name && issuer === bareSubject(who.targetSubject)) return `${name}'s Control Card`;
    return "another person's Control Card";
  }
  return String(fields.issuer_ref || fields.control_id || '').trim() || 'Control Card';
}
