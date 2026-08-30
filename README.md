# LLM-Generated Mutators Framework — Peach Pipeline

LLM-assisted generator that reads an RFC (PDF/text) via RAG, prompts an LLM to produce **protocol-aware C# fuzzing code** for the **Peach Fuzzer**, and iteratively validates/fixes the output.

## Requirements

- Python 3.10+
- [Docker](https://docs.docker.com/get-docker/) — for Peach SDK setup and fuzzing images
- [Mono](https://www.mono-project.com/) — `mono` and `mcs` for compiling and running C# code
- `xmllint` (libxml2) — validates generated Peach XML against `peach/peach.xsd`
- Node.js `>=22.13.0` and npm — only required for the bundled Pit visualizer

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

# Peach DataModel generation (optional)
# LLM_PEACH_DATAMODEL_SPLIT=auto       # auto, always, or never
# LLM_PEACH_DATAMODEL_SPLIT_THRESHOLD=6
# LLM_PEACH_DATAMODEL_GROUP_SIZE=4
# LLM_PEACH_DATAMODEL_WORKERS=4
# LLM_PEACH_DATAMODEL_ASSEMBLY_RETRIES=2
# Family agents have a hard limit of 3 repair attempts during early validation.

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

### Re-run split DataModel assembly

Validate the manifest and fragments without replacing the current
`datamodel.xml`:

```bash
.venv/bin/python datamodel_split.py <protocol> --check
```

Run the actual assembly after resolving reported conflicts:

```bash
.venv/bin/python datamodel_split.py <protocol>
```

Ask the integration repair agent to fix existing fragments and then assemble
them, without rerunning schema planning or packet generation:

```bash
python3 main.py --protocol <protocol> --seed-dir <seed-dir> \
  --rfc-path <rfc-file> --target peach --repair-datamodel-assembly
```

The command reports missing fragments/models, undeclared references, unexpected
top-level XML elements, and every duplicate DataModel together with its source
files. Use `--fragment-dir` or `--output` to override the default paths.

## Pipeline steps

| Step | Description |
|------|-------------|
| 1. Packet Types Extraction | Extracts all packet types from the RFC via RAG search. |
| 1.5. Peach Basic Data Type Support | Uses one bounded analysis call and a locally compacted Peach capability catalog to classify protocol wire primitives as supported, unsupported, or uncertain. Uncertain results pause for manual review. Unsupported results are shown with evidence and require explicit approval before one custom DOM generation/compile call. Generation uses the repository-owned, compile-tested `examples/ExampleEscapedUInt.cs` API example and is not given sibling-project DOM implementations as references. Saved analysis is reused on retry. |
| 2. Datamodel Generation | Generates a **Peach Pit XML** file (`datamodel.xml`). In split mode, schema planning and binary-safe seed classification each have an independent run/skip prompt; skipped tasks reuse their existing JSON when available, while selected tasks still run concurrently. Shared/family generators must read `peach.txt`; each eligible family agent calls its validation tool, waits for `shared.xml`, and may repair its own fragment at most three times. Deterministic assembly places every referenced DataModel before its consumer, rejects cycles, can run two integration repairs, and never falls back to single-agent generation. |
| 3. Datamodel Validation & Fix | Parses seed files through the datamodel, re-serializes, and compares byte-for-byte. On failure, a read-only diagnosis agent returns a short summary and at most three actionable issues; the pipeline saves the report. The auto-fix agent then reads only that diagnosis and the current DataModel. Up to 3 auto-retries, then interactive fallback. |
| 4. Mutator Generation | Generates **C# mutator classes** per field per packet type. Each inherits from `LLMMutator` and covers `Add`/`Remove`/`Repeat`/`Mutate` semantics. Parallelized with 4 workers. |
| 5. Mutator Validation & Fix | Runs 100 mutation iterations per mutator × seed × element. Each iteration: clone → mutate → serialize → re-parse. Failures trigger LLM fixes. |
| Final Compilation | Compiles all `.cs` files into a single `{PROTO}.dll`. |

## What gets generated

```
llm/peach/<proto>/
├── data_type_analysis.json             # RFC/Peach primitive compatibility report
├── DataElements/                       # Approved protocol-specific Peach DOM plugins
│   ├── manifest.json                   # Wire type → Pit element/class mapping
│   └── out/<PROTO>DataElements.dll     # Plugin loaded by validators and images
├── datamodel.xml                       # Peach Pit XML datamodel
├── datamodel_fragments/
│   ├── schema_manifest.json            # Shared-model and packet-family contract
│   ├── seed_classification.json        # Binary-safe seed packet classification
│   ├── shared.xml                      # Shared DataModels
│   └── packet_<family>.xml             # Independently generated family fragments
├── datamodel_family_validation/        # Early single-packet family models/logs
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

## Pit visualizer

The repository includes **Pit Studio**, a browser-based Peach Pit visualizer
and editor. It renders the generated packet model as a protocol canvas or
topology tree, supports editing and exporting Pit XML, and can highlight the
root cause from a diagnosis JSON generated by `datamodel_diagnoser.py`.

```bash
cd pit-visualizer
npm ci
npm run dev
```

See [`pit-visualizer/README.md`](pit-visualizer/README.md) for validation and
diagnosis-import instructions.

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
