# Binary Schema DSL

The DSL's purpose is to describe a protocol's wire format structurally so that
protocol packets can be parsed correctly. The same structure also defines how
the parsed fields are serialized back to bytes.

The DSL uses Python classes to declare binary fields in wire order:

```python
from peach_dsl import *


class Header(Schema):
    kind = Int8()
    length = Int16(endian="big", signed=False)
```

Public `Schema` class attributes are fields. Declaration order is wire order.

## Modeling rules

Choose elements by their wire encoding, not their application meaning:

- fixed-width integers: `Int4`, `Int8`, `Int16`, `Int32`, `Int64`;
- exact-width numeric bit fields: `Bit[width]`;
- encoded text: `String`;
- base-10 integer encoded as text: `DecimalString`;
- uninterpreted bytes: `Blob`;
- structured data: `Schema`, `Block`, `Array`, `Optional`, and `Union`.

A basic wire type is one atomic scalar codec. Field names, aliases, enumerated
meanings, ranges, and relationships do not create new basic types. Packets,
records, headers, framed bodies, lists, optional sections, and alternatives are
compositions, not basic types.

The basic-type audit asks only whether all scalar codecs required for lossless
parsing and serialization exist. Express semantic constraints where the DSL
allows. A semantic constraint that the DSL cannot express does not make an
otherwise lossless scalar unsupported.

Use `ExtendedType` only when ordinary fields and containers cannot implement a
scalar's wire codec. Semantic meaning alone is not enough: a CRC encoded as a
fixed-width integer is still `Int16`, `Int32`, etc., not an `ExtendedType`.

## Values, constraints, and references

Integer defaults and fixed values must fit the field's effective width and
signedness. Compilation checks inherited defaults and instance overrides as
well. Flag values must fit their own unsigned bit width.

### Fixed and initial values

```python
protocol_id = Int16(fixed(0))  # Always zero on the wire
retry_count = Int8(3)          # Initial value; still mutable
```

Use `fixed(...)` only for values required by every matching packet, such as
magic values, discriminators, fixed function codes, and reserved bits. Do not
fix lengths, counters, checksums, IDs, or payload data merely because a seed has
that value.

### Constraints and expressions

Prefer a one-argument lambda for constraints:

```python
quantity = Int16(constraint=lambda value: 1 <= value <= 125)
code = String(
    type="ascii",
    constraint=lambda value: 1 <= len(value) <= 4 and value.isalpha(),
)
```

The lambda must declare exactly one positional parameter (positional-only is
also supported), without additional keyword-only or variadic parameters.
Its parameter has the field's
logical Python type (`str`, `int`, `float`, or `bytes`) for static checking. The
compiler rewrites the expression from the Python AST; it does not call the
lambda while declaring or compiling the schema.

Nested lambdas and comprehensions retain their own variable scopes. Their
parameters and loop variables may reuse `value` or conversion names such as
`int`; only the outer field parameter receives the field's type conversion.

Lambda constraints snapshot referenced module and closure constants when the
field is declared. Supported values are `None`, booleans, integers, floats,
strings, bytes, and tuples of these values. References to external functions,
modules, or mutable objects are rejected instead of exporting unresolved names.

An expression may reference at most ONE field, such as `length - 4` or
`(flags & 1) != 0`.

### Reference rules

Lengths, counts, conditions, and relations may refer to a field or a
single-field expression.
When writing a size or count through a multiplication expression, the reverse
calculation requires exact division. An unrepresentable payload length or
element count raises an error instead of silently rounding the source field.
Left shifts of a field are reversed by exact division by the corresponding
power of two. XOR with a constant is reversed by XOR with the same constant.
For expressions without a unique inverse, such as integer division, remainder,
or bit masks, writing preserves the parsed/default source value when it still
matches the actual target length. A mismatch raises an error; supply a source
value consistent with the new target instead of relying on an ambiguous inverse.
An expression may reference at most ONE field.

```python
length = Int16()
payload = Blob[length]()
checksum = Optional[Int32](when=(flags & 1) != 0)
```

Keep the dependency graph acyclic. **Circular references are forbidden**, both
directly and indirectly. This applies to field relations and to dependencies between
reusable schemas or generated DSL modules. Declare and import dependencies in
one direction; a referenced value must be resolvable without returning to the
field, schema, or module currently being defined.

### Computed field declarations

Use `@compute` to declare a function that calculates a field's value from
other fields or schemas:

Use this feature only for checksums, CRCs, and similar calculated values.
Describe lengths and counts with the DSL's built-in references and relations,
not with `@compute`.

```python
@compute
def calc_crc(header: bytearray) -> int:
    return sum(header) % 65536


class Packet(Schema):
    @Block
    class header(Schema):
        kind = Int8()
        length = Int16()

    crc = Int16(calc_crc(header))
```

A function decorated with `@compute` must have one to four required positional parameters with
no defaults. Scalar fields use their corresponding Python types; a `Schema` or
`Block` argument must be annotated as `bytearray`. Its return value must match
the target field's type.
Binary results are converted to a .NET byte array before being returned to
Peach's ScriptFixup.
Integer results are converted to CLR integer types without losing precision;
results outside the range from `-2**63` to `2**64 - 1` raise an overflow error
for binary integer fields. DecimalString results use decimal text and may exceed
64 bits; a constant field width still limits the number of characters. For a
fixed-width DecimalString, the generated fixup pads the text and includes any
requested null terminator within that width.
Floating-point results use round-trip decimal text because ScriptFixup does
not accept CLR floating-point objects directly; Peach's Double field performs
the final conversion using its configured size and byte order.

Module and closure constants used by the function are captured at compilation
time, including constants read inside comprehensions. Supported captures are
`None`, booleans, integers, floats, strings, bytes, and tuples of these values.
Class bodies preserve their namespace lookup and fall back to captured constants
before assignment, after deletion, or when a branch skips an assignment.
External functions, modules, and mutable objects cannot be captured; put helper logic or
required imports inside the computed function. `global` and `nonlocal`
declarations are rejected because generated fixups do not share the DSL
module's runtime state.
Computed functions and constraints execute in Peach's IronPython runtime.
Assignment expressions (`:=`), pattern matching (`match`), and async/await
syntax are unsupported and rejected before export. Ordinary f-strings and
list/dictionary unpacking remain supported.

Prefer references to fields declared earlier in wire order. Anonymous
`Block(...)` and `Union(...)` members are not path-addressable; use their
decorated class forms when another field must reference an internal path.

`Optional` exposes its wrapped schema's fields directly. `Array` needs
`.internal()` to select its element before accessing a field:

```python
class LengthHeader(Schema):
    value = Int16()


class Container(Schema):
    optional_header = Optional[LengthHeader]()
    headers = Array[LengthHeader, Occurs(0, 8)]()


class Packet(Schema):
    container = Container()
    optional_payload = Blob[container.optional_header.value]()
    array_payload = Blob[container.headers.internal().value]()
```

An optional scalar can be referenced directly.
Decorated unions are accessed by alternative name and need no `.internal()`.

## Scalar fields

### Numeric fields

`Int4`, `Int8`, `Int16`, `Int32`, and `Int64` accept:

```python
IntN(
    value=None,
    *,
    endian=None,       # "big" | "little"
    signed=None,       # bool
    constraint=None,
    field_id=None,
    mutable=None,
    token=None,
    value_type=None,
)
```

Represent Boolean wire values with the numeric type that matches their encoded 
width (for example, `Bit[1]` or `Int8`) and use numeric values such as `0` and `1`.

The common `value_type` option accepts `"string"`, `"hex"`, `"literal"`,
`"ipv4"`, or `"ipv6"` (case-insensitive). Unknown formats are rejected when
declaring a built-in field. The value must also be valid for the chosen format
and field type.
References to fixed fields with a non-`string` value format remain runtime
references: the encoded literal can differ from its decoded scalar value
(for example, `Int8(fixed(12), value_type="hex")` stores decimal 18).
`fixed(...)` cannot be combined with `token=False`; declarations and overrides
using this combination are rejected. To supply a non-token default, pass a plain
value, such as `Int8(2, token=False)`.
Fixed `String` fields with a length or a NUL terminator also keep references
dynamic, because Peach's runtime text can include padding and terminators.

`Double` accepts the common parameters plus `endian` and `size=32 | 64`, but
not `signed`. Finite defaults and fixed values must fit the selected width;
explicit positive/negative infinity and NaN remain supported.
`Bit[width]` accepts:

```python
Bit[width](value=None, *, constraint=None, field_id=None,
           length_type=None, mutable=None, token=None, value_type=None)
```

Set inherited numeric defaults with `@Default`:

```python
@Default(endian="big", signed=False)
class Header(Schema):
    length = Int16()
```

Pack related bits with `@Flags`:

```python
@Flags(Int8, endian="big")
class Control(Schema):
    enabled = Bit[1]()
    mode = Bit[2]()
    reserved = Bit[5](fixed(0))
```

Like `@Block` and `@Union`, `@Flags` turns its decorated declaration into a
field. This permits an inline packed field:

```python
class MqttFixedHeader(Schema):
    @Flags(Int8, endian="big")
    class first_byte(Schema):
        control_packet_type = Bit[4]()
        flags = Bit[4]()

    remaining_length = MqttVarInt()
```

`Flags(storage, *, endian)` requires an integer storage type and `"big"` or
`"little"` endian. Declaration order determines bit positions starting at
zero; the compiler does not use endian to calculate them. Bit ranges must not
overlap or exceed the storage width, and their total must exactly fill it,
including after field overrides. When a Flags declaration is compiled directly
as the root model, the DataModel contains one packed Flags child named `value`.

`Bit` members inside `@Flags` cannot use `constraint`, including in field
overrides and subclasses. Use `fixed(...)` for a required constant bit value.
Outside `@Flags`, `Bit[width]` supports `constraint`.

### String, DecimalString, and Blob

Text defaults and fixed values are checked against the declared encoding during
compilation; for example, an ASCII String cannot contain non-ASCII text.
Explicit hexadecimal String values are checked for valid hex bytes and valid
text in the declared encoding, including complete UTF-16/UTF-32 characters.
String defaults and fixed values containing XML-forbidden characters (such as
NUL) are emitted as hexadecimal bytes using the declared string encoding.
This preserves their wire value without placing invalid characters in the XML.
Text containing literal backslashes is also emitted as hex, preventing Peach
from interpreting sequences such as `\n` a second time.
Explicit NUL padding uses Peach's default padding without emitting an invalid
XML attribute. Other padding characters must fit one UTF-16 code unit and be
valid XML characters.

```python
String(value=None, *, constraint=None, field_id=None, length_type=None,
       mutable=None, token=None, value_type=None, type=None,
       null_terminated=None, pad_character=None)
String[length](...)

DecimalString(value=None, *, constraint=None, field_id=None, length_type=None,
              mutable=None, token=None, value_type=None, type=None,
              null_terminated=None, pad_character=None)
DecimalString[length](...)

Blob(value=None)
Blob[length](value=None, *, constraint=None, field_id=None,
             length_type=None, mutable=None, token=None, value_type=None)
```

Blob byte defaults preserve the supplied bytes in every supported value format.
The default is hex. Explicit string values use ASCII text; high bytes, XML
control characters, and backslash sequences fall back to hex. Literal values
encode a quoted text expression when ASCII permits it. IPv4/IPv6 formats accept
packed addresses of exactly 4/16 bytes and emit address text.
Blob lengths use bytes or bits. A byte default or fixed value must fit the
declared capacity; shorter values retain Peach's zero padding. Character length
units are not valid for Blob fields.

`String.type` is one of `"ascii"`, `"utf7"`, `"utf8"`, `"utf16"`,
`"utf16be"`, `"utf32"`, or `"utf32be"`.
For a constant string length, defaults and fixed values must fit the encoded
capacity, including the terminator when enabled. Padding must fill the remaining
capacity exactly. Bit lengths must be divisible by eight. Character lengths
follow .NET UTF-16 code-unit counting, so a supplementary Unicode character
occupies two units.
An omitted default on a bounded String is treated as an empty string for these
checks, including padding and terminator capacity.
Padding encoding is checked when Peach's padding path is used. A value that
already fills its capacity does not need an encodable padding character.
Character-length padding validates characters actually inserted; byte-length
padding also computes the padding character's encoded width when adding only
a terminator, matching Peach's runtime behavior.
Fixed capacities are checked for computed and literal values as well: bit
lengths must be divisible by eight, UTF-16 byte lengths by two, and UTF-32 byte
lengths by four. A zero-length string cannot request a null terminator.
For UTF-7, the whole padded value must also encode to the declared byte length.
Adjacent shifted characters can merge during encoding; configurations where
Peach's padding would silently produce a shorter value are rejected.
UTF-7 length checks and automatic hex values use Peach/.NET's explicit shift
terminators; Python's UTF-7 encoder can omit those terminators before punctuation.
UTF-7 hex defaults use an implicit shift end at EOF when needed to avoid
Peach's trailing-terminator decoder error. Their decoded text and serialized
wire value remain the same.

For `Blob` and `String`, `length_type="bits"` also applies to dynamic length
references and expressions. Their generated size relations read and write bit
counts; the default unit is bytes.
Static lengths and generated relation expressions reject values outside
Peach's representable range. Byte lengths cannot exceed `2**60 - 1`, bit
lengths cannot exceed `2**63 - 1`, and array counts cannot exceed `2**31 - 1`.
Dynamic relation results must also be nonnegative; the checks run before
Peach converts byte sizes into bits, preventing integer overflow.
String length units must be `bytes`, `bits`, or `chars`, including unbounded
and computed fields; invalid unit names are rejected before XML is generated.
String and Blob length units are normalized to lowercase in generated XML.
Dynamic `length_type="chars"` is supported for ASCII and UTF-16 (either byte
order). Relations convert character units to encoded bytes and invert that
conversion exactly. Other encodings require a byte length or a constant
character length: Peach's size relation cannot infer their byte count from
the character count alone.

Inline `Block(...)` members can reference fields from their enclosing schema
or Block. References follow the original field declaration, even when an inner
Block has a field with the same name, and honor overrides on the enclosing
instance. A `Block[length](...)` used as a Union alternative retains its length
boundary.
Conditional Optional sources use dotted element paths in the generated XML,
qualified with the DataModel name when inner fields shadow the intended source.
The compiler checks Peach lookup order and rejects sources that cannot be
resolved unambiguously. Generated references do not use `../` or `./`.
Size and count relations qualify their target with the DataModel name when
another nested path would otherwise shadow that target during Peach lookup.
If an inner Block also shadows the DataModel name, the compiler checks
Peach's lookup order and uses a shorter path when it resolves the intended
element. If no unambiguous path exists, compilation fails with a request to
rename the DataModel or conflicting nested element.
Computed arguments inside inline Blocks retain the scope where their source
was declared, including inherited fields and references wrapped in Optional,
arrays, or Union alternatives. Their ScriptFixup reference is qualified with
the DataModel name when a nested path would shadow the intended source.
Computed references also retain their declaration scope. Reusing a computed
field or Block in an unrelated model does not rebind an external source by
name. Inheritance and reuse of a Block whose computed sources are local to
that Block remain supported, including instance overrides.

An unsized `String()` consumes the bytes made available by its surrounding
grammar. As the final member of a schema or bounded block, it consumes the
remainder. A following fixed field can delimit it; model the delimiter as its
own field:

```python
class DelimitedText(Schema):
    text = String(type="ascii")
    delimiter = String(fixed("\r\n"), type="ascii")
```

Use `DecimalString` when a textual base-10 integer participates in a numeric
expression:

```python
class TextLengthPacket(Schema):
    length = DecimalString(
        type="ascii",
        constraint=lambda value: 0 <= value <= 9999,
    )
    separator = String(fixed("\r\n"), type="ascii")
    payload = Blob[length]()
```

`DecimalString` has logical type `int` and accepts valid base-10 integer text.
When used as a size/count relation source, its value must fit a signed 64-bit
integer: Peach parses the source before evaluating the relation expression.
Known out-of-range defaults are rejected during compilation; arithmetic relation
writeback also checks this range before emitting a new value. Direct size/count
writeback uses Peach's already bounded integer value. This relation
restriction does not apply to standalone decimal text or computed text results.
Give an unsized value a boundary, such as a following fixed delimiter, or use
`DecimalString[length]` for fixed-width text.
Integer defaults and fixed values with a constant width are zero-padded on the
left, preserving the sign and numeric value. The compiler accounts for the
encoding, length units, and a requested null terminator, and rejects values that
do not fit.
Size and count relations writing to a constant-width DecimalString likewise
format decimal text within that width, reserve space for a requested NUL
terminator, and reject overflow.
Numeric constraints, Optional conditions, and computed inputs strip trailing
NUL padding or terminators before parsing decimal text. Empty text and embedded
NUL characters remain invalid integers.

### Custom scalar types

```python
VarInt = ExtendedType[int]("VarInt")
length = VarInt()
fixed_length = VarInt(fixed(10), encoding="custom")
```

`ExtendedType[T](type_name)` creates a custom scalar factory. `T` must be
`int`, `float`, `str`, or `bytes`; compose container and schema types
with the standard DSL elements instead. The first field argument is a value or
`fixed(value)`. Extra attributes may be `str`, `int`, `float`, or `bool`;
`name` is reserved.

## Composition

### Reusable schemas and overrides

A schema can be used as a field. Override existing members when instantiating
it:

```python
class Header(Schema):
    kind = Int8()
    flags = Int8()


class Request(Schema):
    header = Header(kind=fixed(1), flags=Int8(fixed(0)))
```

An override may be a plain value, `fixed(value)`, or any field declaration,
including a schema instance, `Union`, `Array`, `Optional`, or direct
`Block(...)`. Any field kind may replace any other field kind. Plain and fixed
scalar values require an existing scalar field because they do not describe a
wire type. Overrides apply only to immediate members.
Replacing a `fixed(...)` declaration with a plain value changes it to an initial
value; dependent lengths and counts remain dynamic. Use `fixed(...)` in the
override when the replacement must stay fixed.

For repeated complex construction, use a function or class method:

```python
class Envelope(Schema):
    @Block
    class body(Schema):
        tag = Int8()
        payload = Block(data=Blob())

    @classmethod
    def with_payload(cls, payload: Override):
        return cls(body=cls.body(payload=payload))


class TextPayload(Schema):
    text = String(type="ascii")


message = Envelope.with_payload(TextPayload())
```

Use `Override` for a helper argument that accepts any supported override. It
may be forwarded to a schema constructor or direct block member. A scalar-only
override cannot create a new direct block member because it has no base field
type.

### Block, Array, and Optional

```python
@Block[length]
class body(Schema):
    data = Blob()

direct = Block(first=Int8(), rest=Blob())
items = Array[Item, count]()
bounded = Array[Int8, Occurs(0, 8)]()
remaining = Array[Item]()
checksum = Optional[Int32](when=(flags & 1) != 0)
label = Optional[str]()
```

- `Block[length]` bounds the entire block. Decorated and direct blocks support
  nested overrides.
- A decorated block is path-addressable. A direct block is anonymous.
- `Array[element, count]()` has an exact count from an integer, reference, or
  expression.
- `Array[element, Occurs(minimum, maximum)]()` has an inclusive range.

- `Array[element]()` parses until the next element fails or its containing
  block ends. Put it last or inside a bounded block.
- `Optional[element]()` is unconditional optional content;
  `Optional[element](when=expression)` is conditionally present.

Explicit occurrence counts and range endpoints cannot exceed `2147483647`,
matching Peach's signed 32-bit array attributes. Constant expressions are
checked after evaluation as well.

### Union

```python
choice = Union(request=Request, response=Response)

@Union
class choice(Schema):
    request = Request()
    response = Response()

unnamed = Request | Response
```

A union needs at least two alternatives. It tries them in declaration order
and selects the first that parses successfully. Fixed fields can reject an
alternative, so put specific alternatives before broad ones:

```python
class Open(Schema):
    kind = Int8(fixed(1))
    channel = Int16()


class Close(Schema):
    kind = Int8(fixed(2))
    channel = Int16()


message = Union(open=Open, close=Close)
```

A decorated union is path-addressable and supports alternative overrides, such
as `choice(response=EmptyResponse())`. Direct unions and `Request | Response`
are anonymous.

Mark the packet-entry choice with `@PacketUnion`:

```python
@PacketUnion
class Packet(Schema):
    packet_union = Union(request=Request, response=Response)
```

## Nested class scope

Nested `@Block` and `@Union` class bodies follow normal Python scope rules.
They may refer to earlier fields in the same nested class, but not to names in
the enclosing class body. A decorator itself is evaluated in the enclosing
scope:

```python
class Packet(Schema):
    length = Int8()

    @Block[length]  # Valid: decorator is evaluated in Packet.
    class body(Schema):
        flags = Int8()
        item = Optional[Int8](when=(flags & 1) != 0)  # Valid: local field.
```

When nested members must refer to outer fields, use direct forms, whose
arguments are evaluated in the enclosing class body:

```python
class Packet(Schema):
    header = Header()

    body = Block[header.length](
        item=Optional[Int8](when=(header.flags & 1) != 0),
        payload=Blob(),
    )
    payload = Union(
        short=Blob[header.short_length](),
        long=Blob[header.long_length](),
    )
```

## Complete examples

### HTTP-like headers with a Content-Length relation

This simplified model mixes `Content-Length` with generic header lines. The
specific alternative comes first because `Union` selects the first alternative
that parses successfully:

```python
from peach_dsl import *


class ContentLengthHeader(Schema):
    name = String(fixed("Content-Length"), type="ascii")
    separator = String(fixed(": "), type="ascii")
    value = DecimalString(type="ascii")
    terminator = String(fixed("\r\n"), type="ascii")


class GenericHeader(Schema):
    name = String(type="ascii")
    separator = String(fixed(": "), type="ascii")
    value = String(type="ascii")
    terminator = String(fixed("\r\n"), type="ascii")


@Union
class HeaderLine(Schema):
    content_length = ContentLengthHeader()
    generic = GenericHeader()


class Headers(Schema):
    lines = Array[HeaderLine]()
    terminator = String(fixed("\r\n"), type="ascii")


class Request(Schema):
    headers = Headers()
    body = Blob[headers.lines.internal().content_length.value]()
```

`Headers.lines` is an array of union alternatives, so the body reference first
uses `.internal()` to enter the array element, then selects the
`content_length` alternative and its `value` field. The compiler emits a size
Relation from that `DecimalString` to `body`. The final fixed CRLF terminates
the unbounded header array.

### Modbus TCP Read Holding Registers

The MBAP `length` counts the following Unit Identifier and PDU bytes:

```python
from peach_dsl import *


@Default(endian="big", signed=False)
class MbapHeader(Schema):
    transaction_id = Int16()
    protocol_id = Int16(fixed(0))
    length = Int16(constraint=lambda value: 2 <= value <= 254)


@Default(endian="big", signed=False)
class ReadHoldingRegistersRequest(Schema):
    header = MbapHeader(length=fixed(6))

    @Block[header.length]
    class message(Schema):
        unit_id = Int8()
        function_code = Int8(fixed(0x03))
        starting_address = Int16()
        quantity = Int16(constraint=lambda value: 1 <= value <= 125)
```

A matching packet is:

```text
00 01  00 00  00 06  01  03  00 6B  00 03
^^^^^  ^^^^^  ^^^^^  ^^  ^^  ^^^^^  ^^^^^
Tx ID  Proto  Length Unit Func Address Quantity
```

### Evaluated Peach element paths

`evaluate_schema(target, name=None, include_header=True)` assigns `path` to every
result node using the element hierarchy instantiated from the generated XML. Paths are dotted and start with the
DataModel name, for example `Packet.body.payload.value`. Pass the same `name`
and `include_header` arguments as the XML export being inspected.

For model libraries, `ref` is expanded at each instance. A field inherited from
`Header` through `Packet.header` has path `Packet.header.flags`. Reusing `Header`
at `Packet.other` produces the distinct path `Packet.other.flags`. Explicit
overrides use the same instance hierarchy. Compiler-created Optional and
nested-array nodes are included, and resolved model names include any collision suffix.
`OptionalResult.source_path` identifies the source field under the same root
field; it is distinct from the lookup string written to the XML `src` attribute.

Evaluation now exports and validates the model to ensure these paths correspond
to XML that can actually be generated. It does not execute computed functions.
Error reports continue to use their existing DSL paths for diagnosis.

Paths use `[]` to represent an array item at any index. The Array container path
is `Packet.items`, an item path is `Packet.items[]`, and a field inside an item
has path `Packet.items[].value`. Nested arrays use paths such as
`Packet.matrix[].item[]`. This notation abstracts Peach's concrete names:
`Packet.items[].value` represents `Packet.items.items_0.value`,
`Packet.items.items_1.value`, etc. A path containing `[]` is a pattern, so it
must be matched against runtime items before calling Peach's `find` with a
concrete path. Before expansion, the corresponding `OriginalElement` has
native path `Packet.items.items`; the stored DSL path remains `Packet.items[]`.
Choice paths include all
template alternatives; after a branch is selected, `find` can only access that
selected branch through the Choice.

Instance path patterns are unique; an array item pattern can match multiple
runtime items. Export and evaluation reject duplicate paths and
names with dots or slash separators, which would make dotted paths ambiguous.
Result wrappers that represent the same Peach element may share its path.

### Node classifications

`get_node_types(target, name=None, include_header=True)` returns
`list[tuple[NodeType, str]]` in traversal order under the requested root,
visiting each instantiated template node once, including referenced descendants,
native Array wrappers and prototypes, compiler-created wrappers and empty
Optional branches. Paths use the same model names and export options as XML;
unattached library definitions are not separate traversal roots.

`NodeType` is an `IntFlag`: combine properties with `|`, and test membership with
`&`. `FIXED` and `MUTABLE` are mutually exclusive. Fields declared with
`fixed(...)`, token fields (`token=True`), and nodes with
`mutable=False` are `FIXED`; all others are `MUTABLE`. `fixed(...)` with
`token=False` is forbidden.
`DERIVED` is an independent flag on fields whose values are calculated by a
compiled Peach `Relation` (such as size/count fields). It combines with
`MUTABLE` or `FIXED` and does not change the DSL value, XML token/mutability,
or relation behavior. The referenced payload/list is not marked `DERIVED`,
and Optional condition sources are not derived merely because they control
presence. Classification uses the effective expanded XML, including model
references and overrides; the flag does not propagate to parents or children.
The mutator planner skips `DERIVED` fields just as it skips `FIXED` fields.
Container mutators should modify content/structure and let Relations update
derived fields. This flag describes Relations, not arbitrary runtime Fixups.
Array nodes are `REPEATABLE`, including arrays with a constant occurrence count.
Optional wrappers are `OPTIONAL`; arrays are also optional when their count can
be zero (unspecified/dynamic counts, zero constants, or `Occurs` with minimum 0).
`REPEATABLE` and `OPTIONAL` apply only to the corresponding Array or Optional
wrapper. They do not propagate to item prototypes, contained fields, reference
descendants, or empty branches. A nested Array or Optional wrapper still has
its own properties. For example, `Packet.items` can be
`MUTABLE|REPEATABLE|OPTIONAL`, while `Packet.items[]` and
`Packet.items[].value` are `MUTABLE` unless those nodes have their own fixed
properties or wrappers.

```python
for node_type, path in get_node_types(Packet):
    if node_type & NodeType.OPTIONAL:
        print(node_type.name, path)
# Example: FIXED|REPEATABLE|OPTIONAL Packet.payload.value
```

### Declared mutation targets

`get_mutation_nodes(target, name=None, include_header=True)` returns immutable
`MutationNode` records for deterministic mutator planning. Each record contains
the rooted `path`, local `node_type` flags, actual Peach `runtime_type` tag,
DSL `kind`, diagnostic `dsl_path`, and a stable human-readable `description`.
For an unconditional Optional, `empty_branch_name` is the compiler-selected
empty Choice branch, including collision handling. For a conditional Optional,
`source_path` is the rooted controlling field path and `condition` retains the
evaluated expression tree.

The traversal follows declared logical nodes. It excludes the root, generated
Optional empty branches, nested-array scaffold Blocks, and the compiler's
single-packet duplicate alternative. Optional Union wrappers appear once,
followed by their declared alternatives. Declared array item prototypes retain
`[]` paths. Referenced model instances and field overrides use their effective
runtime tags and the same rooted paths as the XML compiler. Flags are never
propagated from a wrapper to its children.

```python
for node in get_mutation_nodes(Packet, name="Root"):
    if node.node_type & NodeType.OPTIONAL:
        print(node.path, node.runtime_type, node.empty_branch_name, node.source_path)
```

This API describes all declared nodes, including fixed/derived fields and protocol
containers. The caller selects mutable targets and excludes pipeline-specific
envelopes such as the outer packet array.

### Generating supportedDataElement expressions

`get_supported_data_element_expressions(target, name=None, include_header=True)`
returns `(NodeType, path, expression)` for each node, in the same order as
`get_node_types()`. The expression is C# code for the parameter `obj` in
`supportedDataElement(DataElement obj)`. Classifications remain local to each
node; callers can select rows using the `NodeType` flags.

```python
for node_type, path, expression in get_supported_data_element_expressions(Packet):
    if node_type & NodeType.MUTABLE:
        print(path, expression)
```

`supported_data_element_expression(path)` generates one expression directly:

```python
supported_data_element_expression("Root.header.value")
# obj.MatchesPath(@"Root.header.value")

supported_data_element_expression("Root.items[].value")
# obj.MatchesPath(@"Root.items[].value")
```

Copy the generated expression into the method's return statement:

```csharp
using Peach.LLM.Core;

public static bool supportedDataElement(DataElement obj)
{
    return obj.MatchesPath(@"Root.items[].value");
}
```

`MatchesPath` is provided by `Peach.LLM.Core` in the updated LLM-Peach SDK.
The full root and ancestry must match. Array patterns require native Array
parents and expanded item names with nonempty ASCII numeric indices. Nested
arrays are supported; unexpanded `OriginalElement` prototypes do not match `[]`.
Array containers have their own paths (`Root.items`). Null elements and invalid
paths return false. The matcher uses no regex. Use an updated `Peach.LLM.dll`
when compiling generated mutators.
