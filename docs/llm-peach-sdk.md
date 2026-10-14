# LLM-Peach generated-code API

The pipeline generates constructors, attributes, static
`supportedDataElement(DataElement obj)` predicates and mutation entry points
in `.g.cs` partial classes. Agents implement only the required
`private void MutateCore(DataElement obj)` methods in `.semantic.cs`;
the host's `LLMMutator` entry point calls them. Add, Remove and Repeat classes
inherit the structural bases below and need no semantic implementation by
default. See [mutator generation](mutator-generation.md) for the exact pipeline
contract.

## Structural mutator bases

The updated SDK provides three abstract subclasses of `LLMMutator` in
`Peach.LLM.Core.Mutators`. Concrete protocol classes inherit their structural
implementation and declare their own static `supportedDataElement` predicate:

| Base class | Operation | Extension points |
| --- | --- | --- |
| `LLMAddMutator` | Activate absent conditional Optional content, select a nonempty optional Choice branch, or create one item in a zero-count optional Array. | `InitializeAddedContent(DataElement content)`, `UpdatePresenceSource(Optional optional, bool exists)` |
| `LLMRemoveMutator` | Hide conditional Optional content, select the empty Choice branch, or remove all items from an optional Array. | `UpdatePresenceSource(Optional optional, bool exists)` |
| `LLMRepeatMutator` | Deep-clone an existing serialized Array item and append it with a unique indexed name. | `GetRepeatCount(Peach.Core.Dom.Array array)` |

Add and Remove constructors accept an optional `emptyBranchName` (default
`"empty"`). Pass the same name to `CanApply` for a DSL-generated Choice whose
empty branch was renamed to avoid a collision. The named branch must be an
ordinary empty Block; ordinary Unions without such a branch are unsupported.
An Array must have `minOccurs == 0` for Add/Remove. Repeat also supports
fixed-count Arrays. A fixed/token wrapper is rejected, while fixed children
can still be added, removed, or repeated through a mutable wrapper.

```csharp
using Peach.Core;
using Peach.Core.Dom;
using Peach.LLM.Core;
using Peach.LLM.Core.Mutators;

namespace Peach.LLM.Generated.Mutators.DEMO.Packet
{
    [Mutator("DemoAddItems")]
    public partial class DemoAddItems : LLMAddMutator
    {
        public DemoAddItems(DataElement obj) : base(obj) { }

        public new static bool supportedDataElement(DataElement obj)
        {
            return obj.MatchesPath(@"Root.items")
                && LLMAddMutator.CanApply(obj);
        }
    }
}
```

Generate selectors for the stable Optional/Array wrapper, not its possibly
absent children. `CanApply` checks the current presence/count state; inspecting
an unexpanded Array may materialize its initial `occurs` value. The constructor
does not retain the target: mutations use the cloned element passed to
`randomMutation`.

The structural `PerformMutation` implementation is sealed. Implement only the
extension points in another partial declaration when protocol logic is needed.
`InitializeAddedContent` receives the attached runtime Choice branch or Array
item, or the conditional Optional wrapper. `UpdatePresenceSource` receives
`Peach.LLM.Core.Dom.Optional`; override it when the controlling source field
must agree with the new presence state. The default hook leaves that field
unchanged, allowing inconsistent-presence fuzz cases. Hooks finish before the
base invalidates the Optional and sets its final `Exists` state.

`GetRepeatCount` defaults to one and returns the number of copies to add, not
the final count. It must return a positive count that fits a signed 32-bit
total. Templates do not enforce `maxOccurs` or protocol constraints; generated
selectors and hooks can impose a protocol policy. Such structural fuzz cases
may legitimately fail re-cracking. Existing `LLMMutator` mutation-mode and
random-helper behavior is preserved.

Array operations normalize count overrides to the actual modified children
and use unique `arrayName_<ASCII index>` names compatible with `MatchesPath`.
Repeat ignores hidden suffix items and rejects overrides that serialize
virtual byte copies beyond the physical child count. Remove retains the
wrapper and prototype, and cleans up removed child bindings.

These bases require a rebuilt `Peach.LLM.dll`; run `./setup.sh` to publish the
updated llm-peach sources into the local `peach/sdk/` installation. Generated
protocol code uses C# 12 and targets .NET 8. Generation and repair prompts use
the same language/API guidance as the compiler. Protocol-semantic Mutate classes
continue to inherit `LLMMutator` and can split generated matching/entry code
from LLM-written logic with partial declarations compiled into the same DLL.
The structural base implementation itself uses C# 12 and is built through
llm-peach's maintained .NET 8/.NET 10 SDK projects; its source files require
the modern compiler.

## Other generated-code APIs

Generated fixups inherit `Peach.LLM.Core.Fixups.LLMFixup` and implement `fixupImpl()`.
Fixer tests use `DataElementMaker.Make<T>(...)` and methods marked with
`[FixerTest("name")]` from `Peach.LLM.Validations.Common`.

Useful extension methods in `Peach.LLM.Core` include:

- `elem.Bytes()` returns a `byte[]` representation.
- `elem.MatchesPath("Root.items[].value")` matches a complete rooted DSL path, including indexed array items and nested arrays, without regex. Import `Peach.LLM.Core` and use the updated SDK.
- `elem.Get<T>()` reads a typed value with element-kind validation and numeric,
  string or byte-stream conversion, for example `num.Get<ushort>()`.

Assign mutations through `elem.MutatedValue = new Variant(value)`. Use byte-backed
variants for raw encoded bytes and numeric variants for Number values. Protocol
encoding helpers can be implemented in the semantic source when needed.

Use `Search_Class` to inspect the exact API exposed by the installed .NET SDK.
Python.NET uses CoreCLR for reflection over these assemblies.
