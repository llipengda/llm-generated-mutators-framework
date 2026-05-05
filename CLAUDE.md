# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

LLM-assisted generator that reads an RFC (PDF/text) via RAG, prompts an LLM to produce protocol-aware fuzzing code (C or C#), and iteratively validates/fixes the output. Two targets: **AFLNet** (C) and **Peach** (C#).

## Build, test, and lint

```bash
# Install dependencies
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Setup Peach SDK (requires Docker + mono)
./setup.sh peach

# Run full pipeline (interactive, auto-continues after 60s)
python3 main.py --protocol mqtt --seed-dir tests/seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf
python3 main.py --protocol mqtt --seed-dir tests/seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf --target peach

# AFLNet sanity checks
./tests/PR_mr/mr_test.sh mqtt tests/seeds/mqtt          # parser + reassembler metamorphic tests
./tests/mutator_sanity/run_mutator_sanity.sh mqtt tests/seeds/mqtt
./tests/fixer_sanity/run_fixer_sanity.sh mqtt

# Peach sanity checks
./tests/datamodel/run_datamodel_test.sh mqtt tests/seeds/mqtt
./tests/peach_mutator/run_peach_mutator_test.sh mqtt tests/seeds/mqtt
./tests/peach_fixer/run_peach_fixer_test.sh mqtt tests/seeds/mqtt

# Generate Peach Docker images for fuzzing
./peach_gen.sh mqtt [--udp] [--packets] [--sleep]
```

No linter or type-checker is configured. A `.env` file with `OPENAI_API_KEY` is required.

## Architecture

```
main.py                  # CLI entry point (click). Routes --target to AFLNetPipeline or PeachPipeline.
config.py                # Global mutable state: protocol_name, seed_dir, rfc_path. Set by build_config_from_args().
state.py                 # PipelineState persistence (.pipeline_state.json): packet_types, constraints, token usage.
agent.py                 # LangChain agent factory. Creates ChatOpenAI + tools + MemorySaver checkpoint.
rag.py                   # RFC retriever: loads PDF/text → splits → FAISS vector store (cached under .cache/rag/).
tools.py                 # AFLNet LangChain tools: Save_And_Verify_Code, Read_File, Append_And_Verify_Code, RFC_Search.
dotnet_tools.py          # Peach LangChain tools: Search_Class (DLL reflection), Build_DotNet_DLL (mcs compile), Validate_Data.
ui.py                    # Rich-based console UI + interactive ask_before_step() prompt with 60s auto-continue.
usage_tracking.py        # TokenUsageTracker callback; accumulates prompt/completion tokens per step.
log.py                   # Dual logger: rich Console for user, file_logger for tool_usage.log (reset each run).
pipeline/base.py         # BasePipeline: step orchestration loop, call_agent(), token tracking, state save.
pipeline/aflnet.py       # 9-step C pipeline (see steps below).
pipeline/peach.py        # 10-step C# pipeline (see steps below); uses ThreadPoolExecutor for parallelism.
peach_gen.sh             # Compiles Peach mutators/fixers, generates Pit XML configs and Docker images.
setup.sh                 # Prerequisite setup: for peach, pulls Docker SDK image and extracts DLLs into peach/sdk/.
process_peach_txt.py     # Filters peach/peach.txt to keep only Analyzer/DataElement/Relation/Transformer sections.
```

### AFLNet pipeline steps (C target)

1. Fetch packet types from RFC
2. Generate C structs (`_packets.h/.c`) with tagged fields (`/* Fixed */`, `/* Optional */`, `/* Repeatable */`)
3. Generate parser (`_parser.c`)
4. Generate reassembler (`_reassembler.c`)
5. Verify via metamorphic tests (`mr_test.sh`), auto-fix on failure
6. Generate mutators per packet type (`_mutators.c`)
7. Mutator sanity check (`run_mutator_sanity.sh`), auto-fix on failure
8. Extract constraints → generate fixers (`_fixers.c`)
9. Fixer sanity check (`run_fixer_sanity.sh`), auto-fix on failure

Generated code lands in `llm/<proto>/`.

### Peach pipeline steps (C# target)

1. Packet types extraction from RFC
2. Datamodel generation (Peach Pit XML)
3. Datamodel validation & fix
4. Mutator generation (C# classes per packet type, parallelized with ThreadPoolExecutor)
5. Mutator validation & fix
6. Constraint extraction from RFC
6.1. Constraint filtering (checks if datamodel already guarantees each constraint)
7. Fixer generation (chunked, parallelized)
7.5. Fixer-constraint mapping
8. Fixer test generation (chunked, parallelized)
Step 9 (fixer validation & fix) is commented out.

Generated code lands in `llm/peach/<proto>/` with subdirectories `Mutators/`, `Fixers/`, `Fixers/Validations/`.

### Key conventions

- **Global config**: `config.py` uses module-level mutable state. `build_config_from_args()` must be called before any getter.
- **Pipeline state**: Persisted to `.pipeline_state/<proto>.json` (gitignored). Contains `packet_types`, `constraints`, and `token_usage_*`. Atomic writes via temp file + `os.replace()`. On startup, if a saved state exists for the protocol, the user is asked whether to resume or start fresh.
- **Agent tools differ by target**: AFLNet gets file I/O + GCC syntax checks; Peach gets DLL reflection, C# compilation (`mcs`), and data validation.
- **Peach SDK**: DLLs extracted into `peach/sdk/` by `setup.sh`. The `dotnet_tools.py` module loads them at import time via `clr.AddReference` — if `peach/sdk/` is missing, the import fails.
- **Token tracking**: Each `call_agent()` creates a local `TokenUsageTracker` (per-invocation, thread-safe). Tracks `prompt_tokens`, `completion_tokens`, `cached_tokens`, and `calls` (LLM API invocations). Summarized at pipeline end.
- **Model configuration**: Set via environment variables. `LLM_MODEL` / `LLM_TEMPERATURE` apply to both targets. `LLM_PEACH_MODEL` / `LLM_PEACH_TEMPERATURE` override for Peach only. Defaults: AFLNet `gpt-5.2` / 0.0; Peach `gpt-5.4` / 0.7.
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

**Custom DataElements** (usable in `datamodel.xml`):

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
