from __future__ import annotations

from typing import Annotated, Any, Protocol

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from reference_service.admission_client import AdmissionClient, AdmissionResult
from reference_service.settings import Settings


class AdmissionEvaluator(Protocol):
    async def evaluate(
        self,
        *,
        delegated_bearer: str,
        operation: str,
    ) -> AdmissionResult: ...


class CustomerSearch(BaseModel):
    query: str = Field(default="", max_length=100)


CUSTOMERS = (
    {"id": "customer-101", "name": "Northwind Labs", "status": "active"},
    {"id": "customer-102", "name": "Contoso Research", "status": "active"},
)


def _bearer(authorization: str) -> str:
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return value.strip()


def create_app(
    *,
    settings: Settings | None = None,
    admission: AdmissionEvaluator | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    evaluator = admission or AdmissionClient(resolved_settings)
    app = FastAPI(title="Prokura direct-admission reference service")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/customers/search")
    async def search_customers(
        payload: CustomerSearch,
        authorization: Annotated[str, Header()] = "",
    ) -> Any:
        delegated_bearer = _bearer(authorization)
        if not delegated_bearer:
            return JSONResponse(
                status_code=401,
                content={
                    "ok": False,
                    "error": {
                        "code": "delegated_bearer_missing",
                        "message": "A delegated bearer is required.",
                    },
                },
            )

        decision = await evaluator.evaluate(
            delegated_bearer=delegated_bearer,
            operation="customers.search",
        )
        if not decision.allowed:
            return JSONResponse(
                status_code=decision.status_code,
                content=dict(decision.payload),
            )

        # Prokura establishes delegated authority. This service still owns its
        # domain rule: only active customer records participate in this search.
        query = payload.query.casefold().strip()
        matches = [
            row
            for row in CUSTOMERS
            if row["status"] == "active"
            and (not query or query in row["name"].casefold())
        ]
        principal = decision.payload.get("principal")
        return {
            "ok": True,
            "principal": principal if isinstance(principal, dict) else {},
            "customers": matches,
        }

    return app


__all__ = ["create_app"]
