from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

from connection_hub_cli.models import HelperLaunch


STATE_DIRECTORY_ENV = "CONNECTION_HUB_STATE_DIR"


@dataclass(frozen=True, slots=True)
class StatePaths:
    root: Path

    @classmethod
    def default(cls) -> StatePaths:
        configured = os.environ.get(STATE_DIRECTORY_ENV, "").strip()
        if configured:
            return cls(Path(configured).expanduser())
        return cls(Path(user_config_dir("connection-hub", appauthor=False)))

    @property
    def profiles(self) -> Path:
        return self.root / "profiles.json"

    @property
    def installations(self) -> Path:
        return self.root / "client-installations.json"

    @property
    def host(self) -> Path:
        return self.root / "host.json"

    @property
    def oauth_sessions(self) -> Path:
        return self.root / "oauth-sessions.json"


def resolve_helper_launch(argv0: str | None = None) -> HelperLaunch:
    invoked = Path(argv0 or sys.argv[0]).expanduser()
    if invoked.name == "connection-hub":
        if invoked.exists():
            return HelperLaunch(command=str(invoked.resolve()))
        installed = shutil.which("connection-hub")
        if installed:
            return HelperLaunch(command=str(Path(installed).resolve()))

    return HelperLaunch(
        command=str(Path(sys.executable).resolve()),
        prefix_args=("-m", "connection_hub_cli"),
    )
