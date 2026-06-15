"""File storage for uploaded RFCs and seed files."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone

from fastapi import UploadFile

DATA_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "uploads")
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# RFCs
# ---------------------------------------------------------------------------


def save_rfc(file: UploadFile) -> dict:
    """Save an uploaded RFC file. Returns metadata dict."""
    rfc_id = uuid.uuid4().hex
    rfc_dir = os.path.join(DATA_ROOT, "rfcs", rfc_id)
    _ensure_dir(rfc_dir)

    # Determine safe filename.
    safe_name = file.filename or "rfc.pdf"
    dest = os.path.join(rfc_dir, safe_name)
    size = 0
    with open(dest, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            f.write(chunk)
            size += len(chunk)

    meta = {
        "rfc_id": rfc_id,
        "filename": safe_name,
        "size_bytes": size,
        "uploaded_at": _now(),
    }
    with open(os.path.join(rfc_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return meta


def list_rfcs() -> list[dict]:
    """Return metadata for all stored RFCs."""
    rfcs_dir = os.path.join(DATA_ROOT, "rfcs")
    if not os.path.isdir(rfcs_dir):
        return []
    result = []
    for rfc_id in os.listdir(rfcs_dir):
        meta_path = os.path.join(rfcs_dir, rfc_id, "metadata.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                result.append(json.load(f))
    return result


def get_rfc_meta(rfc_id: str) -> dict | None:
    meta_path = os.path.join(DATA_ROOT, "rfcs", rfc_id, "metadata.json")
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


def get_rfc_path(rfc_id: str) -> str | None:
    """Return the full path to the RFC file for *rfc_id*, or None."""
    meta = get_rfc_meta(rfc_id)
    if meta is None:
        return None
    return os.path.join(DATA_ROOT, "rfcs", rfc_id, meta["filename"])


def delete_rfc(rfc_id: str) -> bool:
    rfc_dir = os.path.join(DATA_ROOT, "rfcs", rfc_id)
    if os.path.isdir(rfc_dir):
        shutil.rmtree(rfc_dir)
        return True
    return False


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def save_seeds(files: list[UploadFile]) -> dict:
    """Save uploaded seed files. Returns metadata dict."""
    seeds_id = uuid.uuid4().hex
    seeds_dir = os.path.join(DATA_ROOT, "seeds", seeds_id)
    _ensure_dir(seeds_dir)

    filenames = []
    total_size = 0
    for fobj in files:
        safe_name = fobj.filename or f"seed_{uuid.uuid4().hex[:8]}.bin"
        dest = os.path.join(seeds_dir, safe_name)
        with open(dest, "wb") as f:
            while chunk := fobj.file.read(1024 * 1024):
                f.write(chunk)
                total_size += len(chunk)
        filenames.append(safe_name)

    meta = {
        "seeds_id": seeds_id,
        "file_count": len(filenames),
        "filenames": filenames,
        "size_bytes": total_size,
        "uploaded_at": _now(),
    }
    with open(os.path.join(seeds_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return meta


def list_seeds() -> list[dict]:
    seeds_root = os.path.join(DATA_ROOT, "seeds")
    if not os.path.isdir(seeds_root):
        return []
    result = []
    for seeds_id in os.listdir(seeds_root):
        meta_path = os.path.join(seeds_root, seeds_id, "metadata.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                result.append(json.load(f))
    return result


def get_seeds_meta(seeds_id: str) -> dict | None:
    meta_path = os.path.join(DATA_ROOT, "seeds", seeds_id, "metadata.json")
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


def get_seeds_dir(seeds_id: str) -> str | None:
    """Return the full path to the seeds directory for *seeds_id*, or None."""
    meta = get_seeds_meta(seeds_id)
    if meta is None:
        return None
    return os.path.join(DATA_ROOT, "seeds", seeds_id)


def delete_seeds(seeds_id: str) -> bool:
    seeds_dir = os.path.join(DATA_ROOT, "seeds", seeds_id)
    if os.path.isdir(seeds_dir):
        shutil.rmtree(seeds_dir)
        return True
    return False


# ---------------------------------------------------------------------------
# Session files
# ---------------------------------------------------------------------------


def setup_session_files(
    session_id: str,
    *,
    rfc_file: UploadFile | None = None,
    seed_files: list[UploadFile] | None = None,
    rfc_id: str | None = None,
    seeds_id: str | None = None,
) -> tuple[str, str]:
    """Prepare RFC & seed directories for a session.

    Returns (rfc_path, seed_dir) pointing to the session-local copies.
    """
    session_dir = os.path.join(DATA_ROOT, "sessions", session_id)
    _ensure_dir(session_dir)

    # --- RFC ---
    if rfc_id is not None:
        src_rfc_path = get_rfc_path(rfc_id)
        if src_rfc_path is None:
            raise ValueError(f"Unknown rfc_id: {rfc_id}")
        dest_rfc_dir = os.path.join(session_dir, "rfc")
        _ensure_dir(dest_rfc_dir)
        rfc_name = os.path.basename(src_rfc_path)
        rfc_dest = os.path.join(dest_rfc_dir, rfc_name)
        if not os.path.exists(rfc_dest):
            shutil.copy2(src_rfc_path, rfc_dest)
        rfc_path = rfc_dest
    elif rfc_file is not None:
        dest_rfc_dir = os.path.join(session_dir, "rfc")
        _ensure_dir(dest_rfc_dir)
        safe_name = rfc_file.filename or "rfc.pdf"
        rfc_dest = os.path.join(dest_rfc_dir, safe_name)
        with open(rfc_dest, "wb") as f:
            while chunk := rfc_file.file.read(1024 * 1024):
                f.write(chunk)
        rfc_path = rfc_dest
    else:
        raise ValueError("Either rfc_file or rfc_id must be provided.")

    # --- Seeds ---
    if seeds_id is not None:
        src_seeds_dir = get_seeds_dir(seeds_id)
        if src_seeds_dir is None:
            raise ValueError(f"Unknown seeds_id: {seeds_id}")
        dest_seeds_dir = os.path.join(session_dir, "seeds")
        if not os.path.exists(dest_seeds_dir):
            _ensure_dir(dest_seeds_dir)
            for name in os.listdir(src_seeds_dir):
                if name == "metadata.json":
                    continue
                src = os.path.join(src_seeds_dir, name)
                dst = os.path.join(dest_seeds_dir, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
        seed_dir = dest_seeds_dir
    elif seed_files is not None and len(seed_files) > 0:
        dest_seeds_dir = os.path.join(session_dir, "seeds")
        _ensure_dir(dest_seeds_dir)
        for fobj in seed_files:
            safe_name = fobj.filename or f"seed_{uuid.uuid4().hex[:8]}.bin"
            dest = os.path.join(dest_seeds_dir, safe_name)
            with open(dest, "wb") as f:
                while chunk := fobj.file.read(1024 * 1024):
                    f.write(chunk)
        seed_dir = dest_seeds_dir
    else:
        raise ValueError("Either seed_files or seeds_id must be provided.")

    return rfc_path, seed_dir


def delete_session_files(session_id: str) -> None:
    session_dir = os.path.join(DATA_ROOT, "sessions", session_id)
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir)
