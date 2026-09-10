/**
 * Folding rule for chip rows. A card can carry dozens of permission tokens;
 * the reader needs the shape at a glance (a few tokens and how many more)
 * and the exact list on demand. The rule never hides fewer than three: a
 * "+2 more" behind three chips costs a click and saves nothing.
 */
export const CHIP_FOLD_LIMIT = 3;
const MIN_HIDDEN = 3;

export interface FoldedEntries<T> {
  shown: T[];
  hidden: number;
  foldable: boolean;
}

export function foldEntries<T>(entries: T[], limit = CHIP_FOLD_LIMIT, open = false): FoldedEntries<T> {
  const total = entries.length;
  const foldable = total - limit >= MIN_HIDDEN;
  if (!foldable || open) return { shown: entries, hidden: 0, foldable };
  return { shown: entries.slice(0, limit), hidden: total - limit, foldable };
}
