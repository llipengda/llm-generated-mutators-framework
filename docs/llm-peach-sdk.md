# SDK reference for mutator generation

Generated mutators use C# 12 and target .NET 8. Use the validated DSL for field
types, layout and descriptions, and RFC_Search for protocol semantics. Use
Search_Class to inspect the installed SDK when an API is not covered here.

## Implementation contract

Before these sources are prepared, a packet-local agent reviews field-level
semantic value. Only retained fields receive generated classes; the host passes
their brief selection reasons to the implementation agent.

Each packet has two partial source files, compiled into the same DLL:

- `.g.cs` is read-only. It defines the namespaces, class names, base classes,
  attributes, constructors, exact target selectors and mutation entry points.
- `.semantic.cs` is the only editable file. Implement every required method
  with the exact signature supplied in the generation prompt and `.g.cs`.
  Keep helpers in this file.

For a Mutate class, the generated `PerformMutation(DataElement obj)` calls
`private void MutateCore(DataElement obj)`. Implement that method in the matching
partial class without repeating the base class, constructor, attributes,
selector or entry point:

```csharp
using Peach.Core;
using Peach.Core.Dom;
using Peach.LLM.Core;

namespace Peach.LLM.Generated.Mutators.DEMO.Packet;

// Example only: use the exact namespace and class name from your .g.cs.
public partial class DemoMutatePacketIdentifier
{
    private void MutateCore(DataElement obj)
    {
        // Example policy for an unsigned 16-bit identifier excluding zero.
        obj.SetValue((uint)(1 + Next(ushort.MaxValue)));
    }
}
```

`obj` is the runtime element in the cloned model being mutated. Read and mutate
this element; do not retain a target from a constructor. Keep changes local to
the planned target. Do not add or remove mutator classes or change the DSL.
Required methods must contain real implementations, not empty bodies or
`NotImplementedException` stubs.

## Reading and assigning values

Import `Peach.LLM.Core` for extension methods. Pass raw CLR values to `SetValue`; do not wrap them in `Variant`.

| API | Use |
| --- | --- |
| `obj.Get<ushort>()`, `obj.Get<uint>()`, `obj.Get<ulong>()` | Read unsigned numeric values from Number, Flag or Flags. Choose a type that fits the modeled value. Signed numeric types are also supported. |
| `obj.Get<string>()` | Read text from `Peach.Core.Dom.String`. |
| `obj.Get<byte[]>()` | Read a byte representation. Numbers use their modeled width and byte order; strings use their configured encoding. |
| `obj.Get<Variant>()` | Read the current internal Variant. |
| `obj.Bytes()` | Read bytes using the SDK's existing convenience conversion. Numbers use modeled serialization; text-valued Strings are converted as ASCII. Use `Get<byte[]>()` when the String's configured encoding matters. |

`Get<T>()` validates the element kind and performs checked conversions; it is
not a way to interpret an arbitrary Blob as a Number. Match the requested type
to the DSL and runtime type. For custom DataElements, inspect their API with
Search_Class or handle their byte representation explicitly.

Assign every mutation through `SetValue(rawValue)`:

```csharp
obj.SetValue((uint)42);       // Number or Flag: normal numeric encoding.
obj.SetValue("example");     // String: configured text encoding.
obj.SetValue(encodedBytes);  // byte[]: replace the raw wire encoding.
obj.SetValue(new Peach.Core.IO.BitStream(encodedBytes)); // Raw stream is also supported.
```

`SetValue` wraps the raw value internally. For numeric and text values it clears
`TypeTransform`, allowing normal element conversion. For `byte[]` and
`BitwiseStream` values it enables `TypeTransform` to replace the raw encoding.
Other mutation overrides are preserved, and cached serialized output is invalidated.
Do not assign `MutatedValue` directly or manually toggle `TypeTransform`.
The same element may be mutated repeatedly: raw bytes followed by a numeric
assignment must clear the previous raw-encoding override automatically.

Raw encoding bypasses the element's normal conversion, relations and fixups;
transformers still run unless separately overridden. Use raw bytes only when
that behavior fits the planned mutation. Numeric and text assignments retain
normal relation and fixup processing unless the caller separately overrides it.
`SetValue` does not enforce protocol constraints or synchronize sibling fields.

Implement protocol encoding helpers in the semantic file when needed.
Avoid ambiguous `String` and `Array` names:
qualify `Peach.Core.Dom.String`, `Peach.Core.Dom.Array` or `System.Array`.

## Finding other fields

Mutators inherit `Find(string path)` from `LLMMutator`. The mutation entry point
automatically binds the current iteration's `obj` for the duration of the
mutation, including structural initialization and presence hooks. Pass an exact
rooted DSL path or an explicit `./` / `../` relative path, using the same
`name[]` array-item syntax as `MatchesPath`:

```csharp
var flag = Find(
    "mqtt_packet_array.packets[].packet_union.connect.body.connect_flags.will_flag")
    ?? throw new System.InvalidOperationException("CONNECT will_flag was not found.");
var enabled = flag.Get<byte>();
```

Relative paths start at the bound mutation target. For example, when `obj` is
`connect.body.client_identifier`:

```csharp
var text = Find("./text");
var willFlag = Find("../connect_flags.will_flag");
```

Each leading `../` moves up one actual Peach DOM parent, including Optional,
Choice and Array wrappers. The rest of the path uses dots between child names;
for example `../../header.flags`. `.`/`./` return the target itself, and
`..`/`../` return its parent. Moving above the root returns null. A relative path
must have an explicit prefix: bare `text` is treated as a root name, not as a
child lookup. `MatchesPath` continues to accept only rooted paths.

Each `[]` binds to the enclosing item of `obj` in that same Array, so a lookup
from a CONNECT field stays in the current packet. An unbound wildcard must
produce exactly one match; multiple matches throw `InvalidOperationException`.
Use a concrete expanded path such as `Root.items.items_1.value` to explicitly
select another item. Numeric bracket syntax such as `items[1]` is not supported.

Missing fields or a different root name return null. Invalid path syntax throws
`ArgumentException`. There is no suffix search or fallback to another array
item. Find reads structure, including content beneath an absent Optional, so
Add initializers can locate that content; it does not enable the Optional.
Only the selected Choice branch and actual Array children are searched, not
unselected branches or Array prototypes. Resolve references again for each
mutation rather than caching elements across model clones.

The binding is cleared when the mutation returns or throws; calling `Find(path)`
without an active mutation throws `InvalidOperationException`. An explicit
`Find(DataElement obj, string path)` overload is available when a different
origin is needed or when resolving outside mutation hooks.

## Randomness

Mutate classes inherit these methods from `Peach.LLM.Core.Mutators.LLMMutator`:

| API | Behavior |
| --- | --- |
| `Next(int max)` | Return an integer in `[0, max)`. Use a positive bound. |
| `NextBytes(byte[] buffer)` | Fill an existing buffer with random bytes. |
| `PickWeighted(int[] weights)` | Return a weighted alternative's index. Use nonnegative weights with a positive total that fits `int`. |

Use these helpers for mutation choices and randomized content so mutations use
Peach's random context. Cover the applicable semantic categories requested in
the prompt; explain in a short code comment when a category does not apply.

## Structural operations

Add, Remove and Repeat classes already inherit complete structural operations
from `Peach.LLM.Core.Mutators`. Their `PerformMutation` methods are sealed.
Every Add requires `InitializeAddedContent`, an abstract SDK method. Only
conditional Optional Add/Remove require `UpdatePresenceSource`, declared as a
required partial override in `.g.cs`. Missing implementations fail compilation.
Choice/Array Remove and Repeat require no semantic implementation.

| Base class | Behavior |
| --- | --- |
| `LLMAddMutator` | Make an absent conditional Optional present, select a nonempty optional Choice branch, or create one item in a zero-count optional Array. |
| `LLMRemoveMutator` | Hide conditional Optional content, select the empty optional Choice branch, or remove all items from an optional Array. Keep the wrapper and prototype. |
| `LLMRepeatMutator` | Deep-clone an existing serialized Array item and append 1–5 copies, preserving its values and assigning a unique indexed name. |

The generated selector combines the exact DSL path with the base's
`CanApply(...)` check. Add/Remove on Arrays require `minOccurs == 0`; Repeat
requires at least one real serialized item and also supports fixed-count
Arrays. The host supplies the actual empty branch name for optional Choices.
Do not reproduce this selection or structural logic in the semantic file.

Implement every required override listed in the plan:

```csharp
// Required for LLMAddMutator: receives the runtime Choice branch or Array item after
// attachment, or the conditional Optional wrapper.
protected override void InitializeAddedContent(DataElement content)

// Required only for conditional Optional Add/Remove. Match the partial declaration.
protected override partial void UpdatePresenceSource(
    Peach.LLM.Core.Dom.Optional optional, bool exists)
```

`InitializeAddedContent` initializes the newly attached content using the DSL
and protocol semantics. `UpdatePresenceSource` synchronizes the conditional
Optional's controlling field with `exists`; read the DSL expression and preserve
unrelated source bits. The base invalidates the Optional and sets its final
`Exists` state after these hooks finish.

Repeat chooses the number of extra copies using Peach's random context:
1, 2, 3, 4 and 5 copies have probabilities 50%, 25%, 15%, 7% and 3%, respectively.
There is no repetition-count hook. Structural bases do not enforce `maxOccurs`
or protocol constraints.

## Compilation

Call `Build_Mutator_DLL` with no arguments to compile the packet's generated
and semantic partial files together. Repair errors in `.semantic.cs` and repeat
until compilation succeeds. Do not edit `.g.cs` to bypass a missing required
method or change the interface contract.
