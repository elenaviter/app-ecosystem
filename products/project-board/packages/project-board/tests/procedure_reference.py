"""Read a worker procedure reference whole, including the modules of a split one (W563).

coordinator.md and collaboration.md are indexes over one module per section,
so a one-rule change reloads one module. A test that checks the reference's
text reads the index followed by its modules, in the index's order.
"""

from __future__ import annotations

import re
from pathlib import Path

_MODULE_LINK = re.compile(r"\]\(((?:coordinator|collaboration)/[^)#\s]+\.md)\)")


def reference_text(path: str | Path) -> str:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.name in {"coordinator.md", "collaboration.md"} and (path.parent / path.stem).is_dir():
        modules = dict.fromkeys(path.parent / link for link in _MODULE_LINK.findall(text))
        text = "\n".join([text, *(module.read_text(encoding="utf-8") for module in modules)])
    return text
