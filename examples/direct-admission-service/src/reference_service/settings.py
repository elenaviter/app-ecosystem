from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    admission_url: str
    service_id: str
    service_secret: str
    resource: str

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            admission_url=os.environ.get("CONNECTION_HUB_ADMISSION_URL", "").strip(),
            service_id=os.environ.get("PROKURA_SERVICE_ID", "").strip(),
            service_secret=os.environ.get("PROKURA_SERVICE_SECRET", ""),
            resource=os.environ.get("PROKURA_RESOURCE", "").strip(),
        )
        missing = [
            name
            for name, value in (
                ("CONNECTION_HUB_ADMISSION_URL", settings.admission_url),
                ("PROKURA_SERVICE_ID", settings.service_id),
                ("PROKURA_SERVICE_SECRET", settings.service_secret),
                ("PROKURA_RESOURCE", settings.resource),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing required settings: {', '.join(missing)}")
        return settings


__all__ = ["Settings"]
