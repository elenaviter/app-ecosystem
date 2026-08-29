# Applications

This folder holds public application bundles: complete apps that run on
[KDCube](https://github.com/kdcube/kdcube) and demonstrate the components
of this repository as live implementations.

First resident: the **Connection Hub** application, the
frontend of Prokura. It is the working proof of the package: the same
boundary and client contracts the `prokura` package exports are what this
app's screens drive: connecting accounts, issuing and editing identity
cards, reviewing what each caller did, and answering a caller's denial
with the exact edit that resolves it.

An application bundle here is registered in a KDCube deployment by its
repository, ref, and subdirectory. Its documentation lives under
[`../docs/`](../docs/README.md) beside the component it belongs to.
