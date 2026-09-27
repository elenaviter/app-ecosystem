/**
 * Which actions a Card offers in the detailed view.
 *
 * A link opens a Card to read (W304 finding 21), so the Card it opened must
 * carry every action its kind has, Edit included, whatever its kind. #220
 * drew Edit only for connected-app and manual Cards; Control Cards and My
 * Card then showed Revoke alone and nobody could edit them (incident,
 * 2026-09-26). Whether this viewer may edit at all is decided separately
 * (cardReadOnlyReason), and hides the button for a viewer who may only read.
 */

export interface CardActionFields {
  access_id: string;
  source?: string;
  client_id?: string | null;
}

/** Whether the detailed view draws an Edit button for this Card. */
export function detailedCardOffersEdit(
  item: CardActionFields,
  viewedAccessId: string | null,
): boolean {
  if (item.source === 'agent') return true;
  if (item.source === 'manual') return true;
  if (item.source === 'oauth' && Boolean(item.client_id)) return true;
  // The Card a link opened: a Control Card (the project's or a person's), a
  // person's own Card (My Card), or any other kind.
  return viewedAccessId !== null && item.access_id === viewedAccessId;
}
