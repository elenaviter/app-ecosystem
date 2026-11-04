---
id: kdcube-services.slides-typed-action-pipeline
title: "Typed presentation tools share the action pipeline"
summary: "Expose advanced Slides geometry and guarded batches while preserving shared media, artifact, account and comment behavior."
tags: [slides, mcp, productivity, connected-accounts]
keywords: [requiredRevisionId, EMU, typed tools, action pipeline, comment claims]
see_also:
  - ../../interface/README.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/recipes/connections/integrations/google-service-README.md
---

# Typed presentation tools share the action pipeline

Advanced MCP clients need native object IDs and EMU geometry for controlled
batch editing. `productivity_slides_get_structure` and
`productivity_slides_batch_update` provide that route with a mandatory revision,
an allowlist, bounded input and existing-element-only deletion. Comment writes
and slide deletion use dedicated tools with their own policy.

The ordinary typed tools reuse the SDK action pipeline so staged image/import
ownership, signed exports and comment save/read-back checks remain consistent.
Each tool enforces its own exact connected-account requirements and uses its
own tool name during credential resolution. A bound presentation ref fixes the
account; provider credentials remain server-side. The app host supplies
request-owned media/artifact factories to the typed surface explicitly.

The public Google recipe owns payload and batch details. Source tests cover
both the native adapter boundary and actual MCP discovery; real deployment
acceptance remains a separate check.
