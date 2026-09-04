# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project overview

LLM-assisted generator that reads one or more RFCs (PDF/text) via RAG, prompts an LLM to produce protocol-aware C# fuzzing code, and iteratively validates/fixes the output for **Peach**.

## Build, test, and lint

All Python commands in this repository must use the project virtual environment.
Use `.venv/bin/python` and `.venv/bin/pip` explicitly; do not run project code,
tests, or dependency installation with the system Python. If `.venv` does not
exist yet, create it before continuing.

```bash
# Install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Setup Peach SDK (requires Docker + mono)
./setup.sh

# Run full pipeline (interactive, auto-continues after 60s)
.venv/bin/python main.py --protocol mqtt --seed-dir tests/seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf

# Multiple RFCs can be specified by repeating --rfc-path
.venv/bin/python main.py --protocol someip --seed-dir tests/seeds/someip \
    --rfc-path rfc/someip.pdf --rfc-path rfc/someip-sd.pdf

# Peach sanity checks
.venv/bin/python -m core.datamodel_dsl mqtt --check
./tests/datamodel/run_datamodel_test.sh mqtt tests/seeds/mqtt
./tests/peach_mutator/run_peach_mutator_test.sh mqtt tests/seeds/mqtt
./tests/peach_fixer/run_peach_fixer_test.sh mqtt tests/seeds/mqtt

# Generate Peach Docker images for fuzzing
./peach_gen.sh mqtt [--udp] [--packets] [--sleep]
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
docs/peach-dsl.md        # Authoritative DSL language reference for agents and developers.
pipeline/base.py         # BasePipeline: step orchestration loop, call_agent(), token tracking, state save.
pipeline/peach.py        # Lightweight PeachPipeline composition root and step ordering.
pipeline/peach_steps/    # Peach implementations split by discovery, DataModel, mutator, fixer, and compilation steps.
peach_gen.sh             # Compiles Peach mutators/fixers, generates Pit XML configs and Docker images.
setup.sh                 # Prerequisite setup: for peach, pulls Docker SDK image and extracts DLLs into peach/sdk/.
```

### Peach pipeline steps (C# target)

1. Packet types extraction from RFC
2. Combined DSL type-support analysis and DataModel planning, then
   `shared_model.py`, followed by parallel family DSL generation → derived Peach Pit XML
3. Datamodel validation & DSL fix
4. Mutator generation (C# classes per packet type, parallelized with ThreadPoolExecutor)
5. Mutator validation & fix
6. Constraint extraction from RFC
6.1. Constraint filtering (checks if datamodel already guarantees each constraint)
7. Fixer generation (chunked, parallelized)
7.5. Fixer-constraint mapping
8. Fixer test generation (chunked, parallelized)
Step 9 (fixer validation & fix) is commented out.

Generated code lands in `llm/peach/<proto>/` with subdirectories `Mutators/`, `Fixers/`, `Fixers/Validations/`.
Editable DataModel sources live in `datamodel_dsl/shared_model.py` and
`datamodel_dsl/family_<id>.py`; `root.py` and `datamodel.xml` are deterministic
derived artifacts and must not be hand-edited.

### Key conventions

- **Global config**: `config.py` uses module-level mutable state. `build_config_from_args()` must be called before any getter.
- **Pipeline state**: Persisted to `logs/<proto>/pipeline_state.json` (gitignored) alongside tool lifecycle logs. Contains `packet_types`, `constraints`, and `token_usage_*`. Atomic writes via temp file + `os.replace()`. Older `.pipeline_state/<proto>.json` files are migrated automatically. On startup, if a saved state exists for the protocol, the user is asked whether to resume or start fresh.
- **Agent tools**: Agents get scoped file/DSL tools, DLL reflection, C# compilation (`mcs`), data validation, and RFC search as required by each step.
- **DSL source of truth**: DataModel agents must read `docs/peach-dsl.md`, write only generated DSL modules, and validate them with `Validate_Peach_DSL_Module` / `Validate_Peach_DSL`. Peach runtime scripts continue to consume the compiled `datamodel.xml`.
- **DataModel report conversion**: Step 3 converts Peach validator report nodes to compact DSL-path text in `datamodel_error_report.txt`. When every `@PacketUnion` candidate is rejected solely by its `packet_type` token, the converter keeps only the candidate that parsed furthest (first on ties); ordinary Unions are not filtered this way. The converter otherwise does not analyze, rank, deduplicate, or filter failures; the diagnosis agent performs root-cause analysis and writes `datamodel_diagnosis.json`. Repair agents read only the final diagnosis and referenced DSL modules.
- **Peach SDK**: DLLs extracted into `peach/sdk/` by `setup.sh`. The `dotnet_tools.py` module loads them at import time via `clr.AddReference` — if `peach/sdk/` is missing, the import fails.
- **Token tracking**: Each `call_agent()` creates a local `TokenUsageTracker` (per-invocation, thread-safe). Tracks `prompt_tokens`, `completion_tokens`, `cached_tokens`, and `calls` (LLM API invocations). Summarized at pipeline end.
- **Model configuration**: Set via environment variables. `LLM_PEACH_MODEL` / `LLM_PEACH_TEMPERATURE` override `LLM_MODEL` / `LLM_TEMPERATURE`. Defaults: `gpt-5.4` / 0.7.
- **`fix_verify_loop`**: Generic verify → fix → re-verify loop in `BasePipeline`. Up to 3 auto-retries, then interactive fallback (stop / provide hint and retry). Used by Peach Steps 3 and 5.

## Sibling project: ../llm-peach

The `../llm-peach/` directory is the Peach Fuzzer engine + LLM SDK. The framework depends on it for base classes, custom DataElements, and validation executables.

### SDK Core (`llm/Core/`) — APIs used by generated code

**Base classes:**

| Class | File | Generated code inherits from it |
|-------|------|-------------------------------|
| `LLMMutator` | `Mutators/LLMMutator.cs` | Mutators: implement `PerformMutation(obj)` + static `supportedDataElement(obj)` |
| `LLMFixup` | `Fixups/LLMFixup.cs` | Fixups: implement `fixupImpl()`, hooks into Peach DOM |
| `CMutatorAttribute` | `Mutators/CMutatorAttribute.cs` | `[Mutator("name")]` attribute on mutator classes |

**Key extension methods** (`llm/Core/Extensions.cs`, namespace `Peach.LLM.Core`):

| Method | Purpose |
|--------|---------|
| `elem.Bytes()` | Convert element value to `byte[]` (handles Number big-endian, String ASCII, BitStream) |
| `elem.IsIn("name")` | Walk parent chain to check if element is inside a named container |
| `elem.SetValue(v)` | Set `MutatedValue`, auto-converts strings to ASCII for non-string types |
| `num.GetUint8() / GetUint16()` | Read numeric element as uint8/uint16 (with fallback to raw bytes) |
| `varInt.GetVarInt()` | Decode MQTT variable-byte integer |
| `str.ToMqttString()` | Encode string as MQTT UTF-8 (2-byte length + data) |
| `bytes.Dump()` / `bytes.DumpDiff()` | Hexdump / hexdiff output |

**Custom DataElements** (declared with DSL `ExtendedType`, compiled into `datamodel.xml`):

| Element | Purpose |
|---------|---------|
| `MqttVarInt` | MQTT variable-length integer (1-4 bytes, big-endian, max 268M) |
| `Optional` | Conditional Block — children only included when `expression` on `src` element evaluates true. Example: `src="flags" expression="(value & 0x04) != 0"` |

**Test helpers** (`llm/Validations/Common/Common.cs`):

| Member | Purpose |
|--------|---------|
| `DataElementMaker.Make<T>(name, value/children)` | Programmatically construct DataElement trees for fixer tests |
| `[FixerTest("name")]` | Attribute marking fixer test methods (discovered reflectively by Fixer validator) |
| `DataParser` | Simple wrapper to parse binary data through a Peach Pit |

### Validators — how verification works

Each validator is a C# console app in `llm/Validations/`. They run inside Docker containers invoked by the framework's shell scripts in `tests/`. All use `[PASS]`/`[FAIL]`/`[ERROR]` markers that the pipeline parses.

**DataModel Validator** (`Validations/DataModel/Program.cs`):
1. For each seed file: parse (crack) binary data through the datamodel
2. Re-serialize parsed model via `dm.Bytes()` and compare byte-for-byte with the original
3. Parse failure or bytes mismatch → `[FAIL]`, otherwise → `[PASS]`
4. Logs written to `/logs/<filename>.log`; deleted on pass

```
Usage: run_datamodel_test.sh <proto> <seed_dir>
→ mono Peach.LLM.Validations.DataModel.exe datamodel.xml <proto>_packet_array <seed_dir>
```

**Mutator Validator** (`Validations/Mutator/Program.cs`):
1. Reflection discovers all `LLMMutator` subclasses in the compiled mutator DLL
2. For each seed file × mutator × matching data element: runs 100 iterations
3. Each iteration: clone model → `randomMutation()` → serialize → re-crack
4. Re-crack succeeds = Pass, re-crack fails = Fail, mutation itself throws = Error
5. Writes logs to `fail/<MutatorName>.log` and `error/<MutatorName>.log`
6. Has a replay mechanism that retries failures to produce minimal repro `.raw` files

```
Usage: run_peach_mutator_test.sh <proto> <seed_dir>
→ mcs compiles Mutators/*.cs → DLL
→ mono Peach.LLM.Validations.Mutator.exe datamodel.xml <seed_dir> <proto>_packet_array
```

**Fixer Validator** (`Validations/Fixer/Program.cs`):
1. Reflection discovers static methods with `[FixerTest("name")]` attribute
2. Each method: constructs violating DataElements via `DataElementMaker`, calls the fixer, asserts compliance (NUnit `Assert.*`)
3. No exception → `[PASS]`, exception → `[FAIL]`
4. Per-test logs written to `/logs/<test_name>.log`; deleted on pass

```
Usage: run_peach_fixer_test.sh <proto>
→ mcs compiles Fixers/*.cs + Fixers/Validations/*.cs → DLL
→ mono Peach.LLM.Validations.Fixer.exe

Usage: run_data_test.sh <proto> <hex>
→ mono Peach.LLM.Validations.Fixer.exe -d datamodel.xml <proto>_packet_array <hex>
```

**Result format** (parsed by pipeline):
```
[PASS] 003/003 tests passed.      ← all good
[FAIL] 002/003 tests passed.      ← some failed
```
Per-item: `[PASS] <name>` / `[FAIL] <name>: <error>` / `[ERROR] <name>`
