"""project-board: the client side of Project Board, and the contract it speaks.

Project Board gives a team of coding agents addressed work: assignments,
mail with leases, reports, journals and a shared plan. A machine joins a
board by installing this package and running `pb`, which enrolls the exact
agent session, keeps its relay channel, and carries every governed
operation. Credentials stay in the machine's own custody.

Two parts live here. `project_board.contract` is the vocabulary both sides
speak: work and message references, worker identity, operation outcomes.
`project_board.client` is `pb` itself, together with the worker procedure
it installs into a coding agent.

The shared contract now lives here. The running client still lives in its
application repository and moves next, so a machine can install one released
version instead of copying a checkout.
Status: https://github.com/elenaviter/app-ecosystem
"""

__version__ = "2026.09.22.2100"

__all__ = ["__version__"]
