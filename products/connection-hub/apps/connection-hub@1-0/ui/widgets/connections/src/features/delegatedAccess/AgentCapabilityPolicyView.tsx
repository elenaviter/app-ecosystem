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

const CAPABILITY_FAMILIES = [
  {
    id: 'tools',
    label: 'Tools',
    parentCategory: 'tool_groups',
    childCategory: 'tools',
    parentSelectionLabel: 'Tool group available',
    childNoun: 'tools',
  },
  {
    id: 'mcp',
    label: 'MCP',
    parentCategory: 'mcp_servers',
    childCategory: 'mcp_tools',
    parentSelectionLabel: 'MCP server available',
    childNoun: 'tools',
  },
  {
    id: 'services',
    label: 'Named services',
    parentCategory: 'named_services',
    childCategory: 'named_service_operations',
    parentSelectionLabel: 'Service available',
    childNoun: 'operations',
  },
  {
    id: 'resources',
    label: 'Resources',
    parentCategory: 'resources',
    childCategory: 'resource_operations',
    parentSelectionLabel: 'Resource available',
    childNoun: 'operations',
  },
] as const;

const FAMILY_CATEGORIES: Set<string> = new Set(
  CAPABILITY_FAMILIES.flatMap(({ parentCategory, childCategory }) => [
    parentCategory,
    childCategory,
  ]),
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

function withSingleChoice(
  selection: AgentCapabilitySelection | null,
  category: string,
  capability: string,
): Record<string, string[]> {
  return {
    ...policyMap(selection),
    [category]: [capability],
  };
}

export function AgentCapabilityPolicyView({
  authority,
  selection = null,
  metadata = {},
  editable = false,
  title,
  onChange,
  categories,
  singleChoiceCategories = [],
}: {
  authority: AgentCapabilitySelection;
  selection?: AgentCapabilitySelection | null;
  metadata?: AgentCapabilityMetadata;
  editable?: boolean;
  title: string;
  onChange?: (capabilities: Record<string, string[]>) => void;
  categories?: string[];
  singleChoiceCategories?: string[];
}) {
  const visibleCategories = categories ? new Set(categories) : null;
  const singleChoices = new Set(singleChoiceCategories);
  const visibleGroups = authority.groups.filter(
    (group) => !visibleCategories || visibleCategories.has(group.category),
  );
  const selected = policyMap(selection);
  const selectedCount = visibleGroups.reduce(
    (total, group) => total + group.values.filter(
      (value) => (selected[group.category] || []).includes(value),
    ).length,
    0,
  );
  const total = visibleGroups.reduce((sum, group) => sum + group.values.length, 0);
  const authorityByCategory = policyMap(authority);

  const replaceVisible = (all: boolean) => {
    const next = policyMap(selection);
    visibleGroups.forEach((group) => {
      if (!singleChoices.has(group.category)) {
        next[group.category] = all ? [...group.values] : [];
      } else if (all) {
        const current = (next[group.category] || []).find((value) => (
          group.values.includes(value)
        ));
        next[group.category] = current ? [current] : group.values.slice(0, 1);
      } else {
        next[group.category] = [];
      }
    });
    onChange?.(next);
  };

  const renderEntry = (
    category: string,
    capability: string,
    displayLabel?: string,
  ) => {
    const entry = metadataEntry(metadata, category, capability);
    const label = displayLabel || entry?.title || fallbackLabel(capability);
    const checked = (selected[category] || []).includes(capability);
    const singleChoice = singleChoices.has(category);
    const copy = (
      <span className="agent-capability-policy__entry-copy">
        <span>{label}</span>
        {entry?.description ? <small>{entry.description}</small> : null}
      </span>
    );
    return editable ? (
      <label
        className="agent-capability-policy__entry"
        key={`${category}:${capability}`}
        title={capability}
      >
        <input
          type={singleChoice ? 'radio' : 'checkbox'}
          name={singleChoice ? `${authority.resource}:${category}` : undefined}
          checked={checked}
          onChange={(event) => onChange?.(
            singleChoice
              ? withSingleChoice(selection, category, capability)
              : withToggle(
                  authority,
                  selection,
                  category,
                  capability,
                  event.target.checked,
                ),
          )}
        />
        {copy}
      </label>
    ) : (
      <span
        className="agent-capability-policy__entry agent-capability-policy__entry--readonly"
        key={`${category}:${capability}`}
        title={capability}
      >
        {copy}
      </span>
    );
  };

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
              onClick={() => replaceVisible(true)}
            >
              Select all
            </button>
            <button
              className="btn btn-ghost"
              type="button"
              onClick={() => replaceVisible(false)}
            >
              Clear
            </button>
          </span>
        ) : null}
      </div>
      <div className="agent-capability-policy__groups">
        {CAPABILITY_FAMILIES.map((family) => {
          if (
            visibleCategories
            && !visibleCategories.has(family.parentCategory)
            && !visibleCategories.has(family.childCategory)
          ) return null;
          const parents = authorityByCategory[family.parentCategory] || [];
          const children = authorityByCategory[family.childCategory] || [];
          if (!parents.length && !children.length) return null;
          const knownParents = new Set(parents);
          const orphaned = children.filter((value) => !knownParents.has(memberParent(value)));
          return (
            <fieldset key={family.id} className="agent-capability-policy__group">
              <legend>{family.label}</legend>
              <div className="agent-capability-policy__branches">
                {parents.map((parent) => {
                  const entry = metadataEntry(metadata, family.parentCategory, parent);
                  const label = entry?.title || fallbackLabel(parent);
                  const members = children.filter((value) => memberParent(value) === parent);
                  const selectedMembers = members.filter((value) => (
                    selected[family.childCategory] || []
                  ).includes(value)).length;
                  return (
                    <details className="agent-capability-policy__branch" key={parent}>
                      <summary>
                        <span>
                          <strong>{label}</strong>
                          {entry?.description ? <small>{entry.description}</small> : null}
                        </span>
                        <span className="agent-capability-policy__branch-count">
                          {editable
                            ? `${selectedMembers} of ${members.length} ${family.childNoun} selected`
                            : `${members.length} ${family.childNoun}`}
                        </span>
                      </summary>
                      <div className="agent-capability-policy__entries agent-capability-policy__entries--nested">
                        {renderEntry(
                          family.parentCategory,
                          parent,
                          family.parentSelectionLabel,
                        )}
                        {members.map((capability) => renderEntry(
                          family.childCategory,
                          capability,
                        ))}
                      </div>
                    </details>
                  );
                })}
                {orphaned.length ? (
                  <div className="agent-capability-policy__branch agent-capability-policy__branch--orphaned">
                    <strong>Other {family.childNoun}</strong>
                    <div className="agent-capability-policy__entries agent-capability-policy__entries--nested">
                      {orphaned.map((capability) => renderEntry(
                        family.childCategory,
                        capability,
                        `${fallbackLabel(memberParent(capability))}: ${metadataEntry(metadata, family.childCategory, capability)?.title || fallbackLabel(capability)}`,
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            </fieldset>
          );
        })}
        {visibleGroups.filter((group) => !FAMILY_CATEGORIES.has(group.category)).map((group) => (
          <fieldset key={group.category} className="agent-capability-policy__group">
            <legend>{group.label}</legend>
            <div className="agent-capability-policy__entries">
              {group.values.map((capability) => renderEntry(group.category, capability))}
            </div>
          </fieldset>
        ))}
      </div>
    </section>
  );
}
