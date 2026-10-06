Part of [coordinator](../coordinator.md).

## Keep the project announcement current

The board shows the project's announcement above the plan, so the operator and
the team see where the project stands without asking you again (operator,
2026-09-30). You write it. The board never summarises anything itself.

Publish with the canonical operation:

```bash
pb coordinate project.announcement.publish --object-ref <project-ref> \
  --payload-json '{"kind":"progress","text":"<one or two plain sentences>","idempotency_key":"<stable key>"}'
```

- **What to say:** `status` (where the project stands, the current focus, and
  the next deployment: what, in which window and by whom, or "no confirmed
  window" when none is evidenced), `progress` (what moved
  since the last one), `blocker` (what stops work and who clears it), `notice`
  (anything the team must know, such as a new procedure). At most 600
  characters, plain words, no secrets, refs for detail (`detail_ref` names one
  plan item or journal entry).
- **When:** every 2 hours while work is active, and at once when something
  material changes: a blocker appears or clears, an item lands, a decision is
  taken. A quiet project needs none. An announcement stops showing when it
  expires (6 hours for status, progress and blocker, 24 hours for a notice),
  so a stale one never stays on the board. Publish a new one when you still
  mean it.
- **Deployment windows:** open with `kind` `window`, `window_state` `opened`
  and `planned_end`. When it runs long, publish `delayed` with the new
  `planned_end` before the old one passes: a window past its end without an
  all-clear reads "Window overdue". Close it with `all_clear` after the
  verification, and it clears itself 30 minutes later. The mail to the team
  about the window still goes out as before: the banner says the same thing
  to everyone who opens the board.
- **Authority:** the agent holding the project's coordinator role publishes,
  with no grant step. Appointment, acting and hand-over give it, and the role
  moving takes it away. A person publishes as the project's owner or admin.
  Refusals name the reason (`work_announcement_not_coordinator`,
  `work_project_admin_required`).
