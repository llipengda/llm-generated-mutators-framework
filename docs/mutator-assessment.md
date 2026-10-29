# Semantic value assessment before mutator code generation

Step 4 selects packet types, then dispatches up to four packet-local workers.
Each worker performs these stages in order:

1. A read-only packet agent assesses the DSL candidate fields against the
   existing random mutation capabilities. All fields of that packet are reviewed
   in one batch, with only a keep/skip decision and a brief reason per field.
2. The host validates a complete JSON decision for every candidate field and
   saves `<Packet>Mutators.assessment.json` next to the managed sources.
3. The host generates `.g.cs` and its manifest entry only for retained fields.
4. A semantic agent implements the retained interfaces using the accepted
   reasons, then the host compiles and repairs the packet.

The criterion is:

> If the existing random mutation strategy can effectively cover a field's important semantic values and relevant cross-field relationships, there is no need to generate a dedicated LLM mutator for that field.

Assessment does not estimate budgets, run fuzzing or change validator iteration
counts. It does not require long scenario or evidence lists. The prompt gives
a short random-baseline summary; agents read DSL modules and search the RFC
only as needed.

To reduce token use, candidate paths share a packet prefix and have numeric ids.
The response is compact JSON: `{"decisions": [[0, true, "brief reason"], ...]}`.
The host maps ids back to exact paths and saves readable decisions containing
the path, `generate`, reason and candidate operations. All operations for a field
are retained or omitted together. Duplicate, missing or invalid ids/decisions
are rejected; the agent gets at most one correction call. No sources are
prepared when assessment remains invalid. The implementation prompt receives
only retained-field reasons, without rereading the full assessment report.

The packet worker exists before assessment, so reports and source permissions
remain packet-scoped. Assessment agents cannot write source files or invoke the
compiler. Semantic agents can edit only their packet's `.semantic.cs`.

An all-skipped decision is valid: the packet produces an empty managed contract
and no semantic generation call. It does not change the DSL's mutability or the
ordinary random mutators. Existing outputs explicitly reused by the user are
not reassessed; regenerating a packet runs assessment again.

The saved report is an auditable planning judgment, not evidence of measured
coverage. Later contribution tests can revise a decision. The candidate DSL
plan remains available via `core.mutator_codegen plan`; the active manifest
records only the accepted generated classes.
