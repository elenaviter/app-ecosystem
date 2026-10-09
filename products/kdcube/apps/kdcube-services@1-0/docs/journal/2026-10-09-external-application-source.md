---
id: kdcube-services@1-0/journal/2026-10-09-external-application-source
title: "KDCube Services Gains An Independent App Source"
summary: "The app is extracted into the KDCube product boundary in App Ecosystem, allowing explicit Git deployment with platform examples disabled."
tags: [application, source, deployment, migration]
keywords: [external KDCube services, Git source, disabled examples, stable app ID]
see_also:
  - ../../README.md
  - ../../release.yaml
---
# KDCube services gains an independent app source

KDCube services can be selected through its own Git coordinates while all
platform examples are disabled. Keeping the same `kdcube-services@1-0` ID
preserves MCP URLs, named-service grants, widgets, signed transfers and callers.

The extraction copies the app from `kdcube/kdcube` commit
`c101ba873bb1ea049216339056f60a02c780cd92` into
`products/kdcube/apps/kdcube-services@1-0`. App-local imports remain relative;
SDK implementations and `sdk://` widgets remain in the separately pinned
platform. App adapters, configuration, interface and tests are maintained here.

The original platform copy is retained for installations using image defaults
during the release migration. Removal follows qualification of the external
app and an explicit migration of those descriptors. Deployment needs a
platform that retains explicit Git sources when their IDs match disabled
examples; an app-directory copy alone cannot establish that loader behavior.
