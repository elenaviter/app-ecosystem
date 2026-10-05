import type { DelegatedAccessResourceOption, ProjectPersonControlTargetRole } from '../../api/types';
import { InfoMark } from '../../components/InfoMark';
import { renderOperationGroups } from './OperationGroups';
import { roleDecidedHeld, roleDecidedResources } from './personCardOperations';

/**
 * The operations a person's Control Card holds by project role (W560).
 *
 * Operator, 2026-10-05: they "still must be shown on the card but made
 * non-editable. simply seletced and non-editable". Ticked when the holder
 * administers the project, unticked otherwise, disabled always; with no role
 * in the project's answer they are unticked and say so. Nothing here is part
 * of the edit, so a save never adds or removes one.
 */
export function RoleDecidedOperations({
  catalog,
  catalogAvailable,
  targetRole,
}: {
  catalog: DelegatedAccessResourceOption[];
  catalogAvailable: boolean;
  targetRole: ProjectPersonControlTargetRole | undefined;
}) {
  if (!catalogAvailable) {
    return (
      <section className="role-decided-operations" aria-label="Decided by project role">
        <strong>Decided by project role</strong>
        <small role="status">The project operation catalog could not be read. No role-decided operation is assumed.</small>
      </section>
    );
  }
  const rows = roleDecidedResources(catalog);
  if (!rows.length) return null;
  const held = roleDecidedHeld(targetRole);
  const roleText = held === null
    ? 'This person’s role is not known, so these are shown unticked.'
    : held
      ? `Held: this person is a project ${targetRole?.role || 'admin'}.`
      : `Not held: this person is a project ${targetRole?.role || 'member'}; a project admin holds them.`;
  return (
    <section className="role-decided-operations" aria-label="Decided by project role">
      <div className="role-decided-operations__head">
        <strong>Decided by project role</strong>
        <InfoMark text="The project decides these for a person by their role, not by this Card. A project admin or owner holds all of them; a member holds none. They cannot be ticked or unticked here; changing a person's role changes them." />
      </div>
      <small className="role-decided-operations__state" role="status">{roleText}</small>
      {rows.map((row) => (
        <div className="edit-section__body resource-grants resource-operations" key={`role-decided:${row.resource}`}>
          {rows.length > 1 ? <h5 className="operation-group__label">{row.label}</h5> : null}
          {renderOperationGroups(
            row.operations,
            (operation) => operation.group,
            catalog.find((option) => option.resource === row.resource)?.operation_groups,
            (operation) => (
              <div className="outer-operation-editor" key={`${row.resource}:role-decided:${operation.name}`}>
                <span className="tool-choice">
                  <label className="grant-chip" title={`${operation.name}: decided by project role`}>
                    <input type="checkbox" checked={held === true} disabled readOnly />
                    <span className="operation-name">
                      <span>{operation.label || operation.name}</span>
                      {operation.label && operation.label !== operation.name ? (
                        <code className="operation-id">{operation.name}</code>
                      ) : null}
                      <span className="badge">by role</span>
                    </span>
                  </label>
                  {operation.description ? <InfoMark text={operation.description} /> : null}
                </span>
              </div>
            ),
          )}
        </div>
      ))}
    </section>
  );
}
