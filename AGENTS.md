# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project overview

LLM-assisted generator that reads one or more RFCs (PDF/text) via RAG, prompts an LLM to produce protocol-aware C# fuzzing code, and iteratively validates/fixes the output for **Peach**.

## Build, test, and lint

All Python commands in this repository must use uv. Run project code and tests
with `uv run`; manage dependencies with `uv add`, `uv remove`, and `uv sync`.
Do not invoke the system Python or pip directly.

```bash
# Install the locked Python version and dependencies
uv sync

# Publish the local llm-peach checkout for .NET 8
./setup.sh
# Or use another source checkout
./setup.sh --source /path/to/llm-peach

# Run full pipeline (interactive, auto-continues after 60s)
uv run python main.py --protocol mqtt --seed-dir seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf

# Multiple RFCs can be specified by repeating --rfc-path
uv run python main.py --protocol someip --seed-dir seeds/someip \
    --rfc-path rfc/someip.pdf --rfc-path rfc/someip-sd.pdf

# Peach sanity checks
uv run python -m core.datamodel_dsl mqtt --check
./tests/datamodel/run_datamodel_test.sh mqtt seeds/mqtt
./tests/peach_mutator/run_peach_mutator_test.sh mqtt seeds/mqtt

# Generate native Peach fuzzing bundles
./peach_gen.sh mqtt
HOST=127.0.0.1 PORT=1883 ./llm/peach/mqtt/native/llm/run.sh
```

No linter or type-checker is configured. A `.env` file with `OPENAI_API_KEY` is required.

## Python typing

Avoid `typing.Any` whenever practical. Prefer precise concrete types, union
types, `TypedDict`, `Protocol`, type variables, or explicit JSON-compatible
recursive types. Use `Any` only at an unavoidable untyped third-party boundary,
keep its scope as narrow as possible, and document why it is required.

## Architecture

```
main.py                  # Peach pipeline CLI entry point (click).
core/                    # Runtime core: agent, config, state, RAG, tools, UI, logging, and result types.
peach_dsl/               # Typed declarative DataModel DSL, Pyright validation, compiler, and error mapping.
core/datamodel_dsl.py    # DSL manifest validation, deterministic root generation, and local compilation CLI.
core/mutator_codegen.py  # DSL mutator plan, generated partial classes, manifest and scoped compilation.
core/peach_runtime.py    # Local dotnet validator execution, protocol plugin isolation and logs.
docs/peach-dsl.md        # Authoritative DSL language reference for agents and developers.
pipeline/base.py         # BasePipeline: step orchestration loop, call_agent(), token tracking, state save.
pipeline/peach.py        # Lightweight PeachPipeline composition root and step ordering.
pipeline/peach_steps/    # Peach implementations split by discovery, DataModel, mutator, and compilation steps.
peach_gen.sh             # Compiles manifest mutators and generates native runtime/Pit bundles.
setup.sh                 # Publishes the sibling llm-peach checkout for .NET 8 into peach/sdk/.
```

### Peach pipeline steps (C# target)

1. Packet types extraction from RFC
2. Combined DSL type-support analysis and DataModel planning, then
   `shared_model.py`, followed by parallel family DSL generation → derived Peach Pit XML
3. Datamodel validation & DSL fix
4. Dispatch packet-local workers, assess candidate fields' semantic value against
   random mutation with brief keep/skip reasons, then generate `.g.cs` only for
   retained fields, LLM `.semantic.cs` implementations and scoped compilation
5. Mutator validation using current `[P]` / `[N]` / `[-]` reports: every `[N]` requires user-selected repair or persistent disabling; repairs are grouped by packet and affected packets are retested. `[P]` is accepted and `[-]` is skipped.
6. Final compilation of custom DataElements and active mutators.

Generated code lands in `llm/peach/<proto>/` with subdirectories `Mutators/`, `DataElements/`, and `datamodel_dsl/`.
Editable DataModel sources live in `datamodel_dsl/shared_model.py` and
`datamodel_dsl/family_<id>.py`; `root.py` and `datamodel.xml` are deterministic
derived artifacts and must not be hand-edited.
Assessment agents are read-only; implementation agents may edit only the packet's
`.semantic.cs`. Host-owned `.g.cs` and `mutator_manifest.json` define exact
interfaces and active sources; per-packet `.assessment.json` retains selection
reasons. See `docs/mutator-assessment.md`. Missing required methods must fail compilation.

### Key conventions

- **Global config**: `config.py` uses module-level mutable state. `build_config_from_args()` must be called before any getter.
- **Pipeline state**: Persisted to `logs/<proto>/pipeline_state.json` (gitignored) alongside tool lifecycle logs. Contains `packet_types`, `data_type_analysis`, and `token_usage_*`. Atomic writes via temp file + `os.replace()`. Older `.pipeline_state/<proto>.json` files are migrated automatically. On startup, if a saved state exists for the protocol, the user is asked whether to resume or start fresh.
- **Agent tools**: Agents get scoped file/DSL tools, CoreCLR DLL reflection, C# 12 compilation (`dotnet`), data validation, and RFC search as required by each step.
- **DSL source of truth**: DataModel agents must read `docs/peach-dsl.md`, write only generated DSL modules, and validate them with `Validate_Peach_DSL_Module` / `Validate_Peach_DSL`. Peach runtime scripts continue to consume the compiled `datamodel.xml`.
- **DataModel report conversion**: `tests/datamodel/run_datamodel_test.sh` converts Peach validator report nodes to compact DSL-path text in `datamodel_error_report.txt` immediately after validation. When every `@PacketUnion` candidate is rejected solely by its `packet_type` token, the converter keeps only the candidate that parsed furthest (first on ties); ordinary Unions are not filtered this way. The converter otherwise does not analyze, rank, deduplicate, or filter failures; the diagnosis agent performs root-cause analysis and writes `datamodel_diagnosis.json`. Repair agents read only the final diagnosis and referenced DSL modules.
- **Peach SDK**: `setup.sh` publishes the full .NET 8 runtime into `peach/sdk/`, including validators, dependencies and runtime configurations. `PEACH_SDK_DIR` can override this location. `dotnet_tools.py` uses CoreCLR and loads the SDK at import time; setup must run first. The `codex/legacy-support` branch preserves the prior backend.
- **Token tracking**: Each `call_agent()` creates a local `TokenUsageTracker` (per-invocation, thread-safe). Tracks `prompt_tokens`, `completion_tokens`, `cached_tokens`, and `calls` (LLM API invocations). Summarized at pipeline end.
- **Model configuration**: Set via environment variables. `LLM_PEACH_MODEL` / `LLM_PEACH_TEMPERATURE` override `LLM_MODEL` / `LLM_TEMPERATURE`. Defaults: `gpt-5.4` / 0.7.
- **`fix_verify_loop`**: Generic verify → fix → re-verify loop in `BasePipeline`. Up to 3 auto-retries, then interactive fallback (stop / provide hint and retry). Used by Peach Step 3. Step 5 asks the user to repair or disable `[N]` mutators after each verification, then rebuilds and retests affected packets. `[N]` means ERROR > 0 or PASS = 0; `[P]` is accepted even with some FAIL/UNCHANGED iterations; `[-]` is skipped.

## Sibling project: ../llm-peach

The `../llm-peach/` directory is the Peach Fuzzer engine + LLM SDK. The framework depends on it for base classes, custom DataElements, and validation executables.

### SDK Core (`llm/Core/`) — APIs used by generated code

**Base classes:**

| Class | File | Generated code inherits from it |
|-------|------|-------------------------------|
| `LLMMutator` | `Mutators/LLMMutator.cs` | Mutators: implement `PerformMutation(obj)` + static `supportedDataElement(obj)` |
| `LLMAddMutator` / `LLMRemoveMutator` / `LLMRepeatMutator` | `Mutators/LLM*Mutator.cs` | Structural operations; sealed `PerformMutation` inherited by generated partial classes |
| `CMutatorAttribute` | `Mutators/CMutatorAttribute.cs` | `[Mutator("name")]` attribute on mutator classes |

**Key extension methods** (`llm/Core/Extensions.cs`, namespace `Peach.LLM.Core`):

| Method | Purpose |
|--------|---------|
| `elem.Bytes()` | Convert element value to `byte[]` (handles Number big-endian, String ASCII, BitStream) |
| `elem.MatchesPath("Root.items[].value")` | Match the complete rooted DSL path, including array item indices |
| `elem.Get<T>()` | Typed access with element-kind validation and numeric/string/byte-stream conversion |

Set all mutations through `elem.SetValue(rawValue)` without a Variant wrapper.
Numeric/text values clear TypeTransform; byte[]/BitwiseStream values enable it.
Do not assign MutatedValue directly or manually toggle TypeTransform in generated mutators.

**Custom DataElements** (declared with DSL `ExtendedType`, compiled into `datamodel.xml`):

| Element | Purpose |
|---------|---------|
| `MqttVarInt` | MQTT variable-length integer (1-4 bytes, big-endian, max 268M) |
| `Optional` | Conditional Block — children only included when `expression` on `src` element evaluates true. Example: `src="flags" expression="(value & 0x04) != 0"` |

### Validators — how verification works

Each validator is a C# console app in `llm/Validations/`. Shell scripts in
`tests/` invoke `core.peach_runtime`, which runs them locally with `dotnet` in
isolated temporary runtime directories. Only the current protocol's plugins
are staged. `PEACH_LOG_DIR` directs cleanup and NLog to persistent per-protocol
logs. DataModel uses `[PASS]`/`[FAIL]`/`[ERROR]`; Mutator uses `[P]`/`[N]`/`[-]` with per-class iteration counts.

**DataModel Validator** (`Validations/DataModel/Program.cs`):
1. For each seed file: parse (crack) binary data through the datamodel
2. Re-serialize parsed model via `dm.Bytes()` and compare byte-for-byte with the original
3. Parse failure or bytes mismatch → `[FAIL]`, otherwise → `[PASS]`
4. Logs written to `/logs/<filename>.log`; deleted on pass

```
Usage: run_datamodel_test.sh <proto> <seed_dir>
→ dotnet Peach.LLM.Validations.DataModel.dll datamodel.xml <proto>_packet_array <seed_dir>
```

**Mutator Validator** (`Validations/Mutator/Program.cs`):
1. Reflection discovers all `LLMMutator` subclasses in the compiled mutator DLL
2. For each seed file × mutator × matching data element: runs 100 iterations
3. Each iteration: clone model → `randomMutation()` → serialize → re-crack
4. Changed bytes plus complete re-crack = PASS; parse failure/trailing input = FAIL; mutation exceptions = ERROR; identical bytes = UNCHANGED
5. Prints one completed row per class: `[P] Name TOTAL=... PASS=... FAIL=... ERROR=... UNCHANGED=...`, `[N]` when ERROR > 0 or PASS = 0, or `[-] Name` when skipped. The pipeline parser lives in `core/mutator_validation.py`; old `[PASS]` / `[RESULT]` mutator rows are unsupported.
6. Writes logs to `fail/<MutatorName>.log` and `error/<MutatorName>.log`
7. Has a replay mechanism that retries failures to produce minimal repro `.raw` files

```
Usage: run_peach_mutator_test.sh <proto> <seed_dir>
→ core.mutator_codegen compiles only active manifest partial source pairs → DLL
→ dotnet Peach.LLM.Validations.Mutator.dll datamodel.xml <seed_dir> <proto>_packet_array
```

**DataModel result format** (parsed by pipeline; Mutator uses the compact format above):
```
[PASS] 003/003 tests passed.      ← all good
[FAIL] 002/003 tests passed.      ← some failed
```
Per-item: `[PASS] <name>` / `[FAIL] <name>: <error>` / `[ERROR] <name>`
