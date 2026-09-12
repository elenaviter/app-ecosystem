---
id: connection-hub/frontend/application/ux-story-and-surfaces
title: "Connection Hub UX: The Story And The Surfaces"
summary: "How a person is meant to read Connection Hub (who am I here, what of mine may KDCube use, who may use my KDCube), the reading rules every surface follows (fold long token lists, group by service, details on demand), what the first wave changed, and the proposed second wave (tab story, card summary rows) awaiting sign-off."
status: active
tags: ["connection-hub", "ux", "widget", "granted-access", "delegated-cards", "design"]
keywords: ["connection hub tabs", "granted access card", "fold chips", "claim groups", "named-service access", "invocation policy", "user story"]
updated_at: 2026-09-11
see_also:
  - ./README.md
  - ../../connection-hub-architecture.md
  - ../../custom-mcp-connector.md
  - ../../package/delegated-cards.md
---

# Connection Hub UX: The Story And The Surfaces

Connection Hub is where a person manages everything that connects them to
KDCube and KDCube to their things. The widget grew surface by surface, so a
newcomer meets seven tabs and cards that list every permission token. This
page states the story the widget must tell, the reading rules every surface
follows, what the first wave changed, and what the second wave proposes.

## The story in three questions

Everything a person does here answers one of three questions. The tabs are
the questions, in that order. Operator surfaces sit apart.

```text
 1. Who am I here?                 Identity
      linked identities: Telegram, Google, other channels
      (a messenger link is an identity, not an account)

 2. What of mine may KDCube use?    Accounts          External MCP
      Gmail, Slack, iCloud Mail,    my own MCP servers, registered
      with the access I approved    once, tools chosen per caller

 3. Who may use my KDCube?          Granted access
      agents, apps, scripts: one card each, with exactly
      what I granted, editable and revocable at once

 operator                           Access map · Authenticators
```

The tabs now read: Accounts and links, Provider connections, External MCP,
Access cards, then the operator tabs. "Accounts and links" holds what were
the Identity and Delegated to KDCube tabs: the accounts as provider groups
in columns, one account per row, and the linked identities beneath.
"Access cards" was "Delegated by KDCube". The direction words asked the
reader to keep a mental arrow that the three questions make unnecessary.
Provider connections still answers question 2 a second time (by
connector-app tiers); merging it is the open item.

## The look: a cloud console

The reference is the modular dashboard of a cloud console: panes with a
title and a count, dense rows with columns the eye can scan, one height
per control, the pane's action on the pane's head row, nothing
decorative (no gradients, no fades, no heavy outlines). Ergonomic and
concrete: a reader distinguishes at once what is there and how many.

## The reading rules

Every surface follows the same rules, so a person who learned one card can
read every card.

- **Shape first, tokens on demand.** A row of permission tokens shows three
  and "+N more". Opening it shows the rest grouped by service, one row per
  service with its verbs (`canvas: read write`), never a wall of tokens.
  A fold never hides fewer than three: "+2 more" costs a click and saves
  nothing.
- **Group by service.** Tokens read as `<service>:<verb>`. The editor shows
  one row per service with the verbs as the checkboxes. The full token stays
  in the tooltip and in `data-claim`.
- **Closed until opened.** A service catalog of twelve namespaces with five
  to twenty-five operations each starts closed. Each namespace summary
  carries its count and All / None, so a whole service is granted or
  withdrawn without expanding it.
- **Show a working-size tool list.** A resource with at most twelve tools
  opens its Tools section when editing starts. Larger lists stay folded behind
  their count, and either size remains directly collapsible by the user. All
  and None act on the exact tool list. Ticking a service permission selects
  each tool whose complete claim requirement is then satisfied; individual
  tool choices remain editable.
- **Refresh before an embedded edit.** A scene may keep Connection Hub mounted
  while its pane is hidden. Opening a card from that scene reloads the card and
  service catalog before the editor appears, so descriptor changes and drift
  review match the standalone page. A refused save stays in the editor and its
  reason appears beside the pinned actions.
- **A selection route owns its service hierarchy.** The `remote_mcp_proxy`
  transport route appears as **My MCP connectors**. Selecting it exposes the
  owner's configured MCP servers directly beneath it, and each server row
  owns its exact tools and permissions. The hierarchy explains reachability;
  the saved card keeps the route and every selected server as separate
  authority rows.
- **A permission appears only where it decides something.** The service
  permissions required by an operation live in the row's tooltip. The row
  names them only when the card does not carry one ("needs canvas:read"),
  because then the person has to act.
- **Every dense level is a disclosure.** Each card resource is a bounded,
  collapsible section. Service permissions, tools, service actions, and
  connected-account permissions have their own summaries and selected counts,
  so the grantor opens only the level being decided.
- **Client-reported data is folded and labelled.** The client's OAuth
  registration document stays behind "Client metadata", marked as reported
  by the client, never as authority.
- **Help behind information marks.** No paragraph above a form. A control
  explains itself in its label, and the rest is a tooltip.
- **An expired card stays, and stays editable.** Expiry ends the credential,
  not the grants. The card keeps its place on the list with an "expired"
  badge and one line saying how it comes back: Prolong on a connected app
  whose credential has not ended yet (the client keeps its token), Reissue
  on a manual token (a new token, in place, every grant kept), a new grant
  from the chat for a hosted agent. Revoke is the only way a card leaves.
- **Leaving a dirty edit asks once, in the widget.** Switching cards or
  leaving the editor with unsaved changes opens the widget's own dialog;
  a clean edit leaves without a question.
- **Colour carries meaning.** Teal for the interactive and for "ready", a
  muted banana yellow for a connected app and for the selected card, lilac
  for an agent, grey for a manual token, red for expired. Chips are neutral.

## What a card reads like after the first wave

```text
 Claude · named_services                        CONNECTED APP     Edit   Revoke
 CLIENT ID  https://claude.ai/oauth/mcp-oauth-client-metadata
 DOOR       named_services  CLIENT DOOR
            https://<host>/api/integrations/bundles/<tenant>/<project>/kdcube-services@1-0/public/mcp/named_services
 ACCESS     canvas:read  canvas:write  conversations:read  +35 more
 OPERATIONS ▸ 11 operations
 SERVICES   ▸ 12 services
 ACCOUNTS   Google      lena@…    docs:comment  docs:read  docs:write  +7 more
            iCloud Mail elena@…   email:read  email:send
            Slack       elena @ … slack:assistant:search  slack:channels  slack:files:read  +4 more
 APPROVED   9/10/2026, 6:31 PM · expires 3/9/2027, 5:31 PM · not renewed since consent
 ▸ Client metadata 7
```

"+35 more" opens the list grouped by service:

```text
 ACCESS     canvas         read  write
            conversations  read
            docs           comment  read  write
            drive          read  write
            …                                              show less
```

The editor keeps every choice it had and changes only how it reads:

```text
 named_services                                          Remove from card
 canvas         [x] read   [x] write
 conversations  [x] read
 docs           [x] comment  [x] read  [x] write
 …
 Operations
 [x] Named service about     named_services_about   runs  Always | Once   policy not set
 [x] Named service action    named_services_action  runs  Always | Once   policy not set
 …
 Named-service access                    Select all currently available · Clear   12 namespaces
 ▸ Canvas          KDCube canvas objects for the approving user.       All · None   5/5
 ▸ Conversations   KDCube conversations for the approving user.        All · None   5/5
 ▸ Documents       Find, inspect, read, create, and edit documents …   All · None  25/25
```

## The second wave, proposed

These change what the widget says, not only how much of it shows, so they
wait for a decision.

Done in the second wave: the tabs renamed and ordered as the story, the
editor head with a one-line summary of what the card holds, collapsible
resource sections, section heads with an info mark each (Service permissions,
Tools, Service actions, Connected accounts),
working-size tool lists open with their count, compact tool columns with
documentation behind an information mark, policy in the person's words
("Every time · Once", "choose one"), selection routes grouped with their exact
child services, actions in columns on a wide pane, Save and Cancel pinned to
the viewport while the form is in view, permission-to-tool shortcuts and exact
All / None controls, fresh embedded card summons with in-place save errors, the
resource picker in plain words,
group-by on the rail (kind, service, state), and the styled leave dialog. The
selected policy segment carries the effective state; the editor does not
repeat it on a second line.

Still open:

1. **Merge Accounts and Provider connections** into one list of providers,
   each showing its own way of asking (claims or tiers) as a detail of the
   connect step. The two state slices and consent flows stay; the tab is
   one.
2. **A card is a summary row with details.** One row per caller with the
   summary line, details opening in place; the workbench stays for editing.
3. **Catalog rows in Read / Write groups** inside a namespace, with the
   exact actions as the expandable detail.
4. **Group by metadata fields** on the rail, the client-reported fields
   the filter already knows.
5. **Authenticators shows every authority, the platform's one highlighted,
   and edits them.** Done. The tab opens with "Sign-in authorities", read
   by the administrator operation `authorities_describe`: the platform's
   selection and the runtime authenticator built from it, its login lane and
   referenced authenticator, every authority and provider of this
   app's registry with the pools it trusts (mixed mode) and the descriptor
   path each lives at, and what apps registered. Editing is a buffer
   mapped onto the real descriptor: `Edit` on a provider opens its block,
   `Validate` checks it on the server (`authority_provider_validate`:
   parsed, secret-bearing keys merged from the file where the buffer says
   `<unchanged>`, resolvable, rendered as the pane shows a provider),
   `Apply` writes it through the platform's descriptor editor
   (`authority_provider_set`: comments and every other key kept, a backup
   beside the file). The platform switch selects another provider in
   `auth.connection_hub` and keeps `auth.type: bundle` because the definition
   is in this app (`platform_sign_in_set`). "New provider from a construct"
   opens the editor with a template for each shape the platform can sign
   in with. Every edit says how it activates. A provider edit is sent
   immediately to ingress and the result says whether
   ingress received it; without a listener, the widget asks for a refresh.
   The lane switch remains refresh-only by design.
6. **Access map** is the least clear surface; operator surface; after the
   user flow and the authenticators.

## Where the rules live in code

- `ui/widgets/connections/src/components/foldRule.ts`: the fold rule, with
  tests in `tests/chip-fold.test.mjs`.
- `src/components/claimGroups.ts`: token grouping by service, with tests in
  `tests/claim-groups.test.mjs`.
- `src/components/ChipFold.tsx`: `FoldedChipRow` and `ClaimGroupsView`,
  used by the Granted access cards, the account rows and the External MCP
  tool parameters.
- `src/features/delegatedAccess/DelegatedResourceCatalog.tsx`: namespaces
  closed by default, All / None per namespace, door tokens as tooltip.
- `src/styles.css`, the block "Folded chip rows, claim groups, compact
  operations".
