# Journal pointer

This repository's design and build history is journaled by its
maintainers in their centralized journal store, one dated entry per
significant move. This file is the pointer index: it names the entries
that concern this repository so the history is discoverable from here.

| Date | Entry | What it holds for this repository |
| --- | --- | --- |
| 2026-08-29 | Design note: the ecosystem components repo, Prokura to PyPI first | The founding decisions: the repository's purpose and name, the Prokura naming and its rationale, the two-phase packaging shape, the stability constraints. |
| 2026-08-29 | Handoff: the Connection Hub implementation moves into the prokura package | The scope ruling (application bundles live in apps/, all docs under docs/, journals centralized with this pointer index) and the extraction plan: implementation and tests move here, KDCube re-references the package, behavior and its acceptance evidence unchanged. |

Entries are listed newest last. The store itself is not public; the
addresses here are titles, and the decisions that matter to users of this
repository are always reflected in [`docs/`](../docs/README.md).
