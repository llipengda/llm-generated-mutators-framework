# Random-only baseline for semantic value assessment

The baseline is Peach's ordinary `Random` strategy with non-LLM mutators. It is
not limited to replacing each field with uniformly random bytes. The strategy
selects from the mutators applicable to the runtime element and may mutate
multiple fields in an iteration, subject to configuration, model state and
each mutator's supportedDataElement predicate.

The bundled Peach implementation contains these relevant capabilities:

| Area | Existing non-LLM capabilities |
|---|---|
| Numbers | `NumberRandom` samples the underlying numeric range for supported numbers wider than 8 bits; `NumberVariance` perturbs values; `NumberEdgeCase` supplies integer edge cases. Small numeric/bit fields also have ordinary numeric/bit mutation support. |
| Strings | Case changes, length variations and edges, static strings, Unicode categories, invalid UTF-8 and encoding/BOM variants. See `StringCaseRandom`, `StringLengthEdgeCase`, `StringUtf8Invalid` and related mutators. |
| Bytes | Bit flipping, byte/content changes, expansion/reduction and special values, subject to the applicable Blob/DataElement mutators. |
| Choice | `ChoiceSwitch` selects another modeled alternative. A branch switch by itself does not implement a protocol's cross-field dependencies. |
| Arrays | Count edge/variance mutations and order changes, including `ArrayEdgeCase`, `ArrayVariance`, `ArrayRandomizeOrder` and `ArrayReverseOrder`. Runtime predicates and output-size limits apply. |
| Relations | `SizedDataVariance`/`SizedDataEdgeCase` and related sized mutators act on size relations, including deliberate length/content variation. Normal serialization may also recompute relation-derived lengths/counts. |

These names refer to the `pro/Core/Mutators` sources in the llm-peach checkout.
Use Search_Class to inspect installed classes and their actual APIs when needed.
Do not assume that every listed mutator applies to every custom DataElement,
that all values will be exhausted, or that a particular seed activates an
Optional/Choice branch.

Reason about whether ordinary mutation already covers the important values AND relevant cross-field
relationships. Independent random updates can reach combinations, but do not
inherently construct rare coordinated assignments, RFC-specific strings or
protocol-state sequences. Conversely, do not assume every relationship needs
an LLM mutator: the DSL, relations, existing structural operations and ordinary
mutation can already implement some of them.
