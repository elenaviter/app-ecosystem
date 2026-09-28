---
id: connection-hub@1-0/integrations/github
title: "Connection Hub — GitHub setup"
summary: "Operator step-by-step to create the GitHub App that Connection Hub uses as a connected-account provider: each person authorizes the App and gets a user token limited to what both they and the App can reach; covers the callback, permissions, installation, refresh-token rotation and where the client id/secret go."
status: "active"
tags: ["integration", "connections", "oauth", "github", "github-app", "operator-setup", "prerequisites"]
keywords: ["github app", "user access token", "refresh token rotation", "installation", "delegated_to_kdcube_oauth_callback", "github client_secret", "github.app"]
see_also:
  - ./README.md
---

# Connection Hub — GitHub setup

GitHub rides the **delegated to KDCube framework** as the `github.app`
provider type. See [the overview](./README.md) for the callback URL and the
state secret.

> **One App, each person's own access.** You create **one** GitHub App. A
> person connects GitHub by authorizing that App; Connection Hub stores
> **their** user token. The App then acts **as that person**, and only where
> both the person and the App reach: a repository is usable when the person
> can use it **and** the App is installed on it. Commits and pull requests
> show the person, marked as made through the App.

> **No scopes.** A GitHub App asks for no OAuth scopes. What the token may do
> is the App's **permissions** (set once, below), narrowed by the person's own
> access. The adapter therefore sends no `scope` parameter, and a claim's
> `provider_scopes` is left empty.

Official refs: registering a GitHub App
<https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app>
· user access tokens
<https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app>
· refreshing them
<https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens>

| # | Where | Action | Output |
| --- | --- | --- | --- |
| 1 | GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App** (under an organization's settings for an organization-owned App) | Name it; the name becomes the App's **slug** in its public link. Homepage: any page about your KDCube. | App form |
| 2 | **Identifying and authorizing users** | **Callback URL**: the delegated to KDCube callback, `…/connection-hub@1-0/public/delegated_to_kdcube_oauth_callback` on your KDCube's public address. Keep **Expire user authorization tokens** checked (it gives each token a refresh token). | Callback registered |
| 3 | **Webhook** | Uncheck **Active**: Connection Hub reads installations when asked and needs no events. | No webhook |
| 4 | **Permissions → Repository** | **Contents**: Read and write. **Pull requests**: Read and write. **Metadata**: Read-only (required). Add others only when a tool needs them. | Permissions set |
| 5 | **Where can this GitHub App be installed?** | **Any account**, so a person can install it on an owner you do not control. **Only on this account** is enough when every repository has one owner. | Installability |
| 6 | **Create GitHub App** → the App's **General** page | Copy the **Client ID**; **Generate a new client secret** and copy it. The private key is not needed: Connection Hub uses user tokens only. | Credentials |
| 7 | The App's **Install App** page | Install it on each owner whose repositories people will use, on **Only select repositories** or **All repositories**. | Installed |
| 8 | `bundles.yaml` | Client ID and the slug → the GitHub provider (below). | config updated |
| 9 | `bundles.secrets.yaml` | Client secret → the matching secret (below). | secret updated |

> **Installing on an owner** needs its admin: the organization owner, or the
> person for their own account. A person without that right asks the owner's
> admin; the link Connection Hub shows opens the installation page.

`bundles.yaml`:

```yaml
config:
  connections:
    delegated_to_kdcube:
      enabled: true
      providers:
        github:
          label: GitHub
          adapter: github.app
          enabled: true
          adapter_config:
            app_slug: <APP_SLUG>
          claims:
            github:repositories:
              label: Work in GitHub repositories
              description: Read, push and open pull requests where you and the App both have access.
          connector_apps:
            app:
              label: GitHub
              client_id: <GITHUB_APP_CLIENT_ID>
              client_secret_ref: connections.delegated_to_kdcube.providers.github.connector_apps.app.client_secret
              allowed_claims:
                - github:repositories
              enabled: true
```

`bundles.secrets.yaml`:

```yaml
secrets:
  connections:
    delegated_to_kdcube:
      providers:
        github:
          connector_apps:
            app:
              client_secret: <GITHUB_APP_CLIENT_SECRET>
```

## Tokens, rotation and reconnect

A user access token lasts **8 hours**; its refresh token lasts **6 months**
and is **rotated**: every refresh returns a new refresh token, and the old
refresh token and old access token stop working. Connection Hub stores the
new pair each time.

Two requests for the same person's GitHub account could otherwise refresh at
once, and the loser would hold a dead refresh token. The broker refreshes **one
at a time per connected account**: a request that finds a refresh in progress
waits, then uses the token that refresh produced. Within one process an
in-memory lock does this; across Connection Hub processes the host passes a
`RedisRefreshLock` to the broker.

A refresh GitHub refuses (the person revoked the App, the refresh token
passed its six months, or it was already used) is **not retried**: the
account shows **Reconnect required**, and the person connects GitHub again.

## Which repositories a person's token reaches

`connection_hub.delegated_to_kdcube.providers.github.repository_coverage`
answers, for a list of `owner/name` repositories and a person's token, per
owner:

| Field | Meaning |
| --- | --- |
| `installed` | The App is installed on this owner (`None` when GitHub could not be read). |
| `install_url` | The page that fixes a gap: the installation's settings page to add a repository, or the App's install page when it is not installed. |
| `repositories[].state` | `covered`, `missing` (installed on the owner, not on this repository), `not_installed`, or `unknown`. |

Only installations the person can reach are listed, so `not_installed` can
also mean the App is installed there but the person has no access.
