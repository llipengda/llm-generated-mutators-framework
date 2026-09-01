# pyright: strict
from __future__ import annotations

import ast
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field as dataclass_field, replace
import hashlib
import inspect
import re
from types import MappingProxyType
from typing import Callable, Generic, Literal, Self, TypeVar, cast, overload
from xml.etree import ElementTree as ET


T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
S = TypeVar("S", bound="Schema")
Endian = Literal["big", "little"]
StringEncoding = Literal[
    "ascii",
    "utf7",
    "utf8",
    "utf16",
    "utf16be",
    "utf32",
    "utf32be",
]
_STRING_ENCODINGS = frozenset(
    {"ascii", "utf7", "utf8", "utf16", "utf16be", "utf32", "utf32be"}
)


@dataclass(frozen=True, slots=True)
class SchemaDefaults:
    endian: Endian | None = None
    signed: bool | None = None

    def merge(self, inherited: SchemaDefaults) -> SchemaDefaults:
        return SchemaDefaults(
            self.endian if self.endian is not None else inherited.endian,
            self.signed if self.signed is not None else inherited.signed,
        )


ROOT_DEFAULTS = SchemaDefaults(signed=False)


class Fixed(Generic[T_co]):
    """A value that is fixed by a schema instead of read from the wire."""

    def __init__(self, value: T_co) -> None:
        self._value = value

    @property
    def value(self) -> T_co:
        return self._value

    def __repr__(self) -> str:
        return f"fixed({self.value!r})"


def fixed(value: T) -> Fixed[T]:
    return Fixed(value)


ConstraintLiteral = int | float | bool | str


class _ConstraintNode:
    pass


@dataclass(frozen=True, slots=True, eq=False)
class ConstraintValue(_ConstraintNode, Generic[T]):
    """The symbolic ``value`` supplied to a typed constraint lambda."""

    def __len__(self: ConstraintValue[str] | ConstraintValue[bytes]) -> int:
        """Allow ``len(value)`` in a constraint lambda's source expression."""

        return 0

    @overload
    def __getitem__(self: ConstraintValue[str], index: int) -> str: ...

    @overload
    def __getitem__(self: ConstraintValue[bytes], index: int) -> int: ...

    def __getitem__(self, index: int) -> str | int:
        """Allow indexed string and byte constraints without executing them."""

        raise TypeError("ConstraintValue is symbolic and cannot be indexed")

    def __mod__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("%", self, other)

    def __rmod__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("%", other, self)

    def __add__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("+", self, other)

    def __radd__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("+", other, self)

    def __sub__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("-", self, other)

    def __rsub__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("-", other, self)

    def __mul__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("*", self, other)

    def __rmul__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("*", other, self)

    def __floordiv__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("//", self, other)

    def __rfloordiv__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("//", other, self)

    def __and__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("&", self, other)

    def __rand__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("&", other, self)

    def __or__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("|", self, other)

    def __ror__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("|", other, self)

    def __xor__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("^", self, other)

    def __rxor__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("^", other, self)

    def __lshift__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("<<", self, other)

    def __rlshift__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr("<<", other, self)

    def __rshift__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr(">>", self, other)

    def __rrshift__(self: ConstraintValue[int], other: int) -> Expr:
        return Expr(">>", other, self)

    def __eq__(self, other: T) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("==", self, cast(ConstraintLiteral, other))

    def __ne__(self, other: T) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("!=", self, cast(ConstraintLiteral, other))

    def __lt__(
        self: ConstraintValue[int] | ConstraintValue[float],
        other: int | float,
    ) -> Expr:
        return Expr("<", self, other)

    def __le__(
        self: ConstraintValue[int] | ConstraintValue[float],
        other: int | float,
    ) -> Expr:
        return Expr("<=", self, other)

    def __gt__(
        self: ConstraintValue[int] | ConstraintValue[float],
        other: int | float,
    ) -> Expr:
        return Expr(">", self, other)

    def __ge__(
        self: ConstraintValue[int] | ConstraintValue[float],
        other: int | float,
    ) -> Expr:
        return Expr(">=", self, other)


class Field(Generic[T_co]):
    """Description of one value in a schema."""

    def __init__(self, kind: str, **options: object) -> None:
        self.kind = kind
        self.options = MappingProxyType(dict(options))
        self.owner: type[Schema] | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type[Schema], name: str) -> None:
        self.owner = owner
        self.name = name

    @overload
    def __get__(self, instance: None, owner: type[Schema]) -> Field[T_co]: ...

    @overload
    def __get__(
        self,
        instance: _SchemaInstance,
        owner: type[Schema],
    ) -> MemberRef[T_co]: ...

    @overload
    def __get__(self, instance: object, owner: type[object]) -> Field[T_co]: ...

    def __get__(
        self,
        instance: object | None,
        owner: type[object],
    ) -> Field[T_co] | MemberRef[T_co]:
        if not isinstance(instance, _SchemaInstance):
            return self
        if self.name is None:
            raise ValueError("a bound field has no name")
        return MemberRef(instance, instance.binding_path + (self.name,), self)

    def __repr__(self) -> str:
        name = self.name or "?"
        options = ", ".join(f"{key}={value!r}" for key, value in self.options.items())
        suffix = f", {options}" if options else ""
        return f"Field({name!r}, kind={self.kind!r}{suffix})"

    def __add__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("+", self, other)

    def __radd__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("+", other, self)

    def __sub__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("-", self, other)

    def __rsub__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("-", other, self)

    def __mul__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("*", self, other)

    def __rmul__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("*", other, self)

    def __floordiv__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("//", self, other)

    def __rfloordiv__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("//", other, self)

    def __mod__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("%", self, other)

    def __rmod__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("%", other, self)

    def __eq__(self: Field[int] | Field[str], other: ExprOperand) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("==", self, other)

    def __ne__(self: Field[int] | Field[str], other: ExprOperand) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("!=", self, other)

    def __lt__(self: Field[int] | Field[str], other: ExprOperand) -> Expr:
        return Expr("<", self, other)

    def __le__(self: Field[int] | Field[str], other: ExprOperand) -> Expr:
        return Expr("<=", self, other)

    def __gt__(self: Field[int] | Field[str], other: ExprOperand) -> Expr:
        return Expr(">", self, other)

    def __ge__(self: Field[int] | Field[str], other: ExprOperand) -> Expr:
        return Expr(">=", self, other)

    def __and__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("&", self, other)

    def __rand__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("&", other, self)

    def __or__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("|", self, other)

    def __ror__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("|", other, self)

    def __xor__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("^", self, other)

    def __rxor__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("^", other, self)

    def __lshift__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("<<", self, other)

    def __rlshift__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr("<<", other, self)

    def __rshift__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr(">>", self, other)

    def __rrshift__(self: Field[int], other: ExprOperand) -> Expr:
        return Expr(">>", other, self)


@dataclass(frozen=True, slots=True, eq=False)
class MemberRef(Generic[T_co]):
    """A field reference bound to a concrete Schema instance and member path."""

    instance: _SchemaInstance
    path: tuple[str, ...]
    definition: Field[T_co]

    @property
    def name(self) -> str:
        return ".".join(self.path)

    def __add__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("+", self, other)

    def __radd__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("+", other, self)

    def __sub__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("-", self, other)

    def __rsub__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("-", other, self)

    def __mul__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("*", self, other)

    def __rmul__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("*", other, self)

    def __floordiv__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("//", self, other)

    def __rfloordiv__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("//", other, self)

    def __mod__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("%", self, other)

    def __rmod__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("%", other, self)

    def __eq__(self: MemberRef[int] | MemberRef[str], other: ExprOperand) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("==", self, other)

    def __ne__(self: MemberRef[int] | MemberRef[str], other: ExprOperand) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("!=", self, other)

    def __lt__(self: MemberRef[int] | MemberRef[str], other: ExprOperand) -> Expr:
        return Expr("<", self, other)

    def __le__(self: MemberRef[int] | MemberRef[str], other: ExprOperand) -> Expr:
        return Expr("<=", self, other)

    def __gt__(self: MemberRef[int] | MemberRef[str], other: ExprOperand) -> Expr:
        return Expr(">", self, other)

    def __ge__(self: MemberRef[int] | MemberRef[str], other: ExprOperand) -> Expr:
        return Expr(">=", self, other)

    def __and__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("&", self, other)

    def __rand__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("&", other, self)

    def __or__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("|", self, other)

    def __ror__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("|", other, self)

    def __xor__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("^", self, other)

    def __rxor__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("^", other, self)

    def __lshift__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("<<", self, other)

    def __rlshift__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr("<<", other, self)

    def __rshift__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr(">>", self, other)

    def __rrshift__(self: MemberRef[int], other: ExprOperand) -> Expr:
        return Expr(">>", other, self)


@dataclass(frozen=True, slots=True, eq=False)
class Expr:
    """An expression containing at most one field and integer constants."""

    operation: str
    left: ExprOperand
    right: ExprOperand

    def __post_init__(self) -> None:
        if _count_expr_fields(self) > 1:
            raise ValueError("an Expr may reference at most one Field")

    @property
    def op(self) -> str:
        return self.operation

    def __add__(self, other: ExprOperand) -> Expr:
        return Expr("+", self, other)

    def __radd__(self, other: ExprOperand) -> Expr:
        return Expr("+", other, self)

    def __sub__(self, other: ExprOperand) -> Expr:
        return Expr("-", self, other)

    def __rsub__(self, other: ExprOperand) -> Expr:
        return Expr("-", other, self)

    def __mul__(self, other: ExprOperand) -> Expr:
        return Expr("*", self, other)

    def __rmul__(self, other: ExprOperand) -> Expr:
        return Expr("*", other, self)

    def __floordiv__(self, other: ExprOperand) -> Expr:
        return Expr("//", self, other)

    def __rfloordiv__(self, other: ExprOperand) -> Expr:
        return Expr("//", other, self)

    def __mod__(self, other: ExprOperand) -> Expr:
        return Expr("%", self, other)

    def __rmod__(self, other: ExprOperand) -> Expr:
        return Expr("%", other, self)

    def __eq__(self, other: ExprOperand) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("==", self, other)

    def __ne__(self, other: ExprOperand) -> Expr:  # pyright: ignore[reportIncompatibleMethodOverride]
        return Expr("!=", self, other)

    def __lt__(self, other: ExprOperand) -> Expr:
        return Expr("<", self, other)

    def __le__(self, other: ExprOperand) -> Expr:
        return Expr("<=", self, other)

    def __gt__(self, other: ExprOperand) -> Expr:
        return Expr(">", self, other)

    def __ge__(self, other: ExprOperand) -> Expr:
        return Expr(">=", self, other)

    def __and__(self, other: ExprOperand) -> Expr:
        return Expr("&", self, other)

    def __rand__(self, other: ExprOperand) -> Expr:
        return Expr("&", other, self)

    def __or__(self, other: ExprOperand) -> Expr:
        return Expr("|", self, other)

    def __ror__(self, other: ExprOperand) -> Expr:
        return Expr("|", other, self)

    def __xor__(self, other: ExprOperand) -> Expr:
        return Expr("^", self, other)

    def __rxor__(self, other: ExprOperand) -> Expr:
        return Expr("^", other, self)

    def __lshift__(self, other: ExprOperand) -> Expr:
        return Expr("<<", self, other)

    def __rlshift__(self, other: ExprOperand) -> Expr:
        return Expr("<<", other, self)

    def __rshift__(self, other: ExprOperand) -> Expr:
        return Expr(">>", self, other)

    def __rrshift__(self, other: ExprOperand) -> Expr:
        return Expr(">>", other, self)

    def __repr__(self) -> str:
        return _format_expr(self)


ExprOperand = (
    ConstraintLiteral
    | Fixed[int]
    | Fixed[str]
    | Field[int]
    | Field[str]
    | MemberRef[int]
    | MemberRef[str]
    | _ConstraintNode
    | Expr
)

ConstraintBuilder = Callable[[T], bool]
ConstraintInput = str | ConstraintBuilder[T]


def _compile_constraint(
    constraint: ConstraintInput[T], *, value_cast: str | None = None
) -> str:
    expression = (
        constraint
        if isinstance(constraint, str)
        else _constraint_source_expression(constraint)
    )
    if value_cast is None:
        return expression
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise TypeError("constraint must be a valid Python expression") from error
    rewritten = _ConstraintValueCastRewriter(value_cast).visit(tree.body)
    ast.fix_missing_locations(rewritten)
    return ast.unparse(rewritten)


def _constraint_source_expression(constraint: ConstraintBuilder[T]) -> str:
    """Compile a constraint lambda without evaluating its body."""

    try:
        lines, _ = inspect.findsource(constraint)
        source = "".join(lines)
        tree = ast.parse(source)
    except (OSError, TypeError, IndentationError, SyntaxError) as error:
        raise TypeError(
            "constraint lambda source is unavailable; use a string constraint instead"
        ) from error

    target_line = constraint.__code__.co_firstlineno
    lambda_node = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Lambda) and node.lineno == target_line
        ),
        None,
    )
    if lambda_node is None:
        raise TypeError("could not locate the constraint lambda in its source")
    if len(lambda_node.args.args) != 1:
        raise TypeError("a constraint lambda must have exactly one parameter")

    parameter = lambda_node.args.args[0].arg
    expression = _ConstraintParameterRewriter(parameter).visit(lambda_node.body)
    ast.fix_missing_locations(expression)
    return ast.unparse(expression)


class _ConstraintParameterRewriter(ast.NodeTransformer):
    def __init__(self, parameter: str) -> None:
        self.parameter = parameter

    def visit_Name(self, node: ast.Name) -> ast.expr:
        if node.id == self.parameter:
            return ast.copy_location(ast.Name(id="value", ctx=node.ctx), node)
        return node


class _ConstraintValueCastRewriter(ast.NodeTransformer):
    """Restore the field type erased by Peach's constraint evaluator."""

    def __init__(self, value_cast: str) -> None:
        self.value_cast = value_cast

    def visit_Name(self, node: ast.Name) -> ast.expr:
        if node.id != "value" or not isinstance(node.ctx, ast.Load):
            return node
        value = ast.copy_location(ast.Name(id="value", ctx=ast.Load()), node)
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id=self.value_cast, ctx=ast.Load()),
                args=[value],
                keywords=[],
            ),
            node,
        )


def _count_expr_fields(operand: ExprOperand) -> int:
    if isinstance(operand, Expr):
        return _count_expr_fields(operand.left) + _count_expr_fields(operand.right)
    return 1 if isinstance(operand, (Field, MemberRef)) else 0


ScalarValue = int | float | bool | str | bytes
AnyField = Field[ScalarValue]
FieldOverride = T | Fixed[T] | Field[T]
PeachAttributeValue = int | float | bool | str
ExtendedValue = TypeVar("ExtendedValue", bound=ScalarValue)
FixedInput = T | Fixed[T]


def _fixed(value: FixedInput[T] | None) -> Fixed[T] | None:
    if isinstance(value, Fixed):
        return cast(Fixed[T], value)
    return None


def _default_value(value: FixedInput[T] | None) -> T | None:
    if value is None or isinstance(value, Fixed):
        return None
    return value


class ScalarType(Generic[T]):
    def __init__(self, name: str, width: int | None = None) -> None:
        self.name = name
        self.width = width

    def __call__(
        self,
        value: FixedInput[T] | None = None,
        *,
        constraint: ConstraintInput[T] | None = None,
        field_id: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
    ) -> Field[T]:
        return Field(
            self.name,
            constant=_fixed(value),
            value=_default_value(value),
            **_common_peach_options(
                constraint=constraint,
                field_id=field_id,
                mutable=mutable,
                token=token,
                value_type=value_type,
            ),
        )


class IntegerType(ScalarType[int]):
    """An integer of a fixed width whose signedness is a field option."""

    def __call__(
        self,
        value: FixedInput[int] | None = None,
        *,
        endian: Endian | None = None,
        signed: bool | None = None,
        constraint: ConstraintInput[int] | None = None,
        field_id: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
    ) -> Field[int]:
        return Field(
            self.name,
            constant=_fixed(value),
            value=_default_value(value),
            endian=endian,
            signed=signed,
            integer=True,
            **_common_peach_options(
                constraint=constraint,
                constraint_value_cast="int",
                field_id=field_id,
                mutable=mutable,
                token=token,
                value_type=value_type,
            ),
        )


class DoubleType(ScalarType[float]):
    """A Peach floating-point value with a fixed bit width."""

    def __call__(
        self,
        value: FixedInput[float] | None = None,
        *,
        size: Literal[32, 64] = 64,
        endian: Endian | None = None,
        constraint: ConstraintInput[float] | None = None,
        field_id: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
    ) -> Field[float]:
        return Field(
            self.name,
            constant=_fixed(value),
            value=_default_value(value),
            size=size,
            endian=endian,
            floating=True,
            **_common_peach_options(
                constraint=constraint,
                constraint_value_cast="float",
                field_id=field_id,
                mutable=mutable,
                token=token,
                value_type=value_type,
            ),
        )


class ExtendedType(Generic[ExtendedValue]):
    """A Peach element type with caller-defined XML attributes.

    ``ExtendedType[str]("Asn1")`` can be used like a scalar factory.  Its
    keyword arguments are preserved on the generated ``<Asn1>`` element.
    """

    def __init__(self, name: str) -> None:
        if not name:
            raise ValueError("an ExtendedType needs a non-empty element name")
        self.name = name

    def __call__(
        self,
        value: FixedInput[ExtendedValue] | None = None,
        /,
        **attributes: PeachAttributeValue,
    ) -> Field[ExtendedValue]:
        return Field(
            self.name,
            constant=_fixed(value),
            value=_default_value(value),
            extended_attributes=MappingProxyType(dict(attributes)),
        )


def _common_peach_options(
    *,
    constraint: ConstraintInput[T] | None,
    constraint_value_cast: str | None = None,
    field_id: str | None,
    mutable: bool | None,
    token: bool | None,
    value_type: str | None,
) -> dict[str, object]:
    options: dict[str, object] = {}
    if constraint is not None:
        options["peach_constraint"] = _compile_constraint(
            constraint, value_cast=constraint_value_cast
        )
    if field_id is not None:
        options["field_id"] = field_id
    if mutable is not None:
        options["mutable"] = mutable
    if token is not None:
        options["token"] = token
    if value_type is not None:
        options["value_type"] = value_type
    return options


Length = int | Field[int] | MemberRef[int] | Fixed[int] | Expr


@dataclass(frozen=True, slots=True)
class Occurs:
    """Inclusive minimum and maximum occurrence counts for a repeated field."""

    min_occurs: int
    max_occurs: int

    def __post_init__(self) -> None:
        if self.min_occurs < 0:
            raise ValueError("min_occurs must be non-negative")
        if self.max_occurs < self.min_occurs:
            raise ValueError("max_occurs must be at least min_occurs")

    def __repr__(self) -> str:
        return f"Occurs({self.min_occurs}, {self.max_occurs})"


ArrayCount = Length | Occurs | None


class SizedType(Generic[T]):
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, value: FixedInput[T] | None = None) -> Field[T]:
        """Create an unsized value, for example a Blob consuming its container."""

        return Field(
            self.name, constant=_fixed(value), value=_default_value(value)
        )

    def __getitem__(self, length: Length) -> BoundSizedType[T]:
        return BoundSizedType(self.name, length)


class StringType(SizedType[str]):
    def __getitem__(self, length: Length) -> BoundStringType:
        return BoundStringType(self.name, length)

    def __call__(
        self,
        value: FixedInput[str] | None = None,
        *,
        constraint: ConstraintInput[str] | None = None,
        field_id: str | None = None,
        length_type: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
        type: StringEncoding | None = None,
        null_terminated: bool | None = None,
        pad_character: str | None = None,
    ) -> Field[str]:
        options: dict[str, object] = {
            "constant": _fixed(value),
            "value": _default_value(value),
            **_common_peach_options(
                constraint=constraint,
                field_id=field_id,
                mutable=mutable,
                token=token,
                value_type=value_type,
            ),
        }
        _append_string_options(
            options,
            length_type=length_type,
            type=type,
            null_terminated=null_terminated,
            pad_character=pad_character,
        )
        return Field(self.name, **options)


class BoundSizedType(Generic[T]):
    def __init__(self, name: str, length: Length) -> None:
        self.name = name
        self.length = length

    def __call__(
        self,
        value: FixedInput[T] | None = None,
        *,
        constraint: ConstraintInput[T] | None = None,
        field_id: str | None = None,
        length_type: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
    ) -> Field[T]:
        options: dict[str, object] = {
            "length": self.length,
            "constant": _fixed(value),
            "value": _default_value(value),
            **_common_peach_options(
                constraint=constraint,
                field_id=field_id,
                mutable=mutable,
                token=token,
                value_type=value_type,
            ),
        }
        if length_type is not None:
            options["length_type"] = length_type
        return Field(self.name, **options)


class BoundStringType(BoundSizedType[str]):
    def __call__(
        self,
        value: FixedInput[str] | None = None,
        *,
        constraint: ConstraintInput[str] | None = None,
        field_id: str | None = None,
        length_type: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
        type: StringEncoding | None = None,
        null_terminated: bool | None = None,
        pad_character: str | None = None,
    ) -> Field[str]:
        field = super().__call__(
            value,
            constraint=constraint,
            field_id=field_id,
            length_type=length_type,
            mutable=mutable,
            token=token,
            value_type=value_type,
        )
        options = dict(field.options)
        _append_string_options(
            options,
            length_type=None,
            type=type,
            null_terminated=null_terminated,
            pad_character=pad_character,
        )
        return Field(self.name, **options)


class BoundDecimalStringType(BoundSizedType[int]):
    def __call__(
        self,
        value: FixedInput[int] | None = None,
        *,
        constraint: ConstraintInput[int] | None = None,
        field_id: str | None = None,
        length_type: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
        type: StringEncoding | None = None,
        null_terminated: bool | None = None,
        pad_character: str | None = None,
    ) -> Field[int]:
        options: dict[str, object] = {
            "length": self.length,
            "constant": _fixed(value),
            "value": _default_value(value),
            **_common_peach_options(
                constraint=constraint,
                constraint_value_cast="int",
                field_id=field_id,
                mutable=mutable,
                token=token,
                value_type=value_type,
            ),
        }
        _append_string_options(
            options,
            length_type=length_type,
            type=type,
            null_terminated=null_terminated,
            pad_character=pad_character,
        )
        return Field(self.name, **options)


def _append_string_options(
    options: dict[str, object],
    *,
    length_type: str | None,
    type: StringEncoding | None,
    null_terminated: bool | None,
    pad_character: str | None,
) -> None:
    if length_type is not None:
        options["length_type"] = length_type
    if type is not None:
        if type not in _STRING_ENCODINGS:
            raise ValueError(f"unsupported Peach string type: {type}")
        options["type"] = type
    if null_terminated is not None:
        options["null_terminated"] = null_terminated
    if pad_character is not None:
        if len(pad_character) != 1:
            raise ValueError("pad_character must contain exactly one character")
        options["pad_character"] = pad_character


class DecimalStringType(ScalarType[int]):
    """A decimal integer encoded as a Peach String on the wire."""

    def __getitem__(self, length: Length) -> BoundDecimalStringType:
        return BoundDecimalStringType(self.name, length)

    def __call__(
        self,
        value: FixedInput[int] | None = None,
        *,
        constraint: ConstraintInput[int] | None = None,
        field_id: str | None = None,
        length_type: str | None = None,
        mutable: bool | None = None,
        token: bool | None = None,
        value_type: str | None = None,
        type: StringEncoding | None = None,
        null_terminated: bool | None = None,
        pad_character: str | None = None,
    ) -> Field[int]:
        options: dict[str, object] = {
            "constant": _fixed(value),
            "value": _default_value(value),
            **_common_peach_options(
                constraint=constraint,
                constraint_value_cast="int",
                field_id=field_id,
                mutable=mutable,
                token=token,
                value_type=value_type,
            ),
        }
        _append_string_options(
            options,
            length_type=length_type,
            type=type,
            null_terminated=null_terminated,
            pad_character=pad_character,
        )
        return Field(self.name, **options)


class OptionalField(Generic[T_co]):
    """A scalar or schema field that is optional unconditionally or by expression."""

    def __init__(
        self,
        element: OptionalElement,
        constraint: FixedInput[ScalarValue] | None,
        condition: Expr | None,
    ) -> None:
        self.element = element
        self.constraint = constraint
        self.condition = condition
        self.owner: type[Schema] | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type[Schema], name: str) -> None:
        self.owner = owner
        self.name = name

    @overload
    def __get__(self, instance: None, owner: type[Schema]) -> OptionalField[T_co]: ...

    @overload
    def __get__(
        self, instance: _SchemaInstance, owner: type[Schema]
    ) -> "BoundOptionalField[T_co]": ...

    def __get__(
        self, instance: _SchemaInstance | None, owner: type[Schema]
    ) -> "OptionalField[T_co] | BoundOptionalField[T_co]":
        if instance is None:
            return self
        if self.name is None:
            raise ValueError("a bound optional field has no name")
        return BoundOptionalField(instance, self)


class BoundOptionalField(Generic[T_co]):
    """A concrete optional member; call :meth:`internal` to access its value."""

    def __init__(self, instance: _SchemaInstance, field: OptionalField[T_co]) -> None:
        self.instance = instance
        self.field = field

    def internal(self) -> T_co:
        """Return the explicitly requested member wrapped by this optional."""
        if self.field.name is None:
            raise ValueError("a bound optional field has no name")
        return cast(
            T_co,
            _bind_internal_member(
                self.field.element,
                self.instance,
                self.instance.binding_path + (self.field.name,),
            ),
        )


class OptionalType:
    """Factory for optional fields, used as ``Optional[type](..., when=expr)``."""

    def __getitem__(self, element: OptionalElementInput) -> BoundOptionalType[ScalarValue]:
        return BoundOptionalType(_normalize_optional_element(element))


class BoundOptionalType(Generic[T_co]):
    def __init__(self, element: OptionalElement) -> None:
        self.element = element

    def __call__(
        self,
        constraint: FixedInput[ScalarValue] | None = None,
        *,
        when: Expr | None = None,
    ) -> OptionalField[T_co]:
        return OptionalField(self.element, constraint, when)


def _normalize_optional_element(element: OptionalElementInput) -> OptionalElement:
    if isinstance(element, ScalarType):
        return element()
    if isinstance(element, Field):
        return element
    if element is int:
        return Field[int]("int")
    if element is float:
        return DoubleType("double")()
    if element is bool:
        raise TypeError("bool is not a built-in Peach DSL field type")
    if element is str:
        return Field[str]("str")
    if element is bytes:
        return Field[bytes]("bytes")
    if isinstance(element, (_SchemaInstance, SchemaUnion, NamedUnion, ArrayField)):
        return element
    return cast(type[Schema], element)()


class BlockField:
    """A length-delimited group of named schema members."""

    def __init__(
        self,
        length: Length | None,
        fields: Mapping[str, BlockMemberInput | Override],
        schema: _SchemaInstance | None = None,
        overrides: Mapping[str, Override] | None = None,
    ) -> None:
        self._length = length
        self.schema = schema
        self.overrides = MappingProxyType(dict(overrides or {}))
        normalized: dict[str, SchemaMember] = {}
        bindings: dict[int, tuple[str, _SchemaInstance]] = {}
        for name, member in fields.items():
            child = _normalize_block_member(member)
            normalized[name] = child
            if isinstance(member, _SchemaInstance) and isinstance(
                child, _SchemaInstance
            ):
                bindings[id(member)] = (name, child)
        normalized = {
            name: _bind_schema_member_references(child, bindings)
            for name, child in normalized.items()
        }
        self.fields = MappingProxyType(normalized)
        self.owner: type[Schema] | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type[Schema], name: str) -> None:
        self.owner = owner
        self.name = name

    def __get__(
        self, instance: _SchemaInstance | None, owner: type[Schema]
    ) -> BlockField | _SchemaInstance:
        if instance is None or self.schema is None:
            return self
        if self.name is None:
            raise ValueError("a bound block field has no name")
        return self.schema.bind_to(
            instance,
            instance.binding_path + (self.name,),
        )

    def __call__(
        self,
        **overrides: Override,
    ) -> _SchemaInstance | BlockField:
        """Construct a decorated or anonymous block with nested overrides."""
        if isinstance(self.schema, _SchemaInstance):
            return self.schema(**overrides)

        unknown = overrides.keys() - self.fields.keys()
        if unknown:
            element = self.name or "anonymous Block"
            if self.owner is not None:
                element = f"{self.owner.__name__}.{element}"
            names = ", ".join(sorted(unknown))
            raise TypeError(f"{element} has no field(s): {names}")

        merged_overrides = {**self.overrides, **overrides}
        effective_fields = dict(self.fields)
        for name, override in merged_overrides.items():
            if isinstance(override, (_SchemaInstance, BlockField)):
                effective_fields[name] = override
        return BlockField(
            self._length,
            effective_fields,
            overrides=merged_overrides,
        )

    def __getattr__(self, name: str) -> object:
        if name in self.fields:
            element = self.name or "anonymous Block"
            if self.owner is not None:
                element = f"{self.owner.__name__}.{element}"
            raise AttributeError(
                f"cannot access internal field {name!r} through anonymous Block "
                f"{element!r}; use the @Block class form when internal field paths "
                "are required"
            )
        raise AttributeError(name)

    def __repr__(self) -> str:
        name = self.name or "?"
        return f"BlockField({name!r}, length={self._length!r})"


class _SchemaInstance:
    """Concrete Schema reference with scalar or nested-Schema overrides."""

    __schema_fields__: Mapping[str, SchemaMember]
    overrides: Mapping[str, Override]

    def __init__(
        self,
        **overrides: Override,
    ) -> None:
        schema = type(self)
        unknown = overrides.keys() - schema.__schema_fields__.keys()
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"{schema.__name__} has no field(s): {names}")
        self.overrides = MappingProxyType(dict(overrides))
        self._root: _SchemaInstance = self
        self._path: tuple[str, ...] = ()

    def __call__(self, **overrides: Override) -> Self:
        """Create another reference with these nested field overrides applied."""
        return type(self)(**{**self.overrides, **overrides})

    @property
    def binding_path(self) -> tuple[str, ...]:
        return self._path

    @property
    def binding_root(self) -> _SchemaInstance:
        return self._root

    def clone_unbound(self) -> Self:
        clone = object.__new__(type(self))
        clone.overrides = self.overrides
        clone._root = clone
        clone._path = ()
        return clone

    def bind_to(
        self, container: _SchemaInstance, path: tuple[str, ...]
    ) -> Self:
        clone = object.__new__(type(self))
        clone.overrides = self.overrides
        clone._root = container.binding_root
        clone._path = path
        return clone

    @overload
    def __get__(
        self,
        instance: _SchemaInstance,
        owner: type[Schema],
    ) -> Self: ...

    @overload
    def __get__(self, instance: object | None, owner: type[object]) -> Self: ...

    def __get__(
        self,
        instance: object | None,
        owner: type[object],
    ) -> Self:
        if not isinstance(instance, _SchemaInstance):
            return self
        schema_owner = cast(type[Schema], owner)
        name = next(
            (
                field_name
                for field_name, member in schema_owner.__schema_fields__.items()
                if member is self
            ),
            None,
        )
        if name is None:
            raise AttributeError("a nested Schema has no member name")
        clone = object.__new__(type(self))
        clone.overrides = self.overrides
        clone._root = instance._root
        clone._path = instance._path + (name,)
        return clone

    def __repr__(self) -> str:
        overrides = ", ".join(
            f"{key}={value!r}" for key, value in self.overrides.items()
        )
        suffix = f", {overrides}" if overrides else ""
        return f"{type(self).__name__}({suffix.removeprefix(', ')})"


class SchemaUnion:
    """A field that may contain any one of several schemas."""

    def __init__(self, schemas: tuple[type[Schema], ...]) -> None:
        if len(schemas) < 2:
            raise ValueError("a schema union needs at least two alternatives")
        self.schemas = schemas
        self.owner: type[Schema] | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type[Schema], name: str) -> None:
        self.owner = owner
        self.name = name

    def __get__(
        self, instance: _SchemaInstance | None, owner: type[Schema]
    ) -> "SchemaUnion | BoundSchemaUnion":
        if instance is None:
            return self
        if self.name is None:
            raise ValueError("a bound union has no name")
        return BoundSchemaUnion(instance, self)

    def __or__(self, other: SchemaMeta | SchemaUnion) -> SchemaUnion:
        if isinstance(other, SchemaUnion):
            return SchemaUnion(self.schemas + other.schemas)
        return SchemaUnion(self.schemas + (cast(type[Schema], other),))

    def __call__(self) -> SchemaUnion:
        """Allow both ``field = A | B`` and ``field = (A | B)()``."""
        return SchemaUnion(self.schemas)

    def __repr__(self) -> str:
        alternatives = " | ".join(schema.__name__ for schema in self.schemas)
        return f"SchemaUnion({alternatives})"


class NamedUnion:
    """A schema union whose alternatives have explicit element names."""

    def __init__(
        self,
        alternatives: Mapping[str, UnionInput],
        *,
        exposes_internal_paths: bool = False,
    ) -> None:
        if len(alternatives) < 2:
            raise ValueError("a Union needs at least two alternatives")
        self.alternatives = MappingProxyType(
            {
                name: _normalize_union_alternative(alternative)
                for name, alternative in alternatives.items()
            }
        )
        self.exposes_internal_paths = exposes_internal_paths
        self.owner: type[Schema] | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type[Schema], name: str) -> None:
        self.owner = owner
        self.name = name

    def __get__(
        self, instance: _SchemaInstance | None, owner: type[Schema]
    ) -> "NamedUnion | BoundNamedUnion":
        if instance is None:
            return self
        if self.name is None:
            raise ValueError("a bound union has no name")
        return BoundNamedUnion(instance, self)

    def __call__(self, **overrides: UnionInput) -> NamedUnion:
        """Create another union reference with alternative overrides applied.

        ``@Union`` replaces the decorated class with a ``NamedUnion`` object, so
        the result must retain the constructor-like behavior of the class it
        decorates.  This also makes decorated unions consistent with decorated
        ``@Block`` declarations and ordinary nested ``Schema`` references.
        """
        unknown = overrides.keys() - self.alternatives.keys()
        if unknown:
            element = self.name or "decorated Union"
            if self.owner is not None:
                element = f"{self.owner.__name__}.{element}"
            names = ", ".join(sorted(unknown))
            raise TypeError(f"{element} has no alternative(s): {names}")

        return NamedUnion(
            {**self.alternatives, **overrides},
            exposes_internal_paths=self.exposes_internal_paths,
        )

    def __repr__(self) -> str:
        alternatives = ", ".join(self.alternatives)
        return f"Union({alternatives})"


class BoundSchemaUnion:
    """A bound anonymous schema union without addressable internal paths."""

    def __init__(
        self,
        instance: _SchemaInstance,
        union: SchemaUnion,
        path: tuple[str, ...] | None = None,
    ) -> None:
        self.instance = instance
        self.union = union
        if path is None:
            if union.name is None:
                raise ValueError("a bound union has no name")
            path = instance.binding_path + (union.name,)
        self.path = path

    def __getattr__(self, name: str) -> _SchemaInstance:
        alternative = next(
            (schema for schema in self.union.schemas if schema.__name__ == name), None
        )
        if alternative is None:
            raise AttributeError(name)
        raise AttributeError(
            f"cannot access alternative {name!r} through an anonymous Schema "
            "union; use the @Union class form when internal field paths are required"
        )


class BoundNamedUnion:
    """A bound named union; decorated unions expose alternative paths."""

    def __init__(
        self,
        instance: _SchemaInstance,
        union: NamedUnion,
        path: tuple[str, ...] | None = None,
    ) -> None:
        self.instance = instance
        self.union = union
        if path is None:
            if union.name is None:
                raise ValueError("a bound union has no name")
            path = instance.binding_path + (union.name,)
        self.path = path

    def __getattr__(self, name: str) -> object:
        try:
            alternative = self.union.alternatives[name]
        except KeyError as error:
            raise AttributeError(name) from error
        if not self.union.exposes_internal_paths:
            raise AttributeError(
                f"cannot access alternative {name!r} through an anonymous Union; "
                "use the @Union class form when internal field paths are required"
            )
        return _bind_internal_member(
            alternative,
            self.instance,
            self.path + (name,),
        )


class UnionType:
    """Create a named union with ``Union(name=Schema, ...)``."""

    @overload
    def __call__(self, definition: type[T], /) -> T: ...

    @overload
    def __call__(self, **alternatives: UnionInput) -> NamedUnion: ...

    def __call__(
        self,
        *definitions: type[T],
        **alternatives: UnionInput,
    ) -> T | NamedUnion:
        if len(definitions) > 1:
            raise TypeError("Union accepts at most one decorated class")
        if definitions:
            if alternatives:
                raise TypeError("a decorated Union cannot also receive keyword alternatives")
            definition = definitions[0]
            alternatives = {
                name: alternative
                for name, alternative in cast(
                    Mapping[str, UnionInput], definition.__dict__
                ).items()
                if not name.startswith("_")
            }
        result = NamedUnion(
            alternatives,
            exposes_internal_paths=bool(definitions),
        )
        if definitions:
            return cast(T, result)
        return result


def _normalize_union_alternative(
    alternative: UnionInput,
) -> _SchemaInstance | AnyField | BlockField:
    if isinstance(alternative, Field):
        return alternative
    if isinstance(alternative, _SchemaInstance):
        return alternative.clone_unbound()
    if isinstance(alternative, BlockField):
        return alternative()
    return alternative()


class ArrayField:
    """A repeated scalar or schema value."""

    def __init__(self, element: ArrayElement, count: ArrayCount) -> None:
        self.element = element
        self.count = count
        self.owner: type[Schema] | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type[Schema], name: str) -> None:
        self.owner = owner
        self.name = name

    def __get__(
        self, instance: _SchemaInstance | None, owner: type[Schema]
    ) -> "ArrayField | BoundArrayField":
        if instance is None:
            return self
        if self.name is None:
            raise ValueError("a bound array field has no name")
        return BoundArrayField(instance, self)

    def __repr__(self) -> str:
        name = self.name or "?"
        return f"ArrayField({name!r}, element={self.element!r}, count={self.count!r})"


class BoundArrayField:
    """A concrete array member; call :meth:`internal` to access its item schema."""

    def __init__(self, instance: _SchemaInstance, field: ArrayField) -> None:
        self.instance = instance
        self.field = field

    def internal(self):
        """Return the explicitly requested array element member."""
        if self.field.name is None:
            raise ValueError("a bound array field has no name")
        return _bind_internal_member(
            self.field.element,
            self.instance,
            self.instance.binding_path + (self.field.name,),
        )


def _bind_schema_instance(
    source: _SchemaInstance,
    container: _SchemaInstance,
    path: tuple[str, ...],
) -> _SchemaInstance:
    return source.bind_to(container, path)


def _bind_internal_member(
    member: ArrayElement | OptionalElement | UnionInput,
    container: _SchemaInstance,
    path: tuple[str, ...],
) -> object:
    if isinstance(member, Field):
        return MemberRef(container, path, member)
    if isinstance(member, _SchemaInstance):
        return _bind_schema_instance(member, container, path)
    if isinstance(member, ArrayField):
        return BoundArrayField(container, member)
    if isinstance(member, SchemaUnion):
        return BoundSchemaUnion(container, member, path)
    if isinstance(member, NamedUnion):
        return BoundNamedUnion(container, member, path)
    raise TypeError(f"unsupported internal member: {type(member).__name__}")


class ArrayType:
    """Factory for bounded or parse-until-failure array fields."""

    def __getitem__(
        self,
        parameters: ArrayElementInput | tuple[ArrayElementInput, Length | Occurs],
    ) -> BoundArrayType:
        if isinstance(parameters, tuple):
            if len(parameters) != 2:
                raise TypeError("Array expects Array[element] or Array[element, count]")
            element, count = parameters
        else:
            element, count = parameters, None
        return BoundArrayType(_normalize_array_element(element), count)


class BoundArrayType:
    def __init__(self, element: ArrayElement, count: ArrayCount) -> None:
        self.element = element
        self.count = count

    def __call__(self) -> ArrayField:
        return ArrayField(self.element, self.count)


def _normalize_array_element(element: ArrayElementInput) -> ArrayElement:
    if isinstance(element, ScalarType):
        return element()
    if isinstance(element, BoundSizedType):
        return element()
    if isinstance(element, Field):
        return element
    if isinstance(element, (_SchemaInstance, SchemaUnion, NamedUnion, ArrayField)):
        return element
    return cast(type[Schema], element)()


class BlockType:
    """Factory for plain or length-delimited blocks."""

    @overload
    def __call__(self, schema: type[S], /) -> S: ...

    @overload
    def __call__(self, **fields: BlockMemberInput | Override) -> BlockField: ...

    def __call__(
        self,
        *schemas: type[S],
        **fields: BlockMemberInput | Override,
    ) -> S | BlockField:
        if len(schemas) > 1:
            raise TypeError("Block accepts at most one decorated Schema")
        if schemas:
            if fields:
                raise TypeError("a decorated Block cannot also receive keyword fields")
            schema = schemas[0]()
            return cast(
                S,
                BlockField(None, schemas[0].__schema_fields__, schema),
            )
        return BlockField(None, fields)

    def __getitem__(self, length: Length) -> BoundBlockType:
        return BoundBlockType(length)


class BoundBlockType:
    def __init__(self, length: Length) -> None:
        self.length = length

    @overload
    def __call__(self, schema: type[S], /) -> S: ...

    @overload
    def __call__(self, **fields: BlockMemberInput | Override) -> BlockField: ...

    def __call__(
        self,
        *schemas: type[S],
        **fields: BlockMemberInput | Override,
    ) -> S | BlockField:
        if len(schemas) > 1:
            raise TypeError("Block accepts at most one decorated Schema")
        if schemas:
            if fields:
                raise TypeError("a decorated Block cannot also receive keyword fields")
            schema = schemas[0]()
            return cast(
                S,
                BlockField(self.length, schemas[0].__schema_fields__, schema),
            )
        return BlockField(self.length, fields)


def _normalize_block_member(member: BlockMemberInput | Override) -> SchemaMember:
    if isinstance(member, ScalarType):
        return member()
    if isinstance(member, BoundSizedType):
        return member()
    if isinstance(member, _SchemaInstance):
        return member.clone_unbound()
    if isinstance(
        member,
        (Field, SchemaUnion, NamedUnion, ArrayField, OptionalField, BlockField),
    ):
        return member
    if isinstance(member, (Fixed, int, float, bool, str, bytes)):
        raise TypeError(
            "a direct Block member cannot be a scalar-only override because it "
            "has no base field type; use a Field, Schema, Block, Array, Optional, "
            "or Union value"
        )
    if isinstance(member, SchemaMeta):
        return cast(type[Schema], member)()
    raise TypeError(
        f"unsupported direct Block member type: {type(member).__name__}"
    )


SchemaMember = (
    AnyField
    | _SchemaInstance
    | SchemaUnion
    | NamedUnion
    | ArrayField
    | OptionalField[ScalarValue]
    | BlockField
)

Override = BlockField | FieldOverride[ScalarValue] | _SchemaInstance


def _bind_nested_length(
    length: Length,
    bindings: Mapping[int, tuple[str, _SchemaInstance]],
) -> Length:
    if isinstance(length, MemberRef):
        binding = bindings.get(id(length.instance)) or bindings.get(
            id(length.instance.binding_root)
        )
        if binding is None:
            return length
        name, instance = binding
        path = length.path if length.path[:1] == (name,) else (name,) + length.path
        return MemberRef(instance, path, length.definition)
    if isinstance(length, Expr):
        left = _bind_nested_expr_operand(length.left, bindings)
        right = _bind_nested_expr_operand(length.right, bindings)
        return Expr(length.operation, left, right)
    return length


def _bind_nested_expr_operand(
    operand: ExprOperand,
    bindings: Mapping[int, tuple[str, _SchemaInstance]],
) -> ExprOperand:
    if isinstance(operand, Expr):
        left = _bind_nested_expr_operand(operand.left, bindings)
        right = _bind_nested_expr_operand(operand.right, bindings)
        return Expr(operand.operation, left, right)
    if isinstance(operand, MemberRef):
        rebound = _bind_nested_length(cast(MemberRef[int], operand), bindings)
        return cast(MemberRef[int], rebound)
    return operand


def _bind_schema_member_references(
    member: SchemaMember,
    bindings: Mapping[int, tuple[str, _SchemaInstance]],
) -> SchemaMember:
    """Bind references captured inside Block, Array, and Optional declarations."""

    if isinstance(member, Field):
        length = cast(Length | None, member.options.get("length"))
        if isinstance(length, (MemberRef, Expr)):
            rebound = _bind_nested_length(length, bindings)
            if rebound is not length:
                options = dict(member.options)
                options["length"] = rebound
                replacement = Field[ScalarValue](member.kind, **options)
                replacement.owner = member.owner
                replacement.name = member.name
                return replacement
    elif isinstance(member, _SchemaInstance):
        rebound_overrides: dict[str, Override] = {}
        for name, override in member.overrides.items():
            candidate: Override = override
            if isinstance(candidate, _SchemaInstance):
                candidate = candidate.clone_unbound()
            elif isinstance(candidate, BlockField):
                candidate = candidate()
            if isinstance(candidate, (Field, _SchemaInstance, BlockField)):
                candidate = cast(
                    Override,
                    _bind_schema_member_references(
                        cast(SchemaMember, candidate), bindings
                    ),
                )
            rebound_overrides[name] = candidate
        member.overrides = MappingProxyType(rebound_overrides)
    elif isinstance(member, NamedUnion):
        member.alternatives = MappingProxyType(
            {
                name: cast(
                    _SchemaInstance | AnyField | BlockField,
                    _bind_schema_member_references(
                        cast(SchemaMember, alternative), bindings
                    ),
                )
                for name, alternative in member.alternatives.items()
            }
        )
    elif isinstance(member, BlockField):
        if member._length is not None:
            member._length = _bind_nested_length(member._length, bindings)
        member.fields = MappingProxyType(
            {
                name: _bind_schema_member_references(child, bindings)
                for name, child in member.fields.items()
            }
        )
    elif isinstance(member, OptionalField):
        if member.condition is not None:
            member.condition = cast(
                Expr,
                _bind_nested_length(member.condition, bindings),
            )
        member.element = cast(
            OptionalElement,
            _bind_schema_member_references(
                cast(SchemaMember, member.element), bindings
            ),
        )
    elif isinstance(member, ArrayField):
        if member.count is not None and not isinstance(member.count, Occurs):
            member.count = _bind_nested_length(member.count, bindings)
        member.element = cast(
            ArrayElement,
            _bind_schema_member_references(
                cast(SchemaMember, member.element), bindings
            ),
        )
    return member


class SchemaMeta(type):
    __schema_fields__: Mapping[str, SchemaMember]
    __schema_defaults__: SchemaDefaults

    def __new__(
        mcls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, object],
    ) -> SchemaMeta:
        bindings: dict[int, tuple[str, _SchemaInstance]] = {}
        for field_name, member in tuple(namespace.items()):
            if isinstance(member, _SchemaInstance):
                clone = member.clone_unbound()
                bindings[id(member)] = (field_name, clone)
                namespace[field_name] = clone

        for field_name, member in tuple(namespace.items()):
            if isinstance(
                member,
                (Field, _SchemaInstance, SchemaUnion, NamedUnion, ArrayField, OptionalField, BlockField),
            ):
                namespace[field_name] = _bind_schema_member_references(
                    cast(SchemaMember, member), bindings
                )

        cls = super().__new__(mcls, name, bases, namespace)

        fields: dict[str, SchemaMember] = {}
        for base in bases:
            fields.update(getattr(base, "__schema_fields__", {}))
        for field_name, member in namespace.items():
            if isinstance(
                member,
                (
                    _SchemaInstance,
                    SchemaUnion,
                    NamedUnion,
                    ArrayField,
                    OptionalField,
                    BlockField,
                ),
            ):
                fields[field_name] = member
            elif isinstance(member, Field):
                fields[field_name] = cast(AnyField, member)
        cls.__schema_fields__ = MappingProxyType(fields)
        cls.__schema_defaults__ = SchemaDefaults()
        return cls

    def __or__(cls, other: SchemaMeta | SchemaUnion) -> SchemaUnion:
        left = cast(type[Schema], cls)
        if isinstance(other, SchemaUnion):
            return SchemaUnion((left,) + other.schemas)
        return SchemaUnion((left, cast(type[Schema], other)))


class Schema(_SchemaInstance, metaclass=SchemaMeta):
    __schema_fields__: Mapping[str, SchemaMember]
    __flags_layout__: FlagsLayout | None = None
    __schema_defaults__: SchemaDefaults
    __packet_union__: str | None = None


def PacketUnion(schema: type[S]) -> type[S]:
    """Mark the sole Union member of a Schema as its packet union."""

    unions = tuple(
        name
        for name, member in schema.__schema_fields__.items()
        if isinstance(member, (SchemaUnion, NamedUnion))
    )
    if len(unions) != 1:
        raise TypeError(
            f"PacketUnion {schema.__name__} must contain exactly one Union member"
        )
    schema.__packet_union__ = unions[0]
    return schema


@dataclass(frozen=True, slots=True)
class FlagsLayout:
    storage: ScalarType[int]
    endian: Endian


class Flags:
    """Declare that a Schema is packed into one integer bit field."""

    def __init__(self, storage: ScalarType[int], *, endian: Endian) -> None:
        if storage.width is None:
            raise ValueError(f"{storage.name} has no declared bit width")
        if endian not in ("big", "little"):
            raise ValueError("Flags endian must be 'big' or 'little'")
        self.layout = FlagsLayout(storage, endian)

    def __call__(self, schema: type[S]) -> type[S]:
        _validate_flags_schema(schema, self.layout)
        schema.__flags_layout__ = self.layout
        return schema


class Default:
    """Set numeric defaults for a Schema and its nested Schema fields."""

    def __init__(
        self,
        *,
        endian: Endian | None = None,
        signed: bool | None = None,
    ) -> None:
        if endian not in (None, "big", "little"):
            raise ValueError("Default endian must be 'big' or 'little'")
        self.defaults = SchemaDefaults(endian=endian, signed=signed)

    def __call__(self, schema: type[S]) -> type[S]:
        schema.__schema_defaults__ = self.defaults
        return schema


def _validate_flags_schema(schema: type[Schema], layout: FlagsLayout) -> None:
    storage_width = layout.storage.width
    if storage_width is None:
        raise ValueError(f"{layout.storage.name} has no declared bit width")

    total_width = 0
    for name, member in schema.__schema_fields__.items():
        if not isinstance(member, Field) or member.kind != "bit":
            raise TypeError(f"Flags schema {schema.__name__}.{name} must be a bit field")

        width = member.options.get("length")
        if not isinstance(width, int) or width <= 0:
            raise ValueError(f"Flags field {schema.__name__}.{name} needs a positive width")
        total_width += width
        if total_width > storage_width:
            raise ValueError(
                f"Flags field {schema.__name__}.{name} exceeds {layout.storage.name}"
            )
    if total_width != storage_width:
        raise ValueError(
            f"Flags schema {schema.__name__} covers {total_width} of "
            f"{storage_width} bits in {layout.storage.name}"
        )


ArrayElement = AnyField | _SchemaInstance | SchemaUnion | NamedUnion | ArrayField
ArrayElementInput = (
    ScalarType[int]
    | ScalarType[float]
    | BoundSizedType[int]
    | BoundSizedType[str]
    | BoundSizedType[bytes]
    | ArrayElement
    | SchemaMeta
)

OptionalElement = AnyField | _SchemaInstance | SchemaUnion | NamedUnion | ArrayField
OptionalElementInput = (
    ScalarType[int]
    | ScalarType[float]
    | ScalarType[str]
    | ScalarType[bytes]
    | type[int]
    | type[float]
    | type[str]
    | type[bytes]
    | OptionalElement
    | SchemaMeta
)

BlockMemberInput = (
    ScalarType[int]
    | ScalarType[float]
    | ScalarType[str]
    | ScalarType[bytes]
    | BoundSizedType[int]
    | BoundSizedType[str]
    | BoundSizedType[bytes]
    | SchemaMember
    | SchemaMeta
)

UnionInput = type[Schema] | Schema | AnyField | BlockField


@dataclass(frozen=True, slots=True)
class FieldReference:
    """An unresolved reference to another field in the same schema."""

    name: str
    value_cast: str | None = None
    absolute: bool = dataclass_field(default=False, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ExprResult:
    """An evaluated expression that retains its operation and operands."""

    operation: str
    left: EvaluatedExprOperand
    right: EvaluatedExprOperand

    @property
    def op(self) -> str:
        return self.operation

    def __str__(self) -> str:
        return _format_expr_result(self)


EvaluatedExprOperand = ConstraintLiteral | FieldReference | ExprResult


@dataclass(frozen=True, slots=True)
class FieldResult:
    name: str
    kind: str
    fixed: Fixed[ScalarValue] | None = None
    value: ScalarValue | None = None
    length: int | FieldReference | ExprResult | None = None
    position: int | None = None
    signed: bool | None = None
    endian: Endian | None = None
    size: int | None = None
    peach_attributes: Mapping[str, str] = dataclass_field(
        default_factory=lambda: MappingProxyType({})
    )
    path: str | None = None

    def __str__(self) -> str:
        return _format_field_result(self)


@dataclass(frozen=True, slots=True)
class SchemaResult:
    name: str
    fields: Mapping[
        str,
        FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult,
    ]
    flags_layout: FlagsLayout | None = None
    length: int | FieldReference | ExprResult | None = None
    packet_union: str | None = None
    path: str | None = None

    def __str__(self) -> str:
        return format_schema_result(self)


@dataclass(frozen=True, slots=True)
class UnionResult:
    alternatives: tuple[FieldResult | SchemaResult, ...]
    path: str | None = None

    def __str__(self) -> str:
        return format_schema_result(self)


@dataclass(frozen=True, slots=True)
class ArrayResult:
    name: str
    element: FieldResult | SchemaResult | UnionResult | ArrayResult
    count: int | FieldReference | ExprResult | Occurs | None
    path: str | None = None

    def __str__(self) -> str:
        count = _format_array_count(self.count)
        return f"{self.name}: Array[{count}]"


@dataclass(frozen=True, slots=True)
class OptionalResult:
    name: str
    element: FieldResult | SchemaResult | UnionResult | ArrayResult
    condition: ExprResult
    path: str | None = None
    source_path: str | None = None

    def __str__(self) -> str:
        return f"{self.name}: Optional[present when {self.condition}]"


EvaluationResult = SchemaResult | UnionResult
SchemaInput = type[Schema] | Schema | SchemaUnion | NamedUnion


def evaluate_schema(target: SchemaInput) -> EvaluationResult:
    """Compile a schema DSL expression into an immutable result tree."""

    if isinstance(target, Schema):
        result = _evaluate_concrete_schema(
            type(target),
            target.overrides,
            ROOT_DEFAULTS,
        )
    elif isinstance(target, (SchemaUnion, NamedUnion)):
        result = _evaluate_schema_union(target, ROOT_DEFAULTS)
    else:
        result = _evaluate_concrete_schema(target, {}, ROOT_DEFAULTS)
    return _add_packet_paths(result)


ResultMember = FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult
ArrayElementResult = FieldResult | SchemaResult | UnionResult | ArrayResult


def _add_packet_paths(result: EvaluationResult) -> EvaluationResult:
    return _with_packet_path(result, None)


def _with_packet_path(
    result: EvaluationResult,
    parts: tuple[str, ...] | None,
    packet_fields: frozenset[str] | None = None,
) -> EvaluationResult:
    if isinstance(result, UnionResult):
        alternatives: list[FieldResult | SchemaResult] = []
        for alternative in result.alternatives:
            alternative_parts = (
                parts + (alternative.name,) if parts is not None else None
            )
            if isinstance(alternative, FieldResult):
                alternatives.append(
                    replace(alternative, path=_packet_path_text(alternative_parts))
                )
            else:
                alternatives.append(
                    cast(
                        SchemaResult,
                        _with_packet_path(
                            alternative,
                            alternative_parts,
                            _packet_field_paths(alternative),
                        ),
                    )
                )
        return replace(
            result,
            alternatives=tuple(alternatives),
            path=_packet_path_text(parts),
        )

    schema_parts = () if result.packet_union is not None else parts
    if packet_fields is None and schema_parts:
        packet_fields = _packet_field_paths(result)
    fields: dict[str, ResultMember] = {}
    for name, member in result.fields.items():
        child_parts = (
            schema_parts + (name,) if schema_parts is not None else None
        )
        if result.packet_union == name and isinstance(member, UnionResult):
            child_parts = ()
        fields[name] = _with_member_packet_path(member, child_parts, packet_fields)
    return replace(
        result,
        fields=MappingProxyType(fields),
        path=_packet_path_text(schema_parts),
    )


def _with_member_packet_path(
    member: ResultMember,
    parts: tuple[str, ...] | None,
    packet_fields: frozenset[str] | None,
) -> ResultMember:
    if isinstance(member, FieldResult):
        return replace(member, path=_packet_path_text(parts))
    if isinstance(member, SchemaResult):
        return _with_packet_path(member, parts, packet_fields)
    if isinstance(member, UnionResult):
        return _with_packet_path(member, parts, packet_fields)
    if isinstance(member, ArrayResult):
        return replace(
            member,
            element=_with_array_element_packet_path(
                member.element, (), packet_fields
            ),
            path=_packet_path_text(parts),
        )
    element_name = (
        _optional_element_name(parts[-1], member.element)
        if parts is not None
        else "value"
    )
    element_parts = parts + (element_name,) if parts is not None else None
    return replace(
        member,
        element=_with_array_element_packet_path(
            member.element, element_parts, packet_fields
        ),
        path=_packet_path_text(parts),
        source_path=_optional_packet_source(member.condition, parts, packet_fields),
    )


def _with_array_element_packet_path(
    element: ArrayElementResult,
    parts: tuple[str, ...] | None,
    packet_fields: frozenset[str] | None,
) -> ArrayElementResult:
    annotated = _with_member_packet_path(element, parts, packet_fields)
    return cast(ArrayElementResult, annotated)


def _optional_packet_source(
    condition: ExprResult,
    optional_parts: tuple[str, ...] | None,
    packet_fields: frozenset[str] | None,
) -> str | None:
    if optional_parts is None:
        return None

    def find(operand: EvaluatedExprOperand) -> FieldReference | None:
        if isinstance(operand, FieldReference):
            return operand
        if isinstance(operand, ExprResult):
            return find(operand.left) or find(operand.right)
        return None

    reference = find(condition)
    if reference is None:
        return None
    reference_parts = tuple(reference.name.split("."))
    packet_prefix = optional_parts[:1]
    if reference.absolute:
        return _packet_path_text(packet_prefix + reference_parts)
    if packet_fields is not None:
        reference_name = ".".join(reference_parts)
        matches = tuple(
            candidate
            for candidate in packet_fields
            if candidate == reference_name
            or candidate.endswith(f".{reference_name}")
        )
        if len(matches) == 1:
            return _packet_path_text(packet_prefix + tuple(matches[0].split(".")))
    return _packet_path_text(optional_parts[:-1] + reference_parts)


def _packet_field_paths(result: SchemaResult) -> frozenset[str]:
    paths: set[str] = set()

    def collect(schema: SchemaResult, prefix: tuple[str, ...]) -> None:
        for name, member in schema.fields.items():
            member_path = prefix + (name,)
            paths.add(".".join(member_path))
            if isinstance(member, SchemaResult):
                collect(member, member_path)

    collect(result, ())
    return frozenset(paths)


def _packet_path_text(parts: tuple[str, ...] | None) -> str | None:
    if parts is None:
        return None
    return ".".join(parts)


def format_schema_result(result: EvaluationResult) -> str:
    """Render an evaluated schema as a compact, human-readable tree."""

    lines: list[str] = []
    if isinstance(result, SchemaResult):
        lines.append(_format_schema_name(result))
        _append_schema_fields(lines, result, "")
    else:
        lines.append("one of")
        _append_union_alternatives(lines, result, "")
    return "\n".join(lines)


PeachRelation = tuple[str, str, Mapping[str, str]]
PeachRelations = dict[tuple[str, ...], list[PeachRelation]]
PEACH_NAMESPACE = "http://peachfuzzer.com/2012/Peach"
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
PEACH_SCHEMA_LOCATION = f"{PEACH_NAMESPACE} /peach/peach.xsd"
_PEACH_MODEL_NAMES: ContextVar[Mapping[type[Schema], str] | None] = ContextVar(
    "peach_model_names",
    default=None,
)


def _peach_model_name(schema: type[Schema]) -> str:
    names = _PEACH_MODEL_NAMES.get()
    return names.get(schema, schema.__name__) if names is not None else schema.__name__


def to_peach_data_model(
    target: SchemaInput,
    *,
    name: str | None = None,
    include_header: bool = True,
) -> str:
    """Convert a Schema to Peach XML, optionally wrapped as a full document."""

    # A packet union is an API boundary, not merely a Choice nested inside one
    # large model.  Keeping its alternatives as DataModels makes the generated
    # Peach file reusable, and lets a reference carry only its local overrides.
    if include_header and _schema_input_has_packet_union(target):
        return _to_peach_model_library(target, name=name)

    result = evaluate_schema(target)
    model_name = name or _schema_input_name(target)
    if include_header:
        ET.register_namespace("", PEACH_NAMESPACE)
        ET.register_namespace("xsi", XSI_NAMESPACE)
        root = ET.Element(
            f"{{{PEACH_NAMESPACE}}}Peach",
            {f"{{{XSI_NAMESPACE}}}schemaLocation": PEACH_SCHEMA_LOCATION},
        )
        data_model = ET.SubElement(root, "DataModel", {"name": model_name})
    else:
        root = ET.Element("DataModel", {"name": model_name})
        data_model = root
    relations: PeachRelations = {}
    relation_root = ("value",) if isinstance(result, UnionResult) else ()
    _collect_peach_relations(result, relation_root, relations)
    _apply_peach_result_paths(result, relation_root, relations)

    if isinstance(result, SchemaResult):
        _append_peach_schema_fields(data_model, result, (), relations)
    else:
        _append_peach_union(data_model, "value", result, (), relations)

    ET.indent(root, space="  ")
    if not include_header:
        return ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    ).decode("utf-8")


def _schema_input_name(target: SchemaInput) -> str:
    if isinstance(target, Schema):
        return type(target).__name__
    if isinstance(target, (SchemaUnion, NamedUnion)):
        return "Union"
    return target.__name__


def _schema_input_has_packet_union(target: SchemaInput) -> bool:
    if isinstance(target, Schema):
        return _schema_has_packet_union(type(target), set())
    if isinstance(target, (SchemaUnion, NamedUnion)):
        return any(
            _schema_has_packet_union(schema, set())
            for schema in _union_schema_types(target)
        )
    return _schema_has_packet_union(target, set())


def _schema_has_packet_union(
    schema: type[Schema],
    seen: set[type[Schema]],
) -> bool:
    if schema in seen:
        return False
    seen.add(schema)
    if schema.__packet_union__ is not None:
        return True
    return any(
        _member_has_packet_union(member, seen)
        for member in schema.__schema_fields__.values()
    )


def _member_has_packet_union(member: SchemaMember, seen: set[type[Schema]]) -> bool:
    if isinstance(member, _SchemaInstance):
        return _schema_has_packet_union(cast(type[Schema], type(member)), seen)
    if isinstance(member, (SchemaUnion, NamedUnion)):
        return any(
            _schema_has_packet_union(schema, seen)
            for schema in _union_schema_types(member)
        )
    if isinstance(member, (ArrayField, OptionalField)):
        return _array_element_has_packet_union(member.element, seen)
    if isinstance(member, BlockField):
        return any(
            _member_has_packet_union(child, seen) for child in member.fields.values()
        )
    return False


def _array_element_has_packet_union(
    element: ArrayElement | OptionalElement,
    seen: set[type[Schema]],
) -> bool:
    return _member_has_packet_union(cast(SchemaMember, element), seen)


def _union_schema_types(union: SchemaUnion | NamedUnion) -> tuple[type[Schema], ...]:
    if isinstance(union, SchemaUnion):
        return union.schemas
    return tuple(
        dependency
        for alternative in union.alternatives.values()
        for dependency in _peach_member_schema_types(cast(SchemaMember, alternative))
    )


def _to_peach_model_library(target: SchemaInput, *, name: str | None) -> str:
    """Write reachable schemas once and link them with Peach ``ref`` blocks."""

    if isinstance(target, (SchemaUnion, NamedUnion)):
        # There is no containing Schema to act as a model-library root.  The
        # inline form remains the useful representation for this uncommon case.
        return to_peach_data_model(target, name=name, include_header=False)

    root_schema = type(target) if isinstance(target, Schema) else target
    models: dict[type[Schema], SchemaDefaults] = {}
    dependencies: dict[type[Schema], set[type[Schema]]] = {}
    _collect_peach_models(root_schema, ROOT_DEFAULTS, models, dependencies=dependencies)
    packet_prefixes = _peach_packet_model_prefixes(root_schema)
    ordered_models = _order_peach_models(models, dependencies)
    model_names, emitted_models = _resolve_peach_model_names(
        ordered_models,
        models,
        root_schema,
        root_name=name,
    )

    ET.register_namespace("", PEACH_NAMESPACE)
    ET.register_namespace("xsi", XSI_NAMESPACE)
    root = ET.Element(
        f"{{{PEACH_NAMESPACE}}}Peach",
        {f"{{{XSI_NAMESPACE}}}schemaLocation": PEACH_SCHEMA_LOCATION},
    )
    token = _PEACH_MODEL_NAMES.set(model_names)
    try:
        for schema in emitted_models:
            inherited_defaults = models[schema]
            data_model = ET.SubElement(
                root,
                "DataModel",
                {"name": model_names[schema]},
            )
            result = _evaluate_concrete_schema(schema, {}, inherited_defaults)
            prefix = packet_prefixes.get(schema)
            result = cast(
                SchemaResult,
                _with_packet_path(result, (prefix,) if prefix is not None else None),
            )
            relations: PeachRelations = {}
            _collect_peach_relations(result, (), relations)
            _apply_peach_result_paths(result, (), relations)
            _append_extracted_schema_fields(
                data_model,
                schema.__schema_fields__,
                result,
                (),
                relations,
            )
    finally:
        _PEACH_MODEL_NAMES.reset(token)

    ET.indent(root, space="  ")
    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    ).decode("utf-8")


def _resolve_peach_model_names(
    ordered_models: tuple[type[Schema], ...],
    defaults: Mapping[type[Schema], SchemaDefaults],
    root_schema: type[Schema],
    *,
    root_name: str | None,
) -> tuple[dict[type[Schema], str], tuple[type[Schema], ...]]:
    """Keep original names, merging equal collisions and renaming unequal ones."""

    requested: dict[type[Schema], str] = {}
    for schema in ordered_models:
        requested_name = (
            root_name
            if schema is root_schema and root_name is not None
            else schema.__name__
        )
        if schema is root_schema and schema.__name__.endswith("PacketArray"):
            requested_name = _snake_case_model_name(requested_name)
        requested[schema] = requested_name
    reserved = set(requested.values())
    used: set[str] = set()
    names: dict[type[Schema], str] = {}
    emitted: list[type[Schema]] = []
    variants: dict[str, list[tuple[str, str]]] = {}

    for schema in ordered_models:
        base_name = requested[schema]
        signature = _schema_structural_signature(schema, defaults[schema])
        equivalent = next(
            (
                assigned_name
                for existing_signature, assigned_name in variants.get(base_name, [])
                if existing_signature == signature
            ),
            None,
        )
        if equivalent is not None:
            names[schema] = equivalent
            continue

        assigned_name = base_name
        if assigned_name in used:
            digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
            digest_length = 8
            assigned_name = f"{base_name}__variant_{digest[:digest_length]}"
            while assigned_name in used or assigned_name in reserved:
                digest_length += 4
                if digest_length > len(digest):
                    raise ValueError(
                        f"cannot derive a unique structural name for {base_name}"
                    )
                assigned_name = (
                    f"{base_name}__variant_{digest[:digest_length]}"
                )
        names[schema] = assigned_name
        used.add(assigned_name)
        variants.setdefault(base_name, []).append((signature, assigned_name))
        emitted.append(schema)

    return names, tuple(emitted)


def _schema_structural_signature(
    schema: type[Schema],
    inherited_defaults: SchemaDefaults,
) -> str:
    """Return the fully expanded, pre-XML structure used for Schema equality."""

    return repr(_evaluate_concrete_schema(schema, {}, inherited_defaults))


def _snake_case_model_name(name: str) -> str:
    """Convert the ROOT PacketArray model name to lower snake_case."""

    words = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words).lower()


def _collect_peach_models(
    schema: type[Schema] | _SchemaInstance,
    inherited_defaults: SchemaDefaults,
    models: dict[type[Schema], SchemaDefaults],
    seen: set[tuple[type[Schema], int]] | None = None,
    dependencies: dict[type[Schema], set[type[Schema]]] | None = None,
) -> None:
    schema_type = cast(type[Schema], type(schema) if isinstance(schema, _SchemaInstance) else schema)
    marker = (schema_type, id(schema) if isinstance(schema, _SchemaInstance) else 0)
    if seen is None:
        seen = set()
    if dependencies is None:
        dependencies = {}
    if marker in seen or schema_type.__flags_layout__ is not None:
        return
    seen.add(marker)
    models.setdefault(schema_type, inherited_defaults)
    schema_dependencies = dependencies.setdefault(schema_type, set())
    defaults = schema_type.__schema_defaults__.merge(inherited_defaults)
    overrides: Mapping[str, Override]
    if isinstance(schema, _SchemaInstance):
        overrides = schema.overrides
    else:
        overrides = {}
    for name, member in schema_type.__schema_fields__.items():
        override = overrides.get(name)
        effective_member = (
            override
            if isinstance(override, (_SchemaInstance, BlockField))
            else member
        )
        schema_dependencies.update(
            dependency
            for dependency in _peach_member_schema_types(effective_member)
            if dependency.__flags_layout__ is None
        )
        _collect_peach_member_models(
            effective_member, defaults, models, seen, dependencies
        )


def _collect_peach_member_models(
    member: SchemaMember,
    inherited_defaults: SchemaDefaults,
    models: dict[type[Schema], SchemaDefaults],
    seen: set[tuple[type[Schema], int]],
    dependencies: dict[type[Schema], set[type[Schema]]],
) -> None:
    if isinstance(member, _SchemaInstance):
        _collect_peach_models(member, inherited_defaults, models, seen, dependencies)
    elif isinstance(member, (SchemaUnion, NamedUnion)):
        if isinstance(member, NamedUnion):
            for alternative in member.alternatives.values():
                if isinstance(alternative, _SchemaInstance):
                    _collect_peach_models(
                        alternative, inherited_defaults, models, seen, dependencies
                    )
        else:
            for schema in member.schemas:
                _collect_peach_models(
                    schema, inherited_defaults, models, seen, dependencies
                )
    elif isinstance(member, (ArrayField, OptionalField)):
        _collect_peach_array_element_models(
            member.element, inherited_defaults, models, seen, dependencies
        )
    elif isinstance(member, BlockField):
        for child in member.fields.values():
            _collect_peach_member_models(
                child, inherited_defaults, models, seen, dependencies
            )


def _peach_packet_model_prefixes(root: type[Schema]) -> Mapping[type[Schema], str]:
    """Find PacketUnion alternatives and their root-relative model prefixes."""

    prefixes: dict[type[Schema], str] = {}
    seen: set[type[Schema]] = set()

    def visit(schema: type[Schema]) -> None:
        if schema in seen:
            return
        seen.add(schema)
        for name, member in schema.__schema_fields__.items():
            if schema.__packet_union__ == name and isinstance(
                member, (SchemaUnion, NamedUnion)
            ):
                if isinstance(member, NamedUnion):
                    alternatives = tuple(
                        (alternative_name, type(alternative))
                        for alternative_name, alternative in member.alternatives.items()
                        if isinstance(alternative, _SchemaInstance)
                    )
                else:
                    alternatives = tuple(
                        (alternative.__name__, alternative) for alternative in member.schemas
                    )
                for alternative_name, alternative in alternatives:
                    prefixes.setdefault(cast(type[Schema], alternative), alternative_name)
                    visit(cast(type[Schema], alternative))
            else:
                for dependency in _peach_member_schema_types(member):
                    visit(dependency)

    visit(root)
    return MappingProxyType(prefixes)


def _peach_member_schema_types(member: SchemaMember) -> tuple[type[Schema], ...]:
    if isinstance(member, _SchemaInstance):
        return (cast(type[Schema], type(member)),)
    if isinstance(member, (SchemaUnion, NamedUnion)):
        return _union_schema_types(member)
    if isinstance(member, (ArrayField, OptionalField)):
        return _peach_member_schema_types(cast(SchemaMember, member.element))
    if isinstance(member, BlockField):
        return tuple(
            dependency
            for child in member.fields.values()
            for dependency in _peach_member_schema_types(child)
        )
    return ()


def _collect_peach_array_element_models(
    element: ArrayElement | OptionalElement,
    inherited_defaults: SchemaDefaults,
    models: dict[type[Schema], SchemaDefaults],
    seen: set[tuple[type[Schema], int]],
    dependencies: dict[type[Schema], set[type[Schema]]],
) -> None:
    _collect_peach_member_models(
        cast(SchemaMember, element), inherited_defaults, models, seen, dependencies
    )


def _order_peach_models(
    models: Mapping[type[Schema], SchemaDefaults],
    dependencies: Mapping[type[Schema], set[type[Schema]]] | None = None,
) -> tuple[type[Schema], ...]:
    """Order models so Peach sees a referenced DataModel before its user."""

    ordered: list[type[Schema]] = []
    visiting: set[type[Schema]] = set()
    visited: set[type[Schema]] = set()
    model_order = {schema: index for index, schema in enumerate(models)}

    def visit(schema: type[Schema]) -> None:
        if schema in visited or schema in visiting:
            return
        visiting.add(schema)
        model_dependencies = (
            dependencies.get(schema, set())
            if dependencies is not None
            else _peach_model_dependencies(schema)
        )
        for dependency in sorted(
            model_dependencies,
            key=lambda item: model_order.get(item, len(model_order)),
        ):
            if dependency in models:
                visit(dependency)
        visiting.remove(schema)
        visited.add(schema)
        ordered.append(schema)

    for schema in models:
        visit(schema)
    return tuple(ordered)


def _peach_model_dependencies(schema: type[Schema]) -> tuple[type[Schema], ...]:
    dependencies: list[type[Schema]] = []

    def collect(member: SchemaMember) -> None:
        if isinstance(member, _SchemaInstance):
            dependency = cast(type[Schema], type(member))
            if dependency.__flags_layout__ is None:
                dependencies.append(dependency)
        elif isinstance(member, (SchemaUnion, NamedUnion)):
            dependencies.extend(_union_schema_types(member))
        elif isinstance(member, (ArrayField, OptionalField)):
            collect(cast(SchemaMember, member.element))
        elif isinstance(member, BlockField):
            for child in member.fields.values():
                collect(child)

    for member in schema.__schema_fields__.values():
        collect(member)
    return tuple(dict.fromkeys(dependencies))


def _append_extracted_schema_fields(
    parent: ET.Element,
    raw_fields: Mapping[str, SchemaMember],
    result: SchemaResult,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    for name, raw_member in raw_fields.items():
        _append_extracted_member(
            parent,
            name,
            raw_member,
            result.fields[name],
            path,
            relations,
            {},
        )


def _append_extracted_member(
    parent: ET.Element,
    name: str,
    raw_member: SchemaMember,
    result: ResultMember,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if isinstance(raw_member, Field):
        _append_peach_field(parent, name, cast(FieldResult, result), path, relations, occurrence)
    elif isinstance(raw_member, _SchemaInstance):
        _append_extracted_schema_reference(
            parent, name, raw_member, cast(SchemaResult, result), path, relations, occurrence
        )
    elif isinstance(raw_member, (SchemaUnion, NamedUnion)):
        _append_extracted_union(
            parent, name, raw_member, cast(UnionResult, result), path, relations, occurrence
        )
    elif isinstance(raw_member, ArrayField):
        _append_extracted_array(
            parent, name, raw_member, cast(ArrayResult, result), path, relations, occurrence
        )
    elif isinstance(raw_member, OptionalField):
        _append_extracted_optional(
            parent, name, raw_member, result, path, relations, occurrence
        )
    else:
        _append_extracted_block(
            parent, name, raw_member, cast(SchemaResult, result), path, relations, occurrence
        )


def _append_extracted_schema_reference(
    parent: ET.Element,
    name: str,
    raw: _SchemaInstance,
    result: SchemaResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if result.flags_layout is not None:
        _append_peach_flags(parent, name, result, path, relations, occurrence)
        return
    element = ET.SubElement(
        parent,
        "Block",
        {
            "name": name,
            "ref": _peach_model_name(cast(type[Schema], type(raw))),
            **occurrence,
        },
    )
    reference_relations = _peach_reference_relations(
        relations,
        path + (name,),
        type(raw).__schema_fields__,
        result,
    )
    override_names = set(raw.overrides)
    reference_path = path + (name,)
    for source_path in reference_relations:
        if (
            len(source_path) > len(reference_path)
            and source_path[: len(reference_path)] == reference_path
        ):
            override_names.add(source_path[len(reference_path)])
    for child_name, child_raw in type(raw).__schema_fields__.items():
        if child_name in override_names:
            override = raw.overrides.get(child_name)
            effective_raw = (
                override
                if isinstance(override, (_SchemaInstance, BlockField))
                else child_raw
            )
            _append_extracted_member(
                element,
                child_name,
                effective_raw,
                result.fields[child_name],
                reference_path,
                reference_relations,
                {},
            )


def _peach_reference_relations(
    relations: PeachRelations,
    reference_path: tuple[str, ...],
    fields: Mapping[str, SchemaMember],
    result: SchemaResult,
) -> PeachRelations:
    """Keep only relations which must override a referenced DataModel.

    A relation to a sibling of the referenced schema already lives in that
    schema's DataModel.  A relation to a parent sibling (for example
    ``header.body_length -> body``) belongs to this particular reference.
    """

    copied: PeachRelations = {}
    for source_path, entries in relations.items():
        if (
            len(source_path) <= len(reference_path)
            or source_path[: len(reference_path)] != reference_path
        ):
            continue
        external = [
            entry
            for entry in entries
            if not _peach_relation_target_is_local(entry[1], fields, result)
        ]
        if external:
            copied[source_path] = external
    return copied


def _peach_relation_target_is_local(
    target: str,
    fields: Mapping[str, SchemaMember],
    result: SchemaResult,
) -> bool:
    """Return whether a relation target is owned by the referenced DataModel."""

    if target in fields:
        return True

    def owns_path(member: ResultMember) -> bool:
        if getattr(member, "path", None) == target:
            return True
        if isinstance(member, SchemaResult):
            return any(owns_path(child) for child in member.fields.values())
        if isinstance(member, UnionResult):
            return any(owns_path(child) for child in member.alternatives)
        if isinstance(member, (ArrayResult, OptionalResult)):
            return owns_path(member.element)
        return False

    return owns_path(result)


def _append_extracted_union(
    parent: ET.Element,
    name: str,
    raw: SchemaUnion | NamedUnion,
    result: UnionResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    choice = ET.SubElement(parent, "Choice", {"name": name, **occurrence})
    if isinstance(raw, NamedUnion):
        alternatives = tuple(raw.alternatives.items())
    else:
        alternatives = tuple(
            (_peach_model_name(schema), schema()) for schema in raw.schemas
        )
    for (alternative_name, alternative_raw), alternative_result in zip(
        alternatives, result.alternatives
    ):
        if isinstance(alternative_raw, Field):
            _append_peach_field(
                choice,
                alternative_name,
                cast(FieldResult, alternative_result),
                path + (name,),
                relations,
                {},
            )
        elif isinstance(alternative_raw, _SchemaInstance):
            _append_extracted_schema_reference(
                choice,
                alternative_name,
                alternative_raw,
                cast(SchemaResult, alternative_result),
                path + (name,),
                relations,
                {},
            )
        else:
            _append_extracted_block(
                choice,
                alternative_name,
                cast(BlockField, alternative_raw),
                cast(SchemaResult, alternative_result),
                path + (name,),
                relations,
                {},
            )


def _append_extracted_array(
    parent: ET.Element,
    name: str,
    raw: ArrayField,
    result: ArrayResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    attributes = dict(occurrence)
    if result.count is None:
        attributes["minOccurs"] = "0"
        attributes["maxOccurs"] = "-1"
    elif isinstance(result.count, Occurs):
        attributes["minOccurs"] = str(result.count.min_occurs)
        attributes["maxOccurs"] = str(result.count.max_occurs)
    elif isinstance(result.count, int):
        attributes["occurs"] = str(result.count)
    _append_extracted_array_element(
        parent, name, raw.element, result.element, path, relations, attributes
    )


def _append_extracted_array_element(
    parent: ET.Element,
    name: str,
    raw: ArrayElement | OptionalElement,
    result: ArrayElementResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if isinstance(raw, Field):
        _append_peach_field(parent, name, cast(FieldResult, result), path, relations, occurrence)
    elif isinstance(raw, _SchemaInstance):
        _append_extracted_schema_reference(
            parent, name, raw, cast(SchemaResult, result), path, relations, occurrence
        )
    elif isinstance(raw, (SchemaUnion, NamedUnion)):
        _append_extracted_union(
            parent, name, raw, cast(UnionResult, result), path, relations, occurrence
        )
    else:
        _append_extracted_array(
            parent, name, raw, cast(ArrayResult, result), path, relations, occurrence
        )


def _append_extracted_optional(
    parent: ET.Element,
    name: str,
    raw: OptionalField[ScalarValue],
    result: ResultMember,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if isinstance(result, ArrayResult):
        _append_extracted_array_element(
            parent,
            name,
            raw.element,
            result.element,
            path,
            relations,
            {"minOccurs": "0", "maxOccurs": "1", **occurrence},
        )
        return
    optional = cast(OptionalResult, result)
    source, expression = _peach_optional_condition(
        optional.condition, path, optional.path, optional.source_path
    )
    attributes = {"name": name, "expression": expression, **occurrence}
    if source is not None:
        attributes["src"] = source
    element = ET.SubElement(parent, "Optional", attributes)
    element_name = _optional_element_name(name, optional.element)
    _append_extracted_array_element(
        element,
        element_name,
        raw.element,
        optional.element,
        path + (name,),
        relations,
        {},
    )


def _append_extracted_block(
    parent: ET.Element,
    name: str,
    raw: BlockField,
    result: SchemaResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    attributes = {"name": name, **occurrence}
    if isinstance(result.length, int):
        attributes["length"] = str(result.length)
    element = ET.SubElement(parent, "Block", attributes)
    _append_extracted_schema_fields(element, raw.fields, result, path + (name,), relations)


def _collect_peach_relations(
    result: EvaluationResult,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    if isinstance(result, UnionResult):
        for alternative in result.alternatives:
            alternative_path = path + (alternative.name,)
            if isinstance(alternative, FieldResult):
                _collect_peach_length_relation(
                    alternative.length,
                    path,
                    alternative.name,
                    "size",
                    relations,
                )
            else:
                _collect_peach_relations(alternative, alternative_path, relations)
        return

    for name, member in result.fields.items():
        member_path = path + (name,)
        if isinstance(member, FieldResult):
            _collect_peach_length_relation(
                member.length,
                path,
                name,
                "size",
                relations,
            )
        elif isinstance(member, SchemaResult):
            _collect_peach_length_relation(
                member.length,
                path,
                name,
                "size",
                relations,
            )
            _collect_peach_relations(member, member_path, relations)
        elif isinstance(member, UnionResult):
            _collect_peach_relations(member, member_path, relations)
        elif isinstance(member, ArrayResult):
            _collect_peach_array_relations(member, path, name, relations)
        else:
            element_name = _optional_element_name(name, member.element)
            _collect_peach_element_relations(
                member.element,
                member_path + (element_name,),
                relations,
            )


def _apply_peach_result_paths(
    result: EvaluationResult,
    root_path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    """Replace structural relation paths with PacketUnion-relative result paths."""

    paths: dict[str, str] = {}

    def collect(
        node: EvaluationResult | ResultMember,
        path: tuple[str, ...],
        record_self: bool = True,
    ) -> None:
        if isinstance(node, SchemaResult):
            for member_name, member in node.fields.items():
                collect(member, path + (member_name,))
        elif isinstance(node, UnionResult):
            for alternative in node.alternatives:
                collect(alternative, path + (alternative.name,))
        elif isinstance(node, ArrayResult):
            collect(node.element, path, False)
        elif isinstance(node, OptionalResult):
            element_name = _optional_element_name(path[-1], node.element)
            collect(node.element, path + (element_name,))
        node_path = getattr(node, "path", None)
        if record_self and isinstance(node_path, str):
            # Array elements share the array's structural Peach path. Record the
            # container last so count/size relations targeting the array keep
            # the container path, while descendants retain their reset paths.
            paths[_peach_path(path)] = node_path

    collect(result, root_path)
    for source_path, entries in tuple(relations.items()):
        relations[source_path] = [
            (relation_type, paths.get(target, target), attributes)
            for relation_type, target, attributes in entries
        ]


def _collect_peach_array_relations(
    array: ArrayResult,
    path: tuple[str, ...],
    name: str,
    relations: PeachRelations,
) -> None:
    _collect_peach_length_relation(array.count, path, name, "count", relations)
    _collect_peach_element_relations(array.element, path + (name,), relations)


def _collect_peach_element_relations(
    element: FieldResult | SchemaResult | UnionResult | ArrayResult,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    if isinstance(element, FieldResult):
        _collect_peach_length_relation(
            element.length,
            path[:-1],
            path[-1],
            "size",
            relations,
        )
    elif isinstance(element, (SchemaResult, UnionResult)):
        _collect_peach_relations(element, path, relations)
    else:
        _collect_peach_array_relations(element, path[:-1], path[-1], relations)


def _collect_peach_length_relation(
    length: int | FieldReference | ExprResult | Occurs | None,
    container_path: tuple[str, ...],
    target_name: str,
    relation_type: str,
    relations: PeachRelations,
) -> None:
    relation_attributes: Mapping[str, str] = MappingProxyType({})
    if isinstance(length, FieldReference):
        reference = length
    elif isinstance(length, ExprResult):
        reference = _peach_expr_reference(length)
        relation_attributes = MappingProxyType(
            {
                "expressionGet": _format_peach_relation_expr(length, relation_type),
                "expressionSet": _format_peach_relation_inverse(length, relation_type),
            }
        )
    else:
        return
    source_path = _resolve_peach_reference(container_path, reference)
    target_path = container_path + (target_name,)
    relations.setdefault(source_path, []).append(
        (relation_type, _peach_path(target_path), relation_attributes)
    )


def _peach_expr_reference(expr: ExprResult) -> FieldReference:
    for operand in (expr.left, expr.right):
        if isinstance(operand, FieldReference):
            return operand
        if isinstance(operand, ExprResult):
            return _peach_expr_reference(operand)
    raise ValueError("a Peach relation expression must reference a field")


def _format_peach_relation_expr(expr: ExprResult, variable: str) -> str:
    def format_operand(operand: EvaluatedExprOperand) -> str:
        if isinstance(operand, FieldReference):
            return variable
        if isinstance(operand, ExprResult):
            return _format_peach_relation_expr(operand, variable)
        return repr(operand)

    return f"({format_operand(expr.left)} {expr.operation} {format_operand(expr.right)})"


def _format_peach_relation_inverse(expr: ExprResult, variable: str) -> str:
    """Return the inverse of a one-field affine Peach relation expression."""

    field_on_left = isinstance(expr.left, FieldReference)
    field_on_right = isinstance(expr.right, FieldReference)
    if not (field_on_left ^ field_on_right):
        raise ValueError(
            "a Peach relation expression must contain one direct field reference"
        )
    constant = expr.right if field_on_left else expr.left
    if not isinstance(constant, (int, float)):
        raise ValueError("a Peach relation expression requires a numeric constant")

    operation = expr.operation
    if field_on_left:
        inverse = {"+": "-", "-": "+", "*": "/", "/": "*"}.get(operation)
        if inverse is not None:
            return f"({variable} {inverse} {constant!r})"
    elif operation == "+":
        return f"({variable} - {constant!r})"
    elif operation == "*":
        return f"({variable} / {constant!r})"
    elif operation == "-":
        return f"({constant!r} - {variable})"
    elif operation == "/":
        return f"({constant!r} / {variable})"
    raise ValueError(
        f"cannot invert Peach relation expression using {operation!r}"
    )


def _resolve_peach_reference(
    container_path: tuple[str, ...],
    reference: str | FieldReference,
) -> tuple[str, ...]:
    if isinstance(reference, FieldReference):
        parts = tuple(reference.name.split("."))
        return parts if reference.absolute else container_path + parts
    return container_path + tuple(reference.split("."))


def _peach_path(parts: tuple[str, ...]) -> str:
    """Render an unambiguous Peach path for relation attributes."""

    return ".".join(parts)


def _append_peach_schema_fields(
    parent: ET.Element,
    result: SchemaResult,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    for name, member in result.fields.items():
        _append_peach_member(parent, name, member, path, relations, {})


def _append_peach_member(
    parent: ET.Element,
    name: str,
    member: FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if isinstance(member, FieldResult):
        _append_peach_field(parent, name, member, path, relations, occurrence)
    elif isinstance(member, SchemaResult):
        _append_peach_schema(parent, name, member, path, relations, occurrence)
    elif isinstance(member, UnionResult):
        _append_peach_union(parent, name, member, path, relations, occurrence)
    elif isinstance(member, ArrayResult):
        _append_peach_array(parent, name, member, path, relations, occurrence)
    else:
        _append_peach_optional(parent, name, member, path, relations, occurrence)


def _append_peach_field(
    parent: ET.Element,
    name: str,
    field: FieldResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    attributes = {"name": name, **occurrence, **field.peach_attributes}
    tag = _peach_field_tag(field)

    if tag == "Number":
        attributes["size"] = str(_peach_number_size(field))
        if field.signed is not None:
            attributes["signed"] = _peach_bool(field.signed)
        if field.endian is not None:
            attributes["endian"] = field.endian
    elif tag == "Double":
        if field.size is None:
            raise ValueError(f"Double {name} has no size")
        attributes["size"] = str(field.size)
        if field.endian is not None:
            attributes["endian"] = field.endian
    elif tag == "String":
        attributes.setdefault("type", "utf8")

    if isinstance(field.length, int) and tag in ("Blob", "String"):
        attributes["length"] = str(field.length)

    if field.fixed is not None:
        if tag == "Blob" and isinstance(field.fixed.value, bytes):
            attributes.setdefault("valueType", "hex")
        attributes["value"] = _peach_scalar(field.fixed.value)
        attributes.setdefault("token", "true")
    elif field.value is not None:
        if tag == "Blob" and isinstance(field.value, bytes):
            attributes.setdefault("valueType", "hex")
        attributes["value"] = _peach_scalar(field.value)
        attributes.setdefault("token", "false")

    element = ET.SubElement(parent, tag, attributes)
    _append_peach_relations(element, path + (name,), relations)


def _peach_field_tag(field: FieldResult) -> str:
    if field.kind.startswith("int") or field.kind in ("int", "bit"):
        return "Number"
    if field.kind == "double":
        return "Double"
    if field.kind in ("string", "str", "decimal_string"):
        return "String"
    if field.kind in ("blob", "bytes"):
        return "Blob"
    return field.kind


def _peach_number_size(field: FieldResult) -> int:
    if field.kind == "bit":
        if not isinstance(field.length, int) or field.length <= 0:
            raise ValueError(f"Peach Bit {field.name} requires a positive integer width")
        return field.length
    kind = field.kind
    if not kind.startswith("int") or not kind[3:].isdigit():
        raise ValueError(f"Peach Number requires a fixed-width integer: {kind}")
    return int(kind[3:])


def _append_peach_schema(
    parent: ET.Element,
    name: str,
    result: SchemaResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if result.flags_layout is not None:
        _append_peach_flags(parent, name, result, path, relations, occurrence)
        return

    attributes = {"name": name, **occurrence}
    if isinstance(result.length, int):
        attributes["length"] = str(result.length)
    element = ET.SubElement(parent, "Block", attributes)
    _append_peach_schema_fields(element, result, path + (name,), relations)


def _append_peach_flags(
    parent: ET.Element,
    name: str,
    result: SchemaResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    layout = result.flags_layout
    if layout is None or layout.storage.width is None:
        raise ValueError(f"Flags {name} has no storage width")
    element = ET.SubElement(
        parent,
        "Flags",
        {
            "name": name,
            "size": str(layout.storage.width),
            "endian": layout.endian,
            **occurrence,
        },
    )
    for flag_name, member in result.fields.items():
        if not isinstance(member, FieldResult):
            raise TypeError(f"Flags member {flag_name} is not a field")
        attributes = {
            "name": flag_name,
            "size": str(member.length),
            "position": str(member.position),
        }
        if member.fixed is not None:
            attributes["value"] = _peach_scalar(member.fixed.value)
            attributes["token"] = "true"
        elif member.value is not None:
            attributes["value"] = _peach_scalar(member.value)
            attributes["token"] = "false"
        flag = ET.SubElement(element, "Flag", attributes)
        _append_peach_relations(flag, path + (name, flag_name), relations)


def _append_peach_union(
    parent: ET.Element,
    name: str,
    union: UnionResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str] | None = None,
) -> None:
    choice = ET.SubElement(parent, "Choice", {"name": name, **(occurrence or {})})
    for alternative in union.alternatives:
        if isinstance(alternative, FieldResult):
            _append_peach_field(
                choice,
                alternative.name,
                alternative,
                path + (name,),
                relations,
                {},
            )
        else:
            _append_peach_schema(
                choice,
                alternative.name,
                alternative,
                path + (name,),
                relations,
                {},
            )


def _append_peach_array(
    parent: ET.Element,
    name: str,
    array: ArrayResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    attributes = dict(occurrence)
    if array.count is None:
        attributes["minOccurs"] = "0"
        attributes["maxOccurs"] = "-1"
    elif isinstance(array.count, Occurs):
        attributes["minOccurs"] = str(array.count.min_occurs)
        attributes["maxOccurs"] = str(array.count.max_occurs)
    elif isinstance(array.count, int):
        attributes["occurs"] = str(array.count)
    _append_peach_array_element(
        parent,
        name,
        array.element,
        path,
        relations,
        attributes,
    )


def _append_peach_array_element(
    parent: ET.Element,
    name: str,
    element: FieldResult | SchemaResult | UnionResult | ArrayResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if isinstance(element, FieldResult):
        _append_peach_field(parent, name, element, path, relations, occurrence)
    elif isinstance(element, SchemaResult):
        _append_peach_schema(parent, name, element, path, relations, occurrence)
    elif isinstance(element, UnionResult):
        _append_peach_union(parent, name, element, path, relations, occurrence)
    else:
        _append_peach_array(parent, name, element, path, relations, occurrence)


def _append_peach_optional(
    parent: ET.Element,
    name: str,
    optional: OptionalResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    source, expression = _peach_optional_condition(
        optional.condition, path, optional.path, optional.source_path
    )
    attributes = {"name": name, "expression": expression, **occurrence}
    if source is not None:
        attributes["src"] = source
    element = ET.SubElement(parent, "Optional", attributes)
    element_name = _optional_element_name(name, optional.element)
    _append_peach_array_element(
        element,
        element_name,
        optional.element,
        path + (name,),
        relations,
        {},
    )


def _optional_internal_name(name: str) -> str:
    return f"{name}_internal"


def _optional_element_name(
    name: str,
    element: FieldResult | SchemaResult | UnionResult | ArrayResult,
) -> str:
    if isinstance(element, SchemaResult) and element.flags_layout is None:
        return _optional_internal_name(name)
    return "value"


def _peach_optional_condition(
    condition: ExprResult,
    container_path: tuple[str, ...],
    result_path: str | None,
    source_path: str | None,
) -> tuple[str | None, str]:
    references: list[FieldReference] = []

    def render(operand: EvaluatedExprOperand) -> str:
        if isinstance(operand, ExprResult):
            return f"({render(operand.left)} {operand.operation} {render(operand.right)})"
        if isinstance(operand, FieldReference):
            references.append(operand)
            return (
                f"{operand.value_cast}(value)"
                if operand.value_cast is not None
                else "value"
            )
        if isinstance(operand, str):
            return repr(operand)
        return str(operand)

    expression = render(condition)
    source: str | None = None
    if references:
        if source_path is not None:
            source = source_path
        elif result_path is not None:
            parent_path = result_path.rpartition(".")[0]
            source = ".".join(
                part for part in (parent_path, references[0].name) if part
            )
        else:
            source = _peach_path(_resolve_peach_reference(container_path, references[0]))
    return source, expression


def _append_peach_relations(
    element: ET.Element,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    for relation_type, target, attributes in relations.get(path, ()):
        ET.SubElement(
            element,
            "Relation",
            {"type": relation_type, "of": target, **attributes},
        )


def _peach_bool(value: bool) -> str:
    return "true" if value else "false"


def _peach_scalar(value: ScalarValue) -> str:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bool):
        return _peach_bool(value)
    return str(value)


def _peach_attribute(value: PeachAttributeValue) -> str:
    if isinstance(value, bool):
        return _peach_bool(value)
    return str(value)


def _append_schema_fields(
    lines: list[str],
    result: SchemaResult,
    prefix: str,
) -> None:
    fields = tuple(result.fields.items())
    for index, (name, member) in enumerate(fields):
        is_last = index == len(fields) - 1
        branch = "└─ " if is_last else "├─ "
        child_prefix = prefix + ("   " if is_last else "│  ")

        if isinstance(member, FieldResult):
            lines.append(prefix + branch + _format_field_result(member))
        elif isinstance(member, SchemaResult):
            lines.append(prefix + branch + f"{name}: {_format_schema_name(member)}")
            _append_schema_fields(lines, member, child_prefix)
        elif isinstance(member, UnionResult):
            lines.append(
                prefix + branch + f"{name}: one of{_format_path(member.path)}"
            )
            _append_union_alternatives(lines, member, child_prefix)
        elif isinstance(member, ArrayResult):
            count = _format_array_count(member.count)
            lines.append(
                prefix + branch + f"{name}: Array[{count}]{_format_path(member.path)}"
            )
            _append_array_element(lines, member.element, child_prefix)
        else:
            lines.append(
                prefix
                + branch
                + f"{name}: Optional[present when {member.condition}]"
                + _format_path(member.path)
            )
            _append_array_element(lines, member.element, child_prefix)


def _append_union_alternatives(
    lines: list[str],
    result: UnionResult,
    prefix: str,
) -> None:
    for index, alternative in enumerate(result.alternatives):
        is_last = index == len(result.alternatives) - 1
        branch = "└─ " if is_last else "├─ "
        child_prefix = prefix + ("   " if is_last else "│  ")
        if isinstance(alternative, FieldResult):
            lines.append(prefix + branch + _format_field_result(alternative))
        else:
            lines.append(prefix + branch + _format_schema_name(alternative))
            _append_schema_fields(lines, alternative, child_prefix)


def _append_array_element(
    lines: list[str],
    element: FieldResult | SchemaResult | UnionResult | ArrayResult,
    prefix: str,
) -> None:
    if isinstance(element, FieldResult):
        lines.append(prefix + "└─ " + _format_field_result(element))
    elif isinstance(element, SchemaResult):
        lines.append(prefix + "└─ " + _format_schema_name(element))
        _append_schema_fields(lines, element, prefix + "   ")
    elif isinstance(element, UnionResult):
        lines.append(prefix + "└─ one of" + _format_path(element.path))
        _append_union_alternatives(lines, element, prefix + "   ")
    else:
        count = _format_array_count(element.count)
        lines.append(prefix + f"└─ Array[{count}]" + _format_path(element.path))
        _append_array_element(lines, element.element, prefix + "   ")


def _format_array_count(
    count: int | FieldReference | ExprResult | Occurs | None,
) -> str:
    if count is None:
        return "unbounded"
    if isinstance(count, FieldReference):
        return f"ref({count.name})"
    if isinstance(count, ExprResult):
        return str(count)
    if isinstance(count, Occurs):
        return repr(count)
    return str(count)


def _format_expr(expr: Expr) -> str:
    return (
        f"({_format_expr_operand(expr.left)} {expr.operation} "
        f"{_format_expr_operand(expr.right)})"
    )


def _format_expr_operand(operand: ExprOperand) -> str:
    if isinstance(operand, Expr):
        return _format_expr(operand)
    if isinstance(operand, MemberRef):
        return operand.name
    if isinstance(operand, Field):
        return operand.name or "?"
    if isinstance(operand, Fixed):
        return repr(operand.value)
    return str(operand)


def _format_expr_result(expr: ExprResult) -> str:
    return (
        f"({_format_evaluated_expr_operand(expr.left)} {expr.operation} "
        f"{_format_evaluated_expr_operand(expr.right)})"
    )


def _format_evaluated_expr_operand(operand: EvaluatedExprOperand) -> str:
    if isinstance(operand, ExprResult):
        return str(operand)
    if isinstance(operand, FieldReference):
        return f"ref({operand.name})"
    if isinstance(operand, str):
        return repr(operand)
    return str(operand)


def _format_field_result(field: FieldResult) -> str:
    field_type = field.kind
    if isinstance(field.length, FieldReference):
        field_type += f"[ref({field.length.name})]"
    elif isinstance(field.length, ExprResult):
        field_type += f"[{field.length}]"
    elif field.length is not None:
        field_type += f"[{field.length}]"

    if field.position is not None:
        field_type += f" @ {field.position}"

    if field.signed is not None:
        field_type += f" signed={field.signed}"

    if field.endian is not None:
        field_type += f" endian={field.endian}"

    value = (
        f" = {field.fixed!r}"
        if field.fixed is not None
        else f" = {field.value!r}" if field.value is not None else ""
    )
    return f"{field.name}: {field_type}{value}{_format_path(field.path)}"


def _format_schema_name(result: SchemaResult) -> str:
    name = result.name
    if result.flags_layout is not None:
        layout = result.flags_layout
        name += f"<{layout.storage.name}, {layout.endian}>"
    if result.length is not None:
        name += f"[{_format_array_count(result.length)}]"
    return name + _format_path(result.path)


def _format_path(path: str | None) -> str:
    return f" [path={path}]" if path else ""


def _evaluate_concrete_schema(
    schema: type[Schema],
    overrides: Mapping[str, Override],
    inherited_defaults: SchemaDefaults,
    result_name: str | None = None,
) -> SchemaResult:
    fields: dict[
        str,
        FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult,
    ] = {}

    defaults = schema.__schema_defaults__.merge(inherited_defaults)
    for name, member in schema.__schema_fields__.items():
        override = overrides.get(name)
        if isinstance(member, Field):
            if isinstance(override, _SchemaInstance):
                fields[name] = _evaluate_concrete_schema(
                    cast(type[Schema], type(override)), override.overrides, defaults
                )
            elif isinstance(override, BlockField):
                fields[name] = _evaluate_block(override, defaults)
            else:
                fields[name] = _evaluate_field(name, member, overrides, defaults)
        elif isinstance(member, _SchemaInstance):
            if isinstance(override, BlockField):
                fields[name] = _evaluate_block(override, defaults)
            else:
                source = override if isinstance(override, _SchemaInstance) else member
                fields[name] = _evaluate_concrete_schema(
                    cast(type[Schema], type(source)),
                    source.overrides,
                    defaults,
                )
        elif isinstance(member, (SchemaUnion, NamedUnion)):
            fields[name] = _evaluate_schema_union(member, defaults)
        elif isinstance(member, ArrayField):
            fields[name] = _evaluate_array(name, member, overrides, defaults)
        elif isinstance(member, OptionalField):
            fields[name] = _evaluate_optional(name, member, overrides, defaults)
        else:
            if isinstance(override, _SchemaInstance):
                fields[name] = replace(
                    _evaluate_concrete_schema(
                        cast(type[Schema], type(override)), override.overrides, defaults
                    ),
                    length=(
                        _evaluate_length(member._length, overrides)
                        if member._length is not None
                        else None
                    ),
                )
            elif isinstance(override, BlockField):
                fields[name] = _evaluate_block(override, defaults)
            else:
                fields[name] = _evaluate_block(member, defaults)

    flags_layout = schema.__flags_layout__
    if flags_layout is not None:
        position = 0
        storage_width = flags_layout.storage.width
        if storage_width is None:
            raise ValueError(f"{flags_layout.storage.name} has no declared bit width")
        for name, member in tuple(fields.items()):
            if not isinstance(member, FieldResult) or not isinstance(member.length, int):
                raise TypeError(f"Flags member {schema.__name__}.{name} is not a fixed-width bit field")
            if position + member.length > storage_width:
                raise ValueError(
                    f"Flags field {schema.__name__}.{name} exceeds {flags_layout.storage.name}"
                )
            fields[name] = replace(member, position=position)
            position += member.length

    return SchemaResult(
        result_name or schema.__name__,
        MappingProxyType(fields),
        flags_layout,
        packet_union=schema.__packet_union__,
    )


def _evaluate_schema_union(
    union: SchemaUnion | NamedUnion,
    defaults: SchemaDefaults,
) -> UnionResult:
    if isinstance(union, SchemaUnion):
        return UnionResult(
            tuple(
                _evaluate_concrete_schema(alternative, {}, defaults)
                for alternative in union.schemas
            )
        )
    alternatives: list[FieldResult | SchemaResult] = []
    for name, alternative in union.alternatives.items():
        if isinstance(alternative, Field):
            alternatives.append(_evaluate_field(name, alternative, {}, defaults))
        elif isinstance(alternative, BlockField):
            alternatives.append(replace(_evaluate_block(alternative, defaults), name=name))
        else:
            alternatives.append(
                _evaluate_concrete_schema(
                    cast(type[Schema], type(alternative)),
                    alternative.overrides,
                    defaults,
                    name,
                )
            )
    return UnionResult(tuple(alternatives))


def _evaluate_block(
    block: BlockField,
    defaults: SchemaDefaults,
) -> SchemaResult:
    fields: dict[
        str,
        FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult,
    ] = {}
    for name, member in block.fields.items():
        if isinstance(member, Field):
            fields[name] = _evaluate_field(name, member, block.overrides, defaults)
        elif isinstance(member, _SchemaInstance):
            fields[name] = _evaluate_concrete_schema(
                cast(type[Schema], type(member)),
                member.overrides,
                defaults,
            )
        elif isinstance(member, (SchemaUnion, NamedUnion)):
            fields[name] = _evaluate_schema_union(member, defaults)
        elif isinstance(member, ArrayField):
            fields[name] = _evaluate_array(name, member, {}, defaults)
        elif isinstance(member, OptionalField):
            fields[name] = _evaluate_optional(name, member, {}, defaults)
        else:
            fields[name] = _evaluate_block(member, defaults)

    return SchemaResult(
        "Block",
        MappingProxyType(fields),
        length=(
            _evaluate_length(block._length, block.overrides)
            if block._length is not None
            else None
        ),
    )


def _evaluate_array(
    name: str,
    array: ArrayField,
    overrides: Mapping[str, Override],
    defaults: SchemaDefaults,
) -> ArrayResult:
    count = (
        array.count
        if array.count is None or isinstance(array.count, Occurs)
        else _evaluate_length(array.count, overrides)
    )
    element = array.element
    if isinstance(element, Field):
        evaluated: FieldResult | SchemaResult | UnionResult | ArrayResult = (
            _evaluate_field("item", element, {}, defaults)
        )
    elif isinstance(element, _SchemaInstance):
        evaluated = _evaluate_concrete_schema(
            cast(type[Schema], type(element)),
            element.overrides,
            defaults,
        )
    elif isinstance(element, (SchemaUnion, NamedUnion)):
        evaluated = _evaluate_schema_union(element, defaults)
    else:
        evaluated = _evaluate_array("item", element, {}, defaults)
    return ArrayResult(name, evaluated, count)


def _evaluate_optional(
    name: str,
    optional: OptionalField[ScalarValue],
    overrides: Mapping[str, Override],
    defaults: SchemaDefaults,
) -> ArrayResult | OptionalResult:
    source = optional.element
    if isinstance(source, Field):
        element: FieldResult | SchemaResult | UnionResult | ArrayResult = _evaluate_field(
            "item",
            source,
            {"item": optional.constraint} if optional.constraint is not None else {},
            defaults,
        )
    else:
        if optional.constraint is not None:
            raise TypeError("an Optional schema element cannot have a constraint")
        if isinstance(source, _SchemaInstance):
            element = _evaluate_concrete_schema(
                cast(type[Schema], type(source)),
                source.overrides,
                defaults,
            )
        elif isinstance(source, (SchemaUnion, NamedUnion)):
            element = _evaluate_schema_union(source, defaults)
        else:
            element = _evaluate_array("item", source, {}, defaults)
    if optional.condition is None:
        return ArrayResult(name, element, Occurs(0, 1))
    return OptionalResult(name, element, _evaluate_expr(optional.condition, overrides))


def _evaluate_field(
    name: str,
    original: AnyField,
    overrides: Mapping[str, Override],
    defaults: SchemaDefaults,
) -> FieldResult:
    override = overrides.get(name)
    field = override if isinstance(override, Field) else original
    constant = _field_constant(field)
    if isinstance(override, Fixed):
        constant = override
    raw_value = field.options.get("value")
    value = raw_value if isinstance(raw_value, (int, float, bool, str, bytes)) else None
    if isinstance(override, (int, float, bool, str, bytes)):
        value = override

    raw_length = field.options.get("length")
    length: int | FieldReference | ExprResult | None = None
    if raw_length is not None:
        length = _evaluate_length(cast(Length, raw_length), overrides)

    raw_signed = field.options.get("signed")
    if raw_signed is not None and not isinstance(raw_signed, bool):
        raise TypeError(f"a field signed value must be a bool: {name}")

    raw_endian = field.options.get("endian")
    if raw_endian is not None and raw_endian not in ("big", "little"):
        raise TypeError(f"a field endian must be 'big' or 'little': {name}")

    integer = field.options.get("integer") is True or field.kind.startswith("int")
    floating = field.options.get("floating") is True or field.kind == "double"
    signed = raw_signed if integer else None
    endian = raw_endian if integer or floating else None
    if integer:
        signed = defaults.signed if signed is None else signed
    if integer or floating:
        endian = defaults.endian if endian is None else endian

    raw_size = field.options.get("size")
    if raw_size is not None and not isinstance(raw_size, int):
        raise TypeError(f"a field size must be an int: {name}")

    return FieldResult(
        name,
        field.kind,
        constant,
        value,
        length,
        None,
        signed,
        endian,
        raw_size,
        _evaluate_peach_field_attributes(field),
    )


def _evaluate_peach_field_attributes(field: AnyField) -> Mapping[str, str]:
    attributes: dict[str, str] = {}
    string_options = {
        "peach_constraint": "constraint",
        "field_id": "fieldId",
        "value_type": "valueType",
        "length_type": "lengthType",
        "type": "type",
        "pad_character": "padCharacter",
    }
    bool_options = {
        "mutable": "mutable",
        "token": "token",
        "null_terminated": "nullTerminated",
    }
    for option, attribute in string_options.items():
        value = field.options.get(option)
        if value is not None:
            if not isinstance(value, str):
                raise TypeError(f"field option {option} must be a str")
            attributes[attribute] = value
    for option, attribute in bool_options.items():
        value = field.options.get(option)
        if value is not None:
            if not isinstance(value, bool):
                raise TypeError(f"field option {option} must be a bool")
            attributes[attribute] = _peach_bool(value)
    raw_extended = field.options.get("extended_attributes")
    if raw_extended is not None:
        if not isinstance(raw_extended, Mapping):
            raise TypeError("extended attributes must be a mapping")
        extended_attributes = cast(Mapping[str, PeachAttributeValue], raw_extended)
        for attribute, value in extended_attributes.items():
            if attribute == "name":
                raise ValueError("'name' is reserved for the Schema member name")
            attributes[attribute] = _peach_attribute(value)
    return MappingProxyType(attributes)


def _field_constant(field: AnyField) -> Fixed[ScalarValue] | None:
    constant = field.options.get("constant")
    if constant is None:
        return None
    if not isinstance(constant, Fixed):
        raise TypeError(f"invalid constant for field {field.name!r}")
    return cast(Fixed[ScalarValue], constant)


def _evaluate_length(
    length: Length,
    overrides: Mapping[str, Override],
) -> int | FieldReference | ExprResult:
    if isinstance(length, int):
        return length
    if isinstance(length, Fixed):
        return length.value
    if isinstance(length, Expr):
        return _evaluate_expr(length, overrides)
    if isinstance(length, MemberRef):
        result = _evaluate_member_ref(length)
        if not isinstance(result, (int, FieldReference)):
            raise TypeError("a field length must be an int")
        return result

    referenced = length
    if referenced.name is None:
        raise ValueError("the referenced length field has no name")

    override = overrides.get(referenced.name)
    if isinstance(override, Fixed):
        if not isinstance(override.value, int):
            raise TypeError("a field length must be an int")
        return override.value
    if isinstance(override, Field):
        constant = _field_constant(override)
        relation_field = override
    else:
        constant = _field_constant(referenced)
        relation_field = referenced
    if constant is not None:
        if not isinstance(constant.value, int):
            raise TypeError("a field length must be an int")
        return constant.value
    return FieldReference(referenced.name, _field_value_cast(relation_field))


def _evaluate_expr(
    expr: Expr,
    overrides: Mapping[str, Override],
) -> ExprResult:
    return ExprResult(
        expr.operation,
        _evaluate_expr_operand(expr.left, overrides),
        _evaluate_expr_operand(expr.right, overrides),
    )


def _evaluate_expr_operand(
    operand: ExprOperand,
    overrides: Mapping[str, Override],
) -> EvaluatedExprOperand:
    if isinstance(operand, Expr):
        return _evaluate_expr(operand, overrides)
    if isinstance(operand, MemberRef):
        return _evaluate_member_ref(operand)
    if isinstance(operand, Field):
        return _evaluate_expr_field(operand, overrides)
    if isinstance(operand, Fixed):
        return operand.value
    if isinstance(operand, _ConstraintNode):
        raise TypeError("constraint value cannot be used as a schema expression")
    return operand


def _evaluate_expr_field(
    referenced: Field[int] | Field[str],
    overrides: Mapping[str, Override],
) -> ConstraintLiteral | FieldReference:
    if referenced.name is None:
        raise ValueError("the referenced expression field has no name")

    override = overrides.get(referenced.name)
    if isinstance(override, Fixed):
        value = override.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    field = override if isinstance(override, Field) else referenced
    constant = _field_constant(field)
    if constant is not None:
        value = constant.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    return FieldReference(referenced.name, _field_value_cast(field))


def _evaluate_member_ref(
    member: MemberRef[ScalarValue],
) -> ConstraintLiteral | FieldReference:
    override = member.instance.overrides.get(member.path[-1])
    if isinstance(override, Fixed):
        value = override.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    field = override if isinstance(override, Field) else member.definition
    constant = _field_constant(field)
    if constant is not None:
        value = constant.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    return FieldReference(
        member.name,
        _field_value_cast(field),
        absolute=True,
    )


def _field_value_cast(field: AnyField) -> str | None:
    """Return the Peach expression cast required by a logical field value."""

    return "int" if field.kind == "decimal_string" else None


Int4 = IntegerType("int4", 4)
Int8 = IntegerType("int8", 8)
Int16 = IntegerType("int16", 16)
Int32 = IntegerType("int32", 32)
Int64 = IntegerType("int64", 64)
Double = DoubleType("double")
Bit = SizedType[int]("bit")
String = StringType("string")
DecimalString = DecimalStringType("decimal_string")
Blob = SizedType[bytes]("blob")

Array = ArrayType()
Optional = OptionalType()
Block = BlockType()
Union = UnionType()
