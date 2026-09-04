# LLM-Generated Mutators Framework — Peach Pipeline

LLM-assisted generator that reads an RFC (PDF/text) via RAG, prompts an LLM to produce **protocol-aware C# fuzzing code** for the **Peach Fuzzer**, and iteratively validates/fixes the output.

## Requirements

- Python 3.10+
- [Docker](https://docs.docker.com/get-docker/) — for Peach SDK setup and fuzzing images
- [Mono](https://www.mono-project.com/) — `mono` and `mcs` for compiling and running C# code
- `xmllint` (libxml2) — validates DSL-compiled Peach XML against `peach/peach.xsd`
- Node.js `>=22.13.0` and npm — only required for the bundled Pit visualizer

Python packages (see `requirements.txt`):

- `click`, `python-dotenv`, `rich`, `questionary`
- `langchain`, `langchain-core`, `langchain-community`, `langchain-openai`, `langgraph`
- `faiss-cpu`

## Project structure

- `core/` — runtime core for configuration, agents, RAG, tools, UI,
  logging, state, and typed tool results.
- `pipeline/` — Peach pipeline orchestration and step implementations.
- `peach_dsl/` — declarative DataModel DSL, compiler, error mapping, and tests.
- `core/datamodel_dsl.py` and `core/datamodel_diagnoser.py` — standalone CLI entry points.
- `pit-visualizer/` — optional browser-based Pit Studio frontend.

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

# Peach DSL DataModel generation (optional)
# LLM_PEACH_DATAMODEL_GROUP_SIZE=4
# LLM_PEACH_DATAMODEL_WORKERS=6
# LLM_PEACH_DATAMODEL_ASSEMBLY_RETRIES=2

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
pip install -e ./peach_dsl
```

The editable install exposes the `peach_dsl` package and the `peach-dsl`
compiler command. The package can also be installed independently; its only
runtime dependency is Pyright, used to validate generated DSL modules. From
inside the `peach_dsl` directory, install it with `pip install -e .`.

### 3. Set up Peach SDK

```bash
./setup.sh
```

This step requires Docker and Mono. It:

- Pulls `pdli/llm-peach:sdk` (linux/amd64)
- Extracts essential DLLs into `peach/sdk/` (Peach.Core, NLog, NUnit, etc.)
- Generates `peach/README.md` — the LLM-Peach SDK API reference used by the LLM during code generation
- Generates `peach/peach.txt` — the Peach capability reference used during custom DataElement discovery

## Quickstart

```bash
python3 main.py --protocol mqtt --seed-dir tests/seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf
```

- The pipeline is **interactive**. Before each step it prompts: **Continue / Retry previous / Skip / Exit**.
- If you do nothing, it auto-continues after ~60 seconds.
- The RFC can be a `.pdf` or a text file.
- The generated DataModel source uses the DSL language described in
  [`docs/peach-dsl.md`](docs/peach-dsl.md).

### Recompile split DataModel DSL

Validate the manifest and DSL modules without replacing the current
`datamodel.xml`:

```bash
.venv/bin/python -m core.datamodel_dsl <protocol> --check
```

Run the actual compilation after resolving reported conflicts:

```bash
.venv/bin/python -m core.datamodel_dsl <protocol>
```

The command runs strict Pyright syntax/type checking, then reports invalid DSL,
missing symbols/models, and duplicate DataModel names. Use `--dsl-dir` or
`--output` to override paths. DSL execution is currently local; it is explicitly
scheduled to move into a hardened, network-disabled Docker compiler container.

## Pipeline steps

| Step | Description |
|------|-------------|
| 1. Packet Types Extraction | Extracts all packet types from the RFC via RAG search. |
| 2. Datamodel Planning & Generation | One planning call reports only concrete protocol-field encodings that require custom scalar support and produces the split DataModel manifest from the same RFC evidence. Unsupported scalars require explicit approval and receive protocol-prefixed `ExtendedType` names. `shared_model.py` is generated and Pyright-checked first; family modules are then generated in parallel and Pyright-checked before one integrated compilation. |
| 3. Datamodel Validation & Fix | Parses seeds through the compiled Pit and compares re-serialized bytes. The DSL mechanically converts every Peach report node to compact DSL-path text in `datamodel_error_report.txt`; it performs no root-cause selection. A diagnosis agent analyzes that text and produces a repair plan; the auto-fix agent edits only DSL and recompiles. Up to 3 auto-retries, then interactive fallback. |
| 4. Mutator Generation | Generates **C# mutator classes** per field per packet type. Each inherits from `LLMMutator` and covers `Add`/`Remove`/`Repeat`/`Mutate` semantics. Parallelized with 4 workers. |
| 5. Mutator Validation & Fix | Runs 100 mutation iterations per mutator × seed × element. Each iteration: clone → mutate → serialize → re-parse. Failures trigger LLM fixes. |
| Final Compilation | Compiles all `.cs` files into a single `{PROTO}.dll`. |

## What gets generated

```
llm/peach/<proto>/
├── data_type_analysis.json             # Unsupported protocol field encodings
├── DataElements/                       # Approved protocol-specific Peach DOM plugins
│   ├── manifest.json                   # Wire type → Pit element/class mapping
│   └── out/<PROTO>DataElements.dll     # Plugin loaded by validators and images
├── datamodel.xml                       # Derived Peach runtime artifact; never edit
├── datamodel_error_report.txt          # Compact Peach failures converted to DSL paths
├── datamodel_diagnosis.json            # LLM root-cause and repair plan
├── datamodel_dsl/
│   ├── schema_manifest.json            # Shared-model and packet-family contract
│   ├── shared_model.py                 # Shared schemas and custom type declarations
│   ├── family_<family>.py              # Independently generated packet schemas
│   └── root.py                         # Deterministically generated entry module
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
and editor. It renders the compiled packet model as a protocol canvas or
topology tree, supports inspecting/exporting Pit XML, and can highlight the
root cause from a diagnosis JSON generated by `core/datamodel_diagnoser.py`.

`datamodel.xml` is derived from the DSL. Edits exported from Pit Studio are not
source changes and will be overwritten by the next DSL compilation.

```bash
cd pit-visualizer
npm ci
npm run dev
```

See [`pit-visualizer/README.md`](pit-visualizer/README.md) for validation and
diagnosis-import instructions.

## Logs and state

- `logs/<protocol>/tool_usage.jsonl` — records LLM tool lifecycle events.
- `logs/<protocol>/pipeline_state.json` — caches pipeline state (packet types, token usage) so you can resume interrupted runs.

## Troubleshooting

- **`peach/sdk/` missing**: run `./setup.sh`.
- **Docker not found**: install Docker Desktop or Docker Engine.
- **Mono not found**: `brew install mono` on macOS, or `apt install mono-complete` on Linux.
- **RAG setup fails**: the pipeline still runs without RAG, but RFC grounding will be weaker. Ensure the RFC file exists and `faiss-cpu` is installed.
- **OpenAI / API auth errors**: verify `OPENAI_API_KEY` in `.env`. If using a custom endpoint, check `OPENAI_BASE_URL`.
- **Embedding API errors**: your chat LLM provider may not support embeddings. Set `LLM_EMBEDDING_BASE_URL` and `LLM_EMBEDDING_API_KEY` to point to a provider that does.
- **MCS compilation errors**: verify Mono is installed and `mcs` is on `PATH`.
- **Pipeline state corruption**: delete `logs/<protocol>/pipeline_state.json` and restart.
