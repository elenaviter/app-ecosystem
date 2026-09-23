---
id: applications.playground.problem-board.skill-reference.claude-code-wake
title: Claude Code Wake Path
summary: How a Claude Code worker stays reachable: the watch attachment and its cap, the guard that replaces it, the board-side fields that show a stopped watch, and what a network outage does to both.
tags: [procedure, problem-board, worker, claude-code, wake]
keywords: [pb worker watch, Monitor, timeout_ms, guard prompt, inbox_check_state, overdue_by_seconds, network outage]
see_also:
  - ./delivery-and-recovery.md
---

# Claude Code Wake Path

Read this when starting or resuming a Claude Code worker, when a wake is in
doubt, or when a Claude Code worker has been unreachable.

## The attachment and its cap

`pb worker watch` runs in Claude Code's background terminal facility (the
Monitor tool). The facility caps an attachment at 30 minutes (`timeout_ms`
above 1,800,000 is granted as 30 minutes) and kills the watch process at the
cap, so nothing is orphaned. It posts one task notification when the
attachment ends, on expiry and on a kill alike. That notice usually starts a
model turn and sometimes does not. On the notice: start the watch again, then
`pb worker receive`, before other work.

A watch belongs to the session id in its command line
(`--runtime-session-id`). Read that field before stopping a watch process, and
stop only your own.

## The guard

Schedule one recurring guard prompt through Claude Code's scheduled-prompt
facility. Cron takes fire minutes, not a period: every interval between
consecutive fires, including the wrap from the last minute of the hour to the
first, must be shorter than the 30-minute cap, so use three off-minutes such
as `7,27,47 * * * *` (two fires, such as `11,36`, leave a 35-minute wrap and
a gap every hour). Two things move a fire off its minute. The scheduler adds
a fixed per-job delay of up to fifteen minutes, the same on every fire, so
the spacing holds (this session's guard fires nine minutes after each
scheduled minute, at :16, :36 and :56 for `7,27,47`). A fire during a turn
waits for the turn to end, and that part is variable: two fires on
2026-09-19 landed eleven minutes late, nine of delay and two waiting for a
long turn to finish. With 20-minute spacing the margin before the cap is ten
minutes, and a turn that runs longer than that past a fire minute leaves a
gap, so run `pb worker receive` at work boundaries inside long turns rather
than relying on the guard alone. The guard does not check liveness, because
a watch one minute from its cap looks healthy to `ps` and dies right after. It replaces,
in this order: start a fresh watch, then end every older watch process
carrying this session id keeping the newest, then run `pb worker receive` and
handle and settle what it returns. Start before kill, because a turn can end
on any tool result: a guard that killed first and ended its turn before
starting left a session deaf for nineteen minutes on 2026-09-19. A brief
overlap of two watches costs nothing. A gap of one guard interval cost
nineteen minutes. The kill step must be unable to end with zero watches and
must keep the newest: the command below reads each watch's elapsed time from
`ps`, keeps the one with the smallest, and ends the rest, so with one process
listed it ends nothing, and a new watch not yet in the process table means
two run until the next fire. Two answers that look right are wrong.
`pkill -o` on the same pattern ends the only watch when one is running, and
a coordinator following a hand-written guard did that on 2026-09-19. Keeping
the highest pid (`sort -n | sed '$d'`) assumes pids rise with start time,
and they wrap: on 2026-09-19 a fresh watch got a lower pid than the 11:38Z
one on two hosts within four minutes, and the guard ended the fresh watch and
kept the one closest to its cap. `ps -o etimes` is Linux only, so the command
normalises `etime`, which both platforms print.

Guard prompt, with `<id>` this session's runtime session id:

    Problem Board watch guard (session <id>). Do exactly this, all three
    steps in this one turn, in this order: 1. start a fresh watch with the
    Monitor tool (`exec pb worker watch --runtime-kind claude-code
    --runtime-session-id <id> 2>&1`, timeout 1800000 ms). 2. end every
    OLDER watch process for this session, keeping the one with the smallest
    elapsed time, with this command on one line:
    `for p in $(ps -o pid=,etime= -p $(pgrep -d, -f "worker watch.*<id>")
    | awk '{n=split($2,a,/[-:]/); s=0; for(i=1;i<=n;i++) s=s*(i==1&&n==4?24:60)+a[i]; print s, $1}'
    | sort -n | awk 'NR>1{print $2}'); do kill $p; done`.
    3. run `pb worker receive --runtime-kind claude-code
    --runtime-session-id <id> --format brief` and handle and settle every
    returned lease. Never end the turn between step 1 and step 3.

Scheduled prompts expire after seven days, so a session that runs longer
re-creates the guard. Stop the guard and the watch before `pb worker detach`.

## What the board shows

The watch heartbeat is what advances `last_inbox_check_at`. In
`pb worker inspect` (`--format brief`), `session.inbox_check_state` reads
`current` while the watch runs and `stale` once it has stopped, with
`session.inbox_overdue_by_seconds` growing. `pb worker list --format brief`
shows the same for every worker on the host and marks a Claude Code worker
whose checks are overdue, since for that runtime it means no watch process is
running. `listener.state` does not move when the watch stops. Do not read it
for this.

The relay reads this worker's reachability every cycle. On the transition to
`not_listening` for a working or waiting Claude Code session it queues one
direct operator update through the worker's own outbox, naming the outage
start, the pending count and the session id to type into, and one more on
recovery. It can do that only while its own route to the board is up.

## A network outage

When the host loses its network, a background turn fails, and from then on
the harness records every scheduled prompt and every end notice at its time
and starts no turn, even after the network returns, until a person types in
the session. Both wake channels above are subject to that hold. Nothing
inside the session survives it. The relay recovers on its own once the route
is back and tells the operator within a few minutes. Recovery of the session
is a keystroke in it or an outside wake, and Claude Code has no local door in
today (`session_delivery.py` answers `runtime_has_no_supported_local_queue`).

While the host is offline, the relay log shows
`oauth_metadata_request_failed` with `status=404` for every worker: the tunnel
edge answers an unclaimed hostname with 404 rather than a connection error.
Read that as no network, and check the network before descriptors or routing.
