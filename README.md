# LLM-Generated-Mutators-Framework

LLM-assisted generator for protocol-aware C code (packet structs, parser, reassembler) plus fuzzing utilities (mutators, fixers) from an RFC(or other spec).

This repo drives an interactive, step-by-step pipeline that:

1. Reads an RFC (PDF or text) via a lightweight RAG retriever;
2. Asks an LLM (Default GPT-5.2) to generate C code;
3. Runs local sanity checks (metamorphic tests, mutator sanity, fixer sanity);
4. If a check fails, asks the LLM to diagnose and patch the generated code.

## Requirements

- Python 3.10+ (recommended)
- A C compiler
- An OpenAI API key (set in `.env` or environment variables)

Python packages used by the pipeline include:

- `click`, `python-dotenv`
- `rich`, `questionary`
- `langchain`, `langchain-core`, `langchain-community`, `langchain-openai`, `langgraph`
- `faiss-cpu` (for the RAG vector store)

## Setup

Create a `.env` file in the repo root:

```bash
OPENAI_API_KEY=...your key...
# OPENAI_BASE_URL=...your base url... if using a custom endpoint
```

Optional environment variables:

- `RAG_CACHE_DIR=/path/to/cache` (default: `.cache/rag`)
- `RAG_DISABLE_CACHE=1` to disable caching

Install dependencies (example):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## Quickstart

MQTT v5.0 example:

```bash
python3 main.py --protocol mqtt --seed-dir tests/seeds/mqtt --rfc-path rfc/mqtt-v5.0.pdf
```

Notes:

- The pipeline is interactive. Before each step it will prompt you to **Continue / Retry previous / Skip / Exit**.
- If you do nothing, it auto-continues after ~60 seconds.
- The RFC(or other spec) can be a `.pdf` or a text file.

## What gets generated

Generated C artifacts go under:

- `llm/<proto>/<proto>_packets.h`
- `llm/<proto>/<proto>_packets.c`
- `llm/<proto>/<proto>_parser.c`
- `llm/<proto>/<proto>_reassembler.c`
- `llm/<proto>/<proto>_mutators.c`
- `llm/<proto>/<proto>_fixers.c`

The pipeline also generates fixer-test files under:

- `tests/fixer_sanity/<proto>_fixer_registry.c`
- `tests/fixer_sanity/<proto>_fixer_sanity_tests.c`

## Running checks manually

Parser + reassembler metamorphic test:

```bash
./tests/PR_mr/mr_test.sh mqtt tests/seeds/mqtt
```

Mutator sanity check:

```bash
./tests/mutator_sanity/run_mutator_sanity.sh mqtt tests/seeds/mqtt
```

Fixer sanity check (compiles and runs `tests/fixer_sanity/<proto>_fixer_sanity_tests.c`):

```bash
./tests/fixer_sanity/run_fixer_sanity.sh mqtt
```

## Logs and state

- `tool_usage.log`: records tool calls (file reads, RFC search, file writes). It is reset on each run.
- `.pipeline_state.json`: caches pipeline state (e.g., discovered packet types / extracted constraints) so you can resume runs.

## Troubleshooting

- **RAG setup fails**: the pipeline will still run, but RFC grounding will be weaker. Ensure the RFC file exists and dependencies like `faiss-cpu` installed.
- **Compiler not found**: install `gcc`/`clang` and ensure they are on `PATH`.
- **OpenAI auth errors**: verify `OPENAI_API_KEY` is set and reachable from the environment running `python3 main.py`.
