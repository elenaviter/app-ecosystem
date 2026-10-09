---
id: repo:app-ecosystem/products/kdcube/README.md
title: "KDCube Product Components"
summary: "KDCube applications hosted in App Ecosystem and their relationship to the platform SDK and operating procedures."
tags: [kdcube, applications, product]
keywords: [KDCube services, external app, Git source, platform SDK]
see_also:
  - apps/kdcube-services@1-0/README.md
---
# KDCube product components

[KDCube services](apps/kdcube-services@1-0/README.md) is the external KDCube
application for named-service, conversation, productivity and administration
surfaces. Its app adapters, descriptors, interface and tests live under
`apps/kdcube-services@1-0`. It consumes the separately pinned KDCube SDK.

The `procedures/` directory contains operating procedures for platform
maintenance. Connection Hub owns its own app, package and client under
`products/connection-hub` and supplies delegated authority to KDCube services.
