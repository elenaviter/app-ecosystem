import type {
  AgentCapabilityMetadata,
  AgentCapabilitySelection,
} from './agentCapabilitySelection';

const PARENT_CATEGORY: Record<string, string> = {
  tools: 'tool_groups',
  mcp_tools: 'mcp_servers',
  named_service_operations: 'named_services',
  resource_operations: 'resources',
};

const CHILD_CATEGORY: Record<string, string> = Object.fromEntries(
  Object.entries(PARENT_CATEGORY).map(([child, parent]) => [parent, child]),
);

function memberParent(value: string): string {
  const separator = value.indexOf('/');
  if (separator < 0) return '';
  try {
    return decodeURIComponent(value.slice(0, separator));
  } catch {
    return value.slice(0, separator);
  }
}

function fallbackLabel(value: string): string {
  const leaf = value.includes('/') ? value.slice(value.indexOf('/') + 1) : value;
  let decoded = leaf;
  try {
    decoded = decodeURIComponent(leaf);
  } catch {
    decoded = leaf;
  }
  return decoded.replace(/[_:.]+/g, ' ').replace(/\s+/g, ' ').trim() || value;
}

function metadataEntry(
  metadata: AgentCapabilityMetadata,
  category: string,
  capability: string,
) {
  return metadata[category]?.[capability];
}

function policyMap(policy: AgentCapabilitySelection | null): Record<string, string[]> {
  return Object.fromEntries(
    (policy?.groups || []).map((group) => [group.category, [...group.values]]),
  );
}

function withToggle(
  authority: AgentCapabilitySelection,
  selection: AgentCapabilitySelection | null,
  category: string,
  capability: string,
  checked: boolean,
): Record<string, string[]> {
  const next = Object.fromEntries(
    Object.entries(policyMap(selection)).map(([key, values]) => [key, new Set(values)]),
  ) as Record<string, Set<string>>;
  const held = next[category] || new Set<string>();
  next[category] = held;
  if (checked) held.add(capability); else held.delete(capability);

  const parentCategory = PARENT_CATEGORY[category];
  const parent = parentCategory ? memberParent(capability) : '';
  if (checked && parentCategory && parent) {
    (next[parentCategory] ||= new Set<string>()).add(parent);
  }

  const childCategory = CHILD_CATEGORY[category];
  if (childCategory) {
    const children = authority.groups.find((group) => group.category === childCategory)?.values || [];
    const childSet = next[childCategory] || new Set<string>();
    next[childCategory] = childSet;
    children
      .filter((value) => memberParent(value) === capability)
      .forEach((value) => {
        if (checked) childSet.add(value); else childSet.delete(value);
      });
  }

  return Object.fromEntries(
    Object.entries(next).map(([key, values]) => [key, Array.from(values).sort()]),
  );
}

export function AgentCapabilityPolicyView({
  authority,
  selection = null,
  metadata = {},
  editable = false,
  title,
  onChange,
}: {
  authority: AgentCapabilitySelection;
  selection?: AgentCapabilitySelection | null;
  metadata?: AgentCapabilityMetadata;
  editable?: boolean;
  title: string;
  onChange?: (capabilities: Record<string, string[]>) => void;
}) {
  const selected = policyMap(selection);
  const selectedCount = authority.groups.reduce(
    (total, group) => total + group.values.filter(
      (value) => (selected[group.category] || []).includes(value),
    ).length,
    0,
  );
  const total = authority.groups.reduce((sum, group) => sum + group.values.length, 0);

  return (
    <section className="agent-capability-policy" aria-label={title}>
      <div className="agent-capability-policy__head">
        <strong>{title}</strong>
        <span>{editable ? `${selectedCount} of ${total} selected` : `${total} entries`}</span>
        {editable ? (
          <span className="agent-capability-policy__commands">
            <button
              className="btn btn-ghost"
              type="button"
              onClick={() => onChange?.(Object.fromEntries(
                authority.groups.map((group) => [group.category, [...group.values]]),
              ))}
            >
              Select all
            </button>
            <button
              className="btn btn-ghost"
              type="button"
              onClick={() => onChange?.({})}
            >
              Clear
            </button>
          </span>
        ) : null}
      </div>
      <div className="agent-capability-policy__groups">
        {authority.groups.map((group) => (
          <fieldset key={group.category} className="agent-capability-policy__group">
            <legend>{group.label}</legend>
            <div className="agent-capability-policy__entries">
              {group.values.map((capability) => {
                const entry = metadataEntry(metadata, group.category, capability);
                const label = entry?.title || fallbackLabel(capability);
                const checked = (selected[group.category] || []).includes(capability);
                return editable ? (
                  <label className="agent-capability-policy__entry" key={capability} title={entry?.description || capability}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(event) => onChange?.(withToggle(
                        authority,
                        selection,
                        group.category,
                        capability,
                        event.target.checked,
                      ))}
                    />
                    <span>{label}</span>
                  </label>
                ) : (
                  <span className="claim-chip" key={capability} title={entry?.description || capability}>
                    {label}
                  </span>
                );
              })}
            </div>
          </fieldset>
        ))}
      </div>
    </section>
  );
}
