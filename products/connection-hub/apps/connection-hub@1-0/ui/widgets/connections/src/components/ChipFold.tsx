/**
 * Read-only chip rows that fold. Three chips and "+N more" by default; the
 * rest opens in place, either as the plain list or grouped by service
 * (`expanded="groups"`), and folds back with the same control.
 */
import { useState } from 'react';
import { CHIP_FOLD_LIMIT, foldEntries } from './foldRule';
import { groupClaimsByService } from './claimGroups';

export function ClaimGroupsView({
  claims,
  title,
}: {
  claims: string[];
  title?: (entry: string) => string | undefined;
}) {
  return (
    <span className="claim-groups">
      {groupClaimsByService(claims).map((group) => (
        <span className="claim-group" key={group.service}>
          <span className="claim-group__service">{group.service}</span>
          <span className="claim-group__verbs">
            {group.claims.map(({ token, verb }) => (
              <code className="claim-chip" key={token} title={title?.(token) || token}>
                {verb || token}
              </code>
            ))}
          </span>
        </span>
      ))}
    </span>
  );
}

export function FoldedChipRow({
  entries,
  limit = CHIP_FOLD_LIMIT,
  title,
  chipClass = 'claim-chip',
  expanded = 'chips',
}: {
  entries: string[];
  limit?: number;
  title?: (entry: string) => string | undefined;
  chipClass?: string;
  expanded?: 'chips' | 'groups';
}) {
  const [open, setOpen] = useState(false);
  const fold = foldEntries(entries, limit, open);
  if (!entries.length) return null;
  const toggle = fold.foldable ? (
    <button
      type="button"
      className="chip-more"
      aria-expanded={open}
      onClick={() => setOpen((value) => !value)}
    >
      {open ? 'show less' : `+${fold.hidden} more`}
    </button>
  ) : null;
  if (open && expanded === 'groups') {
    return (
      <span className="chip-row chip-row--open">
        <ClaimGroupsView claims={entries} title={title} />
        {toggle}
      </span>
    );
  }
  return (
    <span className="chip-row">
      {fold.shown.map((entry) => (
        <code className={chipClass} key={entry} title={title?.(entry)}>{entry}</code>
      ))}
      {toggle}
    </span>
  );
}
