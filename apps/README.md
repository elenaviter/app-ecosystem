# Applications

This folder holds public application bundles: complete apps that run on
[KDCube](https://github.com/kdcube/kdcube) and demonstrate the components
of this repository as live implementations.

First resident: the **Connection Hub** product, hosted as the technical app id
`connection-hub@1-0`. Its portable Python contracts are shipped in the
`connection-hub` distribution. Its screens let users connect accounts, issue
and edit identity cards, review what each caller did, and answer a caller's
denial with the exact edit that resolves it.

An application bundle here is registered in a KDCube deployment by its
repository, ref, and subdirectory. Its documentation lives under
[`../docs/`](../docs/README.md) beside the component it belongs to.
