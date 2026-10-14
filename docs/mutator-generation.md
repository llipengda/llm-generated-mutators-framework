# DSL-driven mutator generation

Step 4 loads and validates `datamodel_dsl/root.py`, obtains typed
`get_mutation_nodes()` metadata and constructs a `MutatorPlan`. Packet groups
come from `schema_manifest.json`; targets use actual compiled paths, runtime
types, Optional branch names and conditional-source metadata. Flags apply to
each node locally. A fixed child does not make its mutable parent fixed.

| Node properties | Generated operations |
| --- | --- |
| FIXED | None |
| MUTABLE | Mutate |
| MUTABLE + OPTIONAL | Add, Remove, Mutate |
| MUTABLE + REPEATABLE | Repeat, Mutate |
| MUTABLE + OPTIONAL + REPEATABLE | Add, Remove, Repeat, Mutate |

Generated empty Choice branches and compiler scaffolding are excluded. Native
conditional Optional, optional Choice and zero-minimum Arrays retain their
stable wrapper targets. Fixed-count Arrays still receive Repeat.

## Sources and interfaces

Each selected packet has two sources compiled into one DLL:

```text
Mutators/<PROTO><Packet>Mutators.g.cs
Mutators/<PROTO><Packet>Mutators.semantic.cs
Mutators/out/<PROTO><Packet>Mutators.dll
Mutators/mutator_manifest.json
```

The host owns `.g.cs`: stable class names with path hashes, namespaces,
attributes, constructors and exact `MatchesPath` predicates. Structural
predicates additionally call the SDK's `CanApply` for the current element
state. Add/Remove pass the compiler's actual empty-branch name to both the
constructor and predicate.

Add, Remove and Repeat inherit `LLMAddMutator`, `LLMRemoveMutator` and
`LLMRepeatMutator`. Their sealed implementation is already complete. This
pipeline uses the SDK defaults: Add retains default content, conditional
presence-source fields stay unchanged, and Repeat adds one copy. It does not
infer protocol synchronization hooks automatically.

Mutate inherits `LLMMutator`. The generated `PerformMutation` calls an ordinary
required method, `private void MutateCore(DataElement obj)`. The semantic
scaffold declares only the partial class and documents the required signature;
omitting the method fails compilation. Agents implement every required method
and the applicable eight semantic categories from the existing prompt.

Agents can read the generated contract, relevant DSL modules, SDK documentation
and RFC material. File tools allow writes only to the packet's `.semantic.cs`.
The no-argument `Build_Mutator_DLL` compiles exactly the two planned sources with
host-selected SDK references and output path. The host independently checks
compilation before proceeding. Fixer and DataModel agents keep their own
existing tools and file policies.

## Manifest, resume and migration

The manifest records exact filenames, classes, operations, paths, method
signatures and active packet selection. Partial selection includes newly
selected packets and any existing packets reused by Step 4; inactive
records remain available for later regeneration. Validation, aggregate builds
and final compilation consume only active manifest sources.

Step 4 offers regeneration when the recorded source pair and packet DLL exist.
The user's choice controls whether existing outputs are reused. Changes to DSL,
SDK, generated sources or DLL contents do not force regeneration; rerun Step 4
and select regeneration when those changes should be reflected in mutators.
The manifest stores source and class information without build fingerprints or
artifact hashes. Step 5 loads the recorded interfaces, so changing DSL does not
silently change the expected classes for an existing generation.

Manifest updates use a file lock and atomic replacement; parallel packet builds
cannot overwrite one another's entries. Packet compilation stages its output
and publishes it only after a successful build. Mutator compilation references
the stable SDK assemblies and custom DataElement DLLs; generated fixer DLLs
copied into the SDK by Step 8 are excluded.

Step 4 archives the old framework's exact per-packet `*Mutators.cs` file in
`.archive/` before migrating it. A changed class/path/signature set also archives
the old semantic source and creates a fresh scaffold. Other source files remain
on disk and are excluded from manifest builds. Existing outputs without a valid
manifest require Step 4 regeneration.

## Verification and repair

Step 5 requires a completed validator report for all requested classes,
and at least one executed class
when classes are planned. A nonzero command exit, missing result, unknown class
or disagreement between ERROR results and log files stops the pipeline.

Runtime ERROR logs route through manifest class ownership. All errors for one
packet are repaired by one agent in its semantic file, followed by compilation
and retesting every class in that packet. Structural SDK errors and generated
constructor/selector errors stop for a generator or SDK fix. Compilation
diagnostics attributable to semantic methods receive up to three repair
attempts; infrastructure errors stop immediately.

Re-crack FAILs remain reported fuzz outcomes and do not trigger semantic repair.
Partial SKIP results are reported by the validator; an all-SKIP report cannot
pass. Stopping the repair loop also stops the pipeline.

When the user chooses to skip verification, Step 5 reads the existing
`mutator_test_logs/results.txt` and ERROR logs directly. It checks the completed
report and class list without comparing file hashes or SDK/DSL versions. Saved
command failures still stop the pipeline. A filtered packet retest omits classes
outside that packet, so it cannot replace a complete report for a later full
verification skip.

## SDK and commands

Step 4/5 first compile a capability probe for `MatchesPath` and the three
structural bases. `./setup.sh` publishes the local llm-peach sources for .NET 8
and installs matching assemblies and complete validator runtimes in `peach/sdk/`.
Generated protocol code uses C# 12. `PEACH_SDK_DIR` can point at another complete
installation. Updating framework source alone does not update SDK binaries.

From the repository root:

```bash
uv run python -m core.mutator_codegen sources mqtt
uv run python -m core.mutator_codegen compile mqtt \
  --output llm/peach/mqtt/Mutators/out/MQTTMutators.dll
./tests/peach_mutator/run_peach_mutator_test.sh mqtt seeds/mqtt
./peach_gen.sh mqtt
```

Validators run with `dotnet` in isolated temporary runtime directories, using
the installed SDK and exact protocol plugins. Logs and replay samples remain
under the protocol output directory. `peach_gen.sh` produces native fuzzing
bundles with their own runtime, plugins and launch script.
