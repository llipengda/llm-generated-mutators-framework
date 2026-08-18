# Pit Studio

Pit Studio is the browser-based Peach Pit visualizer and editor bundled with
the LLM-Generated Mutators Framework. It can:

- open a Peach `datamodel.xml`/`.pit` file or start from the bundled demo;
- display the protocol as a packet canvas or a navigable topology tree;
- inspect and edit element attributes and child fields;
- follow references, relations, choices, and optional fields;
- import the JSON output produced by `datamodel_diagnoser.py` and highlight the
  most likely root cause in the model; and
- export the edited Pit XML.

## Requirements

- Node.js `>=22.13.0`
- npm

## Run locally

```bash
cd pit-visualizer
npm ci
npm run dev
```

Open the local URL printed by the development server.

## Validate

```bash
npm test
npm run lint
```

`npm test` builds the application and checks the server-rendered Pit Studio
shell. Dependencies, build output, local environment files, and Wrangler state
are intentionally excluded from version control.

## Import a diagnosis

Generate a diagnosis from the repository root, then use **上传诊断结果** in Pit
Studio to import the JSON file:

```bash
python3 datamodel_diagnoser.py \
  llm/peach/<proto>/datamodel.xml \
  llm/peach/<proto>/dm_test_logs \
  --output diagnosis.json
```

The visualizer runs locally without an API key. LLM-backed diagnosis is handled
by the repository's Python diagnoser before the JSON is imported.
