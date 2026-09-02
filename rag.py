from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import SecretStr

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ui import UI


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


def _cache_key_for_files(
    *,
    rfc_paths: list[str],
    chunk_size: int,
    chunk_overlap: int,
    embeddings: OpenAIEmbeddings,
) -> str:
    # Cache must be invalidated when any file content or relevant config changes.
    try:
        model_name: Optional[str] = getattr(embeddings, "model", None)
    except Exception:
        model_name = None

    files = [
        {"abspath": str(Path(p).resolve()), "sha256": _sha256_file(p)}
        for p in sorted(rfc_paths)
    ]
    payload: dict[str, object] = {
        "v": _RAG_CACHE_VERSION,
        "files": files,
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


def build_retriever(rfc_paths: list[str]):
    """Build a retriever from one or more RFC PDF / text files.

    Returns None if setup fails (e.g., missing files or dependencies).
    """
    with UI.status("Setting up RAG components...", spinner="dots"):
        try:
            if os.environ.get("RAG_DISABLE_CACHE") in {"1", "true", "TRUE", "yes", "YES"}:
                cache_root: Optional[Path] = None
            else:
                cache_root = _default_cache_dir()

            splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
            embedding_model = os.environ.get(
                "LLM_EMBEDDING_MODEL", "text-embedding-ada-002"
            )
            embedding_base_url = os.environ.get("LLM_EMBEDDING_BASE_URL")
            embedding_api_key = os.environ.get("LLM_EMBEDDING_API_KEY")
            embeddings = OpenAIEmbeddings(
                model=embedding_model,
                base_url=embedding_base_url,
                api_key=SecretStr(embedding_api_key) if embedding_api_key else None,
            )

            all_exist = all(os.path.exists(p) for p in rfc_paths)
            if cache_root is not None and all_exist:
                cache_key = _cache_key_for_files(
                    rfc_paths=rfc_paths,
                    chunk_size=splitter._chunk_size,  # type: ignore[attr-defined]
                    chunk_overlap=splitter._chunk_overlap,  # type: ignore[attr-defined]
                    embeddings=embeddings,
                )
                cache_dir = cache_root / cache_key
                cached = _load_faiss_cache(cache_dir, embeddings)
                if cached is not None:
                    UI.dim(f"RAG cache hit: {cache_dir}")
                    return cached.as_retriever(search_kwargs={"k": 4})

            # Cache miss -> build index from all RFCs.
            all_docs = []
            for rfc_path in rfc_paths:
                if not os.path.exists(rfc_path):
                    UI.warn(f"RFC file not found, skipping: {rfc_path}")
                    continue
                if rfc_path.endswith(".pdf"):
                    loader = PyPDFLoader(rfc_path)
                else:
                    loader = TextLoader(rfc_path, encoding="utf-8")
                docs = loader.load()
                # Tag each document with its source for traceability
                src = os.path.basename(rfc_path)
                for d in docs:
                    d.metadata.setdefault("source", src)
                all_docs.extend(docs)

            if not all_docs:
                raise FileNotFoundError(f"None of the specified RFC files could be loaded: {rfc_paths}")

            chunks = splitter.split_documents(all_docs)
            UI.dim(f"  Loaded {len(all_docs)} docs / {len(chunks)} chunks from {len(rfc_paths)} RFC(s)")
            vectorstore = FAISS.from_documents(chunks, embeddings)

            if cache_root is not None and all_exist:
                try:
                    cache_key = _cache_key_for_files(
                        rfc_paths=rfc_paths,
                        chunk_size=splitter._chunk_size,  # type: ignore[attr-defined]
                        chunk_overlap=splitter._chunk_overlap,  # type: ignore[attr-defined]
                        embeddings=embeddings,
                    )
                    cache_dir = cache_root / cache_key
                    _save_faiss_cache(vectorstore, cache_dir)
                    UI.dim(f"RAG cache saved: {cache_dir}")
                except Exception as e:
                    UI.dim(f"RAG cache save skipped: {e}")

            return vectorstore.as_retriever(search_kwargs={"k": 4})
        except Exception as e:
            UI.error(f"RAG setup failed: {e}")
            raise
