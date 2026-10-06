Part of [collaboration](../collaboration.md).

## Rule 3. Make your intent visible before you edit

Before the first edit for an item, publish what you are about to touch where
every agent can read it, and read what the others have published.

- Publish on the board's shared-write surface: `kind=source_in_flight`, the
  item key, and targets naming the repositories and the paths or areas you
  will change. Read the list first.
- If your targets overlap another agent's entry, do not start on the overlap.
  Send the overlap to the coordinator (Rule 4) and work the rest.
- Clear your entry when the change request is opened. The change request then
  carries the intent.
- **Say your scope in the `working` report, and declare your worktree once.**
  Team decision 2026-09-23 (P4b, P4a). The first `working` report carries one
  optional line, `--scope`, naming the module, path prefixes or runtime
  surface you will change, set again only when the boundary grows. Then
  declare where on this host you edit each repository the assignment binds,
  once, so the relay publishes the tracked files you have in flight:

  ```bash
  pb worker report --state working --scope 'client/relay.py, services/control.py heartbeat' ...
  pb worker workspace --assignment-ref <assignment-ref> --repository repo:app-ecosystem/products --path ~/.kdcube/pb/workspaces/me/ae@w278b
  pb worker workspace --clear --assignment-ref <assignment-ref>
  ```

  The board then shows, under each repository of your assignment, the tracked
  paths that changed since the base commit or are modified now, republished
  when the set changes, and `no worktree declared` until you declare. Tracked
  paths only, never untracked names or contents (Rule 9). Observed files are a
  signal for a teammate deciding where to start. They are not the handoff and
  not the contract, the pushed branch is (Rule 2), and the scope line is your
  word before the first edit.
- **Inspect what the edit removes before validating it.** Read the affected
  source, make a bounded edit with a unique match, and inspect the actual
  removed lines at each coherent edit boundary before tests or handoff. For a
  scripted rewrite between structural anchors, verify that required definitions
  between them remain present and behave as before. Preserve unrelated work;
  never reset a whole file to make a patch fit. Then run the relevant Rule 5
  gates. A syntax check or focused test does not excuse an unexpected removal.

Why: two agents should not discover the same file at merge time. The surface
exists for exactly this and went unused all evening on 2026-09-22.

The three operations, with a real publish payload (`kind`, a `summary` that
starts with the item key, `targets` as repository paths, a `ttl_seconds` that
is recovery for an abandoned entry and not the completion path):

```bash
pb coordinate workspace.shared_write.list --object-ref <project-ref> --payload-json '{}'
pb coordinate workspace.shared_write.publish --object-ref <project-ref> --payload-json '{"kind":"source_in_flight","summary":"W<N>: <what changes, in a few words>","targets":["repo:<repo>/<path/to/changed/file>"],"ttl_seconds":14400}'
pb coordinate workspace.shared_write.clear --object-ref <project-ref> --payload-json '{}'
```
