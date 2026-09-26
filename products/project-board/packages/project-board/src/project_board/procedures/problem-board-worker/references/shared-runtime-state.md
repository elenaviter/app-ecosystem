---
id: project-board.skill-reference.shared-runtime-state
title: Shared Runtime State
summary: What may live in Redis, when shared state is written, and how a projection recovers after Redis is restarted or rolled back. Operator rulings.
tags: [procedure, problem-board, worker, design, review, redis, durability]
keywords: [redis, projection, cache, registry, discovery, publish, write on change, read-through, run_id, rollback, snapshot restore, durable source]
---

# Shared Runtime State

Read this when you design, implement or review anything that stores, caches or
publishes shared runtime state: a Redis key, a registry, a discovery record, a
projection of a durable row. These are operator rulings of 2026-09-21. Apply
them in your own work, and treat a violation you find in review as a blocker.

## What Redis may hold

Redis holds only projections that can be rebuilt from a durable source, and
state whose loss costs a retry (a lock, a one-time pointer, a rate window).
Disposable temporary state stays in Redis and needs no escalation: a pending
device login, a one-time pointer, a short approval window. Only durable state
escalates, a standing login, a refresh family, a grant, custody, anything
whose loss locks someone out. The operator, 2026-09-23, on a device approval
store: "it is a temp thing, it is disposable, I do not need it in Postgres,
like any other thing like this."
Durable facts go to the durable store (Postgres, or the component's durable
file store). Secrets and one-time secret material go to the secrets manager.
Why: a Redis loss or snapshot restore must never lose or revive a fact.

## When shared state is written

A write happens only when its source changed, at the moment it changed: an
install, an update, a configuration change, a user action that alters the
fact. Nothing else writes: requests, turns, bundle or instance loads,
scheduler runs, data-bus events, heartbeats and timers. That includes a
check that decides whether to write, and a background republish loop.
Why: a write tied to traffic multiplies Redis work by the request rate.
A timer adds writes that no change asked for.

## How a projection recovers

Each projection names its durable source. On a miss, or on a record that is no
longer valid, the reader reads through to that source and answers from it.
Refilling the projection at that moment is the only other write, and a lost
projection is the change that causes it. A projection with no durable source
to read through to is a design gap. Find the source before you build the
projection.
Why: recovery that waits for traffic or a timer can deadlock, because the
request that would republish may itself need the projection.

## Rollback fence

A Redis restart or snapshot restore brings back old keys. A projection record
carries the Redis `run_id` it was written in. A reader reads `INFO server` and
the record in one transaction, and treats a record from another run as a miss
(then reads through). A conditional replace compares versions only within the
same run.
Why: a restored record can look newer than the durable truth.

## Review checklist

- For every write: which change triggers it? If the answer is a request, a
  load or a timer, it is a blocker.
- For every projection: which durable source does it read through to on a
  miss, and does a test cover a Redis restart followed by a lookup with no
  other traffic?
- For every record: is it fenced to the run that wrote it?
