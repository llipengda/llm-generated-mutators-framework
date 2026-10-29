# LLM-Generated Mutators Framework — Peach Pipeline

LLM-assisted generator that reads an RFC (PDF/text) via RAG, prompts an LLM to produce **protocol-aware C# fuzzing code** for the **Peach Fuzzer**, and iteratively validates/fixes the output.

## Requirements

- Python 3.10+ (the checked-in `.python-version` selects Python 3.13 by default)
- [uv](https://docs.astral.sh/uv/) — Python version, virtual environment, and dependency management
- .NET SDK supporting .NET 8, plus the .NET 8 runtime
- The `llm-peach` source checkout (defaults to the sibling `../llm-peach/`)
- `xmllint` (libxml2) — validates DSL-compiled Peach XML against `peach/peach.xsd`
- Node.js `>=22.13.0` and npm — only required for the bundled Pit visualizer

Python packages are declared in `pyproject.toml` and locked in `uv.lock`:

- `click`, `python-dotenv`, `rich`, `questionary`
- `langchain`, `langchain-core`, `langchain-community`, `langchain-openai`, `langgraph`
- `faiss-cpu`

## Project structure

- `core/` — runtime core for configuration, agents, RAG, tools, UI,
  logging, state, and typed tool results.
- `pipeline/` — Peach pipeline orchestration and step implementations.
- `prompts/` — editable model prompts and tool descriptions, with fixed rules
  and output formats written directly in each task; see [the prompt index](prompts/README.md).
- `peach_dsl/` — declarative DataModel DSL, compiler, error mapping, and tests.
- `core/datamodel_dsl.py` and `core/datamodel_diagnoser.py` — standalone CLI entry points.
- `experiments/mqtt/` — MQTT Random / LLM fuzzing and coverage comparison scripts.
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
# For OpenAI-compatible local models returning `reasoning_content`:
# LLM_REASONING_FORMAT=deepseek         # Sends {"reasoning_format":"deepseek"}; also prints it
# LLM_PRINT_REASONING=1                 # Print returned reasoning_content without sending a format

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
uv sync
```

`uv sync` installs the locked dependencies into `.venv` and installs the local
`peach_dsl` package in editable mode. It exposes both the `peach_dsl` package
and the `peach-dsl` compiler command. Use `uv add <package>` to add a dependency
and `uv lock --upgrade-package <package>` to upgrade one deliberately.

### 3. Set up Peach SDK

```bash
./setup.sh
```

Setup publishes Peach, PitTool and the three validators from local llm-peach
sources for .NET 8. The complete runtime, reference assemblies, dependencies,
plugins and runtime configuration files are installed in `peach/sdk/`. It also
generates `peach/peach.xsd`, `peach/peach.txt` and the SDK API reference.
An existing installation is backed up before replacement.

Use another source checkout or an existing installation directory with:

```bash
./setup.sh --source /path/to/llm-peach
# Optional: use the same installation override for setup and pipeline commands.
export PEACH_SDK_DIR=/path/to/peach-runtime
./setup.sh
```

The `codex/legacy-support` branch preserves the previous backend and scripts.
The main branch uses local .NET compilation and execution throughout.

## Quickstart

```bash
uv run python main.py --protocol mqtt --seed-dir seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf
```

Use `--language zh-CN` to request Chinese reasoning and natural-language output
from every model stage, including diagnosis and repair. The default is `en`;
language codes or names such as `English`, `中文`, and `Japanese` are accepted.
This requirement is included in each agent's system prompt and repeated in the
user task on every model request, including tool continuations. Chinese selections
such as `zh-CN`, `zh`, `Chinese`, and `中文` use a native Chinese instruction.
Code identifiers, protocol literals, JSON keys, and schema enum values retain
their required forms.
Reasoning language compliance depends on the model; returned reasoning content
is covered by the same instruction. This option does not translate the CLI UI.

```bash
uv run python main.py --protocol mqtt --seed-dir seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf --language zh-CN
```

Generated protocol code uses C# 12 and targets .NET 8. Validation runs in a
temporary local runtime directory containing the selected protocol's plugins;
logs and replay samples remain in the protocol output directory.

- The pipeline is **interactive**. Before each step it prompts: **Continue / Retry previous / Skip / Exit**.
- If you do nothing, it auto-continues after ~60 seconds.
- Resuming an already completed run opens a menu to return to any step or exit.
  This menu waits for your selection; the selected step then uses the normal step prompt.
- The RFC can be a `.pdf` or a text file.
- The generated DataModel source uses the DSL language described in
  [`docs/peach-dsl.md`](docs/peach-dsl.md).

### Recompile split DataModel DSL

Validate the manifest and DSL modules without replacing the current
`datamodel.xml`:

```bash
uv run python -m core.datamodel_dsl <protocol> --check
```

Run the actual compilation after resolving reported conflicts:

```bash
uv run python -m core.datamodel_dsl <protocol>
```

The command runs strict Pyright syntax/type checking, then reports invalid DSL,
missing symbols/models, and duplicate DataModel names. Use `--dsl-dir` or
`--output` to override paths. DSL compilation and validation execute locally.

### Preview a mutator plan

```bash
uv run python -m core.mutator_codegen plan http --output tmp/http-mutator-plan/plan.yaml
```

The compact YAML previews DSL candidates before semantic-value assessment. It
groups packets with identical target operations and uses
packet-relative field paths. Conditional Optional source fields appear under
`presence_sources`. Class names, signatures and field details are derived from
the DSL and SDK. This command only exports a review plan; omit `--output` to
print it to stdout.

## Pipeline steps

| Step | Description |
|------|-------------|
| 1. Packet Types Extraction | Extracts all packet types from the RFC via RAG search. |
| 2. Datamodel Planning & Generation | One planning call reports only concrete protocol-field encodings that require custom scalar support and produces the split DataModel manifest from the same RFC evidence. Unsupported scalars require explicit approval and receive protocol-prefixed `ExtendedType` names. `shared_model.py` is generated and Pyright-checked first; family modules are then generated in parallel and Pyright-checked before one integrated compilation. |
| 3. Datamodel Validation & Fix | Parses seeds through the compiled Pit and compares re-serialized bytes. The test script immediately converts every Peach report node to compact DSL-path text in `datamodel_error_report.txt`; it performs no root-cause selection. A diagnosis agent analyzes that text and produces a repair plan; the auto-fix agent edits only DSL and recompiles. Up to 3 auto-retries, then interactive fallback. |
| 4. Mutator Generation | Dispatches packet workers first. Each packet agent assesses all candidate fields against ordinary random mutation, returning only keep/skip and a short reason. The host saves `.assessment.json` and generates `.g.cs` only for retained fields; the LLM implements `.semantic.cs`, then the packet compiles. Up to 4 packets run in parallel. |
| 5. Mutator Validation & Fix | Runs 100 mutation iterations per mutator × seed × element. Reports ERROR if any iteration throws; otherwise summarizes total, PASS, FAIL and UNCHANGED counts. Mutators with ERRORs or PASS=0 must be repaired or disabled. Repairs are grouped by packet's semantic file. Disabling persists per class in the manifest and generates selectors that always return false. Both choices rebuild and retest affected packets; unresolved issues prompt again. Compiler and incomplete-validation errors stop the pipeline; generated-code/SDK failures can be disabled but cannot be repaired by semantic agents. |
| Final Compilation | Compiles custom DataElement sources and active manifest mutator sources together into `{PROTO}.dll`, including the custom type definitions. |

See [semantic-value assessment](docs/mutator-assessment.md) for selection,
source ownership and regeneration behavior, and the [SDK reference](docs/llm-peach-sdk.md)
for implementation APIs.

## What gets generated

```
llm/peach/<proto>/
├── data_type_analysis.json             # Unsupported protocol field encodings
├── DataElements/                       # Approved protocol-specific Peach DOM plugins
│   ├── manifest.json                   # Wire type → Pit element/class mapping
│   └── out/<PROTO>DataElements.dll     # Plugin loaded by validators and native bundles
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
│   ├── mutator_manifest.json          # Active packets, interfaces and source files
│   ├── <Proto><PktType>Mutators.assessment.json # Per-field keep/skip reasons
│   ├── <Proto><PktType>Mutators.g.cs   # Deterministic classes and selectors
│   ├── <Proto><PktType>Mutators.semantic.cs # LLM-written semantic methods
│   └── out/
│       └── <Proto><PktType>Mutators.dll
└── mutator_test_logs/
    ├── summary.log                     # Saved mutator summary ([P], [N], [-])
    ├── fail/                           # Re-parse failures, including unconsumed input
    ├── error/                          # Mutation exceptions
    └── unchanged/                      # Serialized packet unchanged after mutation
```

## Running checks manually

Datamodel validation:

```bash
./tests/datamodel/run_datamodel_test.sh mqtt seeds/mqtt
```

Mutator sanity:

```bash
./tests/peach_mutator/run_peach_mutator_test.sh mqtt seeds/mqtt
```

Mutator iterations are `PASS` only when the serialized packet changes and the
entire input is re-parsed. Parse exceptions or remaining bytes/bits are `FAIL`;
mutation exceptions are `ERROR`; identical serialized packets are `UNCHANGED`.
Path selectors exclude elements beneath absent Optional ancestors. Generated
Mutate selectors also require an Optional target itself to exist; Add can still
select an absent Optional when its ancestors exist.
After each mutator completes, the terminal and `summary.log` print one line
with its iteration counts, for example:
`[P] MyMutator TOTAL=100 PASS=60 FAIL=25 ERROR=0 UNCHANGED=15`.
`[N]` means an ERROR occurred or no iteration passed; `[-]` means skipped.
Step 5 accepts `[P]`, skips `[-]`, and requires repair or persistent disabling
for every `[N]`, followed by rebuilding and retesting affected packets. Individual
FAIL and UNCHANGED outcomes are reported without action when at least one
iteration passes. Repairs receive the counts and available ERROR, FAIL and UNCHANGED logs for each `[N]` class.
Each issue category has its own logs and replay sample under `mutator_test_logs/`.
The local `mutator_test_logs/summary.log` keeps one row per mutator: `[P]` means
at least one iteration passed without any ERROR, `[N]` means no iteration passed or mutation
threw an error, and `[-]` means the mutator was skipped.

Step 5 reads only the current compact report format, checks it against the active
manifest, and groups repairs by packet. Skipping verification reuses `results.txt`
(or `summary.log` from a standalone test); an incomplete report or a failed command
cannot be reused as a successful result. Old-format reports must be regenerated.

Generate native fuzzing bundles and run one against the target:

```bash
./peach_gen.sh mqtt
HOST=127.0.0.1 PORT=1883 ./llm/peach/mqtt/native/llm/run.sh
```

The `native/llm/` and `native/peach/` bundles contain a local runtime, protocol
plugins, DataModel, Pit and seeds. `SEED_DIR` and `LOG_DIR` can override the
bundle defaults; arguments to `run.sh` are forwarded to Peach.

To compare the current MQTT artifacts with the Peach installation in `~/peach`:

```bash
./experiments/mqtt/mqtt_fuzz.sh                         # Both strategies, separate coverage containers, 10 minutes
./experiments/mqtt/mqtt_fuzz.sh random                  # Random, without generated LLM mutators
./experiments/mqtt/mqtt_fuzz.sh llm                     # TwoPhaseRandom, with generated LLM mutators
./experiments/mqtt/mqtt_fuzz.sh both --duration 00:30:00 --seed 123
./experiments/mqtt/mqtt_fuzz.sh both --iterations 20    # Short smoke run
```

This launcher uses the existing compiled artifacts in `llm/peach/mqtt/`;
recompile changed mutators before running. It creates fresh `pdli/mosquitto:latest`
containers with automatically allocated localhost ports and isolated plugin
directories. The Pit explicitly names its MQTT root so generated selectors
match. Both strategies use the same seeds and RNG seed;
the LLM strategy defaults to three mutations per element (`--mutations`).
Docker, the coverage image, `uv`, and the runtime required by `~/peach` must
already be installed.

Logs, effective Pits, plugin snapshots, input hashes, final `coverage/final.info`,
coverage summaries, and exit/mutator-call statistics are saved under
`logs/mqtt/campaign-*`. Ctrl-C stops both fuzzers, collects coverage, and removes
their containers. A Peach error produces a nonzero exit status and remains in
`console.log` and `results.json`; the launcher does not retry it automatically.

Use `--peach-dir /path/to/runtime`, `--seed-dir /path/to/seeds`, or
`--prepare-only` to override the installation, select seeds, or inspect the
generated configuration. To use existing targets without Docker or coverage
collection:

```bash
./experiments/mqtt/mqtt_fuzz.sh llm --host 127.0.0.1 --port 1883
./experiments/mqtt/mqtt_fuzz.sh both --host 127.0.0.1 --port 1883 --llm-port 1884
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

- `logs/<protocol>/log.jsonl` — unified log for tool lifecycle events, pipeline steps, validation/subprocess output, UI warnings/errors, model reasoning, and LLM/runtime exceptions with tracebacks. Runs are appended rather than overwritten; records include UTC timestamps, session IDs, protocol, and severity, and the viewer can filter each run separately.
- `logs/<protocol>/pipeline_state.json` — caches pipeline state (packet types, token usage) so you can resume interrupted runs.

### Log viewer

Generate and open a self-contained, offline HTML viewer (no frontend dependencies
or server needed):

```bash
uv run python -m core.log_viewer mqtt
uv run python -m core.log_viewer logs/mqtt/log.jsonl
# Open an empty viewer, then choose or drag in a JSONL file:
uv run python -m core.log_viewer
# Generate a snapshot without launching the browser:
uv run python -m core.log_viewer mqtt --no-open --output tmp/mqtt-log.html
```

The default output is `tmp/log-viewer.html`. The viewer groups tool start/end/error
records by session and call ID, shows inputs, outputs and duration together, and
highlights exceptions and tracebacks. Validation and subprocess output use colored
`[PASS]`, `[FAIL]`, and `[ERROR]` lines. Filter by event category or session, search
full record contents, or show errors only (optionally including warnings).
The compact left column includes file paths, search queries, line ranges, and patch
sizes. Timestamps use the browser's local timezone (shown on hover over the detail timestamp); original
UTC timestamps remain in the raw JSON. Tool details have dedicated layouts for
file contents with line numbers, before/after patches, search matches and RFC
passages, DSL/XML validation, and DLL compilation. Legacy ToolMessage string
wrappers are decoded, and structured `ok: false` results count as failures even
when the tool callback itself completed normally. Original JSON and source line
numbers remain available for every entry. New logs include `task_id` and
`task_name` for each parallel worker and Agent invocation. Worker setup, tool
callbacks, diagnostics, and postchecks share the same task; retries get a new ID.
The viewer displays a compact task label and can filter by task. Older records
without task fields remain available under “无任务标识”.

Legacy `tool_usage.jsonl` files can be opened by path as well. Invalid or unfinished
JSON lines are skipped with a visible line-number report; unmatched calls remain
visible. The full filtered list is shown newest first. This is a snapshot viewer: choose/drop the file again or rerun the command
to see newly appended logs. Files are read locally and no data is uploaded; generated
HTML includes the loaded log contents.

Viewer checks:

```bash
uv run python -m unittest tests.test_log_viewer
node --test tests/test_log_viewer.mjs
```

## Troubleshooting

- **`peach/sdk/` missing**: run `./setup.sh`.
- **dotnet not found**: install a .NET SDK supporting .NET 8 and the .NET 8 runtime.
- **Validator runtime configuration missing**: rerun `./setup.sh` to install the complete runtime, including `.deps.json` and `.runtimeconfig.json` files.
- **RAG setup fails**: the pipeline still runs without RAG, but RFC grounding will be weaker. Ensure the RFC file exists and `faiss-cpu` is installed.
- **OpenAI / API auth errors**: verify `OPENAI_API_KEY` in `.env`. If using a custom endpoint, check `OPENAI_BASE_URL`.
- **Embedding API errors**: your chat LLM provider may not support embeddings. Set `LLM_EMBEDDING_BASE_URL` and `LLM_EMBEDDING_API_KEY` to point to a provider that does.
- **C# compilation errors**: inspect the .NET compiler output and the installed SDK APIs.
- **Pipeline state corruption**: delete `logs/<protocol>/pipeline_state.json` and restart.
