---
id: connection-hub/frontend/application/ux-story-and-surfaces
title: "Connection Hub UX: The Story And The Surfaces"
summary: "How a person is meant to read Connection Hub (who am I here, what of mine may KDCube use, who may use my KDCube), the reading rules every surface follows (fold long token lists, group by service, details on demand), what the first wave changed, and the proposed second wave (tab story, card summary rows) awaiting sign-off."
status: active
tags: ["connection-hub", "ux", "widget", "granted-access", "delegated-cards", "design"]
keywords: ["connection hub tabs", "granted access card", "fold chips", "claim groups", "named-service access", "invocation policy", "user story"]
updated_at: 2026-09-10
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

Today the widget shows, in this order: External MCP, Identity, Delegated to
KDCube, Provider connections, Delegated by KDCube, Access map,
Authenticators. Two of them answer question 2 twice (one by claims, one by
connector-app tiers), and "delegated to" versus "delegated by" asks the
reader to keep a direction in mind that the three questions make
unnecessary.

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
- **A token appears only where it decides something.** The door tokens an
  operation rides on live in the row's tooltip. The row names them only
  when one is not ticked on the door ("needs canvas:read"), because then
  the person has to act.
- **Client-reported data is folded and labelled.** The client's OAuth
  registration document stays behind "Client metadata", marked as reported
  by the client, never as authority.
- **Help behind information marks.** No paragraph above a form. A control
  explains itself in its label, and the rest is a tooltip.
- **An expired card stays.** Expiry ends the token, not the grants. The card
  keeps its place on the list with an "expired" badge and one line saying
  how it comes back: Renew on a manual token (a new token, in place, every
  grant kept), reconnect from the client for a connected app, grant again
  from the chat for a hosted agent. Revoke is the only way a card leaves.

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

1. **Tabs tell the story.** Order and names: Identity · Accounts · External
   MCP · Granted access · (operator) Access map · Authenticators. "Granted
   access" is what the panel already calls itself. "Accounts" merges
   Delegated to KDCube and Provider connections into one list of providers,
   each provider showing its own way of asking (claims or tiers) as a
   detail of the connect step rather than as a tab. The two state slices
   and consent flows stay; the tab is one.
2. **A card is a summary row with details.** The list shows one row per
   caller: name, kind, door, "38 permissions on 12 services · 3 accounts ·
   11 operations", approved and expiry, Edit and Revoke. Details open in
   place. The workbench (rail plus editor) stays for editing.
3. **Policy wording.** "runs Always | Once" and "policy not set" name a
   real state (an operation granted without a policy is refused until one
   is chosen). The row should say so in the person's words: "How may this
   run? Every time · Once", and "choose one" instead of "policy not set".
4. **Catalog rows in Read / Write groups.** Inside a namespace, operations
   grouped as Read and Write with one checkbox per group and the exact
   operations as the expandable detail, the way consent windows already
   speak (see the grant vocabulary rule in the platform's capability
   surfaces).

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
