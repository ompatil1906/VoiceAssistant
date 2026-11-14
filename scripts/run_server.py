#!/usr/bin/env python3
#
# Copyright (c) 2026, QuantumLoopAI
# SPDX-License-Identifier: BSD 2-Clause License
#

"""EMMA Voice Gateway HTTP server for production deployment (e.g. Render).

Provides:
- GET / and GET /health: Health checks for container orchestration and uptime monitoring.
- POST /api/voice/token: Generates a LiveKit JWT room token for the caller participant
  and automatically launches the EmmaAgent in the background to join the room and answer the call.
- POST /api/voice/agent/start: Explicitly launches an EmmaAgent session for a room.
- CORS support enabled for all origins, allowing communication with Netlify-hosted frontends.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

load_dotenv(REPO_ROOT / ".env")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from livekit import api
from pydantic import BaseModel, Field

from emma.config import RuntimeConfig
from emma.security.redaction import redact
from run_agent_session import EmmaAgent, resolve_provider_plan


def log(msg: str) -> None:
    print(redact(f"[Gateway] {msg}"), flush=True)


def warn(msg: str) -> None:
    print(redact(f"[Gateway WARNING] {msg}"), file=sys.stderr, flush=True)


# Track currently running agent tasks per room
active_agents: dict[str, asyncio.Task[Any]] = {}


async def _run_agent_session(room_name: str, caller_data: dict[str, Any]) -> None:
    """Run an EmmaAgent session as an asynchronous background task."""
    log(f"Spawning voice assistant agent for room: '{room_name}'")
    try:
        plan = resolve_provider_plan(RuntimeConfig.from_env())
        agent = EmmaAgent(room_name, caller_data, plan=plan)
        await agent.run()
        log(f"Agent session completed cleanly for room: '{room_name}'")
    except asyncio.CancelledError:
        log(f"Agent session cancelled for room: '{room_name}'")
    except Exception as exc:
        warn(f"Agent session in room '{room_name}' encountered an error: {type(exc).__name__}: {exc}")
    finally:
        active_agents.pop(room_name, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log("EMMA Voice Gateway initialized and ready")
    yield
    log("EMMA Voice Gateway shutting down. Cancelling active sessions...")
    for room, task in list(active_agents.items()):
        task.cancel()
    if active_agents:
        await asyncio.gather(*active_agents.values(), return_exceptions=True)
    log("Shutdown complete.")


app = FastAPI(
    title="EMMA Voice Gateway",
    version="1.0.0",
    description="Real-time voice agent gateway and LiveKit token coordinator for EMMA",
    lifespan=lifespan,
)

# Enable CORS for Netlify and other client origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CallerIntake(BaseModel):
    firstName: str | None = None
    lastName: str | None = None
    email: str | None = None
    mobile: str | None = None
    reason: str | None = None


class TokenRequest(BaseModel):
    room_name: str | None = None
    caller: dict[str, Any] | None = Field(default_factory=dict)


@app.get("/")
async def root():
    return {
        "service": "EMMA Voice Gateway",
        "status": "online",
        "active_rooms": len(active_agents),
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "active_rooms_count": len(active_agents),
        "rooms": list(active_agents.keys()),
    }


@app.post("/api/voice/token")
async def issue_token(req: TokenRequest):
    """Mint a LiveKit join token for the caller and start an agent for the room."""
    api_key = os.getenv("LIVEKIT_API_KEY", "emma_local")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    livekit_url = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")

    if not api_secret:
        raise HTTPException(
            status_code=500,
            detail="LIVEKIT_API_SECRET is not configured on the gateway server",
        )

    room_name = req.room_name or f"emma-room-{uuid.uuid4().hex[:8]}"
    caller_data = req.caller or {}
    first_name = caller_data.get("firstName") or "Caller"
    caller_identity = f"caller-{uuid.uuid4().hex[:8]}"

    # Mint JWT token with LiveKit grants
    try:
        token = (
            api.AccessToken(api_key, api_secret)
            .with_identity(caller_identity)
            .with_name(first_name)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )
    except Exception as exc:
        warn(f"Failed to generate LiveKit JWT: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to generate room token: {exc}")

    # Launch the agent task if not already running for this room
    if room_name not in active_agents or active_agents[room_name].done():
        task = asyncio.create_task(_run_agent_session(room_name, caller_data))
        active_agents[room_name] = task

    return {
        "token": token,
        "livekit_url": livekit_url,
        "room_name": room_name,
        "identity": caller_identity,
    }


@app.post("/api/voice/agent/start")
async def start_agent(req: TokenRequest):
    """Explicitly launch an agent session for a given room."""
    if not req.room_name:
        raise HTTPException(status_code=400, detail="room_name is required")

    room_name = req.room_name
    caller_data = req.caller or {}

    if room_name in active_agents and not active_agents[room_name].done():
        return {"status": "already_running", "room_name": room_name}

    task = asyncio.create_task(_run_agent_session(room_name, caller_data))
    active_agents[room_name] = task
    return {"status": "started", "room_name": room_name}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("scripts.run_server:app", host="0.0.0.0", port=port, reload=False)
