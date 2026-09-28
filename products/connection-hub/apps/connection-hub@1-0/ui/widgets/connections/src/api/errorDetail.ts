/** The sentence a refused call shows (2026-09-28): the server's own words,
 *  never its raw JSON body. OAuth routes answer `error_description`, others
 *  `message` or `detail`; only a body without any of them shows as text. */
export function errorDetail(parsed: unknown, fallback: string): string {
  if (parsed && typeof parsed === 'object') {
    const body = parsed as Record<string, unknown>;
    for (const key of ['error_description', 'message', 'detail']) {
      const value = body[key];
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
    if (typeof body.error === 'string' && body.error.trim()) return body.error.trim();
  }
  return fallback;
}
