# Peach DataModel diagnoser

`datamodel_diagnoser.py` analyzes the existing Peach cracker logs without
rerunning Docker or requiring an RFC. It maps findings back to source lines in
the Pit XML when `--datamodel` is supplied.

## Usage

Analyze every failure log for one protocol:

```bash
python3 datamodel_diagnoser.py \
  --datamodel llm/peach/dds/datamodel.xml \
  llm/peach/dds/dm_test_logs
```

Generate structured output for a later pipeline or LLM step:

```bash
python3 datamodel_diagnoser.py \
  --format json \
  --output llm/peach/dds/datamodel_diagnosis.json \
  --datamodel llm/peach/dds/datamodel.xml \
  llm/peach/dds/dm_test_logs
```

Use LLM-only diagnosis:

```bash
.venv/bin/python datamodel_diagnoser.py \
  --llm \
  --datamodel llm/peach/dds/datamodel.xml \
  llm/peach/dds/dm_test_logs
```

The model defaults to `LLM_DIAGNOSER_MODEL`, then `LLM_PEACH_MODEL`, then
`LLM_MODEL`, and finally `gpt-5.2`. Override it explicitly when needed:

```bash
.venv/bin/python datamodel_diagnoser.py \
  --llm --llm-model deepseek-v4-flash \
  --format json --output /tmp/dds-diagnosis.json \
  --datamodel llm/peach/dds/datamodel.xml \
  llm/peach/dds/dm_test_logs
```

`--llm-language zh-CN` is the default; `en` is also supported. The command
loads `.env` before resolving the model and requires `OPENAI_API_KEY`. Provider
settings supported by `ChatOpenAI`, such as `OPENAI_BASE_URL`, continue to work.

With `--llm`, heuristic diagnosis is skipped completely. The model receives
size-limited, line-numbered raw validator logs and DataModel XML and performs the
root-cause analysis itself. Its JSON response ranks root causes, contributing
factors, symptoms, and uncertainties, and proposes a focused verification for
every candidate. If the model request fails, `llm_judgment.status` is `error`
and the CLI exits with status 3.

The model is explicitly told to respect log-line ordering. For example, if a
preferred little-endian branch first fails because a Relation cannot resolve,
an endian error from a subsequently attempted big-endian fallback should be
classified as a downstream symptom rather than the primary cause.

Multiple log files and directories can be passed in one invocation. Use
`--no-static` to omit warnings produced solely by inspecting the XML.

## Recognized patterns

- unresolved runtime references, including the failing element and XML line;
- unresolved size/count Relation bindings and the attached runtime element;
- unsized non-final elements, linked to a failed Relation when both are present;
- round-trip mismatches, first differing offset, and truncation size;
- declared lengths that exceed the remaining input;
- likely byte-order mistakes when swapping a length exactly matches the input;
- unexpected EOF and the case where every Choice branch hits EOF;
- a Choice token that matched before its branch failed deeper in the body;
- a Choice with no valid branch at a later repeated-item boundary;
- unbounded Blob/String fields that can consume following siblings.

`Token did not match` entries from rejected Choice branches are treated as
normal fallback behavior. The report calls out the branch whose token actually
matched when that can be established from the log.

The LLM-only mode attempts to:

- merge repeated low-level failures into shared root causes;
- distinguish a rewritten length field from the omitted body data that caused it;
- rank candidate XML locations and fixes by confidence;
- state which semantic conclusions cannot be proven from the supplied logs;
- provide a focused re-test for confirming or rejecting each hypothesis.

When Step 3 runs inside the Peach pipeline, diagnosis is executed by a
read-only pipeline agent. It calls `Read_File` for the current DataModel, lists
the validator log directory, and calls `Read_File` for every failure log. It can
then call `RFC_Search` to confirm protocol semantics before ranking root causes.
Finally, it calls `Write_File` itself to save the completed structured diagnosis
to `datamodel_diagnosis.json`; the pipeline does not write the report on the
agent's behalf. This keeps diagnosis on the pipeline's model, tool-calling,
memory, and token-usage path; the standalone CLI remains available for offline
use.

Before starting diagnosis, the pipeline checks for an existing
`datamodel_diagnosis.json` and asks whether to reuse it. The auto-fix agent uses
only the selected diagnosis report and the current DataModel; validator stdout,
failure logs, seeds, and RFC retrieval are intentionally excluded from the
repair context.

## Limits

The diagnoser reports observations and likely locations; it does not claim to
know protocol semantics. A wrong discriminator value, a missing field, or two
fields in the wrong order may only appear as a matched branch failing later or
as a round-trip truncation. Those candidates still need comparison with the RFC
or other protocol knowledge.

Round-trip validation also cannot detect every bad model. For example, one
unbounded Blob can preserve all bytes while hiding the intended field
structure. The static warnings cover common versions of this problem but are
necessarily conservative.

## Tests

```bash
python3 -m unittest -v tests.test_datamodel_diagnoser
```
