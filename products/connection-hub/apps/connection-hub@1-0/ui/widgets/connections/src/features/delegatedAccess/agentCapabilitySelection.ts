export const AGENT_CAPABILITY_SELECTION_PROPERTY = 'kdcube.agent_capability_selection';
export const AGENT_CAPABILITY_POLICY_SCHEMA = 'connection_hub.agent_capability_policy.v1';

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function agentCapabilityCategoryLabel(category: string): string {
  return CATEGORY_LABELS[category]
    || category.replace(/_/g, ' ').replace(/^./, (value: string) => value.toUpperCase());
}

/** Parse only the strict positive policy shape used as an Agent Card base. */
export function cardAgentCapabilitySelection(
  properties: Record<string, unknown> | undefined,
): AgentCapabilitySelection | null {
  const raw = properties?.[AGENT_CAPABILITY_SELECTION_PROPERTY];
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
