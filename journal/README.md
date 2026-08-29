# Journal pointer

This repository's design and build history is journaled by its
maintainers in their centralized journal store, one dated entry per
significant move. This file is the pointer index: it names the entries
that concern this repository so the history is discoverable from here.

| Date | Entry | What it holds for this repository |
| --- | --- | --- |
| 2026-08-29 | Design note: the ecosystem components repo, Prokura to PyPI first | The founding decisions: the repository's purpose and name, the Prokura naming and its rationale, the two-phase packaging shape, the stability constraints. |
| 2026-08-29 | Handoff: the Connection Hub implementation moves into the prokura package | The scope ruling (application bundles live in apps/, all docs under docs/, journals centralized with this pointer index) and the extraction plan: implementation and tests move here, KDCube re-references the package, behavior and its acceptance evidence unchanged. |
| 2026-08-29 | Prokura extraction boundary and execution plan | The measured dependency boundary, Prokura and host ownership, compatibility rule, staged implementation plan, and acceptance gates. |
| 2026-08-29 | Prokura portable authority core moved | The first implementation slice: host-neutral authority, card, catalog, OAuth, provider, Hub, and metadata modules moved; exact compatibility aliases and standalone test evidence recorded. |
| 2026-08-29 | Prokura host ports and delegated automation access | The second implementation slice: focused host capabilities replace platform imports, and Prokura becomes the sole owner of delegated automation-card policy while KDCube supplies adapters. |
| 2026-08-29 | Connection Hub implementation history archive | The 15 dated app-local records preserved when the built-in KDCube copy was retired. |
| 2026-08-29 | Prokura package and Connection Hub cutover | The completed offline source cutover, built-in retirement, documentation ownership, and full regression evidence; live Git-loaded verification remains pending. |
| 2026-08-30 | Standalone Connection Hub architecture and execution baseline | The post-extraction product boundary, `app-foundation` ownership, canonical-app versus standalone-wrapper dependency direction, service-consumer flow, missing contracts, implementation slices, and conformance gates. |
| 2026-08-30 | Current Connection Hub cross-repository capability map | The verified import/runtime graph after extraction: host-neutral Prokura imports, retained KDCube vocabulary, the KDCube-hosted canonical app, concrete infrastructure bindings, and the standalone-host gap. |
| 2026-08-30 | Connection Hub uses KDCube as its first supported host | The release-shape decision: KDCube plus the external app descriptor is the supported starter; standalone hosting and infrastructure extraction are deferred. |
| 2026-08-30 | External service integration boundary for Connection Hub | The supported KDCube-managed gateway pattern for non-KDCube backends, and the service registration, workload identity, and admission API still required for direct integration. |
| 2026-08-30 | Direct external protected-service admission contract | The concrete resource-server contract for direct backend calls: independent bearer and workload proofs, service/resource binding, one shared live decision service, bounded projection, and implementation slices. |
| 2026-08-30 | Direct external protected-service admission implemented | The shipped KDCube-hosted endpoint, portable Prokura signing and response contract, shared live card/catalog evaluator, pairwise identity projection, canonical architecture and storage map, and offline conformance evidence; live deployment verification remains pending. |
| 2026-08-30 | Both packages live on PyPI | prokura 0.0.1 (the extracted authority modules) and app-foundation 0.0.1 (the host-neutral foundation layer, name and scope claimed) published; built from a committed snapshot, smoke-tested before upload. |
| 2026-08-30 | The foundations family published | service-foundation 0.0.1 and harness-foundation 0.0.1 join prokura and app-foundation on PyPI; scope statements in each package README, name and boundaries claimed. |

Entries are listed newest last. The store itself is not public; the
addresses here are titles, and the decisions that matter to users of this
repository are always reflected in [`docs/`](../docs/README.md).
