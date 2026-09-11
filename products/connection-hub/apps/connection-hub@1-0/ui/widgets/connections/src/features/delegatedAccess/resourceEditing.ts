/**
 * Rules of the compatible-resource editor, kept pure so a test can pin them.
 *
 * A card holds several resources under one stable identity. Editing may add
 * an owner-visible, identity-compatible resource, remove one resource while
 * the others stay untouched, and accept a changed descriptor for exactly the
 * selected operations the grantor reviewed. The save is refused, with the
 * reason, when it would leave the card empty (that is a revoke, decided
 * explicitly), grant an added resource nothing, or grant a new operation
 * without an invocation choice.
 */

export type ResourceOfferReason =
  | 'compatible'
  | 'already_on_card'
  | 'identity_scope_incompatible'
  | 'admin_only'
  | 'outside_client_door'
  | string;

export interface ResourceOffer {
  resource: string;
  label: string;
  identity_scope: string;
  compatible: boolean;
  reason: ResourceOfferReason;
  card_identity_scope?: string;
  /** On an OAuth card's offers: the door the client connected to. */
  client_door?: string;
}

export interface ResourceSelectionOption {
  resource: string;
  selectable_resources?: string[];
}

export interface ResourceSelectionIndex {
  childrenByParent: Record<string, string[]>;
  parentsByChild: Record<string, string[]>;
}

/** The catalog's transport-door hierarchy. A selection door remains one card
 * resource; the exact child resources retain their own grants and operations.
 * This index only arranges those existing rows for the editor. */
export function resourceSelectionIndex(
  options: ResourceSelectionOption[],
): ResourceSelectionIndex {
  const childrenByParent: Record<string, string[]> = {};
  const parentsByChild: Record<string, string[]> = {};
  const known = new Set(options.map((option) => option.resource));
  options.forEach((option) => {
    const children = Array.from(new Set(
      (option.selectable_resources || []).filter(
        (resource) => resource && resource !== option.resource && known.has(resource),
      ),
    ));
    if (!children.length) return;
    childrenByParent[option.resource] = children;
    children.forEach((resource) => {
      parentsByChild[resource] = Array.from(new Set([
        ...(parentsByChild[resource] || []),
        option.resource,
      ]));
    });
  });
  return { childrenByParent, parentsByChild };
}

/** Roots first, followed immediately by the resources each selection door
 * serves. A row reachable through several doors appears once, under the first
 * door in catalog order. */
export function orderResourceSelection<T extends ResourceSelectionOption>(
  options: T[],
  index: ResourceSelectionIndex = resourceSelectionIndex(options),
): T[] {
  const byResource = new Map(options.map((option) => [option.resource, option]));
  const children = new Set(Object.keys(index.parentsByChild));
  const emitted = new Set<string>();
  const out: T[] = [];
  const emit = (resource: string) => {
    if (emitted.has(resource)) return;
    const option = byResource.get(resource);
    if (!option) return;
    emitted.add(resource);
    out.push(option);
  };
  options.forEach((option) => {
    if (children.has(option.resource)) return;
    emit(option.resource);
    (index.childrenByParent[option.resource] || []).forEach(emit);
  });
  options.forEach((option) => emit(option.resource));
  return out;
}

/** The short name of a door: the last path segment of an MCP surface
 *  (".../mcp/remote_mcp_proxy" -> "remote_mcp_proxy"), else the resource. */
export function doorName(resource: string): string {
  const path = String(resource || '').replace(/[?#].*$/, '').replace(/\*+$/, '').replace(/\/+$/, '');
  const match = path.match(/\/mcp\/([^/]+)$/);
  return match ? match[1] : path;
}

/** What the picker shows of a card's offers. An OAuth client reaches one
 *  door, so the resources outside it are not listed at all, not even as
 *  blocked: the client will never call them, and a grantor reading a list of
 *  doors would grant authority nothing can use. The door is named once. */
export function pickerOffers(
  offers: ResourceOffer[],
  added: string[],
): { compatible: ResourceOffer[]; blocked: ResourceOffer[]; clientDoor: string } {
  const candidates = offers.filter((offer) => offer.reason !== 'already_on_card' && !added.includes(offer.resource));
  const clientDoor = offers.find((offer) => offer.client_door)?.client_door || '';
  return {
    compatible: candidates.filter((offer) => offer.compatible),
    blocked: candidates.filter((offer) => !offer.compatible && offer.reason !== 'outside_client_door'),
    clientDoor,
  };
}

export interface ResourceDriftState {
  status: 'current' | 'changed' | 'removed' | 'unknown' | string;
  kind?: string;
  accepted_revision?: string;
  current_revision?: string;
  accepted_digest?: string;
  current_digest?: string;
  changed_operations?: string[];
  removed_operations?: string[];
  added_operations?: string[];
  removed_claims?: string[];
  added_claims?: string[];
}

/** The resources an edit will submit: the card's own minus the ones marked for
 *  removal, plus the ones being added, in that order. */
export function editedResourceKeys(
  cardResources: string[],
  added: string[],
  removed: Iterable<string>,
): string[] {
  const gone = new Set(removed);
  const out: string[] = [];
  cardResources.forEach((resource) => {
    if (!gone.has(resource) && !out.includes(resource)) out.push(resource);
  });
  added.forEach((resource) => {
    if (!gone.has(resource) && !out.includes(resource)) out.push(resource);
  });
  return out;
}

/** Why the offer picker shows a resource as unavailable, in the grantor's
 *  words. Empty for a compatible offer. */
export function offerReasonText(offer: ResourceOffer): string {
  switch (offer.reason) {
    case 'compatible':
      return '';
    case 'already_on_card':
      return 'Already on this card.';
    case 'identity_scope_incompatible':
      return `Runs under ${offer.identity_scope}; this card acts as ${offer.card_identity_scope || 'grantor'}, so it cannot be added.`;
    case 'admin_only':
      return 'Only a platform administrator may delegate it.';
    case 'outside_client_door':
      return `Not reachable through ${doorName(offer.client_door || '')}, the service endpoint this client uses.`;
    default:
      return offer.reason.replace(/_/g, ' ');
  }
}

export interface SaveProblem {
  code: 'no_resources_left' | 'added_resource_without_claims' | 'operation_without_choice';
  resource?: string;
  operations?: string[];
}

/** Everything that blocks Save, so the button can say why. */
export function saveProblems(input: {
  resourceKeys: string[];
  addedResources: string[];
  claimsFor: (resource: string) => string[];
  missingChoices: Array<{ resource: string; operation: string }>;
}): SaveProblem[] {
  const problems: SaveProblem[] = [];
  const withClaims = input.resourceKeys.filter((resource) => input.claimsFor(resource).length > 0);
  if (!withClaims.length) {
    problems.push({ code: 'no_resources_left' });
  }
  input.addedResources
    .filter((resource) => input.resourceKeys.includes(resource) && !input.claimsFor(resource).length)
    .forEach((resource) => problems.push({ code: 'added_resource_without_claims', resource }));
  const byResource = new Map<string, string[]>();
  input.missingChoices.forEach(({ resource, operation }) => {
    byResource.set(resource, [...(byResource.get(resource) || []), operation]);
  });
  byResource.forEach((operations, resource) => {
    problems.push({ code: 'operation_without_choice', resource, operations });
  });
  return problems;
}

export function saveProblemText(problem: SaveProblem, labelFor: (resource: string) => string): string {
  switch (problem.code) {
    case 'no_resources_left':
      return 'Removing every resource revokes the card. Use Revoke instead.';
    case 'added_resource_without_claims':
      return `Select at least one access claim on ${labelFor(problem.resource || '')} or remove it again.`;
    case 'operation_without_choice':
      return `Choose once or always for ${(problem.operations || []).join(', ')} on ${labelFor(problem.resource || '')}.`;
    default:
      return '';
  }
}

/** Toggle one changed operation in the set the save will accept. */
export function toggleAccepted(
  accepted: Record<string, string[]>,
  resource: string,
  operation: string,
  on: boolean,
): Record<string, string[]> {
  const current = accepted[resource] || [];
  const next = on
    ? Array.from(new Set([...current, operation]))
    : current.filter((item) => item !== operation);
  const out = { ...accepted };
  if (next.length) out[resource] = next;
  else delete out[resource];
  return out;
}

/** Whether the resource's descriptor review has anything to show. */
export function driftNeedsReview(state: ResourceDriftState | undefined): boolean {
  if (!state) return false;
  return Boolean(
    (state.changed_operations && state.changed_operations.length)
    || (state.removed_operations && state.removed_operations.length)
    || (state.added_operations && state.added_operations.length)
    || (state.removed_claims && state.removed_claims.length)
    || (state.added_claims && state.added_claims.length)
    || state.status === 'removed',
  );
}
