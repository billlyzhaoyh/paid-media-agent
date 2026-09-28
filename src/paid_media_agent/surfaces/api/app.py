"""Small authenticated API: threads, proposals, receipts, artifacts, jobs, health."""

import hmac
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from paid_media_agent.domain.common import JsonValue
from paid_media_agent.reports.bridge import ArtifactBridge, BridgeError
from paid_media_agent.scheduler import JobBusy, Scheduler, UnknownJob
from paid_media_agent.surfaces.api.views import outcome_view
from paid_media_agent.surfaces.runner import AgentRunner, RunOutcome, ThreadAccessDenied
from paid_media_agent.tools.writes import WriteDenied


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class EditIn(BaseModel):
    changes: dict[str, JsonValue]


class RejectIn(BaseModel):
    message: str = Field(default="", max_length=500)


def resolve_caller(token_map: dict[str, str], authorization: str | None) -> str | None:
    """Constant-time bearer token lookup. Tokens never leave this function."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    presented = authorization[7:].strip()
    for token, caller in token_map.items():
        if hmac.compare_digest(token, presented):
            return caller
    return None


def create_app(runtime: Any, *, scheduler: Scheduler | None = None) -> Any:
    from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

    settings = runtime.settings
    token_map = settings.api_token_map()
    runner = AgentRunner(
        agent=runtime.agent,
        service=runtime.components.proposal_service,
        receipts=runtime.profile.receipts,
        threads=runtime.threads,
        executor=runtime.components.write_executor,
    )
    bridge = ArtifactBridge(runtime.profile.workspace_root / "out")
    app = FastAPI(title="Paid Media Agent", version="0.1.0")

    def caller(authorization: str | None = Header(default=None)) -> str:
        if not token_map:
            raise HTTPException(status_code=503, detail="PAID_MEDIA_API_TOKENS is not configured")
        resolved = resolve_caller(token_map, authorization)
        if resolved is None:
            raise HTTPException(status_code=401, detail="invalid bearer token")
        return resolved

    def _outcome(outcome: RunOutcome) -> dict[str, Any]:
        return outcome_view(outcome).model_dump(mode="json")

    if (
        settings.slack_transport == "http"
        and settings.slack_signing_secret
        and settings.slack_bot_token
    ):
        from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

        from paid_media_agent.surfaces.slack.service import build_slack_service
        from paid_media_agent.surfaces.slack.socket_mode import build_bolt_app

        transport = AsyncSlackRequestHandler(build_bolt_app(settings, build_slack_service(runtime)))

        @app.post("/slack/events")
        async def slack_events(request: Request) -> Any:
            return await transport.handle(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "persistence": runtime.persistence,
            "catalog_revision": runtime.catalog.revision,
            "catalog_source": runtime.catalog.source,
            "selection": runtime.components.metadata.selection,
            "writes_enabled": settings.paid_media_writes_enabled,
            # Platforms whose live catalog did not load; their tools are missing until restart.
            "catalog_failures": {
                platform.value: reason
                for platform, reason in getattr(
                    runtime.profile.catalog_provider, "failures", {}
                ).items()
            },
        }

    @app.get("/jobs", dependencies=[Depends(caller)])
    def jobs() -> dict[str, Any]:
        if scheduler is None:
            raise HTTPException(status_code=404, detail="jobs run only under `serve`")
        return {
            "jobs": list(scheduler.names),
            "recent": [run.as_json() for run in scheduler.history()],
        }

    @app.post("/jobs/{name}", dependencies=[Depends(caller)])
    async def run_job(name: str) -> dict[str, Any]:
        if scheduler is None:
            raise HTTPException(status_code=404, detail="jobs run only under `serve`")
        try:
            run = await scheduler.run_now(name)
        except UnknownJob:
            raise HTTPException(status_code=404, detail=f"unknown job {name}") from None
        except JobBusy:
            raise HTTPException(status_code=409, detail=f"{name} is already running") from None
        return run.as_json()

    @app.post("/threads/{thread_id}/messages")
    async def post_message(
        thread_id: str, body: MessageIn, who: str = Depends(caller)
    ) -> dict[str, Any]:
        try:
            outcome = await runner.send(thread_id=thread_id, caller_ref=who, text=body.text)
        except ThreadAccessDenied:
            raise HTTPException(
                status_code=403, detail="thread belongs to another caller"
            ) from None
        return _outcome(outcome)

    @app.get("/proposals")
    def list_proposals(
        state: str = "awaiting_approval", limit: int = 50, who: str = Depends(caller)
    ) -> dict[str, Any]:
        """Proposals awaiting a decision, including the budget bandit's; approvers only."""
        if who not in runtime.profile.approval_policy.approver_refs:
            raise HTTPException(status_code=403, detail="only approvers can list proposals")
        if state != "awaiting_approval":
            raise HTTPException(status_code=422, detail="state must be awaiting_approval")
        views = runner.pending_proposals(max(1, min(limit, 200)))
        return {"proposals": [v.model_dump(mode="json") for v in views]}

    @app.get("/proposals/{proposal_id}")
    def get_proposal(proposal_id: UUID, who: str = Depends(caller)) -> dict[str, Any]:
        view = runner.proposal(proposal_id)
        if view is None:
            raise HTTPException(status_code=404, detail="unknown proposal")
        if view.requester_ref != who and who not in runtime.profile.approval_policy.approver_refs:
            raise HTTPException(status_code=403, detail="not visible to this caller")
        receipt = runner.receipt(proposal_id)
        return {
            "proposal": view.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json") if receipt else None,
        }

    @app.post("/proposals/{proposal_id}/approve")
    async def approve(proposal_id: UUID, who: str = Depends(caller)) -> dict[str, Any]:
        try:
            outcome = await runner.approve(proposal_id=proposal_id, approver_ref=who)
        except WriteDenied as exc:
            # State conflicts are 409; every other denial is about the caller.
            conflicts = {"conversation_expired", "not_awaiting_approval", "unknown_proposal"}
            status = 409 if exc.reason in conflicts else 403
            raise HTTPException(
                status_code=status, detail=f"{exc.reason}: {exc.detail}".rstrip(": ")
            ) from None
        return _outcome(outcome)

    @app.post("/proposals/{proposal_id}/reject")
    async def reject(
        proposal_id: UUID, body: RejectIn, who: str = Depends(caller)
    ) -> dict[str, Any]:
        try:
            outcome = await runner.reject(
                proposal_id=proposal_id, actor_ref=who, message=body.message
            )
        except WriteDenied as exc:
            raise HTTPException(
                status_code=409, detail=f"{exc.reason}: {exc.detail}".rstrip(": ")
            ) from None
        return _outcome(outcome)

    @app.post("/proposals/{proposal_id}/edit")
    def edit(proposal_id: UUID, body: EditIn, who: str = Depends(caller)) -> dict[str, Any]:
        try:
            view = runner.edit(proposal_id=proposal_id, editor_ref=who, changes=body.changes)
        except WriteDenied as exc:
            raise HTTPException(
                status_code=409, detail=f"{exc.reason}: {exc.detail}".rstrip(": ")
            ) from None
        return {"proposal": view.model_dump(mode="json")}

    @app.get("/threads/{thread_id}/artifacts/{name}")
    async def artifact(thread_id: str, name: str, who: str = Depends(caller)) -> Response:
        if runtime.threads.owner(thread_id) != who:
            raise HTTPException(status_code=403, detail="thread belongs to another caller")
        if "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="invalid artifact name")
        allowed = await runner.report_files(
            thread_id=thread_id, caller_ref=who, artifacts=runtime.profile.artifacts
        )
        if name not in allowed:
            raise HTTPException(status_code=404, detail="artifact is not part of this thread")
        try:
            receipt = bridge.validate(runtime.profile.workspace_root / "out" / name)
            data = bridge.open_bytes(receipt)
        except BridgeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return Response(content=data, media_type=receipt.media_type)

    return app
