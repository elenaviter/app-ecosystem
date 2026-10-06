"""Read a worker procedure reference whole, including the modules of a split one (W563).

SKILL.md reads as the entrypoint followed by the act modules it links to.

coordinator.md and collaboration.md are indexes over one module per section,
so a one-rule change reloads one module. A test that checks the reference's
text reads the index followed by its modules, in the index's order.
"""

from __future__ import annotations

import re
from pathlib import Path

_MODULE_LINK = re.compile(r"\]\(((?:coordinator|collaboration)/[^)#\s]+\.md)\)")
# The skill's act modules (W563): each section moved out of SKILL.md keeps a
# trigger and a link there and its full text in the module.
SKILL_ACT_MODULES = (
    "references/start-or-resume.md",
    "references/source-and-review.md",
    "references/foundations-and-procedure-gaps.md",
)


_SIBLING_LINK = re.compile(r"\]\((?!\.\./|https?:|repo:|#|references/)([^)\s]+\.md(?:#[^)\s]*)?)\)")


def _skill_relative(text: str) -> str:
    return _SIBLING_LINK.sub(r"](references/\1)", text)


def reference_text(path: str | Path) -> str:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.name == "SKILL.md":
        # The skill is its entrypoint plus the act modules it routes to, as a
        # split reference is its index plus its modules.
        routed = [path.parent / module for module in SKILL_ACT_MODULES if f"]({module})" in text]
        # A module links its siblings by bare name; read in the skill's frame,
        # a pinned sentence is the same wherever it lives.
        return "\n".join([text, *(_skill_relative(module.read_text(encoding="utf-8")) for module in routed)])
    if path.name in {"coordinator.md", "collaboration.md"} and (path.parent / path.stem).is_dir():
        modules = dict.fromkeys(path.parent / link for link in _MODULE_LINK.findall(text))
        text = "\n".join([text, *(module.read_text(encoding="utf-8") for module in modules)])
    return text
