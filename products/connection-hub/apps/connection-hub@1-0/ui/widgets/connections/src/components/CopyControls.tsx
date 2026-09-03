import { useState } from 'react';

/** Copy-to-clipboard icon for any identifier the operator pastes elsewhere
 *  (a door address, a client id, an MCP endpoint). Confirms with a check for
 *  a moment. Shared by the delegated-access cards and the External MCP tab so
 *  every copyable value in the widget behaves the same way. */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };
  return (
    <button
      type="button"
      className="icon-btn"
      onClick={copy}
      title={copied ? 'Copied' : label}
      aria-label={copied ? 'Copied' : label}
    >
      {copied ? (
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M3.5 8.5l3 3 6-6" fill="none" stroke="currentColor" strokeWidth="1.6"
                strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <rect x="5.6" y="5.6" width="8" height="8" rx="1.8" fill="none"
                stroke="currentColor" strokeWidth="1.4" />
          <path d="M10.4 5.6V4.2A1.8 1.8 0 0 0 8.6 2.4H4.2A1.8 1.8 0 0 0 2.4 4.2v4.4a1.8 1.8 0 0 0 1.8 1.8h1.4"
                fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      )}
    </button>
  );
}

/** A long URL or pattern an operator pastes into a client config. Shown on one
 *  truncated line (full value on hover) that expands to the full, wrapped,
 *  selectable value on click or Enter/Space, with a copy button beside it. */
export function DoorRef({ value, copyLabel = 'Copy address' }: { value: string; copyLabel?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="door-ref">
      <code
        className={open ? 'door-uri open' : 'door-uri'}
        title={open ? undefined : value}
        onClick={() => setOpen((v) => !v)}
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setOpen((v) => !v); } }}
      >
        {value}
      </code>
      <CopyButton value={value} label={copyLabel} />
    </span>
  );
}
