"""Rawaj Pay transfer API. See README.md for the endpoints, errors and rules."""

import json
import math
import threading
import uuid
from collections.abc import Generator
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import Account, IdempotencyRecord, Transfer
from app.settings import Settings


class AuthError(Exception):
    """Raised when an API token is missing or invalid."""


class TransferBody(BaseModel):
    """Strict input schema for creating a transfer."""

    model_config = ConfigDict(strict=True, extra="forbid")

    from_account_id: str
    to_account_id: str
    amount_piastres: int = Field(gt=0)
    memo: str | None = Field(default=None, max_length=140)


def problem(
    status: int,
    title: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Return an RFC 9457 problem response."""
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
    }
    if detail is not None:
        body["detail"] = detail

    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )


def transfer_json(transfer: Transfer) -> dict[str, Any]:
    return {
        "id": transfer.id,
        "from_account_id": transfer.from_account_id,
        "to_account_id": transfer.to_account_id,
        "amount_piastres": transfer.amount_piastres,
        "memo": transfer.memo,
        "status": transfer.status,
    }


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI()

    engine = make_engine(settings.database_url)
    app.state.engine = engine
    session_factory = make_session_factory(engine)

    # Token bucket state is kept per customer, not per API token.
    buckets: dict[str, tuple[float, float]] = {}
    bucket_lock = threading.Lock()

    def get_session() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    def get_customer_id(
        authorization: str | None = Header(default=None),
    ) -> str:
        if authorization is None:
            raise AuthError()

        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not token
            or token not in settings.api_tokens
        ):
            raise AuthError()

        return settings.api_tokens[token]

    @app.exception_handler(AuthError)
    async def handle_auth_error(request: Request, exc: AuthError):
        response = problem(
            401,
            "Unauthorized",
            "A valid Bearer token is required.",
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    def check_rate_limit(customer_id: str) -> JSONResponse | None:
        """Consume one token, or return a 429 response."""
        now = settings.clock()
        capacity = settings.rate_limit_capacity
        refill_rate = settings.rate_limit_refill_per_second

        with bucket_lock:
            if customer_id in buckets:
                tokens, last_time = buckets[customer_id]
                tokens = min(capacity, tokens + max(0.0, now - last_time) * refill_rate)
            else:
                tokens = float(capacity)

            if tokens < 1.0:
                buckets[customer_id] = (tokens, now)
                seconds = math.ceil((1.0 - tokens) / refill_rate)
                return problem(
                    429,
                    "Too Many Requests",
                    "Transfer rate limit exceeded.",
                    headers={"Retry-After": str(max(1, seconds))},
                )

            buckets[customer_id] = (tokens - 1.0, now)

        return None

    def save_idempotency_response(
        session: Session,
        customer_id: str,
        key: str,
        request_json: str,
        status: int,
        response_json: dict[str, Any],
    ) -> None:
        session.add(
            IdempotencyRecord(
                id=uuid.uuid4().hex,
                customer_id=customer_id,
                key=key,
                request_json=request_json,
                response_status=status,
                response_json=json.dumps(
                    response_json,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )

    @app.get("/v1/accounts/{account_id}")
    def get_account(
        account_id: str,
        customer_id: str = Depends(get_customer_id),
        session: Session = Depends(get_session),
    ):
        account = session.scalar(
            select(Account).where(
                Account.id == account_id,
                Account.owner_id == customer_id,
            )
        )

        if account is None:
            return problem(404, "Not Found", "Account not found.")

        return {
            "id": account.id,
            "balance_piastres": account.balance_piastres,
            "currency": account.currency,
        }

    @app.post("/v1/transfers")
    async def create_transfer(
        request: Request,
        customer_id: str = Depends(get_customer_id),
        session: Session = Depends(get_session),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
    ):
        # Authentication is resolved first. Rate limiting happens before
        # checking the idempotency key or parsing/validating the body.
        limited = check_rate_limit(customer_id)
        if limited is not None:
            return limited

        if idempotency_key is None or idempotency_key == "":
            return problem(400, "Bad Request", "Idempotency-Key is required.")

        try:
            raw_body = await request.json()
            body = TransferBody.model_validate(raw_body)
        except (ValueError, ValidationError):
            return problem(422, "Unprocessable Entity", "Invalid transfer body.")

        if body.amount_piastres > settings.max_transfer_piastres:
            return problem(
                422,
                "Unprocessable Entity",
                "amount_piastres exceeds the configured maximum.",
            )

        # Canonical JSON lets equivalent JSON formatting replay the same request.
        request_json = json.dumps(
            body.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        response_headers: dict[str, str] = {}

        with session.begin():
            existing = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.customer_id == customer_id,
                    IdempotencyRecord.key == idempotency_key,
                )
            )

            if existing is not None:
                if existing.request_json != request_json:
                    return problem(
                        422,
                        "Unprocessable Entity",
                        "Idempotency-Key was already used with a different request.",
                    )

                replay_body = json.loads(existing.response_json)
                return JSONResponse(
                    status_code=existing.response_status,
                    content=replay_body,
                    headers={"Idempotent-Replayed": "true"},
                )

            def remember(
                status: int,
                response_body: dict[str, Any],
            ) -> None:
                save_idempotency_response(
                    session,
                    customer_id,
                    idempotency_key,
                    request_json,
                    status,
                    response_body,
                )

            if body.from_account_id == body.to_account_id:
                response_body = {
                    "type": "about:blank",
                    "title": "Unprocessable Entity",
                    "status": 422,
                    "detail": "Source and destination accounts must be different.",
                }
                remember(422, response_body)
                response_headers = {"Content-Type": "application/problem+json"}
                result = JSONResponse(
                    status_code=422,
                    content=response_body,
                    media_type="application/problem+json",
                )

            else:
                source = session.scalar(
                    select(Account).where(
                        Account.id == body.from_account_id,
                        Account.owner_id == customer_id,
                    )
                )

                if source is None:
                    response_body = {
                        "type": "about:blank",
                        "title": "Not Found",
                        "status": 404,
                        "detail": "Source account not found.",
                    }
                    remember(404, response_body)
                    result = JSONResponse(
                        status_code=404,
                        content=response_body,
                        media_type="application/problem+json",
                    )
                else:
                    destination = session.get(Account, body.to_account_id)

                    if destination is None:
                        response_body = {
                            "type": "about:blank",
                            "title": "Unprocessable Entity",
                            "status": 422,
                            "detail": "Destination account not found.",
                        }
                        remember(422, response_body)
                        result = JSONResponse(
                            status_code=422,
                            content=response_body,
                            media_type="application/problem+json",
                        )

                    elif source.balance_piastres < body.amount_piastres:
                        response_body = {
                            "type": "about:blank",
                            "title": "Conflict",
                            "status": 409,
                            "detail": "Insufficient funds.",
                        }
                        remember(409, response_body)
                        result = JSONResponse(
                            status_code=409,
                            content=response_body,
                            media_type="application/problem+json",
                        )

                    else:
                        source.balance_piastres -= body.amount_piastres
                        destination.balance_piastres += body.amount_piastres

                        transfer = Transfer(
                            id=f"trf_{uuid.uuid4().hex[:28]}",
                            from_account_id=source.id,
                            to_account_id=destination.id,
                            amount_piastres=body.amount_piastres,
                            memo=body.memo,
                            status="completed",
                        )
                        session.add(transfer)
                        session.flush()

                        response_body = transfer_json(transfer)
                        remember(201, response_body)
                        result = JSONResponse(
                            status_code=201,
                            content=response_body,
                        )

        return result

    @app.get("/v1/transfers/{transfer_id}")
    def get_transfer(
        transfer_id: str,
        customer_id: str = Depends(get_customer_id),
        session: Session = Depends(get_session),
    ):
        transfer = session.get(Transfer, transfer_id)
        if transfer is None:
            return problem(404, "Not Found", "Transfer not found.")

        source = session.get(Account, transfer.from_account_id)
        destination = session.get(Account, transfer.to_account_id)

        source_owned = source is not None and source.owner_id == customer_id
        destination_owned = (
            destination is not None and destination.owner_id == customer_id
        )

        if not source_owned and not destination_owned:
            return problem(404, "Not Found", "Transfer not found.")

        return transfer_json(transfer)

    return app