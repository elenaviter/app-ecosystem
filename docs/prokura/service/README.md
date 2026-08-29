---
id: prokura-service-hosting
title: Prokura Service Hosting
summary: Defines the current KDCube-hosted Connection Hub service and the boundary of a future standalone wrapper around the same canonical application.
tags: [prokura, connection-hub, hosting, service]
keywords: [KDCube host, standalone service, application, admission endpoint]
see_also:
  - ../connection-hub-architecture.md
  - ../recipes/direct-protected-service.md
  - ../../../apps/connection-hub@1-0/README.md
---

# Prokura service hosting

The current supported service is the
[`connection-hub@1-0`](../../../apps/connection-hub@1-0/README.md) application
running on KDCube. The app is canonical: KDCube supplies authenticated user
sessions, descriptor properties and secret resolution, durable storage, Redis
protocol state, locks, HTTP routes, lifecycle, and widget serving; Prokura
supplies delegated-access authority policy.

External protected services do not need to run inside KDCube. They use the
app's direct admission operation through the [protected-service
recipe](../recipes/direct-protected-service.md).

A future `services/connection-hub` standalone launcher will wrap the same app
contracts rather than become their owner. It requires the planned generic host
contracts in `app-foundation` and `service-foundation`; no standalone launcher
is shipped by this repository today.
