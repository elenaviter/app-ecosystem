---
id: project-board-github
title: GitHub Access For Agents
summary: How an agent pushes, pulls and opens pull requests as the person it works for. The person connects GitHub once in Connection Hub and links it to a project on their My Card; the agent asks Connection Hub for its owner's short-lived token per repository; the board decides; no GitHub token is stored on the agent's machine.
tags:
  - project-board
  - github
  - connection-hub
  - credentials
  - security
keywords:
  - GitHub App
  - Connection Hub Gateway
  - github.app
  - My Card
  - Use GitHub
  - project.github.use
  - pb worker git-credential
  - pb worker gh
  - peer proof
  - project_agent_github_token_issue
  - project_agent_github_authorize
  - commit email
  - noreply
see_also:
  - ./README.md
  - ./cards.md
  - ./concepts.md
  - ./add-a-machine.md
  - repo:app-ecosystem/docs/connection-hub/frontend/application/integrations/github.md
  - repo:app-ecosystem/products/connection-hub/apps/connection-hub@1-0/interface/README.md
---

# GitHub Access For Agents

An agent that works for a person pushes, pulls and opens pull requests **as
that person**. GitHub credits the commits and the pull requests to the
person; it does not label them with the App. The App's part shows in the
person's GitHub settings (Applications, Authorized GitHub Apps, with its last
use), in an organization's audit log, and in Connection Hub's own audit line
for every token it issues. The person grants this once, and can take it back
at any time.

- **One key per person, not per agent and not per machine.** It is the
  person's GitHub authorization of the deployment's GitHub App. Every agent
  the person approved can use it, on the repositories of the projects it
  works on.
- **The key never lives on the agent's machine.** Connection Hub holds it. An
  agent asks for a short-lived copy per git or gh operation, and git or gh
  forget it when the operation ends.
- **The board decides.** Before Connection Hub releases the token, it asks the
  board whether this agent works on the project now, whether its Card holds
  **Use GitHub**, and whether the repository is on the project card.

## Who holds what

| Where | What it holds | Changed by |
|---|---|---|
| **GitHub** | The deployment's GitHub App, installed on each repository owner (an organization or a person), and each person's authorization of that App. | The owners' admins install; each person authorizes. |
| **Connection Hub** (server) | Each person's GitHub connection: the user token and its refresh token, stored for that person only. Each person's **My Card** for a project: which GitHub account it uses, and the commit email. | The person, on My Card. |
| **Board** (server) | The project card with its repositories, and each agent's Card with its **Use GitHub** permission. | Project admins (repositories); the person (their agents' Cards). |
| **The agent's machine** | The agent's own Card credential, in the operating system's credential store (macOS Keychain, Windows Credential Manager, Linux Secret Service). **No GitHub token.** | `pb worker authorize`. |

```text
 ┌──────────────────────── GitHub ────────────────────────┐
 │  GitHub App "…Gateway"  ── installed on ──► owners/repos │
 │  person ── authorized ──► App                            │
 └──────────────────────────▲─────────────────────────────┘
                            │ user token (8 h), refresh token (6 months)
 ┌──────────────── KDCube deployment ─────────────────────┐
 │                                                         │
 │  Connection Hub                         Board           │
 │  ┌──────────────────────────┐          ┌──────────────┐ │
 │  │ person's GitHub connection│  signed  │ project card │ │
 │  │   (token + refresh token) │ question │ (repos)      │ │
 │  │ person's My Card, per     │◄────────►│ agent Cards  │ │
 │  │   project: account, email │  answer  │ (Use GitHub) │ │
 │  └────────────▲─────────────┘          └──────────────┘ │
 └───────────────┼─────────────────────────────────────────┘
                 │ Card credential in ─► short-lived token out
 ┌───────────────┴──── agent's machine ────────────────────┐
 │  Keychain: the agent's Card credential (no GitHub token) │
 │  pb worker git-credential ─► git push / pull / fetch     │
 │  pb worker gh -- …        ─► gh pr create / view / …     │
 └──────────────────────────────────────────────────────────┘
```

## Once per deployment: the operator

1. **Register one GitHub App** for the deployment. Register it under the
   organization that people will recognize, because the consent screen names
   the App and its owner. The Connection Hub setup article has every field:
   [GitHub setup](repo:app-ecosystem/docs/connection-hub/frontend/application/integrations/github.md).
   In short:
   - Repository permissions: Contents and Pull requests read and write,
     Metadata read.
   - Keep **Expire user authorization tokens** on.
   - Keep **Device Flow** off: the Client ID is public, and device flow would
     let anyone start a login in the App's name and phish a token.
   - No webhook.
   - Installable on any account.
2. **Install the App** on every owner whose repositories agents will use. That
   needs each owner's admin.
3. **Configure Connection Hub:**
   - the `github` provider (adapter `github.app`, the App's slug and Client
     ID);
   - the client secret;
   - `project_membership.provider.github_authorize_operation` and
     `peer_proof_secret_ref`.
4. **Configure the board:** `connection_hub.peer_proof_secret_ref` is in its
   template.
5. **Set one random peer-proof value** (at least 32 bytes) in both apps' secrets.
   - On a local deployment whose assembly has `secrets.provider:
     secrets-file`, write it to `config/bundles.secrets.yaml`, then reload
     both apps.
   - Elsewhere, use the Connection Hub CLI: `connection-hub host authorize`,
     then `connection-hub secrets host set …`.

   Plain `kdcube secrets set` first asks for a delegated management bearer,
   which a person at a terminal does not have.

Without the peer-proof value in both apps, every token request is refused by
name (`project_github_peer_proof_not_configured`), never allowed.

## Once per person: connect and link

```text
 person (browser)            board             Connection Hub            GitHub
      │  "Set up GitHub" on     │                     │                     │
      │  their own card ───────►│ opens My Card ─────►│                     │
      │                         │  (this project)     │                     │
      │  Connect GitHub ─────────────────────────────►│ authorize URL ─────►│
      │                                               │  (state: person,     │
      │  approve App (first time only) ◄─────────────────────────────────────│
      │                                               │◄── code + state ─────│
      │                                               │ exchange code ──────►│
      │                                               │◄── user + refresh ───│
      │                                               │ store for this person│
      │                                               │ link to this project │
      │◄── My Card: "connected as <login>" ───────────│  (My Card)           │
      │  set commit email, Save ─────────────────────►│                     │
      │  board card updates ◄───│◄── "key changed" ───│                     │
```

1. **On their own card on the board** (Team, their card), the person presses
   **Set up GitHub**. It opens their **My Card** for this project in Connection
   Hub, at its GitHub section. The project card does not carry it: the GitHub
   link and the commit email are each person's own.
2. **Connect GitHub** sends the browser to GitHub. The first time, GitHub shows
   the App's approval screen, naming the App and its owner. Later, it returns
   at once.
3. Connection Hub exchanges the code for the person's user token and refresh
   token and stores them for that person. A Connect started on My Card for a
   project also **links** the account to that project when it is the person's
   only GitHub account. With several accounts, My Card asks which to use for
   this project. Connecting and linking are separate so that one GitHub
   account can serve several projects.
4. The person sets the **commit email** on My Card. It must be an address
   GitHub credits to them. GitHub's private address,
   `<id>+<login>@users.noreply.github.com` (GitHub Settings, Emails), always
   counts as verified and keeps the real address out of commit history. If
   GitHub's **Block command line pushes that expose my email** is on, use the
   private address, or pushes are refused (GH007).
5. My Card shows **connected as `<login>`** and, per owner, which of the
   project's repositories the token reaches, with a link to fix a gap (the
   App's install page or the installation's settings). The person's card on
   the board updates without a reload.

When managed Card saves are enabled, linking, unlinking and setting or
clearing the commit email use the project's signed, link-only Card save.
Only the authenticated owner can change these settings on their own My
Card; an admin cannot edit another person's managed settings. The Card holds
only the connected-account reference and email, never the GitHub token. A
failed or pending save returns a named refusal and changes no authority
directly; retry the same request. After a committed relink, new token requests
read the new account; after an unlink, they refuse `github_not_linked`.
The exact patch and STAGE evidence contract is in
[Card transactions](repo:app-ecosystem/docs/connection-hub/package/card-transactions.md).

## Once per agent: Use GitHub

An agent's Card holds **Use GitHub** (`project.github.use`) by default when it
is approved as a worker or coordinator, and when it is made coordinator or
worker. The person can untick it on that agent's Card to keep one agent off
GitHub. When an agent is added to a project, **Use GitHub** is added without
touching the rest of its Card.

On each machine, `pb worker connect-project` sets up the agent's clones:

- it clones over HTTPS;
- it names pb as git's credential helper for `https://github.com`, clearing
  any other helper first, so a Keychain login of another account never
  answers;
- it sets the commit identity to the agent's alias and the owner's My Card
  commit email;
- it stamps the clone with the Card's GitHub account and the project
  (`pb.githubAccount`, `pb.projectRef`), from the key's fresh answer.

A machine where the owner has no GitHub key yet still works over SSH with its
deploy keys, as before ([add a machine](add-a-machine.md)).

## Every push, pull and pull request

```text
 agent        git        pb (helper)        Connection Hub            board               GitHub
   │ git push ──►│             │                   │                       │                    │
   │             │ credential? │                   │                       │                    │
   │             │ (github.com,│                   │                       │                    │
   │             │  owner/repo)►│                  │                       │                    │
   │             │             │ Card credential   │                       │                    │
   │             │             │ from Keychain     │                       │                    │
   │             │             │ POST token_issue ►│ check the Card:        │                    │
   │             │             │ (project, repo)   │  live, whose agent     │                    │
   │             │             │                   │ signed question ──────►│ agent attends?     │
   │             │             │                   │                       │ Card: Use GitHub?  │
   │             │             │                   │                       │ repo on the card?  │
   │             │             │                   │◄────── allowed ────────│                    │
   │             │             │                   │ owner's token (refresh │                    │
   │             │             │                   │  first if due, one at a│                    │
   │             │             │                   │  time per account)     │                    │
   │             │             │◄─ token, login,   │ audit line (never the  │                    │
   │             │             │   commit email ───│  token)                │                    │
   │             │◄─ x-access- │                   │                       │                    │
   │             │   token +   │ (exits, forgets)  │                       │                    │
   │             │   token     │                   │                       │                    │
   │             │ HTTPS push with the owner's token ───────────────────────────────────────────►│
   │◄── done ────│                                                                              │
```

1. The agent runs an ordinary `git push`, `git pull` or `git fetch` in its
   clone.
2. git asks its credential helper, `pb worker git-credential`, for
   `github.com` and the repository path.
3. pb reads **this session's Card credential** from the operating system's
   credential store. It calls Connection Hub's
   `project_agent_github_token_issue` with the project and the repository
   (`owner/name`), sending the Card credential in its own header. pb refuses
   a host other than `github.com`, and a repository not on the project card.
4. **Connection Hub checks the Card for identity only:** it is live, and it
   belongs to the person the agent works for. The decision is not
   Connection Hub's.
5. **Connection Hub asks the board**, in a signed question (next section):
   does this agent attend the project now, does its Card hold Use GitHub, is
   the repository on the project card?
6. **On "allowed"**, Connection Hub takes the owner's stored token. It
   refreshes the token first when it is due, one refresh at a time per
   account, so two agents cannot spend the same refresh token. It returns the
   token, the owner's GitHub login and their commit email, and writes one
   audit line naming the project, repository, agent and owner, never the
   token.
7. pb answers git with username `x-access-token` and the token, then exits.
   Nothing is written to disk.
8. git talks to GitHub over HTTPS with the owner's token. GitHub allows it only
   where **both** the owner and the App reach.

`gh` works the same way: `pb worker gh -- pr create …` (or any gh command)
does steps 3 to 6 for the repository the command names (its `-R/--repo`, else
the clone's origin), then runs gh with `GH_TOKEN` in that one process's
environment. gh needs no login of its own. `gh api` takes no `-R/--repo`, so
for `api` that flag only selects the repository's key and gh does not receive
it. A clone's origin may be github.com over HTTPS or SSH, or the
`github-<alias>` SSH host alias a machine's deploy key uses
([add a machine](add-a-machine.md)). Any other host names no GitHub
repository, and the key is still issued only for a repository on the project
card.

## The Card's account, forced on pb's side (W762)

The operator, 2026-10-12: "for pb workers their side must be forced to work
with the account as configured in connection hub for the operator card". pb
enforces this itself; a machine's own git or gh routing is at most a safety
net.

Five things are kept apart:

- **Commit identity:** the clone's `user.name` (the agent's alias) and
  `user.email` (the Card's commit email). It decides whose commits these are on
  GitHub.
- **The Card's authentication:** the token Connection Hub issues for one
  command. It is never stored or printed, and it is what GitHub checks.
- **Non-secret metadata:** the recorded identity and the clone's
  `pb.githubAccount` / `pb.projectRef` stamp. Anyone on the machine can edit
  them, so they are never proof on their own.
- **A machine's safety net:** for example a host gh wrapper or git hooks.
  pb never relies on it.
- **Operating-system isolation:** not provided. Agents running as one user can
  reach that user's files.

What pb does:

- **A fresh identity, never a recorded one.** `pb worker workspace-report
  --set-identity` asks the Card's key and writes its email and account, or
  refuses (`identity_refresh_unavailable`). It never reapplies the last
  recorded email. A project whose Card has no GitHub key route keeps the
  project's email, and only while none of its clones is stamped.
- **Clones follow the Card.** Every successful key issue brings the clone the
  command runs in up to the Card's identity, only if that clone belongs to the
  same project. A Card change therefore reaches existing clones without
  recloning, and their work is untouched. A worktree that overrides
  `user.email` is refused (`clone_identity_overridden`).
- **The token is proven.** Connection Hub's `login` is the connected account's
  stored name, so before `gh` runs and before a push, pb asks GitHub which
  account the token acts as (`GET /user`). It refuses on any other answer
  (`card_account_mismatch`), and when that cannot be confirmed
  (`card_account_unverified`).
- **The real gh.** `pb worker gh` runs gh from its install locations, never
  from `PATH` and never a script. A wrapper that swaps `GH_TOKEN` (for example
  one that routes a machine's own accounts) therefore cannot replace the Card's
  token.
- **Pushes are checked first.** In a clone that is stamped, or that pb's helper
  answers, `pb worker push` (with or without `--owner-key-only`) refuses
  before pushing when:
  - the clone is unstamped (`clone_unstamped`);
  - a remote does not push over HTTPS;
  - another credential helper or an `extraheader` could answer
    (`clone_credential_override`);
  - the key, the stamp or the email is not the Card's (`card_account_mismatch`);
  - a new commit is not authored and committed with the Card's email
    (`push_commit_identity_mismatch`). Commits the remote already has are not
    checked.

Every check compares against the Card of the agent's own project. No account
name is fixed: the account the operator links on a project's Card is the one
its agents act as. For example, one operator's Quickstart and KDCube
Maintenance Cards name `kdcube-agents`.

## The board's decision: a signed question

The token request comes from an agent, not from a signed-in person, and a call
between two apps of the platform does not say which app made it. So
Connection Hub signs the question, and the board answers only a correctly
signed one:

```text
 Connection Hub                                   board
   body = { access_id, grantor_subject,             │
            project_ref, repository }               │
   service_proof = HMAC(peer-proof secret,          │
      service "connection-hub", timestamp, nonce,   │
      operation "project_agent_github_authorize",   │
      the four fields)                              │
   ── project_agent_github_authorize(body+proof) ──►│ verify the HMAC (same secret)
                                                    │ timestamp within 300 s
                                                    │ nonce never seen before
                                                    │ agent exists and is active  → worker_unknown
                                                    │ owner is the one named      → grantor_mismatch
                                                    │ attends the project now     → not_attending
                                                    │ repository on the card      → repository_not_on_card
                                                    │ Card holds Use GitHub       → card_denies
   ◄────────────── { allowed, reason, owner } ──────│
```

- The secret is shared by these two apps only, set by the operator in both.
- The proof binds the exact agent, owner, project and repository, and the
  operation name. A proof for one question cannot pass as another, or as
  Connection Hub's admission proof.
- The board answers only **allowed or why not**. **No token ever reaches the
  board.**
- In Connection Hub's descriptor, the board is the configured
  `project_membership.provider`. Connection Hub does not hard-code it.

## Tokens: lifetime and custody

- **User token:** 8 hours. **Refresh token:** 6 months. Each refresh rotates
  both, and Connection Hub stores the new pair.
- A refresh GitHub refuses is not retried: the connection shows **Reconnect
  required**, and the person connects GitHub again. Causes are an App revoked,
  a refresh token older than 6 months, or one already spent.
- The agent's machine holds no GitHub token or refresh token. pb holds a token
  only in memory during one git or gh command. The Card credential stays in
  the operating system's credential store and never appears in a command line
  or an environment variable.

## Taking it back

| To stop | Do | Takes effect |
|---|---|---|
| one agent | untick **Use GitHub** on that agent's Card | its next git or gh operation |
| all agents of a person, one project | **Unlink GitHub from this project** on My Card | the next operation |
| all of a person's agents, everywhere | revoke the App on GitHub (Settings, Applications) | the next operation; My Card shows Reconnect required |
| an agent leaving the project | remove it from the project | the next operation (`not_attending`) |

Nothing on the agent's machine needs cleaning: it never stored a GitHub token.

## Named refusals

| Code | Meaning | Fix |
|---|---|---|
| `github_not_linked` | The owner has no GitHub account linked to this project. | The owner links it on My Card. |
| `commit_email_not_set` | No commit email on the owner's My Card for this project. | Set it on My Card. |
| `github_reconnect_required` | GitHub refused the refresh. | The owner connects GitHub again. |
| `not_attending` | The agent does not work on this project now. | Add it to the project. |
| `repository_not_on_card` | The repository is not on the project card. | A project admin adds it. |
| `card_denies` | The agent's Card lacks Use GitHub. | Tick it on the agent's Card. |
| `worker_unknown`, `grantor_mismatch` | The Card does not match an active agent of that owner. | Re-authorize the agent. |
| `project_github_peer_proof_not_configured` | The peer-proof secret is missing in either app. | The operator sets it in both (setup, step 5). |
| `project_github_board_update_required` | The board predates this feature. | Deploy the current board. |
| `project_person_my_card_property_self_only` | Another person's managed My settings were targeted. | The owner changes their own My Card. |
| `managed_card_edit_route_unavailable` | The managed settings save has no configured project route. | The operator configures the managed Card save; no direct fallback is made. |
| `identity_refresh_unavailable` | `--set-identity` could not read the Card's identity fresh; nothing was written. | Retry when Connection Hub answers. |
| `card_account_mismatch` | The token, the clone's stamp or its email is not the Card's. | Rerun once, since the clone is brought up to the Card; if it repeats, the operator reconnects GitHub on the Card. |
| `card_account_unverified` | GitHub did not confirm which account the token acts as. | Retry; nothing ran. |
| `clone_unstamped` | A Card-managed clone has no `pb.githubAccount`. | `pb worker workspace-report --set-identity`. |
| `clone_credential_override` | Another credential helper or an `extraheader` could answer for github.com. | Remove it from the clone's config. |
| `clone_identity_overridden` | A worktree sets its own `user.email`. | Remove the worktree override. |
| `push_commit_identity_mismatch` | New commits are not authored and committed with the Card's email. | Fix those commits' author and committer (amend or rebase) before pushing. |

A push that GitHub itself refuses:
- **403 on an owner:** the App is not installed there or not on that
  repository. My Card's coverage list links the fix.
- **GH007:** the commit email is private on GitHub. Use the noreply address.

## Status (2026-09-28)

Live on the development deployment, end to end:
- the GitHub App;
- connecting and linking on My Card, in one step when started from My Card;
- the board card's GitHub section;
- Connection Hub's identity-only Card check, with Connection Hub then asking
  the board as itself.

The first push and pull request under an owner's key went through on
2026-09-28 at 16:15Z: `git push` through `pb worker git-credential`, and the
pull request through `pb worker gh`. Connection Hub audited each issued token.

Still landing, under W371:
- **Add-to-project:** it adds **Use GitHub** to the agent's Card, leaving the
  rest of the Card alone, when a person adds the agent.
- **`pb worker connect-project`:** it takes the commit email from the owner's
  My Card. Today `pb worker context` still names the project-wide email.

Next, under W374: Connection Hub stops knowing about projects at all. The
board becomes one configured authority it asks, and this page will follow.
