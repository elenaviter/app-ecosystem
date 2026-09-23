---
id: applications.playground.problem-board.skill-reference.project-report
title: Answer A Project Report Request
summary: The coordinator's procedure for a project.report request: a capped delta the service composes, previewed, published, and announced only on the service's receipt.
tags: [procedure, problem-board, coordinator, project-report]
keywords: [project.report, preview, publish, published, field_project_report_refused, intent_not_receipt, not-seen]
see_also:
  - ./coordinator.md
---

# Answer A Project Report Request

Read this when a `project.report` message is in your inbox. Only the
project's coordinator receives one. It arrives with its own lease.

**What a report is.** A delta, never a census: at most twenty items, newest
first, each with the reason it is there. The service composes every factual
section from rows it owns, because the work items live in PostgreSQL and not on
your machine. You do not count items, diff statuses, or read the plan to answer.

| Reason | The item is in the report because |
| --- | --- |
| `moved` | its status changed after the previous report |
| `blocked` | an assignee reported it blocked, with the reason they gave |
| `cancelled_dependency` | it is unfinished and depends on cancelled work |
| `mentioned` | you named it in your summary |
| `dependency_of_moved` | an item that moved depends on it, and it is not done |

**What you write.** The summary: prose for a person who was not in the room,
in the shape `docs/project-status-reports.md` describes under How a report
reads. Name an item by its key (`W42`) and it joins the report as `mentioned`
even if it did not move, which is how you put a quiet problem in front of the
operator. Add `--not-seen` for anything you could not see or reach, because a
partial answer that does not say so reads as a complete one.

**The order, every time.**

1. Look before you write. `preview` composes the report on the service and
   stores nothing:

   ```bash
   pb worker project-report preview \
     --project-ref <project-ref> --message-ref <message-ref> --lease-id <lease-id>
   ```

2. Write the summary from what it shows. Put it in a file.
3. Publish, and wait for the answer. The command waits for it by default:

   ```bash
   pb worker project-report publish \
     --project-ref <project-ref> --message-ref <message-ref> --lease-id <lease-id> \
     --summary-file <summary.md> --not-seen "<what you could not reach>" \
     --attach <evidence-file>
   ```

4. Read `published` in the result. It decides what you may do next:

   | Result | What it means | What you do |
   | --- | --- | --- |
   | `published: true` | The service stored the report and the board shows it. | Run the settle command it printed, once. Then you may say the report is published. |
   | error `field_project_report_refused` | The service refused it. Nothing was published. `details.error` carries the service's code and the field. | Do not settle. The lease is the only way to publish again. Fix the cause and publish again. |
   | `published: false`, `intent_not_receipt: true` | It is queued and the service has not answered. It can still refuse. | Do not settle and do not announce. Run the `status` command it printed until it says published or refused. |

5. When a source you need does not answer, record that outcome with
   `pb worker project-report fail` and settle. A failed report is an honest
   answer. A report announced and never stored is not.

**An outbox id is not a receipt.** For every queued control in this system the
id says the machine wrote your intent down, and only the service's answer says
what happened. Say "published" when the result says `published: true`, and at
no other time.
