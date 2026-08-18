# LLM-Generated Mutators Framework — Peach Pipeline

LLM-assisted generator that reads an RFC (PDF/text) via RAG, prompts an LLM to produce **protocol-aware C# fuzzing code** for the **Peach Fuzzer**, and iteratively validates/fixes the output.

## Requirements

- Python 3.10+
- [Docker](https://docs.docker.com/get-docker/) — for Peach SDK setup and fuzzing images
- [Mono](https://www.mono-project.com/) — `mono` and `mcs` for compiling and running C# code

Python packages (see `requirements.txt`):

- `click`, `python-dotenv`, `rich`, `questionary`
- `langchain`, `langchain-core`, `langchain-community`, `langchain-openai`, `langgraph`
- `faiss-cpu`

## Setup

### 1. Environment variables

Create a `.env` file in the repo root. A minimal setup only needs `OPENAI_API_KEY`; all other variables are optional but recommended for production use.

```bash
# --- Required ---
OPENAI_API_KEY=sk-...your key...

# --- LLM (chat) ---
OPENAI_BASE_URL=   # Custom endpoint if not using OpenAI
LLM_MODEL=                            # Model for chat completion
LLM_TEMPERATURE=                         # Sampling temperature

# Peach-specific model overrides (fall back to LLM_MODEL / LLM_TEMPERATURE above)
# LLM_PEACH_MODEL=
# LLM_PEACH_TEMPERATURE=

# --- Embedding (RAG) ---
LLM_EMBEDDING_MODEL=    # Embedding model for RFC vector store
LLM_EMBEDDING_BASE_URL= # Embedding endpoint (falls back to OPENAI_BASE_URL)
LLM_EMBEDDING_API_KEY=     # Embedding API key (falls back to OPENAI_API_KEY)

# --- RAG cache ---
# RAG_CACHE_DIR=.cache/rag                     # Vector store cache directory (default)
# RAG_DISABLE_CACHE=1                          # Set to 1 to skip caching
```

**Why separate embedding config?** The RAG retriever (`rag.py`) uses `LLM_EMBEDDING_*` variables to create a FAISS vector store from the RFC. If your chat LLM provider doesn't support embeddings (e.g., some DeepSeek endpoints), you can point embeddings to a different provider by setting `LLM_EMBEDDING_BASE_URL` and `LLM_EMBEDDING_API_KEY`.

### 2. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Set up Peach SDK

```bash
./setup.sh peach
```

This step requires Docker and Mono. It:

- Pulls `pdli/llm-peach:sdk` (linux/amd64)
- Extracts essential DLLs into `peach/sdk/` (Peach.Core, NLog, NUnit, etc.)
- Generates `peach/README.md` — the LLM-Peach SDK API reference used by the LLM during code generation
- Generates `peach/peach.txt` — the Peach XML element reference, filtered to Analyzer/DataElement/Relation/Transformer sections

## Quickstart

```bash
python3 main.py --protocol mqtt --seed-dir tests/seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf --target peach
```

- The pipeline is **interactive**. Before each step it prompts: **Continue / Retry previous / Skip / Exit**.
- If you do nothing, it auto-continues after ~60 seconds.
- The RFC can be a `.pdf` or a text file.

## Pipeline steps

| Step | Description |
|------|-------------|
| 1. Packet Types Extraction | Extracts all packet types from the RFC via RAG search. |
| 2. Datamodel Generation | Generates a **Peach Pit XML** file (`datamodel.xml`) defining the binary structure of each packet type — fields, types, relations, optional blocks, and packet union. |
| 3. Datamodel Validation & Fix | Parses seed files through the datamodel, re-serializes, and compares byte-for-byte. On failure, an existing diagnosis can be reused or a diagnosis agent uses `Read_File`, `RFC_Search`, and `Write_File` to produce one. The auto-fix agent then reads only that diagnosis and the current DataModel—never raw validator output or logs. Up to 3 auto-retries, then interactive fallback. |
| 4. Mutator Generation | Generates **C# mutator classes** per field per packet type. Each inherits from `LLMMutator` and covers `Add`/`Remove`/`Repeat`/`Mutate` semantics. Parallelized with 4 workers. |
| 5. Mutator Validation & Fix | Runs 100 mutation iterations per mutator × seed × element. Each iteration: clone → mutate → serialize → re-parse. Failures trigger LLM fixes. |
| Final Compilation | Compiles all `.cs` files into a single `{PROTO}.dll`. |

## What gets generated

```
llm/peach/<proto>/
├── datamodel.xml                       # Peach Pit XML datamodel
├── <PROTO>.dll                         # Final compiled DLL
├── dm_test_logs/                       # DataModel test logs (deleted on pass)
├── Mutators/
│   ├── <Proto><PktType>Mutators.cs     # Mutator source per packet type
│   └── out/
│       └── <Proto><PktType>Mutators.dll
└── mutator_test_logs/
    ├── fail/                           # Mutation re-parse failures
    └── error/                          # Mutation exceptions
```

## Running checks manually

Datamodel validation:

```bash
./tests/datamodel/run_datamodel_test.sh mqtt tests/seeds/mqtt
```

Mutator sanity:

```bash
./tests/peach_mutator/run_peach_mutator_test.sh mqtt tests/seeds/mqtt
```

## Logs and state

- `tool_usage.log` — records LLM tool calls. Reset on each run.
- `.pipeline_state.json` — caches pipeline state (packet types, token usage) so you can resume interrupted runs.

## Troubleshooting

- **`peach/sdk/` missing**: run `./setup.sh peach`.
- **Docker not found**: install Docker Desktop or Docker Engine.
- **Mono not found**: `brew install mono` on macOS, or `apt install mono-complete` on Linux.
- **RAG setup fails**: the pipeline still runs without RAG, but RFC grounding will be weaker. Ensure the RFC file exists and `faiss-cpu` is installed.
- **OpenAI / API auth errors**: verify `OPENAI_API_KEY` in `.env`. If using a custom endpoint, check `OPENAI_BASE_URL`.
- **Embedding API errors**: your chat LLM provider may not support embeddings. Set `LLM_EMBEDDING_BASE_URL` and `LLM_EMBEDDING_API_KEY` to point to a provider that does.
- **MCS compilation errors**: verify Mono is installed and `mcs` is on `PATH`.
- **Pipeline state corruption**: delete `.pipeline_state.json` and restart.
