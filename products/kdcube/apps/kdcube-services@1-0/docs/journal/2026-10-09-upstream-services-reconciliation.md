---
id: kdcube-services.external-source-upstream-reconciliation
title: External service source follows the complete paired platform contract
summary: Reconcile the external app with the merged signed-media and Docs changes rather than copying one helper onto an older SDK.
status: source-qualified-release-pending
tags: [kdcube-services, source-ownership, deployment, migration]
---

# External service source follows the complete paired platform contract

The initial application extraction was taken from platform
`c101ba873bb1ea049216339056f60a02c780cd92`. Before migration review, the
complete app was reconciled with the upstream merge
`de146a8f4427f17359bc8b38c7303ea2418a51bb` ([platform PR #305](https://github.com/kdcube/kdcube/pull/305)).
This carries signed provider-fetch media handling, its route/interface tests,
and the paired Docs operations. Copying only the download helper would leave
new entrypoint calls on an incompatible SDK contract.

All 34 application Python files exactly match that merge. The external
application retains its ownership metadata, stable identity, Git descriptor
coordinates, test configuration and original license. Source validation against
the corresponding platform plus the inventory/source-precedence fix passes
131 app tests and four shared import-contract checks. These checks do not
establish a runtime release or live provider acceptance.

The platform compatibility copy stays during deployment migration. Pin both
reviewed application and platform commits, qualify the actual installed image
and service surfaces, and move later application maintenance here. Removal of
the compatibility copy follows migration of existing installations.
