"""REST API + web app.

    uvicorn multiagent.api:app --reload      then open http://localhost:8000/

Env:
    AGENTS_FILE      agent store path (default ./agents.json)
    TOOLS_FILE       HTTP tool store path (default ./http_tools.json)
    TOOL_MODULES     comma-separated modules to import for @tool registration
                     (default multiagent.builtin_tools)
    STT_MODEL / TTS_MODEL / TTS_VOICE   voice WebSocket settings (see speech.py)
    USERS_FILE / ADMIN_USERNAME / ADMIN_PASSWORD / AUTH_TOKEN_HOURS   login (see auth.py)

Every API route and WebSocket requires a logged-in user (see auth.py). Public: POST /auth/login,
the web app's static files (so the login page can load) and /docs.

WebSockets (protocol documented in ws.py):
    /ws/agents/{name}/chat    text to text
    /ws/agents/{name}/voice   speech in, speech out
"""
from __future__ import annotations

import importlib
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (COOKIE, ChangePasswordRequest, CreateUserRequest, LoginRequest, SetPasswordRequest,
                   TokenResponse, User, UserStore, authenticate_connection, unauthorized)
from .http_tools import HttpToolSpec, HttpToolStore
from .llm import Message
from .runtime import AgentValidationError, MultiAgentSystem, RunResult
from .speech import OpenAISpeechToText, OpenAITextToSpeech, SpeechToText, TextToSpeech
from .store import NAME_PATTERN, AgentConfig, AgentSpec, AgentStore
from .ws import register_ws_routes

log = logging.getLogger("multiagent.api")
WEB_DIR = Path(__file__).parent / "static" / "app"
LOGIN_MAX_FAILURES = 5        # per client IP + username ...
LOGIN_WINDOW_SECONDS = 300    # ... within this window, then further attempts get 429


class CreateAgentRequest(AgentConfig):
    name: str = Field(pattern=NAME_PATTERN)


class RunRequest(BaseModel):
    input: str
    history: list[Message] = Field(default_factory=list)
    include_trace: bool = True


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict
    kind: Literal["python", "http"] = "python"
    http: dict[str, Any] | None = Field(None, description="HTTP tool definition (header values masked)")
    used_by: list[str] = Field(default_factory=list, description="Agents that have this tool attached")


def create_app(
    system: MultiAgentSystem | None = None,
    *,
    stt: SpeechToText | None = None,
    tts: TextToSpeech | None = None,
    users: UserStore | None = None,
) -> FastAPI:
    if system is None:
        for mod in filter(None, os.getenv("TOOL_MODULES", "multiagent.builtin_tools").split(",")):
            importlib.import_module(mod.strip())
        system = MultiAgentSystem(
            store=AgentStore(os.getenv("AGENTS_FILE", "agents.json")),
            http_tools=HttpToolStore(os.getenv("TOOLS_FILE", "http_tools.json")),
        )

    if users is None:
        users = UserStore(os.getenv("USERS_FILE", "users.json"))
    users.ensure_admin()

    app = FastAPI(title="Multi-Agent System", version="0.1.0")
    app.state.system = system
    app.state.users = users

    # ------------------------------------------------------------ auth
    # Declared as security schemes so /docs shows an "Authorize" button for both.
    basic_scheme = HTTPBasic(auto_error=False, description="Username and password")
    bearer_scheme = HTTPBearer(auto_error=False, description="Token from POST /auth/login")

    def current_user(request: Request, _basic=Depends(basic_scheme), _bearer=Depends(bearer_scheme)) -> User:
        user = authenticate_connection(users, request)
        if user is None:
            raise unauthorized()
        return user

    def admin_user(user: User = Depends(current_user)) -> User:
        if user.role != "admin":
            raise HTTPException(403, "Admins only")
        return user

    failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def _set_cookie(response: Response, request: Request, token: str) -> None:
        response.set_cookie(COOKIE, token, max_age=users.token_seconds, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")

    auth = APIRouter(prefix="/auth", tags=["auth"])

    @auth.post("/login", response_model=TokenResponse)
    def login(req: LoginRequest, request: Request, response: Response):
        key = (request.client.host if request.client else "?", req.username)
        recent = failures[key]
        while recent and recent[0] < time.time() - LOGIN_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= LOGIN_MAX_FAILURES:
            raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
        user = users.authenticate(req.username, req.password)
        if user is None:
            recent.append(time.time())
            raise unauthorized("Wrong username or password")
        failures.pop(key, None)
        token, expires = users.issue_token(user.username)
        _set_cookie(response, request, token)
        return TokenResponse(token=token, expires_at=expires, user=user)

    @auth.post("/logout", status_code=204)
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")

    @auth.get("/me", response_model=User)
    def me(user: User = Depends(current_user)):
        return user

    @auth.post("/password", response_model=TokenResponse)
    def change_password(req: ChangePasswordRequest, request: Request, response: Response,
                        user: User = Depends(current_user)):
        """Change your own password. Signs you out everywhere else; returns a fresh token."""
        if users.authenticate(user.username, req.current_password) is None:
            raise HTTPException(400, "Current password is wrong")
        users.set_password(user.username, req.new_password)
        token, expires = users.issue_token(user.username)
        _set_cookie(response, request, token)
        return TokenResponse(token=token, expires_at=expires, user=user)

    @auth.get("/users", response_model=list[User], dependencies=[Depends(admin_user)])
    def list_users():
        return users.list()

    @auth.post("/users", response_model=User, status_code=201, dependencies=[Depends(admin_user)])
    def create_user(req: CreateUserRequest):
        try:
            return users.create(req.username, req.password, req.role)
        except ValueError as e:
            raise HTTPException(409 if "already exists" in str(e) else 422, str(e)) from e

    @auth.put("/users/{username}/password", status_code=204, dependencies=[Depends(admin_user)])
    def reset_password(username: str, req: SetPasswordRequest):
        try:
            users.set_password(username, req.password)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e

    @auth.delete("/users/{username}", status_code=204)
    def delete_user(username: str, admin: User = Depends(admin_user)):
        if username == admin.username:
            raise HTTPException(409, "You can't delete your own account")
        try:
            users.delete(username)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    app.include_router(auth)

    # Everything below requires a logged-in user.
    api = APIRouter(dependencies=[Depends(current_user)])

    def _404(e: KeyError) -> HTTPException:
        return HTTPException(404, str(e).strip("'\""))

    def _422(e: AgentValidationError) -> HTTPException:
        return HTTPException(409 if "already exists" in str(e) else 422, str(e))

    def _tool_info(name: str) -> ToolInfo:
        t = system.tools.get(name)
        return ToolInfo(
            **t.schema.model_dump(), kind=t.kind,
            http=system.http_tools.get(name).public() if t.kind == "http" else None,
            used_by=[a.name for a in system.store.list() if name in a.tools],
        )

    # ------------------------------------------------------------ tools
    @api.get("/tools", response_model=list[ToolInfo])
    def list_tools():
        return [_tool_info(t.name) for t in system.tools.list()]

    @api.get("/tools/{name}", response_model=ToolInfo)
    def get_tool(name: str):
        try:
            return _tool_info(name)
        except KeyError as e:
            raise _404(e) from e

    @api.post("/tools", response_model=ToolInfo, status_code=201)
    def create_http_tool(spec: HttpToolSpec):
        try:
            system.add_http_tool(spec)
        except AgentValidationError as e:
            raise _422(e) from e
        return _tool_info(spec.name)

    @api.delete("/tools/{name}", status_code=204)
    def delete_tool(name: str):
        try:
            system.delete_tool(name)
        except KeyError as e:
            raise _404(e) from e
        except AgentValidationError as e:
            raise HTTPException(409, str(e)) from e

    @api.get("/providers", response_model=list[str])
    def list_providers():
        return system.providers.names()

    # ----------------------------------------------------------- agents
    @api.get("/agents", response_model=list[AgentSpec])
    def list_agents():
        return system.store.list()

    @api.post("/agents", response_model=AgentSpec, status_code=201)
    def create_agent(req: CreateAgentRequest):
        try:
            return system.add_agent(req.name, AgentConfig(**req.model_dump(exclude={"name"})))
        except AgentValidationError as e:
            raise _422(e) from e

    @api.get("/agents/{name}", response_model=AgentSpec)
    def get_agent(name: str):
        try:
            return system.store.get(name)
        except KeyError as e:
            raise _404(e) from e

    @api.put("/agents/{name}", response_model=AgentSpec)
    def replace_agent(name: str, cfg: AgentConfig):
        if name not in system.store:
            raise HTTPException(404, f"Unknown agent '{name}'")
        try:
            return system.add_agent(name, cfg, replace=True)
        except AgentValidationError as e:
            raise HTTPException(422, str(e)) from e

    @api.delete("/agents/{name}", status_code=204)
    def delete_agent(name: str):
        try:
            system.delete_agent(name)
        except KeyError as e:
            raise _404(e) from e
        except AgentValidationError as e:
            raise HTTPException(409, str(e)) from e

    def _set_tool(name: str, tool: str, attached: bool) -> AgentSpec:
        try:
            spec = system.store.get(name)
        except KeyError as e:
            raise _404(e) from e
        if (tool in spec.tools) == attached:
            return spec
        cfg = AgentConfig(**spec.model_dump(include=set(AgentConfig.model_fields)))
        cfg.tools = [*cfg.tools, tool] if attached else [t for t in cfg.tools if t != tool]
        try:
            return system.add_agent(name, cfg, replace=True)
        except AgentValidationError as e:
            raise HTTPException(422, str(e)) from e

    @api.post("/agents/{name}/tools/{tool}", response_model=AgentSpec)
    def attach_tool(name: str, tool: str):
        return _set_tool(name, tool, True)

    @api.delete("/agents/{name}/tools/{tool}", response_model=AgentSpec)
    def detach_tool(name: str, tool: str):
        return _set_tool(name, tool, False)

    def _set_sub(name: str, sub: str, attached: bool) -> AgentSpec:
        try:
            return system.set_sub_agent(name, sub, attached)
        except KeyError as e:
            raise _404(e) from e
        except AgentValidationError as e:
            raise HTTPException(422, str(e)) from e

    @api.post("/agents/{name}/sub_agents/{sub}", response_model=AgentSpec)
    def attach_sub_agent(name: str, sub: str):
        return _set_sub(name, sub, True)

    @api.delete("/agents/{name}/sub_agents/{sub}", response_model=AgentSpec)
    def detach_sub_agent(name: str, sub: str):
        return _set_sub(name, sub, False)

    @api.post("/agents/{name}/run", response_model=RunResult)
    async def run_agent(name: str, req: RunRequest):
        if name not in system.store:
            raise HTTPException(404, f"Unknown agent '{name}'")
        try:
            result = await system.run(name, req.input, history=req.history)
        except Exception as e:  # provider / network failure
            log.exception("Run failed")
            raise HTTPException(502, f"{type(e).__name__}: {e}") from e
        if not req.include_trace:
            result.trace = []
        return result

    app.include_router(api)

    # ------------------------------------------------------------- web app
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/app/")

    @app.get("/demo/voice", include_in_schema=False)
    def voice_demo():  # old link from the earlier demo page
        return RedirectResponse("/app/")

    app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="web")

    register_ws_routes(app, system, stt or OpenAISpeechToText(), tts or OpenAITextToSpeech(), users)
    return app


app = create_app()
