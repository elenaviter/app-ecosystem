/**
 * Structured reading of a discovered tool's JSON Schema for the disclosure
 * rows in the External MCP tab. The schema is parsed as data: `properties`,
 * `required`, `type`, `enum`, `default`, `items`, and the `anyOf`/`oneOf`/
 * `$ref` shapes that MCP servers commonly emit. Anything this reader cannot
 * name is kept, verbatim, for the folded raw-schema view; nothing is derived by
 * string splitting.
 */

export interface SchemaParameter {
  name: string;
  /** Human-readable type such as `string`, `integer | null`, `array<string>`, `object (2 fields)`. */
  type: string;
  required: boolean;
  description: string;
  /** Enumerated values, already stringified for display. */
  enumValues: string[];
  /** Stringified default value, or '' when the schema declares none. */
  defaultValue: string;
}

export interface SchemaSummary {
  parameters: SchemaParameter[];
  /** True when the schema exists but has no readable `properties` object. */
  opaque: boolean;
  /** True when the schema allows properties beyond the listed ones. */
  additionalProperties: boolean;
}

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringify(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function refName(ref: unknown): string {
  const text = typeof ref === 'string' ? ref : '';
  const tail = text.split('/').pop() || text;
  return tail ? `ref ${tail}` : 'ref';
}

/** One schema node to a short type label. Recurses one level into arrays and
 *  unions; deeper structure stays in the raw view. */
export function schemaTypeLabel(node: unknown, depth = 0): string {
  if (!isObject(node)) return 'any';
  if (typeof node.$ref === 'string') return refName(node.$ref);
  const union = (node.anyOf ?? node.oneOf) as unknown;
  if (Array.isArray(union) && union.length) {
    if (depth > 1) return 'union';
    const labels = union.map((item) => schemaTypeLabel(item, depth + 1));
    return Array.from(new Set(labels)).join(' | ');
  }
  if (Array.isArray(node.allOf) && node.allOf.length) return 'object';
  if (Array.isArray(node.enum) && node.enum.length) return 'enum';
  const rawType = node.type;
  const types = Array.isArray(rawType)
    ? rawType.filter((item): item is string => typeof item === 'string')
    : typeof rawType === 'string'
      ? [rawType]
      : [];
  if (!types.length) {
    if (isObject(node.properties)) return `object (${Object.keys(node.properties).length} fields)`;
    if (node.items !== undefined) return 'array';
    if (typeof node.const !== 'undefined') return `const ${stringify(node.const)}`;
    return 'any';
  }
  return types.map((type) => {
    if (type === 'array') {
      const items = depth > 1 ? 'any' : schemaTypeLabel(node.items, depth + 1);
      return `array<${items}>`;
    }
    if (type === 'object' && isObject(node.properties)) {
      return `object (${Object.keys(node.properties).length} fields)`;
    }
    if (typeof node.format === 'string' && node.format) return `${type} (${node.format})`;
    return type;
  }).join(' | ');
}

/** Read the top-level parameters of a tool's input or output schema. */
export function summarizeSchema(schema: unknown): SchemaSummary {
  if (!isObject(schema)) return { parameters: [], opaque: schema !== undefined && schema !== null, additionalProperties: false };
  const properties = isObject(schema.properties) ? schema.properties : null;
  const required = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((item): item is string => typeof item === 'string')
      : [],
  );
  const additional = schema.additionalProperties;
  const additionalProperties = additional === undefined ? false : additional !== false;
  if (!properties) {
    return {
      parameters: [],
      opaque: Object.keys(schema).some((key) => key !== 'type' && key !== 'additionalProperties' && key !== '$schema' && key !== 'title'),
      additionalProperties,
    };
  }
  const parameters = Object.entries(properties).map(([name, node]) => {
    const object = isObject(node) ? node : {};
    const enumValues = Array.isArray(object.enum) ? object.enum.map(stringify) : [];
    return {
      name,
      type: schemaTypeLabel(node),
      required: required.has(name),
      description: typeof object.description === 'string' ? object.description : '',
      enumValues,
      defaultValue: object.default === undefined ? '' : stringify(object.default),
    };
  });
  return { parameters, opaque: false, additionalProperties };
}

/** Pretty JSON for the folded raw view; never throws. */
export function formatSchema(schema: unknown): string {
  try {
    return JSON.stringify(schema, null, 2);
  } catch {
    return String(schema);
  }
}
