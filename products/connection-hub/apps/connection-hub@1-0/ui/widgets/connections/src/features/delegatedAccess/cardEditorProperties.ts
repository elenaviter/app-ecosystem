import { changedCardProperties } from './applicationOperationRoles.ts';
import {
  AGENT_CAPABILITY_DEFAULTS_PROPERTY,
  AGENT_CAPABILITY_SELECTION_PROPERTY,
  agentCapabilitySelectionProperty,
  type AgentCapabilitySelection,
} from './agentCapabilitySelection.ts';

/** Send property choices only when they differ from the same editor's seed.
 * Both sides include the derived capability projection, so rebuilding a stored
 * policy while changing permissions does not become an unrelated property edit.
 */
export function changedEditorCardProperties({
  selected, seeded, selectedCapabilities, seededCapabilities,
  capabilityAuthority, descriptorAuthority,
}: {
  selected: Record<string, unknown>;
  seeded: Record<string, unknown>;
  selectedCapabilities: Record<string, string[]>;
  seededCapabilities: Record<string, string[]>;
  capabilityAuthority: AgentCapabilitySelection | null;
  descriptorAuthority: AgentCapabilitySelection | null;
}): Record<string, unknown> | undefined {
  const authority = capabilityAuthority || descriptorAuthority;
  const property = capabilityAuthority
    ? AGENT_CAPABILITY_SELECTION_PROPERTY
    : AGENT_CAPABILITY_DEFAULTS_PROPERTY;
  const project = (properties: Record<string, unknown>, capabilities: Record<string, string[]>) => (
    authority
      ? { ...properties, [property]: agentCapabilitySelectionProperty(authority.resource, capabilities) }
      : properties
  );
  return changedCardProperties(
    project(selected, selectedCapabilities),
    project(seeded, seededCapabilities),
  );
}
