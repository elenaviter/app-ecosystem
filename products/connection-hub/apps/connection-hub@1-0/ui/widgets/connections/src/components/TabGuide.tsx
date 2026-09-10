import { useState } from 'react';
import type { ConnectionsTab } from './AppShell';

// One short line per tab saying what the user can do there, with an
// expandable Learn more list linking the public docs/recipes on GitHub.

const DOCS = 'https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs';

interface GuideLink {
  label: string;
  href: string;
}

interface Guide {
  summary: string;
  links: GuideLink[];
}

const GUIDES: Record<ConnectionsTab, Guide> = {
  identity: {
    summary:
      'Link alternate identities — such as a Telegram account — to your KDCube user, '
      + 'so KDCube recognizes the same person across channels and routes their data accordingly.',
    links: [
      {
        label: 'Link KDCube from an external channel',
        href: `${DOCS}/recipes/connections/link-from-external-channel-README.md`,
      },
      {
        label: 'Use connected identities in a product feature',
        href: `${DOCS}/recipes/connections/use-connected-identities-in-product-feature-README.md`,
      },
      {
        label: 'Telegram integration',
        href: `${DOCS}/recipes/connections/integrations/telegram-README.md`,
      },
      {
        label: 'How identity links are stored (connection edges)',
        href: `${DOCS}/sdk/solutions/connections/connection-edges/connection-edges-README.md`,
      },
    ],
  },
  delegatedToKdcube: {
    summary:
      'Your external accounts (Gmail, Slack, iCloud Mail) with the access you approved for KDCube apps, '
      + 'and the identities linked to you on other channels, such as Telegram. '
      + 'Connect, reconnect, add access, disconnect, or unlink here.',
    links: [
      {
        label: 'Connect Gmail',
        href: `${DOCS}/recipes/connections/integrations/google-service-README.md`,
      },
      {
        label: 'Connect Slack',
        href: `${DOCS}/recipes/connections/integrations/slack-README.md`,
      },
      {
        label: 'Mail accounts as a named service',
        href: `${DOCS}/recipes/connections/integrations/mail-named-service-README.md`,
      },
      {
        label: 'How delegated provider accounts work',
        href: `${DOCS}/sdk/solutions/connections/delegated-accounts/delegated-accounts-README.md`,
      },
      {
        label: 'Link KDCube from an external channel',
        href: `${DOCS}/recipes/connections/link-from-external-channel-README.md`,
      },
      {
        label: 'Telegram integration',
        href: `${DOCS}/recipes/connections/integrations/telegram-README.md`,
      },
      {
        label: 'How identity links are stored (connection edges)',
        href: `${DOCS}/sdk/solutions/connections/connection-edges/connection-edges-README.md`,
      },
    ],
  },
  providerConnections: {
    summary:
      'Connect provider accounts — Slack, Gmail — through KDCube\'s connector apps. '
      + 'Each access tier states what it grants; a connect asks the provider for exactly the tiers you check, '
      + 'and reconnecting an account adds tiers on top of the ones it already holds.',
    links: [
      {
        label: 'Connect Gmail',
        href: `${DOCS}/recipes/connections/integrations/google-service-README.md`,
      },
      {
        label: 'Connect Slack',
        href: `${DOCS}/recipes/connections/integrations/slack-README.md`,
      },
      {
        label: 'Connection Hub solution map',
        href: `${DOCS}/sdk/solutions/connections/connection-hub-solution-README.md`,
      },
    ],
  },
  remoteMcp: {
    summary:
      'Connect a user-owned MCP server, inspect its live tool descriptor, and make selected tools available on delegated access cards.',
    links: [
      {
        label: 'Connection Hub solution map',
        href: `${DOCS}/sdk/solutions/connections/connection-hub-solution-README.md`,
      },
    ],
  },
  delegatedAccess: {
    summary:
      'Access you granted, one card per caller: a hosted agent, a connected app, or a token for your own script. '
      + 'Each caller gets exactly what its card says. Edit applies on the caller\'s next call; revoke stops it at once. '
      + 'An expired card keeps its grants: reissue a manual token, or prolong a connected app, and it works again.',
    links: [
      {
        label: 'Create delegated automation access',
        href: `${DOCS}/recipes/connections/create-delegated-automation-access-README.md`,
      },
      {
        label: 'Delegate a KDCube service to an external client',
        href: `${DOCS}/recipes/connections/delegate-kdcube-service-to-external-client-README.md`,
      },
      {
        label: 'How delegated connections and grants work',
        href: `${DOCS}/sdk/solutions/connections/delegated-connections/delegated-connections-README.md`,
      },
    ],
  },
  accessMap: {
    summary:
      'Operator surface: the read-only map of what this deployment delegates to external clients — '
      + 'each OAuth resource, the named-service namespaces under it, per-operation grants, and the '
      + 'provider-backed connected-account claims. Resolved from the app configuration; edit the descriptor to change it.',
    links: [
      {
        label: 'Delegate KDCube services to an external client',
        href: `${DOCS}/recipes/connections/delegate-kdcube-service-to-external-client-README.md`,
      },
      {
        label: 'Create delegated automation access',
        href: `${DOCS}/recipes/connections/create-delegated-automation-access-README.md`,
      },
      {
        label: 'Named services over MCP',
        href: `${DOCS}/recipes/apps/named-services-mcp-README.md`,
      },
    ],
  },
  authenticators: {
    summary:
      'Operator surface. The platform\'s own sign-in and every sign-in authority this deployment trusts, '
      + 'with the pools it accepts in mixed mode, read from the descriptors; and the request authenticators '
      + '(Telegram bots and the like) you manage here. Secret values stay in the bundle-secret lifecycle.',
    links: [
      {
        label: 'Request authenticators',
        href: `${DOCS}/sdk/solutions/connections/request-authenticators/request-authenticators-README.md`,
      },
      {
        label: 'Connection Hub solution map',
        href: `${DOCS}/sdk/solutions/connections/connection-hub-solution-README.md`,
      },
    ],
  },
};

export function TabGuide({ tab }: { tab: ConnectionsTab }) {
  const [open, setOpen] = useState(false);
  const guide = GUIDES[tab];
  if (!guide) return null;
  return (
    <div className="tab-guide">
      <p className="tab-guide-summary">
        {guide.summary}{' '}
        <button className="tab-guide-toggle" type="button" onClick={() => setOpen((v) => !v)}>
          {open ? 'Less' : 'Learn more'}
        </button>
      </p>
      {open ? (
        <ul className="tab-guide-links">
          {guide.links.map((link) => (
            <li key={link.href}>
              <a href={link.href} target="_blank" rel="noopener noreferrer">
                {link.label}
              </a>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
