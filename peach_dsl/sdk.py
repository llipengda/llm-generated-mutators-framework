# pyright: strict
from __future__ import annotations

import ast
import copy
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field as dataclass_field, replace
import dis
from functools import wraps
import hashlib
import inspect
from ipaddress import IPv4Address, IPv6Address
import re
import textwrap
from types import CodeType, MappingProxyType
from typing import Callable, Generic, Literal, Self, TypeVar, cast, get_args, overload
from xml.etree import ElementTree as ET


T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
A = TypeVar("A")
B = TypeVar("B")
C = TypeVar("C")
D = TypeVar("D")
R = TypeVar("R")
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


class ComputedArgument(Generic[T_co]):
    """Marker for a symbolic DSL value whose logical Python type is ``T_co``."""


@dataclass(frozen=True, slots=True, repr=False)
class Computed(Generic[T_co]):
    """A captured computed-field declaration; its Python function is not run."""

    func: Callable[..., T_co]
    args: tuple[object, ...]
    kwargs: Mapping[str, object]

    def __repr__(self) -> str:
        try:
            source = textwrap.dedent(inspect.getsource(self.func))
        except (OSError, TypeError):
            source = self.func.__qualname__
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
        return (
            f"Computed({self.func.__qualname__}:{digest}, args={self.args!r}, "
            f"kwargs={dict(self.kwargs)!r})"
        )


@dataclass(frozen=True, slots=True)
class ComputedReference(ComputedArgument[T_co], Generic[T_co]):
    """A computed argument bound to a path inside its declaring Schema."""

    path: tuple[str, ...]
    _detached: bool = False
    _scope_up: int = 0
    _scope_token: object | None = dataclass_field(default=None, repr=False, compare=False)


ComputedParameter = T | ComputedArgument[T]


@overload
def compute(
    func: Callable[[A], R],
) -> Callable[[ComputedParameter[A]], Computed[R]]: ...


@overload
def compute(
    func: Callable[[A, B], R],
) -> Callable[[ComputedParameter[A], ComputedParameter[B]], Computed[R]]: ...


@overload
def compute(
    func: Callable[[A, B, C], R],
) -> Callable[
    [ComputedParameter[A], ComputedParameter[B], ComputedParameter[C]],
    Computed[R],
]: ...


@overload
def compute(
    func: Callable[[A, B, C, D], R],
) -> Callable[
    [
        ComputedParameter[A],
        ComputedParameter[B],
        ComputedParameter[C],
        ComputedParameter[D],
    ],
    Computed[R],
]: ...


def compute(func: Callable[..., R]) -> Callable[..., Computed[R]]:
    """Capture calls to a one-to-four-argument function as DSL metadata."""

    signature = inspect.signature(func)
    parameters = tuple(signature.parameters.values())
    if not 1 <= len(parameters) <= 4:
        raise TypeError("a computed function must declare between 1 and 4 parameters")
    unsupported = tuple(
        parameter.name
        for parameter in parameters
        if parameter.kind
        not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        or parameter.default is not inspect.Parameter.empty
    )
    if unsupported:
        names = ", ".join(unsupported)
        raise TypeError(
            "computed functions require positional parameters without defaults; "
            f"unsupported parameter(s): {names}"
        )

    @wraps(func)
    def inner(*args: object, **kwargs: object) -> Computed[R]:
        bound = signature.bind(*args, **kwargs)
        return Computed(
            func,
            tuple(bound.args),
            MappingProxyType(dict(bound.kwargs)),
        )

    return inner


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


class Field(ComputedArgument[T_co], Generic[T_co]):
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
class MemberRef(ComputedArgument[T_co], Generic[T_co]):
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
    if isinstance(constraint, str):
        if value_cast is None:
            return constraint
        try:
            expression = ast.parse(constraint, mode="eval").body
        except SyntaxError as error:
            raise TypeError("constraint must be a valid Python expression") from error
        parameter = "value"
    else:
        parameter, expression = _constraint_source_expression(constraint)
    rewriter = _ConstraintValueRewriter(parameter, value_cast, expression)
    rewritten = rewriter.visit(expression)
    if rewriter.field_name != "value":
        # Resolve conversion names in the outer scope, but evaluate only when
        # the field is read so short-circuited branches stay unevaluated.
        rewritten = ast.Call(
            func=ast.Lambda(
                args=ast.arguments(
                    posonlyargs=[], args=[ast.arg(arg=rewriter.field_name)],
                    kwonlyargs=[], kw_defaults=[], defaults=[],
                ),
                body=rewritten,
            ),
            args=[ast.Lambda(
                args=ast.arguments(
                    posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[],
                ),
                body=rewriter.input_value,
            )], keywords=[],
        )
    ast.fix_missing_locations(rewritten)
    return ast.unparse(rewritten)


def _constraint_source_expression(constraint: ConstraintBuilder[T]) -> tuple[str, ast.expr]:
    """Compile a constraint lambda without evaluating its body."""

    try:
        lines, _ = inspect.findsource(constraint)
        source = "".join(lines)
        tree = ast.parse(source)
    except (OSError, TypeError, IndentationError, SyntaxError) as error:
        raise TypeError(
            "constraint lambda source is unavailable; use a string constraint instead"
        ) from error

    code = constraint.__code__
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Lambda) and node.lineno == code.co_firstlineno
    ]
    if code.co_name != "<lambda>" or not candidates:
        raise TypeError("could not locate the constraint lambda in its source")
    if len(candidates) > 1:
        # A line number alone cannot distinguish adjacent or nested lambdas.
        # Ignore synthetic instructions without a meaningful source span.
        spans = [
            ((line, column), (end_line, end_column))
            for line, end_line, column, end_column in code.co_positions()
            if line is not None and end_line is not None
            and column is not None and end_column is not None
            and (line, column) < (end_line, end_column)
        ]
        candidates = [
            node for node in candidates
            if spans and all(
                (node.body.lineno, node.body.col_offset) <= start
                and node.body.end_lineno is not None and node.body.end_col_offset is not None
                and end <= (node.body.end_lineno, node.body.end_col_offset)
                for start, end in spans
            )
        ]
        # An inner lambda's body is also inside its enclosing lambda. Choose
        # the innermost matching body using its source start position.
        candidates.sort(key=lambda node: (node.body.lineno, node.body.col_offset), reverse=True)
        if not candidates:
            raise TypeError("cannot distinguish constraint lambdas in source; put each on a separate line")
    lambda_node = candidates[0]
    arguments = lambda_node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    if (
        len(positional) != 1 or arguments.kwonlyargs
        or arguments.vararg is not None or arguments.kwarg is not None
    ):
        raise TypeError("a constraint lambda must have exactly one positional parameter")

    names = set(_computed_global_names(code))
    captures = {
        name: constraint.__globals__[name]
        for name in names if name in constraint.__globals__
    }
    captures.update(inspect.getclosurevars(constraint).nonlocals)
    expression = lambda_node.body
    if captures:
        captured_names = sorted(captures)
        expression = ast.Call(
            func=ast.Lambda(
                args=ast.arguments(
                    posonlyargs=[], args=[ast.arg(arg=name) for name in captured_names],
                    kwonlyargs=[], kw_defaults=[], defaults=[],
                ),
                body=expression,
            ),
            args=[_capture_literal(captures[name], name, context="constraint") for name in captured_names],
            keywords=[],
        )
    return positional[0].arg, expression


class _ConstraintValueRewriter(ast.NodeTransformer):
    """Convert only the outer field value, preserving nested lexical scopes."""

    def __init__(self, parameter: str, value_cast: str | None, expression: ast.expr) -> None:
        self.parameter = parameter
        self.value_cast = value_cast
        self.input_value = self._cast_value(ast.Name(id="value", ctx=ast.Load()), value_cast)
        self.scopes: list[set[str]] = []
        reserved = {
            node.id for node in ast.walk(expression) if isinstance(node, ast.Name)
        } | {node.arg for node in ast.walk(expression) if isinstance(node, ast.arg)}
        self.field_name = "value"
        if any(
            isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
            for node in ast.walk(expression)
        ):
            self.field_name = "_peach_constraint_value"
            while self.field_name in reserved:
                self.field_name = "_" + self.field_name
            self.value_cast = None

    def visit_Lambda(self, node: ast.Lambda) -> ast.Lambda:
        node.args.defaults = [self.visit(value) for value in node.args.defaults]
        node.args.kw_defaults = [
            self.visit(value) if value is not None else None for value in node.args.kw_defaults
        ]
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        arguments += [arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None]
        self.scopes.append({arg.arg for arg in arguments})
        node.body = self.visit(node.body)
        self.scopes.pop()
        return node

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> ast.expr:
        # Only the first iterable executes in the enclosing scope. Every
        # target belongs to the comprehension's scope, including later ones.
        node.generators[0].iter = self.visit(node.generators[0].iter)
        names = {
            target.id for generator in node.generators
            for target in ast.walk(generator.target)
            if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
        }
        self.scopes.append(names)
        for index, generator in enumerate(node.generators):
            if index:
                generator.iter = self.visit(generator.iter)
            generator.target = self.visit(generator.target)
            generator.ifs = [self.visit(condition) for condition in generator.ifs]
        if isinstance(node, ast.DictComp):
            node.key = self.visit(node.key)
            node.value = self.visit(node.value)
        else:
            node.elt = self.visit(node.elt)
        self.scopes.pop()
        return node

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    def visit_Name(self, node: ast.Name) -> ast.expr:
        for scope in reversed(self.scopes):
            if node.id in scope:
                return node
        if node.id != self.parameter or not isinstance(node.ctx, ast.Load):
            return node
        value: ast.expr = ast.copy_location(ast.Name(id=self.field_name, ctx=ast.Load()), node)
        if self.field_name != "value":
            return ast.copy_location(ast.Call(func=value, args=[], keywords=[]), node)
        return ast.copy_location(self._cast_value(value, self.value_cast), node)

    @staticmethod
    def _cast_value(value: ast.expr, value_cast: str | None) -> ast.expr:
        if value_cast is None:
            return value
        if value_cast == "decimal_int":
            # Peach strings retain NUL padding/terminators in their values.
            value = ast.Call(
                func=ast.Attribute(value=value, attr="rstrip", ctx=ast.Load()),
                args=[ast.Constant(value="\0")],
                keywords=[],
            )
            value_cast = "int"
        return ast.Call(func=ast.Name(id=value_cast, ctx=ast.Load()), args=[value], keywords=[])


def _count_expr_fields(operand: ExprOperand) -> int:
    if isinstance(operand, Expr):
        return _count_expr_fields(operand.left) + _count_expr_fields(operand.right)
    return 1 if isinstance(operand, (Field, MemberRef)) else 0


ScalarValue = int | float | str | bytes
AnyField = Field[ScalarValue]
FieldOverride = T | Fixed[T] | Field[T]
PeachAttributeValue = int | float | bool | str
ExtendedValue = TypeVar("ExtendedValue", bound=ScalarValue)
FixedInput = T | Fixed[T]
ScalarInput = T | Fixed[T] | Computed[T]


def _fixed(value: ScalarInput[T] | None) -> Fixed[T] | None:
    if isinstance(value, Fixed):
        fixed_value = cast(Fixed[T], value)
        if isinstance(fixed_value.value, bool):
            raise TypeError(
                "bool is not a supported Peach DSL scalar value; use 0 or 1"
            )
        return fixed_value
    return None


def _default_value(value: ScalarInput[T] | None) -> T | Computed[T] | None:
    if value is None or isinstance(value, Fixed):
        return None
    if isinstance(value, bool):
        raise TypeError("bool is not a supported Peach DSL scalar value; use 0 or 1")
    return value


class ScalarType(Generic[T]):
    def __init__(self, name: str, width: int | None = None) -> None:
        self.name = name
        self.width = width

    def __call__(
        self,
        value: ScalarInput[T] | None = None,
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
        value: ScalarInput[int] | None = None,
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
        value: ScalarInput[float] | None = None,
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
        value: ScalarInput[ExtendedValue] | None = None,
        /,
        **attributes: PeachAttributeValue,
    ) -> Field[ExtendedValue]:
        if get_args(getattr(self, "__orig_class__", None)) == (bool,):
            raise TypeError(
                "bool is not a supported Peach DSL scalar value type; "
                "use a numeric ExtendedType and 0/1 values"
            )
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
        if value_type.lower() not in {"string", "hex", "literal", "ipv4", "ipv6"}:
            raise ValueError(
                f"unsupported Peach value_type: {value_type!r}; "
                "expected string, hex, literal, ipv4, or ipv6"
            )
        options["value_type"] = value_type
    return options


Length = int | Field[int] | MemberRef[int] | Fixed[int] | Expr


@dataclass(frozen=True, slots=True)
class Occurs:
    """Inclusive minimum and maximum occurrence counts for a repeated field."""

    min_occurs: int
    max_occurs: int

    def __post_init__(self) -> None:
        for name, value in (("min_occurs", self.min_occurs), ("max_occurs", self.max_occurs)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, not bool or another scalar type")
        if self.min_occurs < 0:
            raise ValueError("min_occurs must be non-negative")
        if self.max_occurs < self.min_occurs:
            raise ValueError("max_occurs must be at least min_occurs")
        if self.max_occurs > 2147483647:
            raise ValueError("occurrence counts must not exceed Peach's limit of 2147483647")

    def __repr__(self) -> str:
        return f"Occurs({self.min_occurs}, {self.max_occurs})"


ArrayCount = Length | Occurs | None


class SizedType(Generic[T]):
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, value: ScalarInput[T] | None = None) -> Field[T]:
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
        value: ScalarInput[str] | None = None,
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
        value: ScalarInput[T] | None = None,
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
        value: ScalarInput[str] | None = None,
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
        value: ScalarInput[int] | None = None,
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
                constraint_value_cast="decimal_int",
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
        if pad_character != "\0" and (not _valid_xml_text(pad_character) or ord(pad_character) > 0xFFFF):
            raise ValueError("pad_character must be representable as a Peach XML character")
        options["pad_character"] = pad_character


class DecimalStringType(ScalarType[int]):
    """A decimal integer encoded as a Peach String on the wire."""

    def __getitem__(self, length: Length) -> BoundDecimalStringType:
        return BoundDecimalStringType(self.name, length)

    def __call__(
        self,
        value: ScalarInput[int] | None = None,
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
                constraint_value_cast="decimal_int",
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
    ) -> T_co: ...

    def __get__(
        self, instance: _SchemaInstance | None, owner: type[Schema]
    ) -> "OptionalField[T_co] | T_co":
        if instance is None:
            return self
        if self.name is None:
            raise ValueError("a bound optional field has no name")
        override = instance.overrides.get(self.name)
        element = override.element if isinstance(override, OptionalField) else self.element
        return cast(
            T_co,
            _bind_internal_member(
                element,
                instance,
                instance.binding_path + (self.name,),
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
        *,
        _scope_token: object | None = None,
    ) -> None:
        self._scope_token = object() if _scope_token is None else _scope_token
        self._length = length
        self.schema = schema
        self.overrides = MappingProxyType(dict(overrides or {}))
        normalized: dict[str, SchemaMember] = {}
        bindings: dict[int, tuple[str, _SchemaInstance]] = {}
        computed_bindings: dict[int, tuple[str, ...]] = {}
        for name, member in fields.items():
            child = _normalize_block_member(member)
            normalized[name] = child
            if isinstance(
                member, (ComputedArgument, _SchemaInstance, BlockField)
            ):
                computed_bindings[id(member)] = (name,)
            if isinstance(child, (ComputedArgument, _SchemaInstance, BlockField)):
                computed_bindings[id(child)] = (name,)
            if isinstance(member, _SchemaInstance) and isinstance(
                child, _SchemaInstance
            ):
                bindings[id(member)] = (name, child)
        normalized = {
            name: _bind_schema_member_references(
                child, bindings, computed_bindings, defer_unbound=True, scope_token=self._scope_token
            )
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
        override = instance.overrides.get(self.name)
        schema = override if isinstance(override, _SchemaInstance) else self.schema
        return schema.bind_to(
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
            replacement = _schema_member_override(override)
            if replacement is not None:
                effective_fields[name] = replacement
        return BlockField(
            self._length,
            effective_fields,
            overrides=merged_overrides,
            _scope_token=self._scope_token,
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

    @property
    def length_spec(self) -> Length | None:
        """Return the block bound without exposing it as a nested DSL field."""
        return self._length

    @length_spec.setter
    def length_spec(self, value: Length | None) -> None:
        self._length = value


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
        override = instance.overrides.get(name)
        source = override if isinstance(override, _SchemaInstance) else self
        return cast(Self, source.bind_to(instance, instance._path + (name,)))

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
        override = instance.overrides.get(self.name)
        union = override if isinstance(override, NamedUnion) else self
        return BoundNamedUnion(instance, union, instance.binding_path + (self.name,))

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

    def __init__(
        self,
        instance: _SchemaInstance,
        field: ArrayField,
        path: tuple[str, ...] | None = None,
    ) -> None:
        self.instance = instance
        if path is None:
            if field.name is None:
                raise ValueError("a bound array field has no name")
            path = instance.binding_path + (field.name,)
            override = instance.overrides.get(field.name)
            if isinstance(override, ArrayField):
                field = override
        self.field = field
        self.path = path

    def internal(self):
        """Return the explicitly requested array element member."""
        return _bind_internal_member(
            self.field.element,
            self.instance,
            self.path + ("item",) if isinstance(self.field.element, ArrayField) else self.path,
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
        return BoundArrayField(container, member, path)
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
    return cast(type[Schema], member)()


SchemaMember = (
    AnyField
    | _SchemaInstance
    | SchemaUnion
    | NamedUnion
    | ArrayField
    | OptionalField[object]
    | BlockField
)

Override = FieldOverride[ScalarValue] | SchemaMember


def _schema_member_override(value: Override | None) -> SchemaMember | None:
    """Return an override that replaces a member's wire structure."""

    if isinstance(
        value,
        (
            Field,
            _SchemaInstance,
            SchemaUnion,
            NamedUnion,
            ArrayField,
            OptionalField,
            BlockField,
        ),
    ):
        return value
    return None


def _effective_schema_member(
    member: SchemaMember,
    override: Override | None,
) -> SchemaMember:
    replacement = _schema_member_override(override)
    return replacement if replacement is not None else member


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
        if length.instance.binding_path:
            # Keep the field owner's overrides while rebasing its path under
            # the cloned outer member.
            instance = length.instance.bind_to(instance, path[:-1])
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


def _bind_computed_argument(
    argument: object,
    bindings: Mapping[int, tuple[str, _SchemaInstance]],
    computed_bindings: Mapping[int, tuple[str, ...]],
    scope_up: int = 0,
    defer_unbound: bool = False,
    scope_token: object | None = None,
) -> object:
    """Rebind a symbolic computed argument to the cloned schema member."""

    computed_path = computed_bindings.get(id(argument))
    if computed_path is not None:
        return ComputedReference[object](computed_path, _scope_up=scope_up, _scope_token=scope_token)
    if isinstance(argument, MemberRef):
        bound = id(argument.instance) in bindings or id(argument.instance.binding_root) in bindings
        if not bound and defer_unbound:
            return argument
        rebound = _bind_nested_length(cast(MemberRef[int], argument), bindings)
        if isinstance(rebound, MemberRef):
            return ComputedReference[object](rebound.path, _detached=not bound, _scope_up=scope_up, _scope_token=scope_token)
        return rebound
    if isinstance(argument, _SchemaInstance):
        binding = bindings.get(id(argument)) or bindings.get(
            id(argument.binding_root)
        )
        if binding is None:
            if defer_unbound:
                return argument
            if argument.binding_path:
                return ComputedReference[bytearray](argument.binding_path, _detached=True)
            return argument
        name, _instance = binding
        if not argument.binding_path:
            return ComputedReference[bytearray]((name,), _scope_up=scope_up, _scope_token=scope_token)
        path = (
            argument.binding_path
            if argument.binding_path[:1] == (name,)
            else (name,) + argument.binding_path
        )
        return ComputedReference[bytearray](path, _scope_up=scope_up, _scope_token=scope_token)
    return argument


def _bind_computed(
    value: Computed[object],
    bindings: Mapping[int, tuple[str, _SchemaInstance]],
    computed_bindings: Mapping[int, tuple[str, ...]],
    scope_up: int = 0,
    defer_unbound: bool = False,
    scope_token: object | None = None,
) -> Computed[object]:
    return Computed(
        value.func,
        tuple(
            _bind_computed_argument(argument, bindings, computed_bindings, scope_up, defer_unbound, scope_token)
            for argument in value.args
        ),
        MappingProxyType(
            {
                name: _bind_computed_argument(
                    argument, bindings, computed_bindings, scope_up, defer_unbound, scope_token
                )
                for name, argument in value.kwargs.items()
            }
        ),
    )


def _bind_schema_member_references(
    member: SchemaMember,
    bindings: Mapping[int, tuple[str, _SchemaInstance]],
    computed_bindings: Mapping[int, tuple[str, ...]],
    scope_up: int = 0,
    container_up: int = 0,
    defer_unbound: bool = False,
    scope_token: object | None = None,
) -> SchemaMember:
    """Bind references captured inside Block, Array, and Optional declarations."""

    if isinstance(member, Field):
        options = dict(member.options)
        changed = False
        length = cast(Length | None, options.get("length"))
        if isinstance(length, (MemberRef, Expr)):
            rebound = _bind_nested_length(length, bindings)
            if rebound is not length:
                options["length"] = rebound
                changed = True
        raw_value = options.get("value")
        if isinstance(raw_value, Computed):
            options["value"] = _bind_computed(
                cast(Computed[object], raw_value), bindings, computed_bindings, scope_up, defer_unbound, scope_token
            )
            changed = True
        if changed:
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
            if _schema_member_override(candidate) is not None:
                candidate = cast(
                    Override,
                    _bind_schema_member_references(
                        cast(SchemaMember, candidate), bindings, computed_bindings, scope_up + 1 + container_up,
                        defer_unbound=defer_unbound,
                        scope_token=scope_token,
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
                        cast(SchemaMember, alternative), bindings, computed_bindings,
                        scope_up, container_up + 1,
                        defer_unbound=defer_unbound,
                        scope_token=scope_token,
                    ),
                )
                for name, alternative in member.alternatives.items()
            }
        )
    elif isinstance(member, BlockField):
        if member.length_spec is not None:
            member.length_spec = _bind_nested_length(member.length_spec, bindings)
        member.fields = MappingProxyType(
            {
                name: _bind_schema_member_references(
                    child, bindings, computed_bindings, scope_up + 1 + container_up,
                    defer_unbound=defer_unbound,
                    scope_token=scope_token,
                )
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
                cast(SchemaMember, member.element), bindings, computed_bindings, scope_up, container_up,
                defer_unbound=defer_unbound,
                scope_token=scope_token,
            ),
        )
    elif isinstance(member, ArrayField):
        if member.count is not None and not isinstance(member.count, Occurs):
            member.count = _bind_nested_length(member.count, bindings)
        member.element = cast(
            ArrayElement,
            _bind_schema_member_references(
                cast(SchemaMember, member.element), bindings, computed_bindings,
                scope_up, container_up + (1 if isinstance(member.element, ArrayField) else 0),
                defer_unbound=defer_unbound,
                scope_token=scope_token,
            ),
        )
    return member


class SchemaMeta(type):
    __schema_fields__: Mapping[str, SchemaMember]
    __schema_defaults__: SchemaDefaults
    __computed_scope_token__: object

    def __new__(
        mcls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, object],
    ) -> SchemaMeta:
        scope_token = object()
        bindings: dict[int, tuple[str, _SchemaInstance]] = {}
        computed_bindings: dict[int, tuple[str, ...]] = {}
        for base in bases:
            for field_name, member in getattr(base, "__schema_fields__", {}).items():
                if isinstance(member, (ComputedArgument, _SchemaInstance, BlockField)):
                    computed_bindings[id(member)] = (field_name,)
                if isinstance(member, _SchemaInstance):
                    bindings[id(member)] = (field_name, member)
        for field_name, member in tuple(namespace.items()):
            if isinstance(member, _SchemaInstance):
                clone = member.clone_unbound()
                bindings[id(member)] = (field_name, clone)
                computed_bindings[id(member)] = (field_name,)
                namespace[field_name] = clone

        for field_name, member in tuple(namespace.items()):
            if isinstance(member, (ComputedArgument, _SchemaInstance, BlockField)):
                computed_bindings[id(cast(object, member))] = (field_name,)

        for field_name, member in tuple(namespace.items()):
            if isinstance(
                member,
                (Field, _SchemaInstance, SchemaUnion, NamedUnion, ArrayField, OptionalField, BlockField),
            ):
                namespace[field_name] = _bind_schema_member_references(
                    cast(SchemaMember, member), bindings, computed_bindings, scope_token=scope_token
                )

        cls = super().__new__(mcls, name, bases, namespace)
        cls.__computed_scope_token__ = scope_token

        fields: dict[str, SchemaMember] = {}
        for base in bases:
            fields.update(getattr(base, "__schema_fields__", {}))
        # Base field maps include inherited entries. Resolve actual declarations
        # in Python's MRO so both ordinary and diamond inheritance agree with
        # attribute access, while retaining the established field order.
        for field_name in tuple(fields):
            for base in cls.__mro__[1:]:
                if field_name in base.__dict__:
                    inherited = _schema_member_override(
                        cast(Override, base.__dict__[field_name])
                    )
                    if inherited is not None:
                        fields[field_name] = inherited
                    else:
                        del fields[field_name]
                    break
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
            else:
                fields.pop(field_name, None)
        cls.__schema_fields__ = MappingProxyType(fields)
        cls.__schema_defaults__ = getattr(cls, "__schema_defaults__", SchemaDefaults())
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

    def __call__(self, schema: type[S]) -> S:
        _validate_flags_schema(schema, self.layout)
        schema.__flags_layout__ = self.layout
        # Match @Block and @Union: a decorated nested declaration is the
        # field that its enclosing Schema should collect, rather than merely
        # a helper class left in the enclosing namespace.
        return schema()


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
        schema.__schema_defaults__ = self.defaults.merge(schema.__schema_defaults__)
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
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
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

UnionInput = type[Schema] | _SchemaInstance | AnyField | BlockField


@dataclass(frozen=True, slots=True)
class FieldReference:
    """An unresolved reference to another field in the same schema."""

    name: str
    value_cast: str | None = None
    absolute: bool = dataclass_field(default=False, compare=False, repr=False)
    definition: AnyField | None = dataclass_field(default=None, compare=False, repr=False)
    _value_kind: str | None = dataclass_field(default=None, compare=False, repr=False)
    _scope_up: int = dataclass_field(default=0, compare=False, repr=False)


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
    computed: Computed[ScalarValue] | None = None
    length: int | FieldReference | ExprResult | None = None
    position: int | None = None
    signed: bool | None = None
    endian: Endian | None = None
    size: int | None = None
    peach_attributes: Mapping[str, str] = dataclass_field(
        default_factory=lambda: MappingProxyType({})
    )
    path: str | None = None
    definition: AnyField | None = dataclass_field(default=None, compare=False, repr=False)

    def __str__(self) -> str:
        return _format_field_result(self)


class _EvaluationResultMixin:
    """Shared traversal behavior for evaluated schema tree roots."""

    __slots__ = ()

    def walk(self) -> Iterator[ResultMember]:
        """Yield this result and its descendants in depth-first pre-order."""

        yield from _walk_result_member(cast(ResultMember, self))


@dataclass(frozen=True, slots=True)
class SchemaResult(_EvaluationResultMixin):
    name: str
    fields: Mapping[
        str,
        FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult,
    ]
    flags_layout: FlagsLayout | None = None
    length: int | FieldReference | ExprResult | None = None
    packet_union: str | None = None
    path: str | None = None
    _computed_scope_tokens: tuple[object, ...] = dataclass_field(default=(), repr=False, compare=False)

    def __str__(self) -> str:
        return format_schema_result(self)


@dataclass(frozen=True, slots=True)
class UnionResult(_EvaluationResultMixin):
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
    optional: bool = False

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


def _walk_result_member(member: ResultMember) -> Iterator[ResultMember]:
    yield member
    if isinstance(member, SchemaResult):
        children = member.fields.values()
    elif isinstance(member, UnionResult):
        children = member.alternatives
    elif isinstance(member, (ArrayResult, OptionalResult)):
        children = (member.element,)
    else:
        return
    for child in children:
        yield from _walk_result_member(child)


def _add_packet_paths(result: EvaluationResult) -> EvaluationResult:
    return _with_packet_path(result, None)


def _with_packet_path(
    result: EvaluationResult,
    parts: tuple[str, ...] | None,
    packet_fields: Mapping[str, AnyField | None] | None = None,
    root_parts: tuple[str, ...] | None = None,
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
                alternative_fields = (
                    _packet_field_paths(alternative)
                    if parts == () or packet_fields is None
                    else packet_fields
                )
                alternative_root = (
                    alternative_parts
                    if parts == () or packet_fields is None
                    else root_parts
                )
                alternatives.append(
                    cast(
                        SchemaResult,
                        _with_packet_path(
                            alternative,
                            alternative_parts,
                            alternative_fields,
                            alternative_root,
                        ),
                    )
                )
        return replace(
            result,
            alternatives=tuple(alternatives),
            path=_packet_path_text(parts),
        )

    schema_parts = () if result.packet_union is not None else parts
    if packet_fields is None and schema_parts is not None:
        packet_fields = _packet_field_paths(result)
        root_parts = schema_parts
    fields: dict[str, ResultMember] = {}
    for name, member in result.fields.items():
        child_parts = (
            schema_parts + (name,) if schema_parts is not None else None
        )
        if result.packet_union == name and isinstance(member, UnionResult):
            child_parts = ()
        fields[name] = _with_member_packet_path(
            member, child_parts, packet_fields, root_parts
        )
    return replace(
        result,
        fields=MappingProxyType(fields),
        path=_packet_path_text(schema_parts),
    )


def _with_member_packet_path(
    member: ResultMember,
    parts: tuple[str, ...] | None,
    packet_fields: Mapping[str, AnyField | None] | None,
    root_parts: tuple[str, ...] | None,
) -> ResultMember:
    if isinstance(member, FieldResult):
        return replace(member, path=_packet_path_text(parts))
    if isinstance(member, SchemaResult):
        return _with_packet_path(member, parts, packet_fields, root_parts)
    if isinstance(member, UnionResult):
        return _with_packet_path(member, parts, packet_fields, root_parts)
    if isinstance(member, ArrayResult):
        # Unconditional Optional members are represented by ArrayResult during
        # evaluation, but Peach emits them as an always-present Choice with an
        # explicit empty alternative. Preserve those inline Choice paths here;
        # ordinary array element paths still restart at the element model.
        if member.optional and parts is not None:
            element_parts = (
                parts
                if isinstance(member.element, UnionResult)
                else parts + (_optional_element_name(parts[-1], member.element),)
            )
        else:
            element_parts = (
                parts + ("item",)
                if isinstance(member.element, ArrayResult) and parts is not None
                else ()
            )
        return replace(
            member,
            element=_with_array_element_packet_path(
                member.element,
                element_parts,
                packet_fields if member.optional else None,
                root_parts if member.optional else (),
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
            member.element, element_parts, packet_fields, root_parts
        ),
        path=_packet_path_text(parts),
        source_path=_optional_packet_source(
            member.condition, parts, packet_fields, root_parts
        ),
    )


def _with_array_element_packet_path(
    element: ArrayElementResult,
    parts: tuple[str, ...] | None,
    packet_fields: Mapping[str, AnyField | None] | None,
    root_parts: tuple[str, ...] | None,
) -> ArrayElementResult:
    annotated = _with_member_packet_path(element, parts, packet_fields, root_parts)
    return cast(ArrayElementResult, annotated)


def _optional_packet_source(
    condition: ExprResult,
    optional_parts: tuple[str, ...] | None,
    packet_fields: Mapping[str, AnyField | None] | None,
    root_parts: tuple[str, ...] | None,
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
    # MemberRef.absolute is relative to its declaring schema, which may be a
    # nested Block, rather than necessarily to the packet root. Resolve exact
    # paths in enclosing scopes, nearest first. A global suffix search can
    # accidentally select a field in an unrelated sibling container.
    packet_prefix = root_parts if root_parts is not None else optional_parts[:1]
    container = optional_parts[len(packet_prefix):-1]
    if reference._scope_up:
        container = container[:-reference._scope_up]
    if packet_fields is None:
        return _packet_path_text(optional_parts[:-1] + reference_parts)
    for depth in range(len(container), -1, -1):
        candidate = container[:depth] + reference_parts
        candidate_name = ".".join(candidate)
        if candidate_name in packet_fields and (
            reference.definition is None
            or packet_fields[candidate_name] is reference.definition
        ):
            return _packet_path_text(packet_prefix + candidate)
    if reference.definition is not None:
        matches = [
            name for name, definition in packet_fields.items()
            if definition is reference.definition
        ]
        if len(matches) == 1:
            return _packet_path_text(packet_prefix + tuple(matches[0].split(".")))
        if len(matches) > 1:
            raise ValueError(
                f"Optional {'.'.join(optional_parts)!r} has ambiguous condition "
                f"field {reference.name!r}: {', '.join(sorted(matches))}"
            )
    raise ValueError(
        f"Optional {'.'.join(optional_parts)!r} cannot resolve condition field "
        f"{reference.name!r} in its enclosing packet scopes"
    )


def _packet_field_paths(result: SchemaResult) -> Mapping[str, AnyField | None]:
    paths: dict[str, AnyField | None] = {}

    def collect(member: ResultMember, prefix: tuple[str, ...]) -> None:
        if isinstance(member, FieldResult):
            paths[".".join(prefix)] = member.definition
        elif isinstance(member, SchemaResult):
            for name, child in member.fields.items():
                collect(child, prefix + (name,))
        elif isinstance(member, UnionResult):
            for alternative in member.alternatives:
                collect(alternative, prefix + (alternative.name,))
        elif isinstance(member, OptionalResult):
            collect(
                member.element,
                prefix + (_optional_element_name(prefix[-1], member.element),),
            )
        elif member.optional:
            # An unconditional Optional is evaluated as an ArrayResult, but
            # its present branch keeps an inline Choice path.
            element_prefix = (
                prefix
                if isinstance(member.element, UnionResult)
                else prefix + (_optional_element_name(prefix[-1], member.element),)
            )
            collect(member.element, element_prefix)

    collect(result, ())
    return MappingProxyType(paths)


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


PeachRelation = tuple[str, str, Mapping[str, str], AnyField | None]
PeachRelations = dict[tuple[str, ...], list[PeachRelation]]
PEACH_NAMESPACE = "http://peachfuzzer.com/2012/Peach"
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
PEACH_SCHEMA_LOCATION = f"{PEACH_NAMESPACE} /peach/peach.xsd"
_PEACH_MODEL_NAMES: ContextVar[Mapping[type[Schema], str] | None] = ContextVar(
    "peach_model_names",
    default=None,
)
_PEACH_CURRENT_MODEL: ContextVar[str | None] = ContextVar(
    "peach_current_model",
    default=None,
)
_COMPUTED_PATHS: ContextVar[Mapping[tuple[str, ...], tuple[tuple[str, ...], ...]]] = ContextVar(
    "computed_paths", default=MappingProxyType({})
)

_NumericSettings = tuple[tuple[bool | None, Endian | None], ...]
_PEACH_MODEL_NUMERIC_SETTINGS: ContextVar[Mapping[str, _NumericSettings]] = ContextVar(
    "peach_model_numeric_settings", default=MappingProxyType({})
)


def _peach_numeric_settings(result: SchemaResult) -> _NumericSettings:
    return tuple(
        (member.signed, member.endian)
        for member in result.walk()
        if isinstance(member, FieldResult)
    )


@dataclass(frozen=True, slots=True)
class _DecimalFixupFormat:
    width: int | None
    terminated: bool


@dataclass(frozen=True, slots=True)
class ScriptFixupDefinition:
    class_name: str
    function: Callable[..., ScalarValue]
    argument_paths: tuple[tuple[str, ...], ...]
    ancestor_path: tuple[str, ...]
    decimal_format: _DecimalFixupFormat | None = None


@dataclass(frozen=True, slots=True)
class PeachArtifacts:
    xml: str
    python_fixup: str | None


_SCRIPT_FIXUPS: ContextVar[dict[str, ScriptFixupDefinition] | None] = ContextVar(
    "peach_script_fixups",
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
    """Convert a Schema to Peach XML."""

    return to_peach_artifacts(
        target,
        name=name,
        include_header=include_header,
    ).xml


def to_peach_artifacts(
    target: SchemaInput,
    *,
    name: str | None = None,
    include_header: bool = True,
) -> PeachArtifacts:
    """Render Peach XML and any Python ScriptFixup module it requires."""

    definitions: dict[str, ScriptFixupDefinition] = {}
    token = _SCRIPT_FIXUPS.set(definitions)
    try:
        xml = _to_peach_data_model(
            target,
            name=name,
            include_header=include_header,
        )
    finally:
        _SCRIPT_FIXUPS.reset(token)

    python_fixup = _render_python_fixups(definitions) if definitions else None
    if definitions and include_header:
        xml = _add_python_fixup_import(xml)
    return PeachArtifacts(xml, python_fixup)


def _to_peach_data_model(
    target: SchemaInput,
    *,
    name: str | None = None,
    include_header: bool = True,
) -> str:
    """Convert a Schema to Peach XML, optionally wrapped as a full document."""

    # A packet union is an API boundary, not merely a Choice nested inside one
    # large model.  Keeping its alternatives as DataModels makes the generated
    # Peach file reusable, and lets a reference carry only its local overrides.
    if (
        include_header
        and not isinstance(target, (SchemaUnion, NamedUnion))
        and _schema_input_has_packet_union(target)
    ):
        return _to_peach_model_library(target, name=name)

    result = evaluate_schema(target)
    if isinstance(result, SchemaResult) and result.flags_layout is not None:
        # A DataModel cannot itself carry a Flags layout. Keep the packed
        # value as one child, just as a top-level Union has a value child.
        result = SchemaResult(result.name, MappingProxyType({"value": result}))
    if isinstance(result, SchemaResult):
        result = _with_packet_path(result, ())
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
    computed_paths = _apply_peach_result_paths(result, relation_root, relations, model_name)

    model_token = _PEACH_CURRENT_MODEL.set(model_name)
    paths_token = _COMPUTED_PATHS.set(computed_paths)
    try:
        if isinstance(result, SchemaResult):
            _append_peach_schema_fields(data_model, result, (), relations)
        else:
            _append_peach_union(data_model, "value", result, (), relations)
    finally:
        _COMPUTED_PATHS.reset(paths_token)
        _PEACH_CURRENT_MODEL.reset(model_token)

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


def _to_peach_model_library(target: type[Schema] | Schema, *, name: str | None) -> str:
    """Write reachable schemas once and link them with Peach ``ref`` blocks."""

    root_schema = type(target) if isinstance(target, Schema) else target
    root_overrides = target.overrides if isinstance(target, Schema) else {}
    models: dict[type[Schema], SchemaDefaults] = {}
    dependencies: dict[type[Schema], set[type[Schema]]] = {}
    _collect_peach_models(target, ROOT_DEFAULTS, models, dependencies=dependencies)
    packet_prefixes = _peach_packet_model_prefixes(target)
    ordered_models = _order_peach_models(models, dependencies)
    model_names, emitted_models = _resolve_peach_model_names(
        ordered_models,
        models,
        root_schema,
        root_name=name,
        root_overrides=root_overrides,
    )

    ET.register_namespace("", PEACH_NAMESPACE)
    ET.register_namespace("xsi", XSI_NAMESPACE)
    root = ET.Element(
        f"{{{PEACH_NAMESPACE}}}Peach",
        {f"{{{XSI_NAMESPACE}}}schemaLocation": PEACH_SCHEMA_LOCATION},
    )
    token = _PEACH_MODEL_NAMES.set(model_names)
    numeric_settings: dict[str, _NumericSettings] = {}
    settings_token = _PEACH_MODEL_NUMERIC_SETTINGS.set(numeric_settings)
    try:
        for schema in emitted_models:
            inherited_defaults = models[schema]
            data_model = ET.SubElement(
                root,
                "DataModel",
                {"name": model_names[schema]},
            )
            overrides = root_overrides if schema is root_schema else {}
            result = _evaluate_concrete_schema(schema, overrides, inherited_defaults)
            numeric_settings[model_names[schema]] = _peach_numeric_settings(result)
            prefix = packet_prefixes.get(schema)
            result = cast(
                SchemaResult,
                _with_packet_path(result, (prefix,) if prefix is not None else ()),
            )
            relations: PeachRelations = {}
            _collect_peach_relations(result, (), relations)
            computed_paths = _apply_peach_result_paths(result, (), relations, model_names[schema])
            model_token = _PEACH_CURRENT_MODEL.set(model_names[schema])
            paths_token = _COMPUTED_PATHS.set(computed_paths)
            try:
                _append_extracted_schema_fields(
                    data_model,
                    schema.__schema_fields__,
                    result,
                    (),
                    relations,
                    overrides,
                )
            finally:
                _COMPUTED_PATHS.reset(paths_token)
                _PEACH_CURRENT_MODEL.reset(model_token)
    finally:
        _PEACH_MODEL_NUMERIC_SETTINGS.reset(settings_token)
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
    root_overrides: Mapping[str, Override] | None = None,
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
        signature = (
            repr(_evaluate_concrete_schema(schema, root_overrides, defaults[schema]))
            if schema is root_schema and root_overrides
            else _schema_structural_signature(schema, defaults[schema])
        )
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
        effective_member = _effective_schema_member(member, override)
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
        # The shared DataModel is emitted from the class declaration even
        # when this instance replaces its children. Its original dependencies
        # must exist before Peach can parse the later reference overrides.
        _collect_peach_models(
            cast(type[Schema], type(member)), inherited_defaults, models, seen, dependencies
        )
        _collect_peach_models(member, inherited_defaults, models, seen, dependencies)
    elif isinstance(member, (SchemaUnion, NamedUnion)):
        if isinstance(member, NamedUnion):
            for alternative in member.alternatives.values():
                _collect_peach_member_models(
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


def _peach_packet_model_prefixes(root: type[Schema] | _SchemaInstance) -> Mapping[type[Schema], str]:
    """Find PacketUnion alternatives and their root-relative model prefixes."""

    prefixes: dict[type[Schema], str] = {}
    seen: set[type[Schema]] = set()

    def visit(target: type[Schema] | _SchemaInstance) -> None:
        schema = cast(type[Schema], type(target) if isinstance(target, _SchemaInstance) else target)
        overrides = target.overrides if isinstance(target, _SchemaInstance) else {}
        if schema in seen:
            return
        seen.add(schema)
        for name, declared in schema.__schema_fields__.items():
            member = _effective_schema_member(declared, overrides.get(name))
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
    overrides: Mapping[str, Override] | None = None,
) -> None:
    for name, raw_member in raw_fields.items():
        effective_raw = _effective_schema_member(
            raw_member,
            overrides.get(name) if overrides is not None else None,
        )
        _append_extracted_member(
            parent,
            name,
            effective_raw,
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
    attributes = {
        "name": name,
        "ref": _peach_model_name(cast(type[Schema], type(raw))),
        **occurrence,
    }
    if isinstance(result.length, int):
        attributes["length"] = str(result.length)
    element = ET.SubElement(
        parent,
        "Block",
        attributes,
    )
    baseline_settings = _PEACH_MODEL_NUMERIC_SETTINGS.get().get(attributes["ref"])
    defaults_changed = (
        baseline_settings is not None
        and baseline_settings != _peach_numeric_settings(result)
    )
    if raw.overrides or defaults_changed:
        # An override can change sibling lengths, counts and conditions after
        # evaluation. Peach replaces child nodes rather than re-evaluating DSL
        # dependencies or inherited numeric defaults, so materialize the whole
        # effective instance and its local relations rather than only the
        # explicitly overridden names.
        _append_peach_schema_fields(element, result, path + (name,), relations)
        return
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
            effective_raw = _effective_schema_member(child_raw, override)
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
    _append_extracted_union_alternatives(
        choice, raw, result, path + (name,), relations
    )


def _append_extracted_union_alternatives(
    choice: ET.Element,
    raw: SchemaUnion | NamedUnion,
    result: UnionResult,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
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
                path,
                relations,
                {},
            )
        elif isinstance(alternative_raw, _SchemaInstance):
            _append_extracted_schema_reference(
                choice,
                alternative_name,
                alternative_raw,
                cast(SchemaResult, alternative_result),
                path,
                relations,
                {},
            )
        else:
            _append_extracted_block(
                choice,
                alternative_name,
                alternative_raw,
                cast(SchemaResult, alternative_result),
                path,
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
    if isinstance(result.element, ArrayResult) and isinstance(raw.element, ArrayField):
        block = ET.SubElement(parent, "Block", {"name": name, **attributes})
        _append_extracted_array(
            block, "item", raw.element, result.element, path + (name,), relations, {}
        )
        return
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
    raw: OptionalField[object],
    result: ResultMember,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str],
) -> None:
    if isinstance(result, ArrayResult):
        choice = ET.SubElement(parent, "Choice", {"name": name, **occurrence})
        if isinstance(raw.element, (SchemaUnion, NamedUnion)):
            _append_extracted_union_alternatives(
                choice,
                raw.element,
                cast(UnionResult, result.element),
                path + (name,),
                relations,
            )
        else:
            element_name = _optional_element_name(name, result.element)
            nested_relations = _nest_peach_relation_sources(
                relations, path + (name,), element_name
            )
            _append_extracted_array_element(
                choice,
                element_name,
                raw.element,
                result.element,
                path + (name,),
                nested_relations,
                {},
            )
        ET.SubElement(choice, "Block", {"name": _optional_empty_name(result.element)})
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
    nested_relations = _nest_peach_relation_sources(
        relations, path + (name,), element_name
    )
    _append_extracted_array_element(
        element,
        element_name,
        raw.element,
        optional.element,
        path + (name,),
        nested_relations,
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
    _append_extracted_schema_fields(
        element,
        raw.fields,
        result,
        path + (name,),
        relations,
        raw.overrides,
    )


def _collect_peach_relations(
    result: EvaluationResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    source_scope: tuple[str, ...] | None = None,
) -> None:
    if isinstance(result, UnionResult):
        for alternative in result.alternatives:
            alternative_path = path + (alternative.name,)
            if isinstance(alternative, FieldResult):
                _collect_peach_length_relation(
                    _peach_field_relation_length(alternative),
                    path,
                    alternative.name,
                    "size",
                    relations,
                    source_scope,
                    length_type=alternative.peach_attributes.get("lengthType"),
                )
            else:
                if isinstance(alternative, SchemaResult):
                    _collect_peach_length_relation(
                        alternative.length, path, alternative.name, "size", relations, source_scope,
                    )
                _collect_peach_relations(alternative, alternative_path, relations)
        return

    for name, member in result.fields.items():
        member_path = path + (name,)
        if isinstance(member, FieldResult):
            _collect_peach_length_relation(
                _peach_field_relation_length(member),
                path,
                name,
                "size",
                relations,
                length_type=member.peach_attributes.get("lengthType"),
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
            _collect_peach_relations(member, member_path, relations, path)
        elif isinstance(member, ArrayResult):
            _collect_peach_array_relations(member, path, name, relations)
        else:
            element_name = _optional_element_name(name, member.element)
            _collect_peach_element_relations(
                member.element,
                member_path + (element_name,),
                relations,
                path,
            )


def _apply_peach_result_paths(
    result: EvaluationResult,
    root_path: tuple[str, ...],
    relations: PeachRelations,
    model_name: str,
) -> Mapping[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Resolve relation paths and map compute references to emitted XML elements."""

    paths: dict[str, str] = {}
    field_paths: dict[tuple[str, ...], list[AnyField | None]] = {}
    packet_alternatives: list[tuple[str, ...]] = []
    node_paths: set[tuple[str, ...]] = set()
    computed_fields: list[tuple[Computed[ScalarValue], tuple[str, ...], tuple[str, ...]]] = []
    physical_paths: dict[tuple[str, ...], tuple[str, ...]] = {}
    physical_field_paths: dict[tuple[str, ...], tuple[str, ...]] = {}
    emitted_targets: dict[str, tuple[str, ...]] = {}
    emitted_nodes: set[tuple[str, ...]] = set()
    scope_tokens: dict[tuple[str, ...], tuple[object, ...]] = {}

    def collect(
        node: EvaluationResult | ResultMember,
        path: tuple[str, ...],
        record_self: bool = True,
        reference_path: tuple[str, ...] = (),
        compute_scope: tuple[str, ...] | None = None,
        emitted_path: tuple[str, ...] | None = None,
    ) -> None:
        if emitted_path is None:
            emitted_path = path
        emitted_nodes.add(emitted_path)
        node_paths.update((path, reference_path))
        if isinstance(node, SchemaResult):
            scope_tokens[reference_path] = node._computed_scope_tokens
            if node.packet_union is not None:
                union = node.fields[node.packet_union]
                if isinstance(union, UnionResult):
                    packet_alternatives.extend(
                        path + (node.packet_union, alternative.name)
                        for alternative in union.alternatives
                    )
            for member_name, member in node.fields.items():
                collect(
                    member,
                    path + (member_name,),
                    reference_path=reference_path + (member_name,),
                    compute_scope=reference_path,
                    emitted_path=emitted_path + (member_name,),
                )
        elif isinstance(node, UnionResult):
            for alternative in node.alternatives:
                collect(
                    alternative,
                    path + (alternative.name,),
                    reference_path=reference_path + (alternative.name,),
                    compute_scope=compute_scope,
                    emitted_path=emitted_path + (alternative.name,),
                )
        elif isinstance(node, ArrayResult):
            if isinstance(node.element, ArrayResult) and node.optional:
                collect(
                    node.element, path + ("value",), reference_path=reference_path,
                    compute_scope=compute_scope,
                    emitted_path=emitted_path + ("value",),
                )
            elif isinstance(node.element, ArrayResult):
                collect(
                    node.element, path + ("item",),
                    reference_path=reference_path + ("item",),
                    compute_scope=compute_scope,
                    emitted_path=emitted_path + ("item",),
                )
            else:
                element_path = emitted_path
                if node.optional and not isinstance(node.element, UnionResult):
                    # Optional adds a Choice child without changing the DSL path.
                    element_path += (_optional_element_name(path[-1], node.element),)
                collect(node.element, path, False, reference_path, compute_scope, element_path)
        elif isinstance(node, OptionalResult):
            element_name = _optional_element_name(path[-1], node.element)
            collect(
                node.element,
                path + (element_name,),
                reference_path=reference_path,
                compute_scope=compute_scope,
                emitted_path=emitted_path + (element_name,),
            )
        elif isinstance(node, FieldResult):
            physical_field_paths[reference_path] = emitted_path
            field_paths.setdefault(reference_path, []).append(node.definition)
            field_paths.setdefault(path, []).append(node.definition)
            if node.computed is not None:
                computed_fields.append((
                    node.computed, emitted_path,
                    compute_scope if compute_scope is not None else reference_path[:-1],
                ))
        if record_self:
            physical_paths[reference_path] = emitted_path
            emitted_targets[_peach_path(path)] = emitted_path
        node_path = getattr(node, "path", None)
        if record_self and isinstance(node_path, str):
            # Array elements share the array's structural Peach path. Record the
            # container last so count/size relations targeting the array keep
            # the container path, while descendants retain their reset paths.
            paths[_peach_path(path)] = node_path

    collect(result, root_path)
    # Peach searches outward from the relation source. A nested element whose
    # path ends with the target path can shadow the intended root descendant.
    qualified_targets = {
        target: _peach_path((model_name,) + emitted)
        for target, emitted in emitted_targets.items()
        if emitted and any(
            candidate != emitted and candidate[-len(emitted):] == emitted
            for candidate in emitted_nodes
        )
    }
    computed_paths: dict[tuple[str, ...], tuple[tuple[str, ...], ...]] = {}
    for computed_value, field_path, argument_scope in computed_fields:
        resolved: list[tuple[str, ...]] = []
        for argument in computed_value.args:
            argument_path = _computed_argument_path(argument, argument_scope)
            if isinstance(argument, ComputedReference) and argument._scope_token is not None:
                scope = argument_scope[:-argument._scope_up] if argument._scope_up else argument_scope
                if not any(token is argument._scope_token for token in scope_tokens.get(scope, ())):
                    raise ValueError(
                        f"cannot resolve computed argument {'.'.join(argument_path)!r}: "
                        "declaration scope is absent from the target model"
                    )
            if argument_path not in node_paths:
                raise ValueError(
                    f"cannot resolve computed argument {'.'.join(argument_path)!r}: "
                    "element is absent from the target model"
                )
            if isinstance(argument, Field) and not any(
                candidate is argument for candidate in field_paths.get(argument_path, ())
            ):
                raise ValueError(
                    f"cannot resolve computed argument {'.'.join(argument_path)!r}: "
                    "a different field has the same path"
                )
            resolved.append(physical_field_paths.get(
                argument_path, physical_paths.get(argument_path, argument_path)
            ))
        ancestor = _common_path_prefix(tuple(resolved))
        if ancestor and any(
            candidate != ancestor and candidate[-len(ancestor):] == ancestor
            for candidate in emitted_nodes
        ):
            resolved = [(model_name,) + path for path in resolved]
        computed_paths[field_path] = tuple(resolved)
    for source_path, entries in tuple(relations.items()):
        for _relation_type, target, _attributes, definition in entries:
            target_parts = tuple(target.split("."))
            alternative_root = next(
                (
                    root for root in packet_alternatives
                    if target_parts[: len(root)] == root
                ),
                None,
            )
            if (
                alternative_root is not None
                and source_path[: len(alternative_root)] != alternative_root
            ):
                candidates = field_paths.get(alternative_root + source_path, ())
            else:
                candidates = field_paths.get(source_path, ())
            if not candidates:
                raise ValueError(
                    f"cannot resolve relation source field {'.'.join(source_path)!r}: "
                    "field is absent from the target model"
                )
            if definition is not None and not any(
                candidate is definition for candidate in candidates
            ):
                raise ValueError(
                    f"cannot resolve relation source field {'.'.join(source_path)!r}: "
                    "a different field has the same path"
                )
        relations[source_path] = [
            (
                relation_type, paths.get(target, target),
                MappingProxyType({**attributes, "of": qualified_targets[target]})
                if target in qualified_targets else attributes,
                definition,
            )
            for relation_type, target, attributes, definition in entries
        ]
    return MappingProxyType(computed_paths)


def _collect_peach_array_relations(
    array: ArrayResult,
    path: tuple[str, ...],
    name: str,
    relations: PeachRelations,
    count_scope: tuple[str, ...] | None = None,
) -> None:
    if count_scope is None:
        count_scope = path
    _collect_peach_length_relation(
        array.count, path, name, "count", relations, count_scope
    )
    element_path = path + (name,)
    if isinstance(array.element, ArrayResult):
        _collect_peach_array_relations(
            array.element, element_path, "value" if array.optional else "item",
            relations, count_scope,
        )
    else:
        _collect_peach_element_relations(
            array.element, element_path, relations, count_scope
        )


def _collect_peach_element_relations(
    element: FieldResult | SchemaResult | UnionResult | ArrayResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    source_scope: tuple[str, ...] | None = None,
) -> None:
    if isinstance(element, FieldResult):
        _collect_peach_length_relation(
            _peach_field_relation_length(element),
            path[:-1],
            path[-1],
            "size",
            relations,
            source_scope,
            length_type=element.peach_attributes.get("lengthType"),
        )
    elif isinstance(element, (SchemaResult, UnionResult)):
        _collect_peach_relations(element, path, relations, source_scope)
    else:
        _collect_peach_array_relations(
            element, path[:-1], path[-1], relations, source_scope
        )


def _peach_field_relation_length(field: FieldResult) -> int | FieldReference | ExprResult | None:
    length = field.length
    if not isinstance(length, (FieldReference, ExprResult)):
        return length
    if _peach_field_tag(field) != "String" or field.peach_attributes.get("lengthType", "bytes").lower() != "chars":
        return length
    encoding = field.peach_attributes.get("type", "utf8")
    widths = {"ascii": 1, "utf16": 2, "utf16be": 2}
    if encoding not in widths:
        raise ValueError(
            f"String {field.name!r} dynamic character length cannot be represented "
            f"by a Peach size relation for {encoding}; use a byte length or a constant character length"
        )
    width = widths[encoding]
    return length if width == 1 else ExprResult("*", length, width)


def _collect_peach_length_relation(
    length: int | FieldReference | ExprResult | Occurs | None,
    container_path: tuple[str, ...],
    target_name: str,
    relation_type: str,
    relations: PeachRelations,
    source_container_path: tuple[str, ...] | None = None,
    *,
    length_type: str | None = None,
) -> None:
    relation_attributes: Mapping[str, str] = MappingProxyType({})
    if isinstance(length, FieldReference):
        reference = length
    elif isinstance(length, ExprResult):
        reference = _peach_expr_reference(length)
        attributes = {
            "expressionGet": _format_peach_relation_expr(length, relation_type),
        }
        inverse = _format_peach_relation_inverse(length, relation_type)
        if inverse is None:
            # Omitting expressionSet makes Peach overwrite the source with
            # the raw size/count, which is incorrect for lossy expressions.
            # Preserve the parsed/default source if it still describes the
            # actual target; otherwise no unique replacement can be inferred.
            forward = _format_peach_relation_expr(length, "original")
            inverse = (
                f"(lambda original, actual: str(original) if {forward} == actual else "
                "(_ for _ in ()).throw(ValueError('cannot uniquely invert relation for the actual length')))"
                f"(int(str(self.DefaultValue).rstrip('\\0')), {relation_type})"
            )
        attributes["expressionSet"] = inverse
        relation_attributes = MappingProxyType(attributes)
    else:
        return
    if relation_type == "size" and length_type is not None and length_type.lower() == "bits":
        relation_attributes = MappingProxyType({**relation_attributes, "lengthType": "bits"})
    source_path = _resolve_peach_reference(
        container_path if source_container_path is None else source_container_path,
        reference,
    )
    target_path = container_path + (target_name,)
    relations.setdefault(source_path, []).append(
        (relation_type, _peach_path(target_path), relation_attributes, reference.definition)
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


def _format_peach_relation_inverse(expr: ExprResult, variable: str) -> str | None:
    """Invert single-field arithmetic, preserving each division step."""

    def format_value(base: str, sign: int, offset: int | float) -> str:
        if sign == 1:
            if offset > 0:
                return f"({base} + {offset!r})"
            if offset < 0:
                return f"({base} - {-offset!r})"
            return base
        if offset:
            return f"({offset!r} - {base})"
        return f"(-{base})"

    def has_field(operand: EvaluatedExprOperand) -> bool:
        return isinstance(operand, FieldReference) or (
            isinstance(operand, ExprResult) and _expr_result_has_field(operand)
        )

    def solve(
        node: ExprResult | FieldReference,
        base: str,
        sign: int,
        offset: int | float,
    ) -> str | None:
        if isinstance(node, FieldReference):
            return format_value(base, sign, offset)

        field_on_left = has_field(node.left)
        field_on_right = has_field(node.right)
        if field_on_left == field_on_right:
            return None
        field = node.left if field_on_left else node.right
        constant = node.right if field_on_left else node.left
        if (
            not isinstance(field, (ExprResult, FieldReference))
            or not isinstance(constant, (int, float))
            or isinstance(constant, bool)
        ):
            return None

        if node.operation == "+":
            offset -= constant
        elif node.operation == "-":
            if field_on_left:
                offset += constant
            else:
                sign = -sign
                offset = constant - offset
        elif node.operation in ("*", "<<"):
            if node.operation == "<<":
                if not field_on_left or not isinstance(constant, int) or constant < 0:
                    return None
                constant = 1 << constant
            if constant == 0:
                return None
            # Preserve integral lengths and avoid precision loss under Python 3.
            # Peach's relation conversion does not accept a floating result.
            division = "//" if isinstance(constant, int) else "/"
            numerator = format_value(base, sign, offset)
            base = f"({numerator} {division} {constant!r})"
            if isinstance(constant, int) and abs(constant) != 1:
                base = (
                    f"(lambda dividend: dividend // {constant!r} if dividend % {constant!r} == 0 else "
                    "(_ for _ in ()).throw(ValueError('relation length is not divisible by its scale')))"
                    f"({numerator})"
                )
            sign, offset = 1, 0
        elif node.operation == "^":
            if not isinstance(constant, int):
                return None
            base = f"({format_value(base, sign, offset)} ^ {constant!r})"
            sign, offset = 1, 0
        elif node.operation == "/":
            if constant == 0:
                return None
            value = format_value(base, sign, offset)
            base = (
                f"({value} * {constant!r})"
                if field_on_left
                else f"({constant!r} / {value})"
            )
            sign, offset = 1, 0
        else:
            return None
        return solve(field, base, sign, offset)

    return solve(expr, variable, 1, 0)


def _resolve_peach_reference(
    container_path: tuple[str, ...],
    reference: str | FieldReference,
) -> tuple[str, ...]:
    if isinstance(reference, FieldReference):
        parts = tuple(reference.name.split("."))
        if reference._scope_up:
            container_path = container_path[:-reference._scope_up]
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
        _validate_integer_default(field, int(attributes["size"]), field.signed is True)
        if field.signed is not None:
            attributes["signed"] = _peach_bool(field.signed)
        if field.endian is not None:
            attributes["endian"] = field.endian
    elif tag == "Double":
        if field.size is None:
            raise ValueError(f"Double {name} has no size")
        value = field.fixed.value if field.fixed is not None else field.value
        maximum = float.fromhex("0x1.fffffep+127" if field.size == 32 else "0x1.fffffffffffffp+1023")
        if (
            isinstance(value, (int, float))
            and field.peach_attributes.get("valueType", "string").lower() == "string"
            and value not in (float("inf"), float("-inf"))
            and abs(value) > maximum
        ):
            raise ValueError(f"Double {name!r} value {value} does not fit finite {field.size}-bit range")
        attributes["size"] = str(field.size)
        if field.endian is not None:
            attributes["endian"] = field.endian
    elif tag == "String":
        attributes.setdefault("type", "utf8")
        if attributes.get("lengthType", "bytes").lower() not in ("bytes", "bits", "chars"):
            raise ValueError(f"String {name!r} length_type must be bytes, bits or chars")

    if tag in ("Blob", "String") and "lengthType" in attributes:
        # Peach's length-unit parser is case sensitive.
        attributes["lengthType"] = attributes["lengthType"].lower()

    if isinstance(field.length, int) and tag in ("Blob", "String"):
        attributes["length"] = str(field.length)

    if field.fixed is not None:
        if tag == "Blob" and isinstance(field.fixed.value, bytes):
            attributes["value"] = _peach_blob_bytes(field.fixed.value, attributes, name)
        else:
            attributes["value"] = _peach_scalar(field.fixed.value)
        attributes.setdefault("token", "true")
    elif field.value is not None:
        if tag == "Blob" and isinstance(field.value, bytes):
            attributes["value"] = _peach_blob_bytes(field.value, attributes, name)
        else:
            attributes["value"] = _peach_scalar(field.value)
        attributes.setdefault("token", "false")

    numeric_value = field.fixed.value if field.fixed is not None else field.value
    if tag == "Blob":
        units = attributes.get("lengthType", "bytes").lower()
        if units not in ("bytes", "bits"):
            raise ValueError(f"Blob {name!r} length_type must be bytes or bits")
        if isinstance(field.length, int) and isinstance(numeric_value, bytes):
            capacity = field.length if units == "bits" else field.length * 8
            if len(numeric_value) * 8 > capacity:
                raise ValueError(f"Blob {name!r} value exceeds its declared length")
    if (
        field.kind == "decimal_string"
        and isinstance(field.length, int)
        and isinstance(numeric_value, int)
        and attributes.get("valueType", "string").lower() == "string"
    ):
        attributes["value"] = _decimal_integer_text(numeric_value, field.length, attributes)

    if tag == "String" and "value" in attributes:
        decoded_value: str | None = None
        valid_xml = _valid_xml_text(attributes["value"])
        encoding = attributes.get("type", "utf8")
        codec = {"utf16": "utf-16-le", "utf16be": "utf-16-be",
                 "utf32": "utf-32-le", "utf32be": "utf-32-be"}.get(encoding, encoding)
        if attributes.get("valueType", "string").lower() == "string":
            decoded_value = attributes["value"]
            try:
                encoded = _encode_peach_string(attributes["value"], codec)
            except UnicodeEncodeError as error:
                raise ValueError(f"String {name!r} value cannot be encoded as {encoding}") from error
            # Peach also unescapes backslash sequences in XML value text.
            # Hex bytes preserve the Python string's already-decoded value.
            if not valid_xml or "\\" in attributes["value"]:
                attributes["value"] = _peach_string_hex(encoded, codec)
                attributes["valueType"] = "hex"
        elif not valid_xml:
            raise ValueError(f"String {name!r} has invalid XML characters in its encoded value")
        elif attributes.get("valueType", "").lower() == "hex":
            # Match Peach's accepted separators and prefixes before checking
            # bytes. Keep the original spelling in the emitted XML.
            escapes = {"\\\\": "\\", "\\n": "\n", "\\r": "\r", "\\t": "\t"}
            value = re.sub(r"\\\\|\\[nrt]", lambda match: escapes[match.group()], attributes["value"])
            value = re.sub(r"[h{},\s:-]+", "", value).replace("0x", "").replace("\\x", "")
            try:
                raw = bytes.fromhex(value)
            except ValueError as error:
                raise ValueError(f"String {name!r} value contains invalid hex bytes") from error
            try:
                decoded_value = raw.decode(codec)
            except UnicodeDecodeError as error:
                raise ValueError(f"String {name!r} hex value cannot be decoded as {encoding}") from error
            if codec == "utf7" and re.search(rb"\+[A-Za-z0-9+/]+-$", raw):
                attributes["value"] = _peach_string_hex(raw, codec)
        if isinstance(field.length, int) and decoded_value is not None:
            _validate_string_default(name, decoded_value, field.length, attributes, codec)
    elif tag == "String" and isinstance(field.length, int) and field.computed is None:
        # An omitted default is still an empty runtime string. Peach pads it
        # using the same encoding and length rules as an explicit empty value.
        encoding = attributes.get("type", "utf8")
        codec = {"utf16": "utf-16-le", "utf16be": "utf-16-be",
                 "utf32": "utf-32-le", "utf32be": "utf-32-be"}.get(encoding, encoding)
        _validate_string_default(name, "", field.length, attributes, codec)

    element = ET.SubElement(parent, tag, attributes)
    _append_peach_relations(element, path + (name,), relations)
    if (
        field.kind == "decimal_string" and element.find("Relation") is not None
        and isinstance(numeric_value, int)
        and not -(2**63) <= numeric_value <= 2**63 - 1
    ):
        raise ValueError(
            f"DecimalString {name!r} relation source must fit Peach's signed 64-bit integer parser"
        )
    if field.kind == "decimal_string":
        width = _decimal_integer_width(field.length, attributes) if isinstance(field.length, int) else None
        suffix = " + '\\0'" if attributes.get("nullTerminated", "false").lower() == "true" else ""
        for relation in element.findall("Relation"):
            inverse = relation.get("expressionSet", relation.attrib["type"])
            checked = (
                "(lambda number: str(number) if -9223372036854775808 <= number <= 9223372036854775807 "
                "else (_ for _ in ()).throw(OverflowError('DecimalString relation source must fit signed 64-bit')))"
                f"(int({inverse}))"
            )
            # Peach formats numeric relation results using the entire width,
            # leaving no room for a terminator. Return checked decimal text.
            if width is not None:
                checked = (
                    f"(lambda text: text.zfill({width}){suffix} if len(text) <= {width} "
                    "else (_ for _ in ()).throw(OverflowError('DecimalString relation result does not fit its declared length')))"
                    f"({checked})"
                )
            relation.set("expressionSet", checked)
    if field.computed is not None:
        decimal_format = None
        if field.kind == "decimal_string":
            width = (
                _decimal_integer_width(field.length, attributes)
                if isinstance(field.length, int) else None
            )
            decimal_format = _DecimalFixupFormat(width, attributes.get("nullTerminated") == "true")
        _append_script_fixup(element, field.computed, path + (name,), decimal_format=decimal_format)


def _decimal_integer_width(length: int, attributes: Mapping[str, str]) -> int:
    units = attributes.get("lengthType", "bytes")
    encoding = attributes.get("type", "utf8")
    bytes_per_character = (
        4 if encoding.startswith("utf32") else 2 if encoding.startswith("utf16") else 1
    )
    if units == "chars":
        width = length
    elif units in ("bytes", "bits"):
        unit_width = bytes_per_character * (8 if units == "bits" else 1)
        width, remainder = divmod(length, unit_width)
        if remainder:
            raise ValueError("DecimalString length must fit complete encoded characters")
    else:
        raise ValueError(f"unsupported DecimalString length type: {units}")
    if attributes.get("nullTerminated") == "true":
        width -= 1
    return width


def _decimal_integer_text(value: int, length: int, attributes: Mapping[str, str]) -> str:
    width = _decimal_integer_width(length, attributes)
    text = str(value)
    if len(text) > width:
        raise ValueError(f"DecimalString value {value} does not fit its declared length")
    # Peach pads textual defaults on the right, which would change the number.
    return text.zfill(width)


def _append_script_fixup(
    element: ET.Element,
    computed_value: Computed[ScalarValue],
    field_path: tuple[str, ...],
    *,
    decimal_format: _DecimalFixupFormat | None = None,
) -> None:
    definitions = _SCRIPT_FIXUPS.get()
    if definitions is None:
        raise ValueError("computed fields require Peach artifact rendering")
    model_name = _PEACH_CURRENT_MODEL.get()
    if model_name is None:
        raise ValueError("a computed field has no containing Peach DataModel")
    if computed_value.kwargs:
        raise ValueError("computed field calls must use positional arguments")

    container_path = field_path[:-1]
    argument_paths = _COMPUTED_PATHS.get().get(field_path)
    if argument_paths is None:
        argument_paths = tuple(
            _computed_argument_path(argument, container_path)
            for argument in computed_value.args
        )
    ancestor_path = _common_path_prefix(argument_paths)
    function = computed_value.func
    source = _computed_function_source(function)
    identity = "\n".join(
        (
            model_name,
            ".".join(field_path),
            source,
            repr(decimal_format),
            *(".".join(argument_path) for argument_path in argument_paths),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    function_name = re.sub(r"\W+", "_", function.__name__).strip("_") or "computed"
    class_name = f"Fixup_{function_name}_{digest}"
    definitions.setdefault(
        class_name,
        ScriptFixupDefinition(
            class_name,
            function,
            argument_paths,
            ancestor_path,
            decimal_format,
        ),
    )

    reference = ".".join(ancestor_path) if ancestor_path else model_name
    fixup = ET.SubElement(element, "Fixup", {"class": "ScriptFixup"})
    ET.SubElement(
        fixup,
        "Param",
        {"name": "class", "value": f"python_fixup.{class_name}"},
    )
    ET.SubElement(fixup, "Param", {"name": "ref", "value": reference})


def _computed_argument_path(
    argument: object,
    container_path: tuple[str, ...],
) -> tuple[str, ...]:
    if isinstance(argument, ComputedReference):
        if argument._detached:
            raise ValueError(
                f"cannot resolve computed argument {'.'.join(argument.path)!r}: "
                "reference belongs to a detached schema instance"
            )
        if argument._scope_up > len(container_path):
            raise ValueError("computed argument scope lies outside the target model")
        scope = container_path[:-argument._scope_up] if argument._scope_up else container_path
        return scope + argument.path
    if isinstance(argument, MemberRef):
        return container_path + argument.path
    if isinstance(argument, Field):
        if argument.name is None:
            raise ValueError("a computed Field argument has no name")
        return container_path + (argument.name,)
    if isinstance(argument, _SchemaInstance):
        if not argument.binding_path:
            raise ValueError("a computed Schema argument is not bound to a field")
        return container_path + argument.binding_path
    if isinstance(argument, BlockField):
        if argument.name is None:
            raise ValueError("a computed Block argument has no name")
        return container_path + (argument.name,)
    raise TypeError(
        "computed field arguments must be Field, MemberRef, Schema, or Block values"
    )


def _common_path_prefix(paths: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    if not paths:
        raise ValueError("a computed field must reference at least one argument")
    prefix = list(paths[0])
    for path in paths[1:]:
        limit = min(len(prefix), len(path))
        index = 0
        while index < limit and prefix[index] == path[index]:
            index += 1
        del prefix[index:]
    return tuple(prefix)


def _capture_literal(value: object, name: str, *, context: str = "computed") -> ast.expr:
    if value is None or type(value) in (bool, int, float, str, bytes):
        return ast.Constant(value=value)
    if type(value) is tuple:
        return ast.Tuple(
            elts=[_capture_literal(item, name, context=context) for item in cast(tuple[object, ...], value)],
            ctx=ast.Load(),
        )
    raise TypeError(
        f"{context} capture {name!r} must be a literal constant or tuple of constants; "
        f"cannot export {type(value).__name__}"
    )


def _computed_global_names(code: CodeType) -> Iterator[str]:
    instructions = tuple(dis.get_instructions(code))
    # LOAD_NAME checks a class namespace first, then falls back to globals.
    # Only a store reached on every incoming path rules out that fallback.
    by_offset = {instruction.offset: index for index, instruction in enumerate(instructions)}
    assigned: dict[int, frozenset[str]] = {0: frozenset()} if instructions else {}
    pending = [0] if instructions else []
    jumps = set(dis.hasjrel) | set(dis.hasjabs) | set(getattr(dis, "hasjump", ()))
    handlers = tuple(dis.Bytecode(code).exception_entries)

    def merge(index: int, names: frozenset[str]) -> None:
        previous = assigned.get(index)
        updated = names if previous is None else previous & names
        if previous != updated:
            assigned[index] = updated
            pending.append(index)

    while pending:
        index = pending.pop()
        instruction = instructions[index]
        incoming = assigned[index]
        outgoing = incoming
        if isinstance(instruction.argval, str):
            if instruction.opname == "STORE_NAME":
                outgoing = incoming | {instruction.argval}
            elif instruction.opname == "DELETE_NAME":
                outgoing = incoming - {instruction.argval}
        if instruction.opcode in jumps and isinstance(instruction.argval, int):
            merge(by_offset[instruction.argval], outgoing)
        unconditional = instruction.opname.startswith("JUMP") and "IF" not in instruction.opname
        if (
            index + 1 < len(instructions) and not unconditional
            and not instruction.opname.startswith(("RETURN", "RAISE", "RERAISE"))
        ):
            merge(index + 1, outgoing)
        for handler in handlers:
            if handler.start <= instruction.offset < handler.end:
                merge(by_offset[handler.target], incoming)

    for index, instruction in enumerate(instructions):
        if (
            instruction.opname == "LOAD_GLOBAL"
            or instruction.opname == "LOAD_NAME" and index in assigned
            and instruction.argval not in assigned[index]
        ) and isinstance(instruction.argval, str):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from _computed_global_names(constant)


class _ComputedClassCaptures(ast.NodeTransformer):
    """Keep class namespace lookup while supplying captured global fallbacks."""

    def __init__(self, aliases: Mapping[str, str], locals_name: str) -> None:
        self.aliases = aliases
        self.locals_name = locals_name
        self.in_class = False
        self.used: set[str] = set()

    def visit_Name(self, node: ast.Name) -> ast.expr:
        if self.in_class and isinstance(node.ctx, ast.Load) and node.id in self.aliases:
            self.used.add(node.id)
            return ast.copy_location(ast.Call(
                func=ast.Attribute(
                    value=ast.Call(func=ast.Name(id=self.locals_name, ctx=ast.Load()), args=[], keywords=[]),
                    attr="get", ctx=ast.Load(),
                ),
                args=[ast.Constant(value=node.id), ast.Name(id=self.aliases[node.id], ctx=ast.Load())],
                keywords=[],
            ), node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node.bases = [self.visit(base) for base in node.bases]
        node.keywords = [self.visit(keyword) for keyword in node.keywords]
        node.decorator_list = [self.visit(decorator) for decorator in node.decorator_list]
        previous = self.in_class
        self.in_class = True
        node.body = [self.visit(statement) for statement in node.body]
        self.in_class = previous
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.args = self.visit(node.args)
        node.decorator_list = [self.visit(decorator) for decorator in node.decorator_list]
        previous = self.in_class
        self.in_class = False
        node.body = [self.visit(statement) for statement in node.body]
        self.in_class = previous
        return node

    def visit_Lambda(self, node: ast.Lambda) -> ast.Lambda:
        node.args = self.visit(node.args)
        previous = self.in_class
        self.in_class = False
        node.body = self.visit(node.body)
        self.in_class = previous
        return node

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> ast.expr:
        first = node.generators[0]
        first.iter = self.visit(first.iter)
        previous = self.in_class
        self.in_class = False
        first.ifs = [self.visit(condition) for condition in first.ifs]
        node.generators[1:] = [self.visit(generator) for generator in node.generators[1:]]
        if isinstance(node, ast.DictComp):
            node.key, node.value = self.visit(node.key), self.visit(node.value)
        else:
            node.elt = self.visit(node.elt)
        self.in_class = previous
        return node

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension


def _computed_function_node(function: Callable[..., ScalarValue]) -> ast.FunctionDef:
    try:
        source = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError) as error:
        raise ValueError(
            f"cannot read source for computed function {function.__qualname__}"
        ) from error
    module = ast.parse(source)
    node = next(
        (
            item
            for item in module.body
            if isinstance(item, ast.FunctionDef) and item.name == function.__name__
        ),
        None,
    )
    if node is None:
        raise ValueError(
            f"cannot find source definition for computed function {function.__qualname__}"
        )
    if any(isinstance(item, (ast.Global, ast.Nonlocal)) for item in ast.walk(node)):
        raise ValueError("computed functions cannot declare global or nonlocal state")
    if not inspect.isfunction(function):
        raise TypeError("computed declarations must use Python functions")
    # Analyze the code actually exported: annotations are removed and must
    # not cause typing objects or annotation-only closure values to be captured.
    runtime_node = copy.deepcopy(node)
    _ComputedAnnotationRemover().visit(runtime_node)
    runtime_node.decorator_list = []
    runtime_module = ast.fix_missing_locations(ast.Module(body=[runtime_node], type_ignores=[]))
    runtime_code = compile(runtime_module, "<computed-dependencies>", "exec")
    runtime_names = set(_computed_global_names(runtime_code))
    closure = inspect.getclosurevars(function)
    captures = {
        name: function.__globals__[name]
        for name in runtime_names
        if name in function.__globals__
    }
    captures.update({name: value for name, value in closure.nonlocals.items() if name in runtime_names})
    reserved = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
    reserved.update(item.arg for item in ast.walk(node) if isinstance(item, ast.arg))
    reserved.update(item.name for item in ast.walk(node) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    reserved.update(item.asname or item.name.split(".")[0] for item in ast.walk(node) if isinstance(item, ast.alias))

    def fresh_name(prefix: str) -> str:
        while prefix in reserved:
            prefix += "_"
        reserved.add(prefix)
        return prefix

    locals_name = fresh_name("_peach_class_locals")
    aliases = {name: fresh_name("_peach_capture_" + name) for name in captures}
    class_captures = _ComputedClassCaptures(aliases, locals_name)
    # Strip annotations before rewriting reads evaluated by class bodies.
    _ComputedAnnotationRemover().visit(node)
    class_captures.visit(node)
    assignments: list[ast.stmt] = [
        ast.Assign(
            targets=[ast.Name(id=name, ctx=ast.Store())],
            value=_capture_literal(value, name),
        )
        for name, value in sorted(captures.items())
    ]
    if class_captures.used:
        assignments.append(ast.ImportFrom(module="builtins", names=[ast.alias(name="locals", asname=locals_name)], level=0))
        assignments.extend(
            ast.Assign(targets=[ast.Name(id=aliases[name], ctx=ast.Store())],
                       value=_capture_literal(captures[name], name))
            for name in sorted(class_captures.used)
        )
    node.body = assignments + node.body
    return node


def _computed_function_source(function: Callable[..., ScalarValue]) -> str:
    node = _computed_function_node(function)
    node.decorator_list = []
    return ast.unparse(ast.fix_missing_locations(node))


def _computed_annotation_name(annotation: object, parameter: str) -> str:
    supported: Mapping[object, str] = {
        int: "int",
        float: "float",
        str: "str",
        bytes: "bytes",
        bytearray: "bytearray",
        "int": "int",
        "float": "float",
        "str": "str",
        "bytes": "bytes",
        "bytearray": "bytearray",
    }
    name = supported.get(annotation)
    if name is None:
        raise TypeError(
            f"computed parameter {parameter!r} must be annotated as "
            "int, float, str, bytes, or bytearray"
        )
    return name


def _computed_argument_expression(converter: str, target: str) -> str:
    if converter == "bytearray":
        return f"bytearray({target}.Bytes())"
    if converter == "bytes":
        return f"bytes(bytearray({target}.Bytes()))"
    if converter == "int":
        # IronPython int(Variant) selects Variant's 32-bit CLR conversion.
        # Decimal text preserves signed and unsigned 64-bit input values.
        return f"int(str({target}.InternalValue).rstrip('\\0'))"
    return f"{converter}({target}.InternalValue)"


class _ComputedAnnotationRemover(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        node.returns = None
        node.type_comment = None
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            argument.annotation = None
            argument.type_comment = None
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.stmt | list[ast.stmt]:
        if node.value is not None:
            return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)
        if isinstance(node.target, ast.Name):
            # Retain local-variable binding without initializing the variable.
            return ast.copy_location(ast.If(
                test=ast.Constant(value=False),
                body=[ast.Assign(targets=[node.target], value=ast.Constant(value=None))],
                orelse=[],
            ), node)
        if isinstance(node.target, ast.Attribute):
            return ast.copy_location(ast.Expr(value=node.target.value), node)
        if isinstance(node.target, ast.Subscript):
            # Annotation-only targets evaluate the container and indices, but
            # never call __getitem__ or assign a value to the target.
            return [
                ast.copy_location(ast.Expr(value=node.target.value), node),
                ast.copy_location(ast.Expr(value=self._annotation_index(node.target.slice)), node),
            ]
        return node

    def _annotation_index(self, node: ast.expr) -> ast.expr:
        if isinstance(node, ast.Slice):
            return ast.Tuple(
                elts=[part for part in (node.lower, node.upper, node.step) if part is not None],
                ctx=ast.Load(),
            )
        if isinstance(node, ast.Tuple):
            return ast.Tuple(elts=[self._annotation_index(part) for part in node.elts], ctx=ast.Load())
        return node


def _render_computed_method(function: Callable[..., ScalarValue]) -> str:
    node = _computed_function_node(function)
    _ComputedAnnotationRemover().visit(node)
    node.name = "func"
    node.decorator_list = []
    parameter_names = {argument.arg for argument in (*node.args.posonlyargs, *node.args.args)}
    receiver = "self"
    while receiver in parameter_names:
        receiver = "_" + receiver
    # Fixup calls are positional. Flatten positional-only parameters so the
    # generated method also remains compatible with the Peach Python runtime.
    node.args.args = [ast.arg(arg=receiver), *node.args.posonlyargs, *node.args.args]
    node.args.posonlyargs = []
    return ast.unparse(ast.fix_missing_locations(node))


def _render_python_fixups(
    definitions: Mapping[str, ScriptFixupDefinition],
) -> str:
    lines = [
        "import clr",
        "clr.AddReference(\"Peach.LLM\")",
        "import Peach.LLM.Core",
        "clr.ImportExtensions(Peach.LLM.Core)",
        "",
        "",
    ]
    for definition in definitions.values():
        signature = inspect.signature(definition.function)
        parameters = tuple(signature.parameters.values())
        if len(parameters) != len(definition.argument_paths):
            raise ValueError(
                f"computed function {definition.function.__qualname__} argument count changed"
            )
        lines.append(f"class {definition.class_name}:")
        lines.append("    def __init__(self, parent):")
        lines.append("        self.parent = parent")
        lines.append("")
        lines.extend(
            textwrap.indent(_render_computed_method(definition.function), "    ").splitlines()
        )
        lines.append("")
        lines.append("    def _convert_result(self, value):")
        if definition.decimal_format is not None:
            formatting = definition.decimal_format
            lines.append("        if not isinstance(value, int) or isinstance(value, bool):")
            lines.append("            raise TypeError('DecimalString computed result must be an integer')")
            lines.append("        text = str(value)")
            if formatting.width is not None:
                lines.append(f"        if len(text) > {formatting.width}:")
                lines.append("            raise OverflowError('DecimalString computed result does not fit its declared length')")
                lines.append(f"        text = text.zfill({formatting.width})")
                if formatting.terminated:
                    lines.append("        text += '\\0'")
            lines.append("        return text")
        else:
            # ScriptFixup does not accept System.Double; Double can read an
            # invariant round-trip decimal string through its normal conversion.
            lines.append("        if isinstance(value, float):")
            lines.append("            if value != value:")
            lines.append("                return 'NaN'")
            lines.append("            if value == float('inf'):")
            lines.append("                return 'Infinity'")
            lines.append("            if value == float('-inf'):")
            lines.append("                return '-Infinity'")
            lines.append("            return repr(value)")
            lines.append("        if isinstance(value, (bytes, bytearray)):")
            lines.append("            from System import Array, Byte")
            lines.append("            return Array[Byte](value)")
            lines.append("        if isinstance(value, int):")
            lines.append("            if value < -9223372036854775808 or value > 18446744073709551615:")
            lines.append("                raise OverflowError('computed integer result is outside Peach 64-bit range')")
            lines.append("            if -2147483648 <= value <= 2147483647:")
            lines.append("                return int(value)")
            lines.append("            from System import Int64, UInt64")
            lines.append("            if value <= 9223372036854775807:")
            lines.append("                return Int64(value)")
            lines.append("            return UInt64(value)")
            lines.append("        return value")
        lines.append("")
        lines.append("    def fixup(self, element):")
        argument_names: list[str] = []
        protected_names = {"self", "element", "int", "float", "str", "bytes", "bytearray"}
        reserved_names = {parameter.name for parameter in parameters} | protected_names
        for index, (parameter, argument_path) in enumerate(zip(parameters, definition.argument_paths)):
            converter = _computed_annotation_name(parameter.annotation, parameter.name)
            relative_path = argument_path[len(definition.ancestor_path) :]
            target = (
                "element"
                if not relative_path
                else f'element.find("{".".join(relative_path)}")'
            )
            expression = _computed_argument_expression(converter, target)
            local_name = parameter.name
            if local_name in protected_names:
                local_name = f"_peach_argument_{index}"
                while local_name in reserved_names:
                    local_name = "_" + local_name
                reserved_names.add(local_name)
            lines.append(f"        {local_name} = {expression}")
            argument_names.append(local_name)
        lines.append(f"        return self._convert_result(self.func({', '.join(argument_names)}))")
        lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _add_python_fixup_import(xml_text: str) -> str:
    ET.register_namespace("", PEACH_NAMESPACE)
    ET.register_namespace("xsi", XSI_NAMESPACE)
    root = ET.fromstring(xml_text)
    root.insert(0, ET.Element("PythonPath", {"path": "."}))
    root.insert(1, ET.Element("PythonPath", {"path": "/generated"}))
    root.insert(2, ET.Element("Import", {"import": "python_fixup"}))
    ET.indent(root, space="  ")
    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    ).decode("utf-8")


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


def _validate_integer_default(field: FieldResult, width: int, signed: bool) -> None:
    value = field.fixed.value if field.fixed is not None else field.value
    if not isinstance(value, int) or field.peach_attributes.get("valueType", "string").lower() != "string":
        return
    minimum = -(1 << (width - 1)) if signed else 0
    maximum = (1 << (width - (1 if signed else 0))) - 1
    if not minimum <= value <= maximum:
        sign = "signed" if signed else "unsigned"
        raise ValueError(
            f"field {field.name!r} value {value} does not fit {sign} {width}-bit range [{minimum}, {maximum}]"
        )


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
        assert isinstance(member.length, int)
        _validate_integer_default(member, member.length, False)
        attributes = {
            "name": flag_name,
            **member.peach_attributes,
            "size": str(member.length),
            "position": str(member.position),
        }
        if member.fixed is not None:
            attributes["value"] = _peach_scalar(member.fixed.value)
            attributes.setdefault("token", "true")
        elif member.value is not None:
            attributes["value"] = _peach_scalar(member.value)
            attributes.setdefault("token", "false")
        flag = ET.SubElement(element, "Flag", attributes)
        _append_peach_relations(flag, path + (name, flag_name), relations)
        if member.computed is not None:
            _append_script_fixup(flag, member.computed, path + (name, flag_name))


def _append_peach_union(
    parent: ET.Element,
    name: str,
    union: UnionResult,
    path: tuple[str, ...],
    relations: PeachRelations,
    occurrence: Mapping[str, str] | None = None,
) -> None:
    choice = ET.SubElement(parent, "Choice", {"name": name, **(occurrence or {})})
    _append_peach_union_alternatives(choice, union, path + (name,), relations)


def _append_peach_union_alternatives(
    choice: ET.Element,
    union: UnionResult,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    for alternative in union.alternatives:
        if isinstance(alternative, FieldResult):
            _append_peach_field(
                choice,
                alternative.name,
                alternative,
                path,
                relations,
                {},
            )
        else:
            _append_peach_schema(
                choice,
                alternative.name,
                alternative,
                path,
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
    if array.optional:
        choice = ET.SubElement(parent, "Choice", {"name": name, **occurrence})
        if isinstance(array.element, UnionResult):
            _append_peach_union_alternatives(
                choice, array.element, path + (name,), relations
            )
        else:
            element_name = _optional_element_name(name, array.element)
            nested_relations = _nest_peach_relation_sources(
                relations, path + (name,), element_name
            )
            _append_peach_array_element(
                choice,
                element_name,
                array.element,
                path + (name,),
                nested_relations,
                {},
            )
        ET.SubElement(choice, "Block", {"name": _optional_empty_name(array.element)})
        return

    attributes = dict(occurrence)
    if array.count is None:
        attributes["minOccurs"] = "0"
        attributes["maxOccurs"] = "-1"
    elif isinstance(array.count, Occurs):
        attributes["minOccurs"] = str(array.count.min_occurs)
        attributes["maxOccurs"] = str(array.count.max_occurs)
    elif isinstance(array.count, int):
        attributes["occurs"] = str(array.count)
    if isinstance(array.element, ArrayResult):
        block = ET.SubElement(parent, "Block", {"name": name, **attributes})
        _append_peach_array(
            block, "item", array.element, path + (name,), relations, {}
        )
        return
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
    nested_relations = _nest_peach_relation_sources(
        relations, path + (name,), element_name
    )
    _append_peach_array_element(
        element,
        element_name,
        optional.element,
        path + (name,),
        nested_relations,
        {},
    )


def _optional_internal_name(name: str) -> str:
    return f"{name}_internal"


def _optional_empty_name(
    element: FieldResult | SchemaResult | UnionResult | ArrayResult,
) -> str:
    used = (
        {alternative.name for alternative in element.alternatives}
        if isinstance(element, UnionResult)
        else set()
    )
    name = "empty"
    while name in used:
        name = "optional_" + name
    return name


def _nest_peach_relation_sources(
    relations: PeachRelations,
    prefix: tuple[str, ...],
    nested_name: str,
) -> PeachRelations:
    nested: PeachRelations = {}
    for source_path, entries in relations.items():
        if (
            source_path[: len(prefix)] == prefix
            and source_path[len(prefix) : len(prefix) + 1] != (nested_name,)
        ):
            source_path = prefix + (nested_name,) + source_path[len(prefix) :]
        nested[source_path] = entries
    return nested


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
            if operand._value_kind == "decimal_string":
                return "int(value.rstrip('\\0'))"
            return (
                f"{operand.value_cast}(value)"
                if operand.value_cast is not None
                else "value"
            )
        if isinstance(operand, str):
            return repr(operand)
        if isinstance(operand, float):
            literal = repr(operand)
            return f"float({literal!r})" if literal in {"inf", "-inf", "nan"} else literal
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
        if references[0]._scope_up and result_path is not None and source_path is not None:
            # Peach's name search can select a same-named sibling of an inner
            # Block. Anchor outer-scope references at the Optional itself so
            # they also survive DataModel reuse under a different name.
            origin = tuple(result_path.split("."))
            target = tuple(source_path.split("."))
            common = 0
            while common < min(len(origin), len(target)) and origin[common] == target[common]:
                common += 1
            source = "../" * (len(origin) - common) + ".".join(target[common:])
    return source, expression


def _append_peach_relations(
    element: ET.Element,
    path: tuple[str, ...],
    relations: PeachRelations,
) -> None:
    for relation_type, target, attributes, _definition in relations.get(path, ()):
        ET.SubElement(
            element,
            "Relation",
            {"type": relation_type, "of": target, **attributes},
        )


def _peach_blob_bytes(value: bytes, attributes: dict[str, str], name: str) -> str:
    mode = attributes.setdefault("valueType", "hex").lower()
    if mode == "hex":
        return value.hex()
    if mode in ("ipv4", "ipv6"):
        width = 4 if mode == "ipv4" else 16
        if len(value) != width:
            raise ValueError(f"Blob {name!r} {mode} value must contain {width} bytes")
        return str(IPv4Address(value) if mode == "ipv4" else IPv6Address(value))
    if mode in ("string", "literal") and value.isascii():
        text = value.decode("ascii")
        if mode == "literal":
            # Peach unescapes XML backslashes before evaluating the literal.
            return ascii(text).replace("\\", "\\\\")
        if _valid_xml_text(text) and "\\" not in text:
            return text
    # Blob's text parser uses ASCII. Hex preserves high bytes and characters
    # which cannot safely survive XML or Peach's extra unescaping pass.
    attributes["valueType"] = "hex"
    return value.hex()


def _peach_string_hex(encoded: bytes, codec: str) -> str:
    if codec == "utf7" and re.search(rb"\+[A-Za-z0-9+/]+-$", encoded):
        # UTF-7 also permits a shift to end at EOF. Peach's byte-stream
        # decoder otherwise leaves the final '-' without a produced character.
        encoded = encoded[:-1]
    return encoded.hex()


def _encode_peach_string(value: str, codec: str) -> bytes:
    encoded = value.encode(codec)
    if codec == "utf7":
        # .NET always closes a shifted UTF-7 sequence with '-'. Python may
        # omit it before a direct character which cannot belong to base64.
        encoded = re.sub(
            rb"\+[A-Za-z0-9+/]+(?![-A-Za-z0-9+/])",
            lambda match: match.group() + b"-",
            encoded,
        )
    return encoded


def _validate_string_default(
    name: str, value: str, length: int, attributes: Mapping[str, str], codec: str,
) -> None:
    units = attributes.get("lengthType", "bytes").lower()
    if units == "bits":
        if length % 8:
            raise ValueError(f"String {name!r} bit length must be divisible by 8")
        length //= 8
    if units == "chars":
        # .NET String.Length counts UTF-16 code units, including surrogate pairs.
        actual = len(value.encode("utf-16-le")) // 2
        terminator_width = pad_width = 1
    else:
        actual = len(_encode_peach_string(value, codec))
        terminator_width = len(_encode_peach_string("\0", codec))
        pad_width = len(_encode_peach_string(attributes.get("padCharacter", "\0"), codec))
    terminated = attributes.get("nullTerminated", "false").lower() == "true"
    if actual > length or (actual == length and terminated and not value.endswith("\0")):
        raise ValueError(f"String {name!r} value does not fit its declared length")
    if actual < length:
        remaining = length - actual - (terminator_width if terminated else 0)
        if remaining < 0 or remaining % pad_width:
            raise ValueError(f"String {name!r} padding cannot satisfy its declared length")
        if units != "chars" and codec == "utf7":
            pad = attributes.get("padCharacter", "\0")
            count = remaining // pad_width
            suffix = "\0" if terminated else ""
            # Repeated UTF-16 units produce a three-character base64 period.
            # Measure a short sample to avoid allocating a potentially huge
            # padded value just to validate the declared capacity.
            sample_count = count if count < 6 else 3 + count % 3
            sample = len(_encode_peach_string(value + pad * sample_count + suffix, codec))
            if count > sample_count:
                step = len(_encode_peach_string(value + pad * (sample_count + 3) + suffix, codec)) - sample
                sample += step * ((count - sample_count) // 3)
            if sample != length:
                raise ValueError(f"String {name!r} UTF-7 padding changes its encoded length")


def _valid_xml_text(value: str) -> bool:
    return all(
        character in "\t\n\r" or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD or 0x10000 <= ord(character) <= 0x10FFFF
        for character in value
    )


def _peach_bool(value: bool) -> str:
    return "true" if value else "false"


def _peach_scalar(value: ScalarValue) -> str:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bool):
        return _peach_bool(value)
    if isinstance(value, float):
        literal = str(value)
        return {"inf": "Infinity", "-inf": "-Infinity", "nan": "NaN"}.get(literal, literal)
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


class _EvaluationOverrides(dict[str, Override]):
    """Keep declaration identity when applying name-based instance overrides."""

    def __init__(
        self,
        values: Mapping[str, Override],
        fields: Mapping[str, SchemaMember],
        schema: type[Schema] | None = None,
        *,
        parent: Mapping[str, Override] | None = None,
        scope_depth: int = 1,
    ) -> None:
        super().__init__(values)
        self.fields = fields
        self.schema = schema
        self.parent = parent
        self.scope_depth = scope_depth


def _reference_scope(
    referenced: AnyField, overrides: Mapping[str, Override],
) -> tuple[Mapping[str, Override], int]:
    current = overrides
    depth = 0
    while isinstance(current, _EvaluationOverrides):
        if current.fields.get(referenced.name or "") is referenced or (
            current.schema is not None and any(
                base.__dict__.get(referenced.name or "") is referenced
                for base in current.schema.__mro__
            )
        ):
            return current, depth
        if current.parent is None:
            break
        depth += current.scope_depth
        current = current.parent
    # Unrelated references still fail the identity/path checks at rendering.
    return overrides, 0


def _resolve_reference_field(
    referenced: AnyField, overrides: Mapping[str, Override]
) -> AnyField:
    """Rebind inherited declarations without accepting unrelated same-name fields."""
    if (
        isinstance(overrides, _EvaluationOverrides)
        and overrides.schema is not None
        and referenced.name is not None
    ):
        effective = overrides.fields.get(referenced.name)
        if any(
            base.__dict__.get(referenced.name) is referenced
            for base in overrides.schema.__mro__
        ):
            if not isinstance(effective, Field):
                raise ValueError(
                    f"cannot resolve referenced field {referenced.name!r}: "
                    "replacement target is not a scalar field"
                )
            return effective
    return referenced


def _validate_scalar_reference_override(name: str, override: Override | None) -> None:
    replacement = _schema_member_override(override)
    if replacement is not None and not isinstance(replacement, Field):
        raise ValueError(
            f"cannot resolve referenced field {name!r}: "
            "replacement target is not a scalar field"
        )


def _reference_override(
    referenced: AnyField, overrides: Mapping[str, Override]
) -> Override | None:
    if referenced.name is None:
        return None
    if isinstance(overrides, _EvaluationOverrides):
        if overrides.fields.get(referenced.name) is not referenced:
            return None
    override = overrides.get(referenced.name)
    _validate_scalar_reference_override(referenced.name, override)
    return override


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

    overrides = _EvaluationOverrides(overrides, schema.__schema_fields__, schema)
    defaults = schema.__schema_defaults__.merge(inherited_defaults)
    for name, member in schema.__schema_fields__.items():
        fields[name] = _evaluate_schema_member(
            name,
            member,
            overrides,
            defaults,
        )

    flags_layout = schema.__flags_layout__
    if flags_layout is not None:
        position = 0
        storage_width = flags_layout.storage.width
        if storage_width is None:
            raise ValueError(f"{flags_layout.storage.name} has no declared bit width")
        for name, member in tuple(fields.items()):
            if (
                not isinstance(member, FieldResult)
                or member.kind != "bit"
                or not isinstance(member.length, int)
            ):
                raise TypeError(f"Flags member {schema.__name__}.{name} is not a fixed-width bit field")
            if member.length <= 0:
                raise ValueError(f"Flags field {schema.__name__}.{name} needs a positive width")
            if position + member.length > storage_width:
                raise ValueError(
                    f"Flags field {schema.__name__}.{name} exceeds {flags_layout.storage.name}"
                )
            fields[name] = replace(member, position=position)
            position += member.length
        if position != storage_width:
            raise ValueError(
                f"Flags schema {schema.__name__} covers {position} of "
                f"{storage_width} bits in {flags_layout.storage.name}"
            )

    return SchemaResult(
        result_name or schema.__name__,
        MappingProxyType(fields),
        flags_layout,
        packet_union=schema.__packet_union__,
        _computed_scope_tokens=tuple(
            base.__dict__["__computed_scope_token__"]
            for base in schema.__mro__ if "__computed_scope_token__" in base.__dict__
        ),
    )


def _evaluate_schema_member(
    name: str,
    member: SchemaMember,
    overrides: Mapping[str, Override],
    defaults: SchemaDefaults,
) -> FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult:
    override = overrides.get(name)
    replacement = _schema_member_override(override)
    effective = replacement if replacement is not None else member

    if override is not None and replacement is None and not isinstance(member, Field):
        raise TypeError(
            f"a scalar value can only override a scalar field: {name}; "
            "use a Field, Schema, Union, Array, Optional, or Block value"
        )
    if isinstance(effective, Field):
        original = member if isinstance(member, Field) else effective
        return _evaluate_field(name, original, overrides, defaults)
    if isinstance(effective, _SchemaInstance):
        result = _evaluate_concrete_schema(
            cast(type[Schema], type(effective)),
            effective.overrides,
            defaults,
        )
        # A decorated, length-delimited Block exposes its nested Schema as the
        # override constructor. Preserve that wrapper's declared boundary.
        if (
            isinstance(member, BlockField)
            and member.length_spec is not None
        ):
            return replace(
                result,
                length=_evaluate_length(member.length_spec, overrides),
            )
        return result
    if isinstance(effective, (SchemaUnion, NamedUnion)):
        return _evaluate_schema_union(effective, defaults, overrides)
    if isinstance(effective, ArrayField):
        return _evaluate_array(name, effective, overrides, defaults)
    if isinstance(effective, OptionalField):
        return _evaluate_optional(name, effective, overrides, defaults)
    return _evaluate_block(effective, defaults, overrides)


def _evaluate_schema_union(
    union: SchemaUnion | NamedUnion,
    defaults: SchemaDefaults,
    reference_overrides: Mapping[str, Override] | None = None,
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
            alternatives.append(_evaluate_field(
                name, alternative, {}, defaults, reference_overrides=reference_overrides
            ))
        elif isinstance(alternative, BlockField):
            alternatives.append(replace(_evaluate_block(
                alternative, defaults, reference_overrides, scope_depth=2,
            ), name=name))
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
    parent_overrides: Mapping[str, Override] | None = None,
    *,
    scope_depth: int = 1,
) -> SchemaResult:
    fields: dict[
        str,
        FieldResult | SchemaResult | UnionResult | ArrayResult | OptionalResult,
    ] = {}
    overrides = _EvaluationOverrides(
        block.overrides, block.fields, parent=parent_overrides, scope_depth=scope_depth,
    )
    for name, member in block.fields.items():
        fields[name] = _evaluate_schema_member(
            name,
            member,
            overrides,
            defaults,
        )

    return SchemaResult(
        "Block",
        MappingProxyType(fields),
        _computed_scope_tokens=(block._scope_token,),
        length=(
            _evaluate_length(block.length_spec, parent_overrides if parent_overrides is not None else overrides)
            if block.length_spec is not None
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
    if isinstance(count, int) and count > 2147483647:
        raise ValueError("array count must not exceed Peach's limit of 2147483647")
    element = array.element
    if isinstance(element, Field):
        evaluated: FieldResult | SchemaResult | UnionResult | ArrayResult = (
            _evaluate_field("item", element, {}, defaults, reference_overrides=overrides)
        )
    elif isinstance(element, _SchemaInstance):
        evaluated = _evaluate_concrete_schema(
            cast(type[Schema], type(element)),
            element.overrides,
            defaults,
        )
    elif isinstance(element, (SchemaUnion, NamedUnion)):
        evaluated = _evaluate_schema_union(element, defaults, overrides)
    else:
        evaluated = _evaluate_array("item", element, overrides, defaults)
    return ArrayResult(name, evaluated, count)


def _evaluate_optional(
    name: str,
    optional: OptionalField[object],
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
            reference_overrides=overrides,
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
            element = _evaluate_schema_union(source, defaults, overrides)
        else:
            element = _evaluate_array("item", source, overrides, defaults)
    if optional.condition is None:
        return ArrayResult(name, element, Occurs(0, 1), optional=True)
    return OptionalResult(name, element, _evaluate_expr(optional.condition, overrides))


def _evaluate_field(
    name: str,
    original: AnyField,
    overrides: Mapping[str, Override],
    defaults: SchemaDefaults,
    *,
    reference_overrides: Mapping[str, Override] | None = None,
) -> FieldResult:
    override = overrides.get(name)
    field = override if isinstance(override, Field) else original
    constant = _field_constant(field, override)
    if isinstance(override, Fixed):
        if isinstance(override.value, bool):
            raise TypeError(
                f"bool is not a supported Peach DSL scalar value: {name}; use 0 or 1"
            )
        constant = override
    raw_value = field.options.get("value")
    if isinstance(raw_value, bool) or isinstance(override, bool):
        raise TypeError(
            f"bool is not a supported Peach DSL scalar value: {name}; use 0 or 1"
        )
    value = raw_value if isinstance(raw_value, (int, float, str, bytes)) else None
    computed_value = (
        cast(Computed[ScalarValue], raw_value)
        if isinstance(raw_value, Computed)
        else None
    )
    if isinstance(override, (Fixed, int, float, str, bytes)):
        computed_value = None
    if isinstance(override, (int, float, str, bytes)):
        value = override

    raw_length = field.options.get("length")
    length: int | FieldReference | ExprResult | None = None
    if raw_length is not None:
        length = _evaluate_length(
            cast(Length, raw_length),
            overrides if reference_overrides is None else reference_overrides,
        )

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
    if raw_size is not None and (isinstance(raw_size, bool) or not isinstance(raw_size, int)):
        raise TypeError(f"a field size must be an int: {name}")
    if floating and raw_size not in (32, 64):
        raise ValueError(f"a Double field size must be 32 or 64 bits: {name}")

    return FieldResult(
        name,
        field.kind,
        constant,
        value,
        computed_value,
        length,
        None,
        signed,
        endian,
        raw_size,
        _evaluate_peach_field_attributes(field),
        definition=original,
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
            if option == "pad_character" and value == "\0":
                # Peach's default is NUL; XML cannot carry that character.
                continue
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


def _field_constant(
    field: AnyField, override: Override | None = None
) -> Fixed[ScalarValue] | None:
    if isinstance(override, (int, float, str, bytes)):
        # A plain override supplies a mutable default, replacing a declaration's
        # fixed value. References must stay dynamic instead of folding the old constant.
        return None
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
    value = _evaluate_length_value(length, overrides)
    if isinstance(value, bool) or not isinstance(value, (int, FieldReference, ExprResult)):
        raise TypeError("a length or count must be an int, not bool or another scalar type")
    if isinstance(value, int) and value < 0:
        raise ValueError("a length or count must be non-negative")
    if isinstance(value, FieldReference):
        _validate_integer_length_expression(value)
    return value


def _field_requires_runtime_reference(field: AnyField) -> bool:
    # Encoded XML text is not the runtime scalar (e.g. hex "12" is 18).
    # Disabling token matching also allows a different value to be parsed.
    if field.options.get("token") is False:
        return True
    if field.kind in ("string", "str") and (
        field.options.get("length") is not None or field.options.get("null_terminated") is True
    ):
        # Peach pads bounded strings and can retain terminators after cracking.
        # Their runtime text need not equal the original Python literal.
        return True
    value_type = field.options.get("value_type", "string")
    return isinstance(value_type, str) and value_type.lower() != "string"


def _validate_integer_length_expression(value: EvaluatedExprOperand) -> None:
    if isinstance(value, ExprResult):
        if value.operation not in ("+", "-", "*", "//", "%", "&", "|", "^", "<<", ">>"):
            raise TypeError("a length expression must use integer arithmetic")
        _validate_integer_length_expression(value.left)
        _validate_integer_length_expression(value.right)
        if value.operation in ("//", "%", "<<", ">>"):
            divisor = value.right
            if isinstance(divisor, ExprResult) and not _expr_result_has_field(divisor):
                divisor = _evaluate_constant_expr_result(divisor)
            if isinstance(divisor, int):
                if value.operation in ("//", "%") and divisor == 0:
                    raise ValueError("a length expression cannot divide or modulo by zero")
                if value.operation in ("<<", ">>") and divisor < 0:
                    raise ValueError("a length expression cannot use a negative shift count")
    elif isinstance(value, FieldReference):
        if value._value_kind in ("string", "str", "blob", "bytes", "double"):
            raise TypeError(
                f"a length must reference an integer field: {value.name!r} "
                f"has type {value._value_kind!r}"
            )
    elif (
        isinstance(value, bool) or not isinstance(value, int)
    ):
        raise TypeError("a length expression must use integer constants")


def _evaluate_length_value(
    length: Length,
    overrides: Mapping[str, Override],
) -> int | FieldReference | ExprResult:
    if isinstance(length, int):
        return length
    if isinstance(length, Fixed):
        return length.value
    if isinstance(length, Expr):
        evaluated = _evaluate_expr(length, overrides)
        _validate_integer_length_expression(evaluated)
        if _expr_result_has_field(evaluated):
            return evaluated
        constant = _evaluate_constant_expr_result(evaluated)
        if isinstance(constant, bool) or not isinstance(constant, int):
            raise TypeError("a field length expression must evaluate to an int")
        return constant
    if isinstance(length, MemberRef):
        result = _evaluate_member_ref(length, overrides)
        if not isinstance(result, (int, FieldReference)):
            raise TypeError("a field length must be an int")
        return result

    overrides, scope_up = _reference_scope(length, overrides)
    referenced = _resolve_reference_field(length, overrides)
    if referenced.name is None:
        raise ValueError("the referenced length field has no name")

    override = _reference_override(referenced, overrides)
    if isinstance(override, Fixed) and not _field_requires_runtime_reference(referenced):
        if not isinstance(override.value, int):
            raise TypeError("a field length must be an int")
        return override.value
    if isinstance(override, Field):
        constant = _field_constant(override)
        relation_field = override
    else:
        constant = _field_constant(referenced, override)
        relation_field = referenced
    if constant is not None and not _field_requires_runtime_reference(relation_field):
        if not isinstance(constant.value, int):
            raise TypeError("a field length must be an int")
        return constant.value
    return FieldReference(
        referenced.name, _field_value_cast(relation_field), definition=referenced,
        _value_kind=relation_field.kind, _scope_up=scope_up,
    )


def _evaluate_expr(
    expr: Expr,
    overrides: Mapping[str, Override],
) -> ExprResult:
    return ExprResult(
        expr.operation,
        _evaluate_expr_operand(expr.left, overrides),
        _evaluate_expr_operand(expr.right, overrides),
    )


def _expr_result_has_field(expr: ExprResult) -> bool:
    return any(
        isinstance(operand, FieldReference)
        or isinstance(operand, ExprResult) and _expr_result_has_field(operand)
        for operand in (expr.left, expr.right)
    )


def _evaluate_constant_expr_result(expr: ExprResult) -> ConstraintLiteral:
    def value(operand: EvaluatedExprOperand) -> ConstraintLiteral:
        if isinstance(operand, ExprResult):
            return _evaluate_constant_expr_result(operand)
        if isinstance(operand, FieldReference):
            raise ValueError("a constant expression cannot reference a field")
        return operand

    left = value(expr.left)
    right = value(expr.right)
    operations: Mapping[str, Callable[[ConstraintLiteral, ConstraintLiteral], ConstraintLiteral]] = {
        "+": lambda a, b: a + b,  # type: ignore[operator]
        "-": lambda a, b: a - b,  # type: ignore[operator]
        "*": lambda a, b: a * b,  # type: ignore[operator]
        "//": lambda a, b: a // b,  # type: ignore[operator]
        "%": lambda a, b: a % b,  # type: ignore[operator]
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b,  # type: ignore[operator]
        "<=": lambda a, b: a <= b,  # type: ignore[operator]
        ">": lambda a, b: a > b,  # type: ignore[operator]
        ">=": lambda a, b: a >= b,  # type: ignore[operator]
        "&": lambda a, b: a & b,  # type: ignore[operator]
        "|": lambda a, b: a | b,  # type: ignore[operator]
        "^": lambda a, b: a ^ b,  # type: ignore[operator]
        "<<": lambda a, b: a << b,  # type: ignore[operator]
        ">>": lambda a, b: a >> b,  # type: ignore[operator]
    }
    operation = operations.get(expr.operation)
    if operation is None:
        raise ValueError(
            f"unsupported constant field length operation: {expr.operation!r}"
        )
    return cast(ConstraintLiteral, operation(left, right))


def _evaluate_expr_operand(
    operand: ExprOperand,
    overrides: Mapping[str, Override],
) -> EvaluatedExprOperand:
    if isinstance(operand, Expr):
        return _evaluate_expr(operand, overrides)
    if isinstance(operand, MemberRef):
        return _evaluate_member_ref(operand, overrides)
    if isinstance(operand, Field):
        return _evaluate_expr_field(operand, overrides)
    if isinstance(operand, Fixed):
        return operand.value
    if isinstance(operand, _ConstraintNode):
        raise TypeError("constraint value cannot be used as a schema expression")
    return operand


def _evaluate_expr_field(
    referenced: AnyField,
    overrides: Mapping[str, Override],
) -> ConstraintLiteral | FieldReference:
    overrides, scope_up = _reference_scope(referenced, overrides)
    referenced = _resolve_reference_field(referenced, overrides)
    if referenced.name is None:
        raise ValueError("the referenced expression field has no name")

    override = _reference_override(referenced, overrides)
    if isinstance(override, Fixed) and not _field_requires_runtime_reference(referenced):
        value = override.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    field = override if isinstance(override, Field) else referenced
    constant = _field_constant(field, override)
    if constant is not None and not _field_requires_runtime_reference(field):
        value = constant.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    return FieldReference(
        referenced.name, _field_value_cast(field), definition=referenced,
        _value_kind=field.kind, _scope_up=scope_up,
    )


def _evaluate_member_ref(
    member: MemberRef[ScalarValue],
    overrides: Mapping[str, Override] | None = None,
) -> ConstraintLiteral | FieldReference:
    # Inline blocks have their own overrides, but a bound member may belong
    # to an enclosing schema instance. Select its scope by identity so local
    # containers with the same name cannot intercept the reference.
    current = overrides
    scope_up = 0
    while isinstance(current, _EvaluationOverrides):
        root_name = member.path[0]
        root = member.instance.binding_root
        if current.fields.get(root_name) is root or (
            current.schema is not None and any(
                base.__dict__.get(root_name) is root for base in current.schema.__mro__
            )
        ):
            overrides = current
            break
        scope_up += current.scope_depth
        current = current.parent
    else:
        scope_up = 0
    if isinstance(overrides, _EvaluationOverrides):
        root_name = member.path[0]
        declared = overrides.fields.get(root_name)
        replacement = overrides.get(root_name, declared)
        root = member.instance.binding_root
        inherited_root = overrides.schema is not None and any(
            base.__dict__.get(root_name) is root
            for base in overrides.schema.__mro__
        )
        if inherited_root and declared is None:
            raise ValueError(
                f"cannot resolve referenced field {member.name!r}: "
                f"{root_name!r} is absent from the target schema"
            )
        if (declared is root or inherited_root) and isinstance(replacement, Field):
            raise ValueError(
                f"cannot resolve referenced field {member.name!r}: "
                f"{root_name!r} was replaced by a scalar field"
            )
        if (
            (declared is root or inherited_root)
            and isinstance(replacement, _SchemaInstance)
        ):
            bound: object = replacement.bind_to(replacement, (root_name,))
            for part in member.path[1:]:
                if (
                    isinstance(bound, BoundArrayField)
                    and isinstance(bound.field.element, ArrayField)
                    and part == "item"
                ):
                    bound = bound.internal()
                    continue
                while isinstance(bound, BoundArrayField):
                    bound = bound.internal()
                if isinstance(bound, _SchemaInstance):
                    original = bound.__schema_fields__.get(part)
                    effective = bound.overrides.get(part, original)
                    if isinstance(original, _SchemaInstance) and isinstance(effective, Field):
                        raise ValueError(
                            f"cannot resolve referenced field {member.name!r}: "
                            f"{part!r} was replaced by a scalar field"
                        )
                try:
                    bound = getattr(bound, part)
                except AttributeError as exc:
                    raise ValueError(
                        f"cannot resolve referenced field {member.name!r}: "
                        "field is absent from the replacement schema"
                    ) from exc
            if not isinstance(bound, MemberRef):
                raise ValueError(
                    f"cannot resolve referenced field {member.name!r}: "
                    "replacement target is not a scalar field"
                )
            member = bound
    name = member.path[-1]
    override = (
        member.instance.overrides.get(name)
        if member.instance.__schema_fields__.get(name) is member.definition
        else None
    )
    declared = member.instance.__schema_fields__.get(name)
    if isinstance(declared, OptionalField):
        replacement = member.instance.overrides.get(name)
        optional = replacement if isinstance(replacement, OptionalField) else declared
        if optional.element is member.definition:
            override = optional.constraint
    _validate_scalar_reference_override(member.name, override)
    if isinstance(override, Fixed) and not _field_requires_runtime_reference(member.definition):
        value = override.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    field = override if isinstance(override, Field) else member.definition
    constant = _field_constant(field, override)
    if constant is not None and not _field_requires_runtime_reference(field):
        value = constant.value
        if isinstance(value, bytes):
            raise TypeError("an expression field value cannot be bytes")
        return value
    return FieldReference(
        member.name,
        _field_value_cast(field),
        absolute=True,
        definition=member.definition,
        _value_kind=field.kind,
        _scope_up=scope_up,
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
