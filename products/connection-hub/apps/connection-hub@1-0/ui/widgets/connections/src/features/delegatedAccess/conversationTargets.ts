export const CONVERSATION_TARGETS_PROPERTY = 'kdcube.conversation_targets';

export function cardConversationTargets(properties: Record<string, unknown> | undefined): string[] {
  const raw = properties?.[CONVERSATION_TARGETS_PROPERTY];
  if (!Array.isArray(raw)) return [];
  if (raw.some((value) => (
    typeof value !== 'string' || value.length === 0 || value !== value.trim() || value === '*'
  ))) return [];
  return Array.from(new Set(raw as string[])).sort();
}

export function withConversationTargets(
  properties: Record<string, unknown>, targets: string[],
): Record<string, unknown> {
  if (!(CONVERSATION_TARGETS_PROPERTY in properties) && targets.length === 0) return properties;
  return { ...properties, [CONVERSATION_TARGETS_PROPERTY]: [...new Set(targets)].sort() };
}
