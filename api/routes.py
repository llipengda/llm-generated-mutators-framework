"""API route handlers."""

from __future__ import annotations

import asyncio
import json
import os
import queue

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)

from api.deps import get_session_manager
from api.file_store import (
    delete_rfc,
    delete_seeds,
    get_rfc_meta,
    get_rfc_path,
    get_seeds_dir,
    get_seeds_meta,
    list_rfcs,
    list_seeds,
    save_rfc,
    save_seeds,
    setup_session_files,
)
from api.models import (
    CreateSessionResponse,
    HealthResponse,
    LlmConfig,
    RfcInfo,
    RunStepRequest,
    RunStepResponse,
    SeedsInfo,
    SessionDetail,
    SessionSummary,
)
from api.session_manager import SessionManager

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    sdk_available = os.path.isdir("./peach/sdk/")
    return HealthResponse(
        status="ok" if sdk_available else "degraded",
        sdk_available=sdk_available,
        api_mode=True,
    )


# ---------------------------------------------------------------------------
# RFCs
# ---------------------------------------------------------------------------


@router.post("/rfcs", response_model=RfcInfo)
def upload_rfc(file: UploadFile = File(...)) -> RfcInfo:
    if not file.filename:
        raise HTTPException(400, "No filename provided.")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".pdf", ".txt"):
        raise HTTPException(400, f"Unsupported RFC format: {ext}. Use .pdf or .txt.")
    meta = save_rfc(file)
    return RfcInfo(**meta)


@router.get("/rfcs", response_model=list[RfcInfo])
def list_rfcs_endpoint() -> list[RfcInfo]:
    return [RfcInfo(**m) for m in list_rfcs()]


@router.get("/rfcs/{rfc_id}/download")
def download_rfc(rfc_id: str):
    meta = get_rfc_meta(rfc_id)
    if meta is None:
        raise HTTPException(404, "RFC not found.")
    path = get_rfc_path(rfc_id)
    if path is None or not os.path.exists(path):
        raise HTTPException(404, "RFC file not found on disk.")
    from fastapi.responses import FileResponse

    return FileResponse(path, filename=meta["filename"])


@router.delete("/rfcs/{rfc_id}")
def delete_rfc_endpoint(rfc_id: str) -> dict:
    if delete_rfc(rfc_id):
        return {"detail": "deleted"}
    raise HTTPException(404, "RFC not found.")


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


@router.post("/seeds", response_model=SeedsInfo)
def upload_seeds(files: list[UploadFile] = File(...)) -> SeedsInfo:
    if not files:
        raise HTTPException(400, "At least one seed file is required.")
    meta = save_seeds(files)
    return SeedsInfo(**meta)


@router.get("/seeds", response_model=list[SeedsInfo])
def list_seeds_endpoint() -> list[SeedsInfo]:
    return [SeedsInfo(**m) for m in list_seeds()]


@router.get("/seeds/{seeds_id}/download")
def download_seeds(seeds_id: str):
    meta = get_seeds_meta(seeds_id)
    if meta is None:
        raise HTTPException(404, "Seed set not found.")
    seeds_dir = get_seeds_dir(seeds_id)
    if seeds_dir is None or not os.path.isdir(seeds_dir):
        raise HTTPException(404, "Seed files not found on disk.")

    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in meta["filenames"]:
            fpath = os.path.join(seeds_dir, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, fname)
    buf.seek(0)
    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="seeds_{seeds_id}.zip"'},
    )


@router.delete("/seeds/{seeds_id}")
def delete_seeds_endpoint(seeds_id: str) -> dict:
    if delete_seeds(seeds_id):
        return {"detail": "deleted"}
    raise HTTPException(404, "Seed set not found.")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    protocol: str = Form(...),
    fixer: bool = Form(False),
    rfc_file: UploadFile | None = File(None),
    seed_files: list[UploadFile] | None = File(None),
    rfc_id: str | None = Form(None),
    seeds_id: str | None = Form(None),
    llm_config: str | None = Form(None),
    manager: SessionManager = Depends(get_session_manager),
) -> CreateSessionResponse:
    # Validate.
    if rfc_file is None and rfc_id is None:
        raise HTTPException(400, "Either rfc_file or rfc_id must be provided.")
    has_seed_files = seed_files is not None and len(seed_files) > 0
    if not has_seed_files and seeds_id is None:
        raise HTTPException(400, "Either seed_files or seeds_id must be provided.")

    # Parse LLM config if provided.
    llm_overrides = None
    if llm_config:
        try:
            parsed = json.loads(llm_config)
            llm_overrides = LlmConfig.model_validate(parsed).to_overrides()
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(400, f"Invalid llm_config: {e}")

    # Create session first to obtain a session_id, using placeholder paths.
    response = manager.create_session(
        protocol=protocol,
        fixer=fixer,
        rfc_path="",  # placeholder — updated below
        seed_dir="",  # placeholder — updated below
        llm_overrides=llm_overrides,
    )
    session_id = response.session_id

    # Now set up the real file directories keyed by the session_id.
    try:
        session_rfc_path, session_seed_dir = setup_session_files(
            session_id,
            rfc_file=rfc_file,
            seed_files=seed_files if has_seed_files else None,
            rfc_id=rfc_id,
            seeds_id=seeds_id,
        )
    except ValueError as e:
        # Clean up the just-created session on failure.
        manager.delete_session(session_id)
        raise HTTPException(400, str(e))

    # Update the session with the real paths.
    manager.update_session_paths(session_id, session_rfc_path, session_seed_dir)

    response.rfc_path = session_rfc_path
    response.seed_dir = session_seed_dir
    return response


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions(
    manager: SessionManager = Depends(get_session_manager),
) -> list[SessionSummary]:
    return manager.list_sessions()


@router.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(
    session_id: str,
    manager: SessionManager = Depends(get_session_manager),
) -> SessionDetail:
    try:
        return manager.get_session_detail(session_id)
    except ValueError:
        raise HTTPException(404, f"Session not found: {session_id}")


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    manager: SessionManager = Depends(get_session_manager),
) -> dict:
    if manager.delete_session(session_id):
        return {"detail": "deleted"}
    raise HTTPException(404, f"Session not found: {session_id}")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/steps/{step_id}/run",
    response_model=RunStepResponse,
)
def run_step(
    session_id: str,
    step_id: str,
    params: RunStepRequest = RunStepRequest(),
    manager: SessionManager = Depends(get_session_manager),
) -> RunStepResponse:
    try:
        return manager.run_step(session_id, step_id, params)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get(
    "/sessions/{session_id}/steps/{step_id}",
)
def get_step(
    session_id: str,
    step_id: str,  # noqa: ARG001 — part of URL path, used for routing
    manager: SessionManager = Depends(get_session_manager),
) -> SessionDetail:
    """Return the full session detail (includes all step statuses)."""
    try:
        return manager.get_session_detail(session_id)
    except ValueError:
        raise HTTPException(404, f"Session not found: {session_id}")


# ---------------------------------------------------------------------------
# WebSocket — real-time log streaming
# ---------------------------------------------------------------------------


@router.websocket("/sessions/{session_id}/ws")
async def session_ws(websocket: WebSocket, session_id: str):
    """Stream real-time logs for *session_id* via WebSocket.

    Connect to ``ws://<host>/api/v1/sessions/{session_id}/ws``.
    Each message is a JSON object: ``{"type": "log", "line": "..."}``.
    """
    await websocket.accept()
    from api.log_broadcaster import broadcaster

    q: queue.Queue[str] = broadcaster.subscribe(session_id)
    try:
        while True:
            try:
                line = await asyncio.to_thread(q.get, timeout=5)
                await websocket.send_json({"type": "log", "line": line})
            except queue.Empty:
                # Heartbeat to keep the connection alive.
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unsubscribe(session_id)


# ---------------------------------------------------------------------------
# Logs (historical, file-based)
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/logs")
def get_logs(
    session_id: str,
    tail: int = 100,
    manager: SessionManager = Depends(get_session_manager),
) -> dict:
    log_path = manager.get_log_path(session_id)
    if not log_path or not os.path.exists(log_path):
        return {"lines": [], "detail": "No log file yet."}

    with open(log_path, encoding="utf-8") as f:
        all_lines = f.readlines()

    lines = all_lines[-tail:] if tail > 0 else all_lines
    return {
        "lines": [line.rstrip("\n") for line in lines],
        "total_lines": len(all_lines),
        "path": log_path,
    }
