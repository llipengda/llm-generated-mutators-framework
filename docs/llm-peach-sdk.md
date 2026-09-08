# LLM-Peach generated-code API

Generated mutators inherit `Peach.LLM.Core.Mutators.LLMMutator`, implement
`PerformMutation(DataElement obj)`, and expose the static
`supportedDataElement(DataElement obj)` predicate. Mark each implementation
with `Peach.LLM.Core.Mutators.CMutatorAttribute` via `[Mutator("name")]`.

Generated fixups inherit `Peach.LLM.Core.LLMFixup` and implement `fixupImpl()`.
Fixer tests use `DataElementMaker.Make<T>(...)` and methods marked with
`[FixerTest("name")]` from `Peach.LLM.Validations.Common`.

Useful extension methods in `Peach.LLM.Core` include:

- `elem.Bytes()` returns a `byte[]` representation.
- `elem.IsIn("name")` checks ancestors.
- `elem.SetValue(value)` sets `MutatedValue` with Peach-compatible conversion.
- `num.GetUint8()` and `num.GetUint16()` read numeric elements.
- `varInt.GetVarInt()` decodes an MQTT variable-byte integer.
- `str.ToMqttString()` creates MQTT's length-prefixed UTF-8 representation.
- `bytes.Dump()` and `bytes.DumpDiff()` format byte diagnostics.

Use `Search_Class` to inspect the exact API exposed by the selected SDK. Both
the legacy Mono SDK and the `modern-sdk` .NET 8 backend expose these generated
code contracts.
