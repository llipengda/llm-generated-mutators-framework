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

The lambda must take exactly one argument. Its parameter has the field's
logical Python type (`str`, `int`, `float`, or `bytes`) for static checking. The
compiler rewrites the expression from the Python AST; it does not call the
lambda while declaring or compiling the schema.

An expression may reference at most ONE field, such as `length - 4` or
`(flags & 1) != 0`.

### Reference rules

Lengths, counts, conditions, and relations may refer to a field or a
single-field expression.
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

Prefer references to fields declared earlier in wire order. Anonymous
`Block(...)` and `Union(...)` members are not path-addressable; use their
decorated class forms when another field must reference an internal path.

`Array` and `Optional` are wrappers. Call `.internal()` before accessing a
wrapped schema's field:

```python
class LengthHeader(Schema):
    value = Int16()


class Container(Schema):
    optional_header = Optional[LengthHeader]()
    headers = Array[LengthHeader, Occurs(0, 8)]()


class Packet(Schema):
    container = Container()
    optional_payload = Blob[container.optional_header.internal().value]()
    array_payload = Blob[container.headers.internal().value]()
```

For a wrapped scalar, `.internal()` returns the field reference itself.
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

`Double` accepts the common parameters plus `endian` and `size=32 | 64`, but
not `signed`. `Bit[width]` accepts:

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

`Flags(storage, *, endian)` requires an integer storage type and `"big"` or
`"little"` endian. Declaration order determines bit positions starting at
zero; the compiler does not use endian to calculate them. Bit ranges must not
overlap or exceed the storage width.

### String, DecimalString, and Blob

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

`String.type` is one of `"ascii"`, `"utf7"`, `"utf8"`, `"utf16"`,
`"utf16be"`, `"utf32"`, or `"utf32be"`.

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
Give an unsized value a boundary, such as a following fixed delimiter, or use
`DecimalString[length]` for fixed-width text.

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

An override may be a plain value, `fixed(value)`, a field declaration, a schema
instance, or a direct `Block(...)`. Overrides apply only to immediate members.

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
