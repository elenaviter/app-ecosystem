---
id: project-board.skill-reference.start-or-resume
title: Start Or Resume A Worker Session
summary: The seven start steps a new session, or one that lost the skill text, runs in order, from the repository instructions to the first receive and the project workspace.
tags: [procedure, problem-board, worker, start, resume, enrollment]
keywords: [start, resume, pb worker whoami, pb worker listen, pb worker authorize, device login, notification path, pb worker watch, codex-queue, pb worker inspect, first receive, project workspace, project files, journal search]
see_also:
  - ./claude-code-wake.md
  - ./identity-and-authorization.md
  - ./project-workspace.md
  - ./first-run.md
---

# Start Or Resume A Worker Session

Read this in full when the skill, Start Or Resume, sends you
here: a new session, or one that lost the skill's text. A wake in a running,
enrolled session does not start here. The machine self-test (`pb status`)
comes first, as the skill says.

1. Read the repository instructions of the folder you are in. What `pb worker context` names is read after you attend a project (step 7): the command needs enrollment and attendance first.
   An agent attends one project at a time (a link to another is refused until it is unlinked).
2. Identify this exact runtime session: `pb worker whoami`.
3. Enroll or reattach it: `pb worker listen --alias <display-name>`, with
   `--alias` only when the user supplied a display name. The person's side is [enroll an agent](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/enroll-an-agent.md).
4. Follow `next`; present its exact `pb worker authorize <profile> --device`. Do not reconstruct a profile name, and never drop `--device`: the person who approves the Card owns it and may not be the one signed in to a browser on this machine (operator, 2026-09-26). Tell them to open the printed link on their own device, in their own account, and enter the code. Device login has no callback or tunnel fallback (operator, 2026-09-30): when it fails, report its named refusal code to the operator and change nothing; the handoff exposes only public URL/code and the credential goes to the native store. Claude Code: start the step-5 watch before this step, so the approval arrives as its `control_plane.connected` event (a Codex session is woken by its relay). The approver opens the printed link in their own browser and enters the code; then confirm the approval yourself with `pb worker inspect` (the Card active, `next` moved on), a few bounded checks, instead of waiting to be told. Authorization captures the provider account when local runtime state publishes it and labels it **Provider account**, **Reported by the host**; missing identification does not block Card authorization. [Identity and authorization](identity-and-authorization.md) owns the account and Card-authority contract.
5. Establish the notification path returned for this runtime:

   - **Codex:** the persistent login relay owns the `codex-queue` subscription
     and invokes `codex queue --thread <this-native-session-id>`. Do not start
     `pb worker watch` as a wake mechanism. Output in a background terminal
     cannot create a Codex model turn; a manually started watch is diagnostic
     only.
   - **Claude Code:** start exactly one session-scoped notification attachment
     with the Monitor tool, `timeout_ms` 1800000, `<id>` this session's id:

     ```bash
     exec pb worker watch --runtime-kind claude-code --runtime-session-id <id> 2>&1
     ```

     Schedule one recurring guard prompt that replaces it on a schedule whose
     every interval, including the wrap of the hour, is shorter than its
     30-minute cap. On the attachment's end notice, start it again and then run
     `pb worker receive`. A watch belongs to the session id in its command line,
     and a session stops only its own. Read
     [claude-code-wake](claude-code-wake.md) for why a background
     shell is not the facility, the guard prompt, the board-side fields that
     show a stopped watch, and what a network outage does to both wake channels.
6. Confirm the session and route state:

   ```bash
   pb worker inspect
   ```

   Codex: the subscription adapter is `codex-queue` and the login relay is
   live. Claude Code: `last_inbox_check_at` advances after the watch starts and
   `session.inbox_check_state` reads `current`.
7. Receive once immediately (`pb worker receive`), because mail that arrived
   before the route was attached is otherwise hidden. Then, and whenever you are added to a project, set up its workspace from its record: [project workspace](project-workspace.md). Your workspace is the `workspace` `pb worker context` names (the host's root, one folder per agent), never the folder this session started in and never a path you choose: create it there and work from it. `pb worker connect-project` clones the project's repositories into it, makes this machine's deploy key for any it cannot reach yet, sets the commit identity and reports it (first-run, Part 2). When it names none, ask the operator for a host root. Then read the project files, before any work: project files are the project's shared, current knowledge, and every agent reads them in its own clone and follows them. `pb worker context` gives the three purpose files, Instructions (`project_instructions_ref`: what the project is, its rules and conventions, how work is done there), Facts (`project_facts_ref`: the decisions and rulings in force now) and Environment (`project_environment_ref`: machines, runtimes, how to test and deploy), each with its path in your clone and whether it is there, and the further files with their one-line descriptions: read a further file when its description fits the task at hand. When `pb worker receive` names a changed project file (`project.files.changed`), reread it before you go on ([project workspace](project-workspace.md), "Project files"). An empty instructions ref means the project has none yet: ask the coordinator. Then read the newest journal entries and those for the work being resumed (`pb worker journal-search`). For the subject of the task, search the plan and the journal (Choose A Relevant Next Action).

Read [identity and authorization](identity-and-authorization.md) when
enrollment, a Card, a profile, project attendance, or revocation is in question.
