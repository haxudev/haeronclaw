"""
Azure Functions + GitHub Copilot SDK
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import azure.functions as func
import frontmatter
from copilot_shim import run_copilot_agent, run_copilot_agent_stream
from model_identity import build_runtime_model_response, is_model_identity_question

from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

_GITHUB_TOKEN_PREFIXES = ("gho_", "ghu_", "github_pat_")


def _require_bearer_token(req: Request) -> str | None:
    """Best-effort protection for public HTTP endpoints.

    Azure-hosted deployments often rely on platform auth (EasyAuth) in front of
    anonymous functions. We cannot validate tokens locally without additional
    dependencies and tenant/app metadata, but we can still require an
    Authorization: Bearer header to avoid unauthenticated public access.

    If ENABLE_BEARER_AUTH is not set to true, no auth is enforced.
    """
    if os.environ.get("ENABLE_BEARER_AUTH", "false").strip().lower() not in {"1", "true", "yes"}:
        return None

    auth = req.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer ") or not auth.split(" ", 1)[1].strip():
        return "Missing or invalid Authorization: Bearer token"
    return None


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def _looks_like_github_token(token: str) -> bool:
    return any(token.startswith(prefix) for prefix in _GITHUB_TOKEN_PREFIXES)


def _extract_github_user_token(req: Request) -> str | None:
    """Extract GitHub OAuth token from request headers without persisting it.

    Priority:
    1. `x-github-token` header
    2. `Authorization: Bearer <token>` when the bearer looks like a GitHub token
    """
    explicit = (req.headers.get("x-github-token") or "").strip()
    if explicit:
        return explicit

    auth = req.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        bearer = auth.split(" ", 1)[1].strip()
        if bearer and _looks_like_github_token(bearer):
            return bearer

    return None


def _require_request_github_token(req: Request) -> tuple[str | None, str | None]:
    """Require a per-user GitHub token when running GitHub models in zero-static-key mode."""
    should_require = _is_true(os.environ.get("REQUIRE_GITHUB_USER_TOKEN"))
    if not should_require:
        return None, None

    github_token = _extract_github_user_token(req)
    if github_token:
        return github_token, None

    if os.environ.get("GITHUB_TOKEN"):
        # Backward-compatible fallback for environments still using app-level token.
        return None, None

    return None, (
        "Missing GitHub OAuth token. Provide `x-github-token` or use "
        "`Authorization: Bearer <gho_/ghu_/github_pat_ token>`."
    )

_MCP_AGENT_TOOL_PROPERTIES = json.dumps(
    [
        {
            "propertyName": "prompt",
            "propertyType": "string",
            "description": "Prompt text sent to the agent.",
            "isRequired": True,
            "isArray": False,
        },
    ]
)


def _load_agents_frontmatter_metadata() -> Dict[str, Any]:
    """Load AGENTS.md frontmatter metadata as a dictionary."""
    # Use __file__ to locate AGENTS.md relative to function_app.py instead of
    # os.getcwd(), because Azure Functions cold starts may use a standby path
    # during module import.
    agents_md_path = Path(__file__).resolve().parent / "AGENTS.md"
    if not agents_md_path.exists():
        return {}

    try:
        raw_content = agents_md_path.read_text(encoding="utf-8")
        parsed = frontmatter.loads(raw_content)
        metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
        return metadata
    except Exception as exc:
        logging.warning(f"Failed to parse AGENTS.md frontmatter: {exc}")
        return {}


def _safe_mcp_tool_name(raw_name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_").lower()
    if not normalized:
        return "agent_chat"
    if normalized[0].isdigit():
        return f"agent_{normalized}"
    return normalized


_AGENTS_FRONTMATTER_METADATA = _load_agents_frontmatter_metadata()

_MCP_AGENT_TOOL_NAME = _safe_mcp_tool_name(
    str(_AGENTS_FRONTMATTER_METADATA.get("name") or "agent_chat")
)

_MCP_AGENT_TOOL_DESCRIPTION = str(
    _AGENTS_FRONTMATTER_METADATA.get("description")
    or "Run an agent chat turn with a prompt."
).strip() or "Run an agent chat turn with a prompt."


def _extract_mcp_session_id(payload: Dict[str, Any]) -> str | None:
    """Extract MCP session id from top-level context payload only."""
    value = payload.get("sessionId") or payload.get("sessionid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _load_agents_functions_from_frontmatter() -> List[Dict[str, Any]]:
    """Load optional function definitions from AGENTS.md frontmatter."""
    metadata = _AGENTS_FRONTMATTER_METADATA
    if not metadata:
        logging.info("AGENTS.md not found or has no parseable frontmatter. No dynamic functions registered.")
        return []

    functions = metadata.get("functions")
    if functions is None:
        logging.info("AGENTS.md frontmatter has no 'functions' section. No dynamic functions registered.")
        return []

    if not isinstance(functions, list):
        logging.warning("AGENTS.md frontmatter 'functions' must be an array. Ignoring dynamic functions.")
        return []

    return [item for item in functions if isinstance(item, dict)]


def _normalize_timer_schedule(schedule: str) -> str:
    """Accept 5-part cron by prepending seconds; keep 6-part schedules unchanged."""
    schedule_parts = schedule.strip().split()
    if len(schedule_parts) == 5:
        return f"0 {schedule.strip()}"
    return schedule.strip()


def _is_valid_timer_schedule(schedule: str) -> bool:
    return len(schedule.strip().split()) == 6


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return default


def _safe_timer_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "timer_agent"
    if name[0].isdigit():
        return f"timer_{name}"
    return name


def _register_dynamic_timer_functions() -> None:
    function_specs = _load_agents_functions_from_frontmatter()
    if not function_specs:
        return

    registered_names = set()

    for index, spec in enumerate(function_specs, start=1):
        trigger_value = spec.get("trigger", "timer")
        trigger = str(trigger_value).strip().lower()
        if trigger != "timer":
            logging.warning(
                f"Rejected AGENTS function #{index}: unsupported trigger '{trigger}' (raw={trigger_value!r}). Only 'timer' is supported."
            )
            continue

        schedule_raw = spec.get("schedule")
        prompt_raw = spec.get("prompt")

        if not isinstance(schedule_raw, str) or not schedule_raw.strip():
            logging.warning(f"Skipping AGENTS function #{index}: missing required 'schedule'")
            continue

        if not isinstance(prompt_raw, str) or not prompt_raw.strip():
            logging.warning(f"Skipping AGENTS function #{index}: missing required 'prompt'")
            continue

        schedule = _normalize_timer_schedule(schedule_raw)
        if not _is_valid_timer_schedule(schedule):
            logging.warning(
                f"Skipping AGENTS function #{index}: invalid schedule '{schedule_raw}' after normalization '{schedule}'"
            )
            continue

        base_name = _safe_timer_name(str(spec.get("name") or f"timer_agent_{index}"))
        function_name = base_name
        suffix = 2
        while function_name in registered_names:
            function_name = f"{base_name}_{suffix}"
            suffix += 1
        registered_names.add(function_name)

        prompt = prompt_raw.strip()
        should_log_response = _to_bool(spec.get("logger", True), default=True)

        def _make_timer_handler(
            timer_function_name: str,
            timer_schedule: str,
            timer_prompt: str,
            log_response: bool,
        ):
            async def _timer_handler(timer_request: func.TimerRequest) -> None:
                if timer_request.past_due:
                    logging.info(f"Timer '{timer_function_name}' is past due.")

                logging.info(f"Timer '{timer_function_name}' running with schedule '{timer_schedule}'")

                try:
                    result = await run_copilot_agent(timer_prompt)
                    if log_response:
                        logging.info(
                            "Timer '%s' agent response: %s",
                            timer_function_name,
                            json.dumps(
                                {
                                    "session_id": result.session_id,
                                    "response": result.content,
                                    "response_intermediate": result.content_intermediate,
                                    "tool_calls": result.tool_calls,
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                        )
                except Exception as exc:
                    logging.exception(f"Timer '{timer_function_name}' failed: {exc}")

            _timer_handler.__name__ = f"timer_handler_{timer_function_name}"
            return _timer_handler

        handler = _make_timer_handler(function_name, schedule, prompt, should_log_response)
        decorated = app.timer_trigger(
            schedule=schedule,
            arg_name="timer_request",
            run_on_startup=False,
        )(handler)
        app.function_name(name=function_name)(decorated)

        logging.info(
            f"Registered dynamic timer function '{function_name}' from AGENTS.md (schedule='{schedule}', logger={should_log_response})"
        )


_register_dynamic_timer_functions()


# ---------------------------------------------------------------------------
# Proxy download endpoint for agent-generated files
# ---------------------------------------------------------------------------

@app.route(
    route="files/{*blob_path}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def download_file(req: Request) -> Response:
    """Proxy download for agent-generated files stored in Azure Blob Storage.

    GET /files/{prefix}/{filename}

    Uses Managed Identity to read from the ``agent-files`` container.
    No SAS token or extra RBAC roles needed — ``Storage Blob Data Contributor``
    on the MI is sufficient.
    """
    blob_path = (req.path_params or {}).get("blob_path", "")
    if not blob_path:
        return Response("Not found", status_code=404)

    try:
        from file_upload import _get_blob_service_client, _CONTAINER_NAME

        client = _get_blob_service_client()
        if not client:
            return Response("Storage not configured", status_code=500)

        blob_client = client.get_blob_client(_CONTAINER_NAME, blob_path)
        download = blob_client.download_blob()
        content = download.readall()

        # Read content type from blob properties (set during upload)
        props = download.properties
        ct = "application/octet-stream"
        if props and props.content_settings:
            ct = props.content_settings.content_type or ct

        filename = blob_path.split("/")[-1]
        # Allow caller to suggest a download filename via query param.
        # This is used to preserve original non-ASCII filenames while keeping
        # blob paths ASCII-safe.
        try:
            qp = getattr(req, "query_params", None)
            if qp and isinstance(qp, dict):
                requested = qp.get("filename")
            elif qp is not None:
                # Starlette-style QueryParams
                requested = qp.get("filename")  # type: ignore[attr-defined]
            else:
                requested = None
            if isinstance(requested, str) and requested.strip():
                filename = requested.strip()
        except Exception:
            pass

        # Build a Content-Disposition header that works with non-ASCII filenames.
        # Use RFC 5987 filename* plus an ASCII fallback filename.
        from urllib.parse import quote
        safe_ascii = ""
        try:
            safe_ascii = filename.encode("ascii", errors="ignore").decode("ascii")
        except Exception:
            safe_ascii = ""
        safe_ascii = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_ascii).strip("._-")
        if not safe_ascii:
            safe_ascii = blob_path.split("/")[-1] or "file"
            safe_ascii = safe_ascii.encode("ascii", errors="ignore").decode("ascii") or "file"
        cd = f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{quote(filename)}'
        return Response(
            content=content,
            status_code=200,
            media_type=ct,
            headers={
                "Content-Disposition": cd,
                "Cache-Control": "private, max-age=3600",
            },
        )
    except Exception as exc:
        logging.error(f"[FileDownload] Failed to serve {blob_path}: {exc}")
        return Response("File not found or expired", status_code=404)


@app.route(
    route="{*ignored}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def root_chat_page(req: Request) -> Response:
    """Serve the chat UI at the root route."""
    ignored = (req.path_params or {}).get("ignored", "")
    if ignored:
        return Response("Not found", status_code=404)

    index_path = Path(__file__).resolve().parent / "public" / "index.html"
    if not index_path.exists():
        return Response("index.html not found", status_code=404)

    return Response(
        index_path.read_text(encoding="utf-8"),
        status_code=200,
        media_type="text/html",
    )

@app.route(route="agent/chat", methods=["POST"])
async def chat(req: Request) -> Response:
    """
    Chat endpoint - send a prompt, get a response.

    POST /agent/chat
    Headers:
        x-ms-session-id (optional): Session ID for resuming a previous session
        x-github-token (recommended): Per-user GitHub OAuth token (gho_/ghu_)
    Body:
    {
        "prompt": "What is 2+2?"
    }
    """
    try:
        auth_error = _require_bearer_token(req)
        if auth_error:
            return Response(
                json.dumps({"error": auth_error}),
                status_code=401,
                media_type="application/json",
            )

        github_user_token, github_token_error = _require_request_github_token(req)
        if github_token_error:
            return Response(
                json.dumps({"error": github_token_error}),
                status_code=401,
                media_type="application/json",
            )

        body = await req.json()
        prompt = body.get("prompt")

        if not prompt:
            return Response(
                json.dumps({"error": "Missing 'prompt'"}),
                status_code=400,
                media_type="application/json",
            )

        if is_model_identity_question(prompt):
            session_id = req.headers.get("x-ms-session-id") or ""
            headers = {"x-ms-session-id": session_id} if session_id else None
            return Response(
                json.dumps(
                    {
                        "session_id": session_id,
                        "response": build_runtime_model_response(),
                        "response_intermediate": [],
                        "tool_calls": [],
                        "files": [],
                        "upload_debug": {"short_circuit": "runtime_model_identity"},
                    }
                ),
                media_type="application/json",
                headers=headers,
            )

        # Create per-request output dir for multi-user isolation
        request_output_dir = None
        try:
            from file_upload import snapshot_tmp_files, create_request_output_dir
            request_output_dir = create_request_output_dir()
            # Snapshot ONLY the scoped output dir to avoid cross-talk between
            # concurrent requests sharing /tmp.
            tmp_before = snapshot_tmp_files(request_output_dir)
        except Exception:
            tmp_before = {}

        # Prepend output dir hint so the agent writes files to the isolated dir
        agent_prompt = prompt
        if request_output_dir:
            agent_prompt = (
                f"[System: Write all generated files to {request_output_dir}/ — "
                f"this is your dedicated output directory for this request.]\n\n{prompt}"
            )

        session_id = req.headers.get("x-ms-session-id")
        result = await run_copilot_agent(
            agent_prompt,
            session_id=session_id,
            github_token=github_user_token,
        )

        response_content = result.content or ""
        uploaded_files_data: list = []

        # Upload any generated files to blob storage
        upload_debug: dict = {}
        try:
            import asyncio
            from file_upload import (
                snapshot_tmp_files, find_new_files, upload_and_replace,
                find_file_paths_in_text,
            )

            # Scan only the scoped output dir for concurrency safety.
            tmp_after = snapshot_tmp_files(request_output_dir) if request_output_dir else snapshot_tmp_files()
            new_files = find_new_files(tmp_before, tmp_after)

            has_tmp_ref = "/tmp/" in response_content
            text_paths = find_file_paths_in_text(response_content) if has_tmp_ref else []
            text_paths_exist = {p: os.path.isfile(p) for p in text_paths}

            upload_debug = {
                "before": len(tmp_before),
                "after": len(tmp_after),
                "new_files": new_files,
                "text_paths": text_paths,
                "text_paths_exist": text_paths_exist,
                "has_tmp_ref": has_tmp_ref,
                "output_dir": request_output_dir,
            }
            logging.info(f"[Chat] File detection: {upload_debug}")

            if new_files or has_tmp_ref:
                response_content, uploaded = await asyncio.to_thread(
                    upload_and_replace, response_content, new_files
                )
                uploaded_files_data = [
                    {"filename": uf.filename, "url": uf.download_url}
                    for uf in uploaded
                ]
                upload_debug["uploaded_count"] = len(uploaded)
                if not uploaded:
                    import glob as glob_mod
                    scoped_listing = glob_mod.glob(f"{request_output_dir}/*") if request_output_dir else []
                    upload_debug["scoped_listing"] = scoped_listing[:10]
        except Exception as upload_exc:
            logging.error(f"Chat file upload failed: {upload_exc}", exc_info=True)
            upload_debug["exception"] = str(upload_exc)[:300]

        response = Response(
            json.dumps(
                {
                    "session_id": result.session_id,
                    "response": response_content,
                    "response_intermediate": result.content_intermediate,
                    "tool_calls": result.tool_calls,
                    "files": uploaded_files_data,
                    "upload_debug": upload_debug,
                }
            ),
            media_type="application/json",
            headers={"x-ms-session-id": result.session_id},
        )
        return response

    except Exception as e:
        error_msg = str(e) if str(e) else f"{type(e).__name__}: {repr(e)}"
        logging.error(f"Chat error: {error_msg}")
        return Response(
            json.dumps({"error": error_msg}), status_code=500, media_type="application/json"
        )


@app.route(route="agent/chatstream", methods=["POST"])
async def chat_stream(req: Request) -> StreamingResponse:
    """
    Streaming chat endpoint - send a prompt, receive SSE events.

    POST /agent/chat/stream
    Headers:
        x-ms-session-id (optional): Session ID for resuming a previous session
    Body:
    {
        "prompt": "What is 2+2?"
    }

    Response: text/event-stream with events:
        data: {"type": "session", "session_id": "..."}
        data: {"type": "delta", "content": "partial text"}
        data: {"type": "tool_start", "tool_name": "...", "tool_call_id": "..."}
        data: {"type": "message", "content": "full message"}
        data: {"type": "files", "files": [{"filename": "...", "url": "..."}]}
        data: {"type": "done"}
    """
    try:
        auth_error = _require_bearer_token(req)
        if auth_error:
            async def error_gen():
                yield f"data: {json.dumps({'type': 'error', 'content': auth_error})}\n\n"
            return StreamingResponse(error_gen(), media_type="text/event-stream")

        github_user_token, github_token_error = _require_request_github_token(req)
        if github_token_error:
            async def error_gen():
                yield f"data: {json.dumps({'type': 'error', 'content': github_token_error})}\n\n"
            return StreamingResponse(error_gen(), media_type="text/event-stream")

        body = await req.json()
        prompt = body.get("prompt")

        if not prompt:
            async def error_gen():
                yield f"data: {json.dumps({'type': 'error', 'content': 'Missing prompt'})}\n\n"
            return StreamingResponse(error_gen(), media_type="text/event-stream")

        session_id = req.headers.get("x-ms-session-id")

        async def _stream_with_file_upload():
            """Wrap the raw agent stream to add file upload after completion."""
            import asyncio as _aio

            # --- Pre-stream: snapshot scoped output dir & create request output dir ---
            request_output_dir = None
            tmp_before: dict = {}
            try:
                from file_upload import snapshot_tmp_files, create_request_output_dir
                request_output_dir = create_request_output_dir()
                tmp_before = snapshot_tmp_files(request_output_dir)
            except Exception:
                pass

            agent_prompt = prompt
            if request_output_dir:
                agent_prompt = (
                    f"[System: Write all generated files to {request_output_dir}/ — "
                    f"this is your dedicated output directory for this request.]\n\n{prompt}"
                )

            # --- Stream events, buffering 'done' and tracking 'message' ---
            buffered_done = None
            final_message_text = ""

            async for sse_line in run_copilot_agent_stream(
                agent_prompt,
                session_id=session_id,
                github_token=github_user_token,
            ):
                try:
                    stripped = sse_line.strip()
                    if stripped.startswith("data: "):
                        payload = json.loads(stripped[6:])
                        evt_type = payload.get("type")
                        if evt_type == "message":
                            final_message_text = payload.get("content", "")
                        elif evt_type == "done":
                            buffered_done = sse_line
                            continue  # Don't yield yet — upload files first
                except (json.JSONDecodeError, Exception):
                    pass
                yield sse_line

            # --- Post-stream: detect and upload generated files ---
            try:
                from file_upload import (
                    snapshot_tmp_files, find_new_files, upload_and_replace,
                )

                tmp_after = snapshot_tmp_files(request_output_dir) if request_output_dir else snapshot_tmp_files()
                new_files = find_new_files(tmp_before, tmp_after)

                logging.info(
                    f"[ChatStream] File detection: new_files={new_files}, "
                    f"output_dir={request_output_dir}, "
                    f"before={len(tmp_before)}, after={len(tmp_after)}"
                )

                if new_files:
                    corrected_text, uploaded = await _aio.to_thread(
                        upload_and_replace, final_message_text, new_files
                    )
                    if uploaded:
                        files_data = [
                            {"filename": uf.filename, "url": uf.download_url}
                            for uf in uploaded
                        ]
                        yield f"data: {json.dumps({'type': 'files', 'files': files_data})}\n\n"
                        # Send corrected message with download links replacing /tmp/ paths
                        if corrected_text != final_message_text:
                            yield f"data: {json.dumps({'type': 'message', 'content': corrected_text})}\n\n"
                        logging.info(f"[ChatStream] Uploaded {len(uploaded)} files")
            except Exception as exc:
                logging.error(f"[ChatStream] File upload failed: {exc}", exc_info=True)

            # --- Yield buffered 'done' ---
            if buffered_done:
                yield buffered_done

        return StreamingResponse(
            _stream_with_file_upload(),
            media_type="text/event-stream",
        )

    except Exception as e:
        error_msg = str(e) if str(e) else f"{type(e).__name__}: {repr(e)}"
        logging.error(f"Chat stream error: {error_msg}")
        async def error_gen():
            yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name=_MCP_AGENT_TOOL_NAME,
    description=_MCP_AGENT_TOOL_DESCRIPTION,
    tool_properties=_MCP_AGENT_TOOL_PROPERTIES,
)
async def mcp_agent_chat(context: str) -> str:
    """MCP tool endpoint that runs the same agent workflow as /agent/chat."""
    try:
        payload = json.loads(context) if context else {}
        arguments = payload.get("arguments", {}) if isinstance(payload, dict) else {}

        prompt = arguments.get("prompt") if isinstance(arguments, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            return json.dumps({"error": "Missing 'prompt'"})

        session_id = _extract_mcp_session_id(payload) if isinstance(payload, dict) else None

        # Snapshot scoped output dir before agent execution to detect generated files
        request_output_dir = None
        try:
            from file_upload import snapshot_tmp_files, create_request_output_dir
            request_output_dir = create_request_output_dir()
            tmp_before = snapshot_tmp_files(request_output_dir)
        except Exception:
            tmp_before = {}

        agent_prompt = prompt.strip()
        if request_output_dir:
            agent_prompt = (
                f"[System: Write all generated files to {request_output_dir}/ — "
                f"this is your dedicated output directory for this request.]\n\n{agent_prompt}"
            )

        result = await run_copilot_agent(agent_prompt, session_id=session_id)

        response_content = result.content or ""
        uploaded_files_data: list = []

        # Upload any generated files to blob storage
        try:
            import asyncio
            from file_upload import snapshot_tmp_files, find_new_files, upload_and_replace

            tmp_after = snapshot_tmp_files(request_output_dir) if request_output_dir else snapshot_tmp_files()
            new_files = find_new_files(tmp_before, tmp_after)

            if new_files or "/tmp/" in response_content:
                response_content, uploaded = await asyncio.to_thread(
                    upload_and_replace, response_content, new_files
                )
                uploaded_files_data = [
                    {"filename": uf.filename, "url": uf.download_url}
                    for uf in uploaded
                ]
        except Exception as upload_exc:
            logging.warning(f"MCP file upload failed: {upload_exc}")

        return json.dumps(
            {
                "session_id": result.session_id,
                "response": response_content,
                "response_intermediate": result.content_intermediate,
                "tool_calls": result.tool_calls,
                "files": uploaded_files_data,
            }
        )
    except Exception as exc:
        error_msg = str(exc) if str(exc) else f"{type(exc).__name__}: {repr(exc)}"
        logging.error(f"MCP tool error: {error_msg}")
        return json.dumps({"error": error_msg})


# ---------------------------------------------------------------------------
# Microsoft Teams Bot endpoint (Bot Framework)
# ---------------------------------------------------------------------------

@app.route(route="messages", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def bot_messages(req: Request) -> Response:
    """Bot Framework messaging endpoint for Microsoft Teams integration.

    POST /api/messages
    Auth is ANONYMOUS at the Azure Functions level because the Bot Framework
    Service authenticates via JWT in the Authorization header, which is
    validated by the BotFrameworkAdapter internally.
    """
    try:
        from teams_bot import handle_incoming_activity
        body = await req.body()
        auth_header = req.headers.get("Authorization", "")

        status_code, response_body = await handle_incoming_activity(body, auth_header)

        return Response(
            content=response_body if response_body else "",
            status_code=status_code,
            media_type="application/json" if response_body else "text/plain",
        )
    except Exception as exc:
        error_msg = str(exc) if str(exc) else f"{type(exc).__name__}: {repr(exc)}"
        logging.error(f"Bot messages error: {error_msg}", exc_info=True)
        return Response(
            content=json.dumps({"error": error_msg}),
            status_code=500,
            media_type="application/json",
        )
