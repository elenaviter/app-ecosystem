import {
  applicationOperationRoleFor,
  applicationOperationRoleIsElevated,
  availableApplicationRoles,
  platformRoleLabel,
  type ApplicationOperationRolePolicy,
} from './applicationOperationRoles';

function selectableRoles(availableRoles: string[], currentRole: string): string[] {
  const available = availableApplicationRoles(availableRoles);
  const current = currentRole.trim();
  return current && !available.includes(current) ? [...available, current] : available;
}

export function ApplicationDefaultRoleControl({
  availableRoles,
  policy,
  disabled,
  onChange,
}: {
  availableRoles: string[];
  policy: ApplicationOperationRolePolicy;
  disabled: boolean;
  onChange: (role: string) => void;
}) {
  const currentAvailable = !policy.defaultRole || availableRoles.includes(policy.defaultRole);
  return (
    <div className="application-role-default">
      <label>
        <span>Default role</span>
        <select
          aria-label="Default application API role"
          value={policy.defaultRole}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">Choose a role</option>
          {selectableRoles(availableRoles, policy.defaultRole).map((role) => (
            <option key={role} value={role}>
              {platformRoleLabel(role)}{availableRoles.includes(role) ? '' : ' (unavailable)'}
            </option>
          ))}
        </select>
      </label>
      <small>Used by each selected operation unless that operation has an override.</small>
      {!currentAvailable ? (
        <small className="application-api-warning" role="alert">
          This role is no longer available from the current Card catalog. Choose another role before saving.
        </small>
      ) : null}
    </div>
  );
}

export function ApplicationOperationRoleControl({
  operationRef,
  availableRoles,
  policy,
  disabled,
  onChange,
}: {
  operationRef: string;
  availableRoles: string[];
  policy: ApplicationOperationRolePolicy;
  disabled: boolean;
  onChange: (role: string) => void;
}) {
  const role = applicationOperationRoleFor(policy, operationRef);
  const override = policy.operationRoles[operationRef] || '';
  const overrideAvailable = !override || availableRoles.includes(override);
  const elevated = applicationOperationRoleIsElevated(policy, operationRef);
  return (
    <div className="application-operation-role">
      <label>
        <span>Invocation role</span>
        <select
          aria-label={`Invocation role for ${operationRef}`}
          value={override}
          disabled={disabled || !policy.defaultRole}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">Default: {platformRoleLabel(policy.defaultRole)}</option>
          {selectableRoles(availableRoles, override).map((candidate) => (
            <option key={candidate} value={candidate}>
              {platformRoleLabel(candidate)}{availableRoles.includes(candidate) ? '' : ' (unavailable)'}
            </option>
          ))}
        </select>
      </label>
      <span className={`badge ${elevated ? 'badge-admin' : 'badge-neutral'}`}>
        {elevated ? 'Elevated' : override ? 'Override' : 'Default'}: {platformRoleLabel(role)}
      </span>
      {!overrideAvailable ? (
        <small className="application-api-warning" role="alert">
          This override is no longer available from the current Card catalog.
        </small>
      ) : null}
    </div>
  );
}

export function ApplicationEffectiveRole({
  operationRef,
  policy,
}: {
  operationRef: string;
  policy: ApplicationOperationRolePolicy;
}) {
  const role = applicationOperationRoleFor(policy, operationRef);
  const elevated = applicationOperationRoleIsElevated(policy, operationRef);
  return (
    <span className={`badge ${elevated ? 'badge-admin' : 'badge-ok'}`}>
      Effective: {platformRoleLabel(role)}{elevated ? ' elevated' : ''}
    </span>
  );
}
