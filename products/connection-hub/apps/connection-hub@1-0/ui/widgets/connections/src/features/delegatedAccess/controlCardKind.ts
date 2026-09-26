/**
 * What a credentialless Card is called.
 *
 * Every credentialless Card is a Control Card: an application issues it and
 * decides what it holds. A project's Control Card caps its agents, and a
 * person's Control Card for one project (issuer kind `operator`) caps what that
 * person may do there. The operator named them so on 2026-09-24 ("control card
 * per person are per project, this is similar to project card"), and a second
 * name read as a third kind of Card (W300).
 */

interface CardKindFields {
  source?: string;
  issuer_kind?: string;
}

/** The badge a credentialless Card carries, whatever issued it. */
export function controlCardLabel(_item: CardKindFields): string {
  return 'control card';
}

/** The Card as the subject of a sentence. */
export function controlCardNoun(_item: CardKindFields): string {
  return 'this control card';
}
