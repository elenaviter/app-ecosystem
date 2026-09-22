from __future__ import annotations

from typing import Any, Mapping


class DomainError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "domain_error")
        self.status = int(status)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": dict(self.details),
        }


def worker_attendance_conflict(
    *,
    current_project_ref: str,
    requested_project_ref: str,
    current_project_title: str = "",
) -> DomainError:
    """Build the stable refusal used by both service and atomic store checks."""

    current_label = current_project_title or current_project_ref
    return DomainError(
        "work_worker_already_attends_project",
        (
            f"The worker already attends {current_label}. "
            "Unlink it there before linking it to another project."
        ),
        status=409,
        details={
            "current_project_ref": current_project_ref,
            "current_project_title": current_project_title,
            "requested_project_ref": requested_project_ref,
            "required_action": "unlink_current_project",
        },
    )


def require(value: Any, code: str, message: str, *, status: int = 400) -> Any:
    if value in (None, "", [], {}):
        raise DomainError(code, message, status=status)
    return value


__all__ = ["DomainError", "require", "worker_attendance_conflict"]
