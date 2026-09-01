"""capabilities-foundation: the capabilities agents and apps use, as plain Python.

Web search, document rendering (markdown to pdf, pptx, docx, html),
isolated code execution, browser automation. A capability here works
classically: importable and callable from any Python code, with no agent
machinery inside. It depends only downward, on `app-foundation` contracts
(storage, config and secret resolution, work paths, observability),
received as arguments rather than imported from a host.

Turning a capability into an agent tool is the harness's job
(`harness-foundation`): binding, tool identity, routing, recorded results.
Connecting capabilities to an application is the host's act, one layer up;
no package here points back at a host. Authority models stay in
`connection_hub`.

This version claims the name; the running implementations live inside
KDCube today. Status: https://github.com/elenaviter/app-ecosystem
"""

__version__ = "2026.09.01.1230"
