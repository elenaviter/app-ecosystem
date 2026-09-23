export const AGENT_CAPABILITY_SELECTION_PROPERTY = 'kdcube.agent_capability_selection';
export const AGENT_CAPABILITY_AUTHORITY_PROPERTY = 'kdcube.agent_capability_authority';
export const AGENT_CAPABILITY_METADATA_PROPERTY = 'kdcube.agent_capability_metadata';
export const AGENT_DESCRIPTOR_CONTROL_PROPERTY = 'kdcube.agent_descriptor_control';
export const AGENT_CAPABILITY_POLICY_SCHEMA = 'connection_hub.agent_capability_policy.v1';
export const AGENT_CAPABILITY_METADATA_SCHEMA = 'connection_hub.agent_capability_metadata.v1';
export const AGENT_DESCRIPTOR_CONTROL_SCHEMA = 'connection_hub.agent_descriptor_control.v1';

const CATEGORY_LABELS: Record<string, string> = {
  tool_groups: 'Tool groups',
  tools: 'Tools',
  mcp_servers: 'MCP servers',
  mcp_tools: 'MCP tools',
  named_services: 'Named services',
  named_service_operations: 'Service operations',
  resources: 'Resources',
  resource_operations: 'Resource operations',
  skills: 'Skills',
  models: 'Models',
  instruction_profiles: 'Instruction profiles',
  conversation_targets: 'Conversation targets',
  resource_families: 'Resource families',
  subagents: 'Helper agents',
};

const CATEGORY_ORDER = Object.keys(CATEGORY_LABELS);

export interface AgentCapabilitySelectionGroup {
  category: string;
  label: string;
  values: string[];
}

export interface AgentCapabilitySelection {
  resource: string;
  groups: AgentCapabilitySelectionGroup[];
  selectedCount: number;
}

export interface AgentCapabilityMetadataEntry {
  title?: string;
  description?: string;
}

export type AgentCapabilityMetadata = Record<string, Record<string, AgentCapabilityMetadataEntry>>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function agentCapabilityCategoryLabel(category: string): string {
  return CATEGORY_LABELS[category]
    || category.replace(/_/g, ' ').replace(/^./, (value: string) => value.toUpperCase());
}

function cardAgentCapabilityPolicy(
  properties: Record<string, unknown> | undefined,
  property: string,
): AgentCapabilitySelection | null {
  const raw = properties?.[property];
  if (!isRecord(raw) || raw.schema !== AGENT_CAPABILITY_POLICY_SCHEMA) return null;
  const resource = typeof raw.resource === 'string' ? raw.resource.trim() : '';
  if (!resource || !isRecord(raw.capabilities)) return null;

  const groups: AgentCapabilitySelectionGroup[] = [];
  for (const [category, rawValues] of Object.entries(raw.capabilities)) {
    if (!Array.isArray(rawValues) || rawValues.some((value) => (
      typeof value !== 'string' || !value.trim() || value !== value.trim()
    ))) return null;
    const values = Array.from(new Set(rawValues as string[])).sort();
    if (values.length) {
      groups.push({
        category,
        label: agentCapabilityCategoryLabel(category),
        values,
      });
    }
  }
  groups.sort((left, right) => {
    const leftIndex = CATEGORY_ORDER.indexOf(left.category);
    const rightIndex = CATEGORY_ORDER.indexOf(right.category);
    if (leftIndex >= 0 || rightIndex >= 0) {
      if (leftIndex < 0) return 1;
      if (rightIndex < 0) return -1;
      if (leftIndex !== rightIndex) return leftIndex - rightIndex;
    }
    return left.label.localeCompare(right.label);
  });
  return {
    resource,
    groups,
    selectedCount: groups.reduce((total, group) => total + group.values.length, 0),
  };
}

/** Parse only the strict positive policy shape used as an Agent Card base. */
export function cardAgentCapabilitySelection(
  properties: Record<string, unknown> | undefined,
): AgentCapabilitySelection | null {
  return cardAgentCapabilityPolicy(properties, AGENT_CAPABILITY_SELECTION_PROPERTY);
}

/** Parse the descriptor-owned ceiling carried by its linked Control Card. */
export function cardAgentCapabilityAuthority(
  properties: Record<string, unknown> | undefined,
): AgentCapabilitySelection | null {
  return cardAgentCapabilityPolicy(properties, AGENT_CAPABILITY_AUTHORITY_PROPERTY);
}

export function cardAgentCapabilityMetadata(
  properties: Record<string, unknown> | undefined,
): AgentCapabilityMetadata {
  const raw = properties?.[AGENT_CAPABILITY_METADATA_PROPERTY];
  if (!isRecord(raw) || raw.schema !== AGENT_CAPABILITY_METADATA_SCHEMA || !isRecord(raw.entries)) {
    return {};
  }
  const entries: AgentCapabilityMetadata = {};
  for (const [category, rawCategory] of Object.entries(raw.entries)) {
    if (!isRecord(rawCategory)) continue;
    const categoryEntries: Record<string, AgentCapabilityMetadataEntry> = {};
    for (const [capability, rawEntry] of Object.entries(rawCategory)) {
      if (!isRecord(rawEntry)) continue;
      const title = typeof rawEntry.title === 'string' ? rawEntry.title.trim() : '';
      const description = typeof rawEntry.description === 'string' ? rawEntry.description.trim() : '';
      categoryEntries[capability] = {
        ...(title ? { title } : {}),
        ...(description ? { description } : {}),
      };
    }
    if (Object.keys(categoryEntries).length) entries[category] = categoryEntries;
  }
  return entries;
}

export function isAgentDescriptorControl(
  properties: Record<string, unknown> | undefined,
): boolean {
  const raw = properties?.[AGENT_DESCRIPTOR_CONTROL_PROPERTY];
  return isRecord(raw)
    && raw.schema === AGENT_DESCRIPTOR_CONTROL_SCHEMA
    && typeof raw.resource === 'string'
    && Boolean(raw.resource.trim());
}

export function agentCapabilitySelectionMap(
  selection: AgentCapabilitySelection | null,
): Record<string, string[]> {
  return Object.fromEntries(
    (selection?.groups || []).map((group) => [group.category, [...group.values]]),
  );
}

export function agentCapabilitySelectionProperty(
  resource: string,
  capabilities: Record<string, string[]>,
): Record<string, unknown> {
  return {
    schema: AGENT_CAPABILITY_POLICY_SCHEMA,
    resource,
    capabilities: Object.fromEntries(
      Object.entries(capabilities)
        .map(([category, values]) => [category, Array.from(new Set(values)).sort()]),
    ),
  };
}

export function agentCapabilitySelectionFromMap(
  resource: string,
  capabilities: Record<string, string[]>,
): AgentCapabilitySelection | null {
  return cardAgentCapabilitySelection({
    [AGENT_CAPABILITY_SELECTION_PROPERTY]: agentCapabilitySelectionProperty(
      resource,
      capabilities,
    ),
  });
}
