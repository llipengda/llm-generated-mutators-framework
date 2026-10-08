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
Likewise, `fixed(...)` with explicit `token=False` keeps references dynamic:
Peach can parse a value different from the declared default.
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
Conditional Optional references that cross these Block scopes use explicit
relative paths in the generated XML. This prevents same-named inner fields
from shadowing the intended source and allows the containing model to be
reused under a different name.
Size and count relations qualify their target with the DataModel name when
another nested path would otherwise shadow that target during Peach lookup.
Computed arguments inside inline Blocks retain the scope where their source
was declared, including inherited fields and references wrapped in Optional,
arrays, or Union alternatives. Their ScriptFixup reference is qualified with
the DataModel name when a nested path would shadow the intended source.

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
Known out-of-range defaults are rejected during compilation; relation writeback
also checks this range before emitting a new value. This relation
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
