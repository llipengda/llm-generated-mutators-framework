from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from console import console


_RAG_CACHE_VERSION = 1


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_cache_dir() -> Path:
    # Prefer a workspace-local cache by default.
    # Can be overridden via env for CI or containerized runs.
    env_dir = os.environ.get("RAG_CACHE_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path(".cache") / "rag"


def _cache_key_for_file(
    *,
    rfc_path: str,
    chunk_size: int,
    chunk_overlap: int,
    embeddings: OpenAIEmbeddings,
) -> str:
    # Cache must be invalidated when the file content or any relevant config changes.
    # We use a stable JSON payload then hash it to get a filesystem-friendly key.
    try:
        model_name: Optional[str] = getattr(embeddings, "model", None)
    except Exception:
        model_name = None

    payload = {
        "v": _RAG_CACHE_VERSION,
        "rfc_abspath": str(Path(rfc_path).resolve()),
        "rfc_sha256": _sha256_file(rfc_path),
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "embeddings": {
            "class": embeddings.__class__.__name__,
            "model": model_name,
        },
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest


def _load_faiss_cache(cache_dir: Path, embeddings: OpenAIEmbeddings) -> Optional[FAISS]:
    try:
        # LangChain's FAISS store persists a pickle (docstore/index metadata).
        # This is safe for our own locally-generated cache, but should not be used
        # with untrusted cache directories.
        return FAISS.load_local(
            str(cache_dir),
            embeddings,
            allow_dangerous_deserialization=True,
        )
    except Exception:
        return None


def _save_faiss_cache(vectorstore: FAISS, cache_dir: Path) -> None:
    cache_dir.parent.mkdir(parents=True, exist_ok=True)

    # Atomic-ish directory replace: write to a temp dir then move into place.
    with tempfile.TemporaryDirectory(prefix="rag-cache-") as tmp:
        tmp_dir = Path(tmp) / "faiss"
        vectorstore.save_local(str(tmp_dir))
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        shutil.move(str(tmp_dir), str(cache_dir))


def build_retriever(rfc_path: str):
    """Build a retriever from the RFC PDF / text file.

    Returns None if setup fails (e.g., missing file or dependencies).
    """
    with console.status(
        "[bold green]Setting up RAG components...[/bold green]", spinner="dots"
    ):
        try:
            if os.environ.get("RAG_DISABLE_CACHE") in {"1", "true", "TRUE", "yes", "YES"}:
                cache_root: Optional[Path] = None
            else:
                cache_root = _default_cache_dir()

            splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
            embeddings = OpenAIEmbeddings()

            if cache_root is not None and os.path.exists(rfc_path):
                cache_key = _cache_key_for_file(
                    rfc_path=rfc_path,
                    chunk_size=splitter._chunk_size,  # type: ignore[attr-defined]
                    chunk_overlap=splitter._chunk_overlap,  # type: ignore[attr-defined]
                    embeddings=embeddings,
                )
                cache_dir = cache_root / cache_key
                cached = _load_faiss_cache(cache_dir, embeddings)
                if cached is not None:
                    console.log(f"[dim]RAG cache hit: {cache_dir}[/dim]")
                    return cached.as_retriever(search_kwargs={"k": 4})

            # Cache miss -> build index.
            if rfc_path.endswith(".pdf"):
                loader = PyPDFLoader(rfc_path)
            else:
                loader = TextLoader(rfc_path, encoding="utf-8")
            docs = loader.load()
            chunks = splitter.split_documents(docs)
            vectorstore = FAISS.from_documents(chunks, embeddings)

            if cache_root is not None and os.path.exists(rfc_path):
                try:
                    cache_key = _cache_key_for_file(
                        rfc_path=rfc_path,
                        chunk_size=splitter._chunk_size,  # type: ignore[attr-defined]
                        chunk_overlap=splitter._chunk_overlap,  # type: ignore[attr-defined]
                        embeddings=embeddings,
                    )
                    cache_dir = cache_root / cache_key
                    _save_faiss_cache(vectorstore, cache_dir)
                    console.log(f"[dim]RAG cache saved: {cache_dir}[/dim]")
                except Exception as e:
                    console.log(f"[dim]RAG cache save skipped: {e}[/dim]")

            return vectorstore.as_retriever(search_kwargs={"k": 4})
        except Exception as e:
            console.print(f"[yellow]Skipping RAG setup due to error: {e}[/yellow]")
            return None
