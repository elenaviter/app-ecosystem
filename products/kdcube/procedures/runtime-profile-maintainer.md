---
id: kdcube.procedures.runtime-profile-maintainer
title: "KDCube Maintainer Runtime Profile"
summary: "The commands for a runtime of kind kdcube that this team maintains: which action makes a change live by the tree it is in (platform refresh with package selectors, bundle reload, descriptor apply, app deploy worktree), the order when a change moves board operations, and how each action's receipt proves it loaded the commit its ref names."
status: current
tags: [procedure, kdcube, maintainer, runtime, runtime-profile, refresh, reload, deploy]
keywords: [runtime profile, kind kdcube, releases, kdcube info, bundle status --live, pb source status, MATCH MISMATCH UNKNOWN, relative gitdir, no git evidence, kdcube refresh, --build, maintainer-local-python-package, bundle reload, bundle config apply, bundles.yaml, bundles.template.yaml, sync_board_descriptor, descriptor sync, deploy worktree, activation block, --local-path, widget dist, eviction count, board operations, receipt names the commit]
see_also:
  - ./maintainer-rebuild.md
  - ./platform-suite.md
  - ../../project-board/docs/projects-runtimes-and-refs.md
  - ../../project-board/packages/project-board/src/project_board/procedures/problem-board-worker/references/runtime-actions.md
  - ../../project-board/packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md
---

# KDCube Maintainer Runtime Profile

## What this is

This is the profile for a runtime of kind `kdcube` that this team maintains:
a KDCube deployment whose platform, SDK and apps the team changes, refreshes
and reloads. The generic Problem Board worker procedure carries no runtime's
commands; they are here. The concepts (runtime, ref, profile, repository
alias) are in
[projects, runtimes and refs](repo:app-ecosystem/products/project-board/docs/projects-runtimes-and-refs.md).

A worker reaches this page through its project: `pb worker context` returns
`runtimes`, and a runtime of this kind names this document as its
`profile_ref` (`repo:app-ecosystem/products/kdcube/procedures/runtime-profile-maintainer.md`),
with `local_profile` the path to it on this host. A project that declares no
such runtime never needs this page.

**Every action releases, in each repository it loads, the commit its ref
names** (`releases`: a platform refresh loads `kdcube` and the `app-ecosystem`
packages it stages, an app reload its app's repository, a client switch
`app-ecosystem`). Before it, the coordinator integrates the
commits to go live onto each ref, pushes it and fetches it on the runtime's
machine (worker procedure,
[coordinator](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md),
Reload, refresh, restart, step 1). The action then loads that commit: the
exports a refresh stages are clean exports of it, and an app's deploy worktree
is checked out at it. **Its receipt names each of those commits**, and a receipt
that names another commit is a failed action, reported as failed with both commits.
A reload is always addressed to a commit: an app's path is its deploy
worktree at that commit, never a working checkout, whose tree at the instant
of a reload is whatever it holds.

## Which action, by the tree the change is in

Name the action by the tree the change is in; the wrong one reports a fix as
live that has never executed. The exact refresh command and its selectors are
in [the maintainer rebuild procedure](repo:app-ecosystem/products/kdcube/procedures/maintainer-rebuild.md);
use the KDCube release's own operating documentation for the exact refresh
flags supported by that installed version.

| You changed | It reaches the runtime when | Not enough |
| --- | --- | --- |
| Platform or SDK Python (`kdcube_ai_app/...`), including server-rendered OAuth consent pages | `kdcube refresh --path "$REPO" --build`, with `$REPO` a clean **git worktree** checked out at the commit the ref names (a plain export is refused: `init --path requires a local git repo`) | bare `refresh --build`, which rebuilds the old staged copy; restarting containers |
| Every `app-ecosystem` distribution the images import (read the list from the runtime's requirements files, `requirements-chat*.txt`, and what they declare, never from memory) | the same refresh, with every such distribution staged in the SAME build through `--maintainer-local-python-package DIST=SOURCE`, SOURCE a clean export of the commit the ref names, as [the maintainer rebuild procedure](repo:app-ecosystem/products/kdcube/procedures/maintainer-rebuild.md) shows | rebuilding without the selector, which keeps the published version |
| Widget `src/` | the app deploy below for the widget's bundle; the pipeline builds `dist/`, and the reload returns before that build finishes | editing `src/` alone, building widgets by hand, or reading the reload receipt as the widget being live |
| Descriptor content (`bundles.yaml`) | `bundle config apply` or `bundle reload <bundle-id>` | `refresh`, which preserves `$WORKDIR/config` |
| Secret values (`bundles.secrets.yaml`, `secrets.yaml`) on a standalone runtime whose `assembly.yaml` has `secrets.service.backend: host-vault` | the value in the private descriptor pair, then `kdcube secrets backend host-vault stage --tenant <t> --project <p> --path "$REPO"` (create-only; it refuses on a differing value), then a `refresh` (the containers get the pair at start) and the reload of each bundle whose config names the secret. Before any deploy, `stage --dry-run` must report no conflict and no missing key | editing the file only: `refresh` hands the file to the tolerant `get_secret` path, but strict reads (`get_secret_strict`, used by the W502 Card-transaction runtime) go to the vault, which nothing syncs automatically |
| An app under `apps/` | the app's **deploy worktree**, a git worktree used for nothing but deploying, is the app's only path, and the app's entry in the staged `config/bundles.yaml` carries **no `activation` block** (neither `commit` nor `require_commit`): `git -C <deploy-worktree> checkout --detach <approved-sha>`, then `kdcube bundle reload <bundle-id>`, whose receipt must read `Loaded: mounted tree at head <approved-sha>, clean`. Web requests, the Data Bus workers and a restart all load that folder, so a restart keeps the commit. The deploy worktrees and commits are on the project facts page. Setting or moving an app's folder is `kdcube bundle <bundle-id> --tenant <t> --project <p> --local-path <container-path>`, then a reload | a reload of an app whose path is a working checkout, which stages whatever that checkout holds at that instant; `activation.commit` in the descriptor, which a restart and the Data Bus workers ignore, so it is not the guarantee until W333 lands, and which a commitless reload still applies in the web proc (`Loaded: snapshot of ...`), splitting it from the Data Bus workers; `--local-path`, which keeps an existing `activation` block |
| A change that adds, removes or renames a board operation, on either side: an operation id in `project_board/contract/worker_operation_contract.py` (App Ecosystem) or a handler in `services/operation_dispatch.py` (applications) | both halves change together, in this order: (1) check the approved board commit out in its deploy worktree **without reloading**; (2) the platform rebuild that stages the matching `project-board` package (the `app-ecosystem` row above), whose restart loads the new board against the new contract; (3) verify the board's receipt and that it answers. The board refuses to import unless its handler table **equals** the contract (`operation_dispatch.py`), so any moment with one side new and the other old is an outage. Find the case with the checks below before the window is planned | a board reload alone (the new board meets the image's old contract and fails to load with `Problem Board operation policy and handler table differ`: 2026-09-26, W326, down 03:32–03:36Z); a platform rebuild before the board commit is checked out (its restart re-imports the old board against the new contract) |

The Problem Board host's own actions (relay restart, client source, procedure
install) are in the worker procedure's
[runtime actions](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/problem-board-worker/references/runtime-actions.md);
a bundle reload never restarts the relay, and a relay restart never moves a
bundle.

Copying a file into a container and restarting a container are not actions
this team has: a container-local patch is invisible to everyone, vanishes
without warning, and makes the running system disagree with the repository
while every test still passes.

## Does This Change Move Board Operations?

Run both checks before planning a window, each with the commit the runtime
runs now and the commit to deploy.

In the App Ecosystem checkout (the operation contract):

```bash
git diff --unified=0 <running-commit> <target-commit> -- \
  products/project-board/packages/project-board/src/project_board/contract/worker_operation_contract.py \
  | grep -E '^[-+][[:space:]]*"[a-z][a-z0-9_.]*": \{'
```

In the applications checkout (the board's handler table):

```bash
git diff --unified=0 <running-commit> <target-commit> -- \
  playground/domain-solution/apps/problem-board@1-0/services/operation_dispatch.py \
  | grep -E '^[-+][[:space:]]*"[a-z][a-z0-9_.]*": _'
```

Any output names an operation id that is new, removed or renamed on that side.
The window is then the ordered change in the table above: board commit checked
out without a reload, then the platform rebuild with the `project-board`
package staged, then the receipt. Output on one side only means the two sides
disagree, and the board cannot load: fix the change before any window. No
output on either side means a board reload may go on its own.

## Before the action (coordinator step 4)

Immediately before, besides the generic `git status --porcelain` and
`pb worker receive`: when the range touches `bundles.template.yaml`, diff the
touched entry against the live `config/bundles.yaml` first: a fix whose
descriptor is behind it deploys and cannot run. For the board, that diff is the
sync tool's dry run (Execute, below). Identical blocks sit under
different bundle ids in that file, so edit the live descriptor by locating the
bundle id, never by the first match of a block.

### A deploy worktree the container can read

The receipt's git evidence comes from inside the container, which sees the
deploy worktree at its bundle path (`/bundles/...`), not at the host path.
`git worktree add` writes an **absolute host path** into the worktree's `.git`
file (`gitdir: /Users/.../<checkout>/.git/worktrees/<name>`), which does not
exist in the container, so the reload loads the tree but its receipt reads
`(no git evidence: bundle_path_not_a_repository)` and names no commit
(W262 line 4 proof, dev-main 2026-09-26). Give every deploy worktree a
**relative** gitdir, which resolves on the host and in the container alike as
long as the checkout's `.git` is mounted beside it at the same relative place:

- git 2.48 or later: `git -C <checkout> worktree add --relative-paths
  <deploy-worktree> <sha>`, or for an existing one `git -C <checkout> worktree
  repair --relative-paths <deploy-worktree>`.
- Before 2.48 (dev-main runs 2.43): after `git worktree add`, rewrite the
  worktree's `.git` file with the path relative to the worktree:
  `printf 'gitdir: %s\n' "$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' <checkout>/.git/worktrees/<name> <deploy-worktree>)" > <deploy-worktree>/.git`.
  The checkout's own back-link (`<checkout>/.git/worktrees/<name>/gitdir`)
  stays a host path, which only the host reads, and `git status` on the host
  stays clean.

**Check before the window**, for each deploy worktree the window moves:
`head -1 <deploy-worktree>/.git` starts with `gitdir: ../`, never
`gitdir: /`, and `git -C <deploy-worktree> rev-parse HEAD` answers on the
host. A worktree that fails either is fixed before the window, not after its
receipt comes back without evidence.

### The host `kdcube` CLI is current

The attestations below are printed by the host's `kdcube` CLI, not by the
platform. A CLI older than the platform's attestation commits prints no
"Source Attestation" section and no per-service comparison at all, which
reads like missing evidence rather than an old tool (dev-main 2026-09-26: an
editable install from a checkout 45 commits behind). **Before the window:**
the CLI's source is at the platform commit the window releases (for an
editable install, `git -C <kdcube checkout> rev-parse HEAD` equals it and
`git -C <kdcube checkout> status --porcelain --untracked-files=no` is empty (tracked changes only: an operator's untracked files do not change the installed CLI), since a dirty
checkout is the same risk as a stale one; for a package, its version is that
release), and `kdcube bundle status <bundle-id>
--live --workdir <workdir>` on an app already loaded prints a "Source
Attestation" line. Advance the CLI first when either fails.

## Execute (coordinator step 5)

Execute the action the table names for the tree, at the commit the ref names:

- **An app:** first read its entry in the staged `config/bundles.yaml`
  (located by bundle id) and remove any `activation` block, `commit` or
  `require_commit`, because a commitless reload still applies it in the web
  proc while the Data Bus workers load the deploy worktree, and `--local-path`
  keeps it; then `git -C <deploy-worktree> checkout --detach <sha>` and
  `kdcube bundle reload <bundle-id>`.
- **The board's descriptor entry**, in every window that reloads
  `problem-board@1-0`: after the deploy worktree checkout and any
  `kdcube refresh`, and before `kdcube bundle reload problem-board@1-0`, run
  the sync tool from the deploy worktree, so it reads the template of the
  commit being deployed. Before planning the window, select an existing
  prepared Python interpreter with PyYAML and verify the import. If that
  precondition fails, stop before the window until the prepared environment
  is supplied; dependency installation is not a window step:

  ```bash
  SYNC_PY=<absolute-path-to-prepared-python>
  "$SYNC_PY" -c 'import yaml'
  DA=<board deploy worktree>
  "$SYNC_PY" "$DA/playground/domain-solution/tools/sync_board_descriptor.py" \
    --deployed <workdir>/config/bundles.yaml            # prints the difference
  "$SYNC_PY" "$DA/playground/domain-solution/tools/sync_board_descriptor.py" \
    --deployed <workdir>/config/bundles.yaml --apply    # backup, then writes
  ```

  The template is the truth for the entry,
  except the deployment's own values the tool names (path, public URLs,
  integrations); it writes only the board's lines and checks every other app
  is unchanged. A refusal names a live key the template lacks: settle it in a
  pull request, never by hand-editing the entry. After the reload run
  `kdcube bundle catalog check --workdir <workdir>`. Report descriptor drift
  according to resource authority: for `catalog`, changed selected operations
  are `in_effect_review`; for `remote_mcp`, they are
  `suspended_until_accepted`. Wording changes affect digests, but are not
  themselves a catalog authorization decision. For capability or grant changes,
  verify Card selection, the project Control Card and resource-enforced grants;
  new operations are not granted by descriptor sync or by accepting unrelated
  changes. Name actual refusals, suspensions and required owner actions in the
  announcement, using Connection Hub's owning
  [per-resource accepted-state contract](repo:app-ecosystem/docs/connection-hub/package/delegated-cards.md#per-resource-accepted-state).
  Why: the entry was hand-synced
  twice on 2026-09-26 by two different scratch scripts and still differed
  from the template (W353).
- **The platform:** `kdcube refresh --path <clean git worktree of the KDCube
  commit the ref names> --build`, with the package selectors above, each
  selector's SOURCE a clean export of the App Ecosystem commit the ref names.
- **The Problem Board client** on the same host: select only the approved
  App Ecosystem commit, under the host agreement in the worker procedure's
  [client source selection](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/problem-board-worker/references/runtime-actions.md#client-source-selection):

  ```bash
  pb source use-code \
    --repository <app-ecosystem-repository> \
    --ref <released-ref> \
    --expect <approved-app-ecosystem-sha>
  ```

  Use the full approved commit. This selects the command and relay together,
  not the KDCube platform or host CLI. Verify their source evidence separately:
  the platform's `kdcube info --workdir <workdir>` attestation and the host
  CLI precondition above; the client has `pb source status` and its matching
  relay startup record below.

Why the deploy worktree: it is the app's only path, read by web requests, the
Data Bus workers and a restart alike, and nobody edits it, so the commit
checked out there is what every process loads and a restart keeps it. The
working checkouts are never an app's path. The descriptor's
`activation.commit` is not the guarantee: a restart and the Data Bus workers
ignore it (W333), and the 2026-09-25 23:23Z window removed it.

**When one window refreshes the platform and moves an app**, check the app's
deploy worktree out at its approved commit **before** `kdcube refresh
--build`, then refresh. The refresh restarts the process, and the process
loads the app from its path at startup. A bundle reload afterwards evicts the
bundle but not submodules already cached, so the process can run new code
against old modules. That happened on 2026-09-25: the board failed with
`ImportError: card_delegable_grants` from 11:23 to 11:27Z, until a restart
(W304 U3).

Then **check the receipt against the approved candidate**: for an app the
reload's line reads `Loaded: mounted tree at head <sha>, clean`, a
`Loaded: snapshot of` line is a failed activation because a pin is still in
effect, a `Loaded: mounted tree` line with `(no git evidence: ...)` is a failed
proof because it names no commit (fix the worktree's gitdir, above, and reload
again), and `git -C <deploy-worktree> rev-parse HEAD` is the commit on disk;
the commits the refresh exported each equal the announced commit. Compare both
with the approved candidate before anything else. A receipt that names another
commit is a failed activation: report it as failed, with both commits, and
stop there.

A bundle reload returns before the widget build finishes, and a widget has
three states after a reload: build pending, no build because the signature
was unchanged and the artifact is already current, and no build because it
broke. The receipt does not tell them apart, only the verification below does.

### A narrow platform rebuild: one package fix, nothing else

When only one change in an App Ecosystem package must reach the image (a
fix the operator needs now), a refresh from `main` would also stage every
other package change since the image was built. Build the narrow candidate
from the image's own base instead:

1. **Find the commit the image was built from**, from the running artifact:
   copy each imported package out of the container (`docker cp
   <chat-proc>:/opt/venv/lib/python3.12/site-packages/<package> ...`), hash
   every `.py` as a git blob, and find the App Ecosystem commit whose package
   trees match with zero differences (`git ls-tree -r <commit> -- <package src>`
   for each candidate commit). Check all imported packages at once; they were
   staged from one commit.
2. **Cherry-pick only the reviewed fix onto that commit**, push it as a
   release branch, and show its changed lines equal the merged change's
   (`git range-diff` or a sorted line-set diff).
3. **Run the package suites inside a throwaway container of the running
   image**, pinned by its ID, never by the `latest` tag (a local build can
   move the tag while the running stack keeps an older image):
   `IMAGE_ID=$(docker inspect --format '{{.Image}}' <running chat-proc container>)`,
   then `docker run --rm --entrypoint sh -v <release tree>:/src:ro "$IMAGE_ID" ...`,
   with the release tree's package sources first on `PYTHONPATH`, so the tests
   see exactly the running image's dependencies. Record `IMAGE_ID` in the
   window ledger.
4. **Refresh with the deployed platform commit, not a newer one:**
   `kdcube refresh --path <git worktree of the deployed KDCube commit> --build`
   plus `--maintainer-local-python-package` for every imported distribution,
   each from a clean export of the release commit. `--path` must be a **git
   worktree**: a plain export is refused before anything runs
   (`init --path requires a local git repo`).
5. When the same window moves an app, check its deploy worktree out before the
   refresh (above).

**After the refresh**, relay channels fail with `oauth_challenge_not_advertised`
and 502/503 for about three minutes while chat-proc warms up, then reopen.
That is expected; a failure that continues past that is not. `kdcube info`
compares the *selected* with the *deployed* platform version; a mismatch that
predates the window (the selection moved without a refresh) is reported as
such, not as this window's failure.

## Verify in the running artifact (coordinator step 6)

Verify with the platform's attestations, which compare versions and symbols,
never timestamps (W31). Run all three after every window that moved anything,
for the tenant and project's workdir
(`~/.kdcube/kdcube-runtime/<tenant>__<project>`):

1. **Platform containers:** `kdcube info --workdir <workdir>` (add `--json`
   for a script). For each
   running service it compares the configured image, the latest recorded
   build and the running container's image ID, and the selected platform
   source version with the one recorded for the running image. A service
   whose source is not the commit the refresh released, or that reads
   `UNKNOWN` (an image built before the receipts existed, or one this CLI did
   not build or pull), is a failed proof.
2. **Each app the window moved**, at least `problem-board@1-0` and
   `connection-hub@1-0` when they moved: `kdcube bundle status <bundle-id>
   --live --json --workdir <workdir>`. It compares the staged descriptor's
   commit with the commit chat-proc prepared and with the commit embedded in
   the published widget (`descriptor.commit`, `chat-proc.source.commit`,
   `widget.source.commit`). **`MATCH`** is the proof; **`MISMATCH`** (the
   command exits nonzero) or **`UNKNOWN`** (evidence missing, for example a
   widget build still running) is a stop: report the fields and values it
   names and do not call the window done. **A descriptor-only apply** (a
   config change with the commit unchanged) is not proven by `MATCH`, which
   compares commits: when the change touches `delegated_catalog`, run
   `kdcube bundle catalog check --workdir <workdir>`, and otherwise read the changed config value
   back from the running bundle and name it.
   **When it adds operations,** they reach an agent of a project that has a
   Control Card only once that Control Card includes them: a project Control
   Card caps every agent Card on the project (AND), and a Card refresh is
   capped by it too; a project without one gets them on the next refresh. After the catalog
   check passes, a project admin ticks the new operations on the project Control
   Card (Team > People), and only then are agents' Cards refreshed (Refresh
   coordinator Card, Make coordinator or Make worker). Otherwise the operation
   is refused with `work_worker_operation_withheld_by_control_card`, which
   names that Control Card.
3. **The Problem Board host client**, on each host whose client moved:
   `pb source status`, and the relay's first stamped startup line
   (`source=snapshot`, `app_ecosystem=<sha>`): both equal the
   App Ecosystem commit the client switch released. The platform source is
   proved separately by step 1, not by a client-selection argument.

These replace the hand checks this section used to prescribe (a symbol asked
of the process, `dist/` read inside the container): the attestations read the
same evidence from what is running and name what they compared. A reload's
eviction count is still no proof of anything outside the bundle: it says
nothing about platform packages, so a reload never applies a change to one.
For a change whose effect the attestations cannot see (a behaviour, an
endpoint), also probe that endpoint unauthenticated or ask the running process,
and name what you asked.

Say what loaded (coordinator step 7): per repository the ref, the commit it
named, and the commit range; the attestation results (`MATCH` per bundle, the
source per service, the client commits); and the eviction count or the
restarted containers.

## Sync secret values on demand

The operator may ask for it at any time (board message to the coordinator, a work item, or Telegram); the coordinator
routes it to the console agent of the runtime's host (deploys run only there, per the project environment file). On a
standalone runtime whose `assembly.yaml` has `secrets.provider: secrets-file` and `secrets.service.backend: host-vault`
(the shadow state of [Host Vault](repo:kdcube/app/ai-app/docs/service/secrets/host-vault-README.md)), the file pair is the
source of truth and the vault holds only staged copies. Nothing mirrors either direction automatically.

- **File to vault (create-only):** `kdcube secrets backend host-vault stage --tenant <t> --project <p> --path "$REPO" --dry-run --json`,
  report its counts (matched, missing, differing) to the operator, then the same command without `--dry-run`. The stage
  checks every key before the first write, creates only absent keys and refuses on any differing value.
- **A differing value:** stop. Report the key names (never values or digests) and ask the operator which side is
  authoritative per key; nothing is overwritten without that decision.
- **Vault to file:** `kdcube secrets export`, an administrator ceremony the operator confirms in the browser each
  time; an agent prepares it and never completes it alone.
- No value, length or digest is printed, exported or passed on a command line, in any direction.

## Who clears a refresh

`kdcube refresh --build` rebuilds the operator's stack, so it is theirs to
clear, like a host service definition. Restarting a service whose definition
exists is a coordinated runtime action and needs no more than the
coordinator's list.

## Incidents this profile carries

- **2026-10-09 14:08-14:45Z, W502 Card transactions refused after the deploy.** The W502 runtime was deployed but never activated: its hosting configuration (Problem Board `connection_hub` service IDs and canonical secret refs, the Hub's `connections.card_transactions` caller) and the protected approval tables were missing, so every person Card or role change answered 503. After the configuration and a new receipt secret were added to `bundles.secrets.yaml` and the runtime refreshed, the strict secret reads still failed: the host vault lacked the receipt key and both peer-proof keys, because nothing stages the file into the vault. A deploy that ships W502 code also ships its activation configuration and tables, and a secret change includes the vault stage (the Secret values row above).
- **2026-09-22 23:52Z, a rebuild without announcement.** The coordinator
  merged KDCube #261 and ran `kdcube refresh --build` straight after, because
  the operator was waiting; three workers were mid-flight and two lost their
  channels for about five minutes. A refresh is announced and ready is
  collected before it runs, whoever is waiting.
- **2026-09-23 03:43Z, a bare rebuild took the published connection-hub.**
  `kdcube refresh --build` without `--maintainer-local-python-package` for
  every first-party package installed the published connection-hub, which has
  no `server_side_login`. chat-ingress and chat-processor died, the web proxy
  answered 502, and every relay channel reported
  `oauth_challenge_not_advertised` for thirteen minutes. The rebuild command
  is the documented maintainer one, and exit 0 is not verification.
- **2026-09-25 11:23-11:27Z, `ImportError: card_delegable_grants`:** a reload
  after a refresh kept cached submodules (Execute, above).
- **2026-10-06 15:38-15:47Z, a narrow package rebuild.** A Connection Hub
  package fix had to go live while `main` carried many unqualified package
  changes. The image's base was found by matching its installed package files
  to one commit, the fix was cherry-picked onto it, and the refresh used the
  deployed platform commit (A narrow platform rebuild, above). The first
  attempt passed a plain export as `--path` and was refused before any change.
- **2026-10-06 15:47:56Z, a reload during an operator's live test.** A
  seconds-long board reload, announced to agents, coincided with the operator
  pressing Edit on a Card. The Hub's permission check got the board's
  `ApplicationNotReadyError` and showed the operator as read-only. Tell the
  operator before a reload when they are testing, and treat "the board is
  restarting" as its own state in the UI.
- **2026-09-26 03:32-03:36Z, W326:** a board reload alone met the image's old
  operation contract (Does This Change Move Board Operations?, above).
