from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence

from connection_hub_cli.errors import ClientConfigurationError


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""


class CommandRunner(Protocol):
    def available(self, executable: str) -> bool: ...

    def run(self, argv: Sequence[str]) -> CommandResult: ...


class SubprocessCommandRunner:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds

    def available(self, executable: str) -> bool:
        return shutil.which(executable) is not None

    def run(self, argv: Sequence[str]) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClientConfigurationError(
                "client_command_failed",
                f"The {argv[0]} client command could not be completed.",
            ) from exc
        return CommandResult(returncode=completed.returncode, stdout=completed.stdout)
