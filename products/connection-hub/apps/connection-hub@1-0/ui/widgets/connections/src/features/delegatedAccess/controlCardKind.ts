/**
 * What a credentialless Card is to the person reading it.
 *
 * An application materializes two kinds of credentialless Card. A Control Card
 * governs the Cards linked to it (a project's agents, say). An operator Card is
 * a person's own Card for operating that application (issuer kind
 * `operator`), for example what a person may decide on one project. Both are
 * stored as `source: control`, and labelling both "control card" told a person
 * looking at their own Card that it governed someone else (operator,
 * 2026-09-24).
 */

export const OPERATOR_ISSUER_KIND = 'operator';

interface CardKindFields {
  source?: string;
  issuer_kind?: string;
}

export function isOperatorCard(item: CardKindFields): boolean {
  return item.source === 'control' && item.issuer_kind === OPERATOR_ISSUER_KIND;
}

/** The badge a credentialless Card carries. */
export function controlCardLabel(item: CardKindFields): string {
  return isOperatorCard(item) ? 'operator card' : 'control card';
}

/** The Card as the subject of a sentence. */
export function controlCardNoun(item: CardKindFields): string {
  return isOperatorCard(item) ? 'this operator card' : 'this control card';
}
