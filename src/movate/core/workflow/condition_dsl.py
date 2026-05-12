"""Tiny JSONPath-like expression DSL for workflow conditional edges.

Used by the LangGraph compiler to route between branches based on the
workflow state. Operators write expressions like::

    edges:
      - from: classify
        to: needs_review
        kind: conditional
        when: "$.score < 0.7"
      - from: classify
        to: auto_approve
        kind: conditional
        when: "$.score >= 0.7 && $.confidence > 0.9"
      - from: classify
        to: fallback
        kind: conditional
        when: null            # default ("else") — required exactly once per source

The DSL is deliberately small so it can be parsed + evaluated without
any third-party dep and without giving operators access to a Python
sandbox-escape vector. ``eval`` is never called.

Supported syntax (v1.1)
-----------------------

* JSONPath: ``$.a.b.c`` resolves ``state["a"]["b"]["c"]``; missing keys
  evaluate to ``None``. No bracket / wildcard / filter notation.
* Comparisons: ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``.
* Boolean ops: ``&&``, ``||``, ``!``. Standard precedence (NOT > AND > OR).
* Membership: ``X in [a, b, c]`` — true iff ``X`` equals one of the
  literals. Right-hand side must be a literal list (no JSONPath inside).
* Literals: numbers (int, float), strings (single OR double quoted),
  booleans (``true`` / ``false``), ``null``.
* Parentheses for grouping.

Explicitly NOT supported (v1.2+):

* Arithmetic (``+``, ``-``, ``*``, ``/``) — defer until a real use case.
* Regex (``=~``) — defer; operators can pre-classify in an agent.
* Function calls (``len(...)``, ``startswith(...)``) — defer.
* JSONPath bracket / filter / wildcard — defer; flat ``$.a.b`` covers v1.1.

Errors are surfaced as :class:`ConditionParseError` (compile-time) and
:class:`ConditionEvalError` (runtime). The compiler validates every
edge's expression at workflow load so syntax errors fail fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConditionParseError(Exception):
    """Raised when the expression fails to parse. Caller maps to
    :class:`WorkflowCompileError` so workflow load surfaces a clean message."""


class ConditionEvalError(Exception):
    """Raised when an expression parses but fails at runtime — e.g. comparing
    an ``int`` to a ``str``. Caller usually doesn't catch; the workflow
    halts with the error visible in the FailureRecord."""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # "JSONPATH" | "NUMBER" | "STRING" | "BOOL" | "NULL" | "OP" | "LBRACK" | ...
    value: Any


# Single source of truth for the keyword / operator vocabulary. Order matters
# for the longest-match regex below — multi-char operators must come before
# their single-char prefixes (e.g. ``<=`` before ``<``).
_TOKEN_PATTERNS = [
    (r"\s+", None),  # skip whitespace
    (r"\$(?:\.[A-Za-z_][A-Za-z0-9_]*)+", "JSONPATH"),
    (r"\b(?:true|false)\b", "BOOL"),
    (r"\bnull\b", "NULL"),
    (r"\bin\b", "IN"),
    (r"-?\d+\.\d+", "NUMBER_FLOAT"),
    (r"-?\d+", "NUMBER_INT"),
    (r'"(?:[^"\\]|\\.)*"', "STRING_DQ"),
    (r"'(?:[^'\\]|\\.)*'", "STRING_SQ"),
    (r"==|!=|<=|>=|&&|\|\||<|>|!", "OP"),
    (r"\(", "LPAREN"),
    (r"\)", "RPAREN"),
    (r"\[", "LBRACK"),
    (r"\]", "RBRACK"),
    (r",", "COMMA"),
]


def _tokenize(expr: str) -> list[_Token]:  # noqa: PLR0912 — one branch per token kind
    tokens: list[_Token] = []
    pos = 0
    while pos < len(expr):
        for pattern, kind in _TOKEN_PATTERNS:
            m = re.match(pattern, expr[pos:])
            if not m:
                continue
            text = m.group()
            pos += len(text)
            if kind is None:
                break
            if kind == "JSONPATH":
                tokens.append(_Token("JSONPATH", text[2:].split(".")))
            elif kind == "BOOL":
                tokens.append(_Token("BOOL", text == "true"))
            elif kind == "NULL":
                tokens.append(_Token("NULL", None))
            elif kind == "IN":
                tokens.append(_Token("IN", "in"))
            elif kind == "NUMBER_FLOAT":
                tokens.append(_Token("NUMBER", float(text)))
            elif kind == "NUMBER_INT":
                tokens.append(_Token("NUMBER", int(text)))
            elif kind in {"STRING_DQ", "STRING_SQ"}:
                # Strip quotes; decode standard escapes via Python's own
                # literal parser. This is safe — `unicode_escape` only
                # interprets `\n` / `\t` / `\xNN` etc., not code.
                inner = text[1:-1]
                tokens.append(_Token("STRING", inner.encode("utf-8").decode("unicode_escape")))
            else:
                tokens.append(_Token(kind, text))
            break
        else:
            raise ConditionParseError(
                f"unexpected character {expr[pos]!r} at column {pos} in {expr!r}"
            )
    return tokens


# ---------------------------------------------------------------------------
# Parser — recursive descent, builds an AST of nested dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Lit:
    value: Any


@dataclass(frozen=True)
class _JsonPath:
    path: list[str]  # ["score"], ["user", "name"], ...


@dataclass(frozen=True)
class _Cmp:
    op: str  # "==" / "!=" / "<" / "<=" / ">" / ">=" / "in"
    left: Any
    right: Any


@dataclass(frozen=True)
class _BoolOp:
    op: str  # "&&" / "||"
    left: Any
    right: Any


@dataclass(frozen=True)
class _Not:
    operand: Any


@dataclass(frozen=True)
class _List:
    items: list[Any]


class _Parser:
    def __init__(self, tokens: list[_Token], source: str) -> None:
        self._tokens = tokens
        self._pos = 0
        self._source = source

    def _peek(self) -> _Token | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _eat(self, kind: str, value: Any = None) -> _Token:
        tok = self._peek()
        if tok is None:
            raise ConditionParseError(
                f"unexpected end of expression in {self._source!r}; expected {kind}"
            )
        if tok.kind != kind or (value is not None and tok.value != value):
            raise ConditionParseError(
                f"expected {kind}={value!r} at token {self._pos}, "
                f"got {tok.kind}={tok.value!r} in {self._source!r}"
            )
        self._pos += 1
        return tok

    # expr ::= or_expr
    def parse(self) -> Any:
        node = self._or_expr()
        if self._pos != len(self._tokens):
            tok = self._tokens[self._pos]
            raise ConditionParseError(
                f"trailing tokens after expression: {tok.kind}={tok.value!r} in {self._source!r}"
            )
        return node

    # or_expr ::= and_expr ('||' and_expr)*
    def _or_expr(self) -> Any:
        left = self._and_expr()
        while (
            (tok := self._peek()) is not None and tok.kind == "OP" and tok.value == "||"
        ):
            self._pos += 1
            right = self._and_expr()
            left = _BoolOp(op="||", left=left, right=right)
        return left

    # and_expr ::= not_expr ('&&' not_expr)*
    def _and_expr(self) -> Any:
        left = self._not_expr()
        while (
            (tok := self._peek()) is not None and tok.kind == "OP" and tok.value == "&&"
        ):
            self._pos += 1
            right = self._not_expr()
            left = _BoolOp(op="&&", left=left, right=right)
        return left

    # not_expr ::= '!' not_expr | cmp_expr
    def _not_expr(self) -> Any:
        tok = self._peek()
        if tok is not None and tok.kind == "OP" and tok.value == "!":
            self._pos += 1
            return _Not(operand=self._not_expr())
        return self._cmp_expr()

    # cmp_expr ::= operand (cmp_op operand)?
    # cmp_op   ::= '==' | '!=' | '<' | '<=' | '>' | '>=' | 'in'
    def _cmp_expr(self) -> Any:
        left = self._operand()
        nxt = self._peek()
        if nxt is None:
            return left
        if nxt.kind == "OP" and nxt.value in {"==", "!=", "<", "<=", ">", ">="}:
            op = nxt.value
            self._pos += 1
            right = self._operand()
            return _Cmp(op=op, left=left, right=right)
        if nxt.kind == "IN":
            self._pos += 1
            right = self._list_literal()
            return _Cmp(op="in", left=left, right=right)
        return left

    # operand ::= jsonpath | literal | '(' expr ')'
    def _operand(self) -> Any:
        tok = self._peek()
        if tok is None:
            raise ConditionParseError(
                f"unexpected end of expression while parsing operand in {self._source!r}"
            )
        if tok.kind == "JSONPATH":
            self._pos += 1
            return _JsonPath(path=tok.value)
        if tok.kind == "NUMBER":
            self._pos += 1
            return _Lit(value=tok.value)
        if tok.kind == "STRING":
            self._pos += 1
            return _Lit(value=tok.value)
        if tok.kind == "BOOL":
            self._pos += 1
            return _Lit(value=tok.value)
        if tok.kind == "NULL":
            self._pos += 1
            return _Lit(value=None)
        if tok.kind == "LPAREN":
            self._pos += 1
            node = self._or_expr()
            self._eat("RPAREN")
            return node
        raise ConditionParseError(
            f"unexpected token {tok.kind}={tok.value!r} while parsing operand in {self._source!r}"
        )

    # list_literal ::= '[' (literal (',' literal)*)? ']'
    def _list_literal(self) -> _List:
        self._eat("LBRACK")
        items: list[Any] = []
        nxt = self._peek()
        if nxt is not None and nxt.kind != "RBRACK":
            items.append(self._literal_only())
            while (tok := self._peek()) is not None and tok.kind == "COMMA":
                self._pos += 1
                items.append(self._literal_only())
        self._eat("RBRACK")
        return _List(items=items)

    def _literal_only(self) -> Any:
        """List members must be literals — no JSONPath on the right of `in`."""
        tok = self._peek()
        if tok is None or tok.kind not in {"NUMBER", "STRING", "BOOL", "NULL"}:
            raise ConditionParseError(
                f"list members must be literal numbers / strings / booleans / null; "
                f"got {tok.kind if tok else 'EOF'} in {self._source!r}"
            )
        return self._operand()


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _lookup(path: list[str], state: dict[str, Any]) -> Any:
    """Walk ``state`` via the JSONPath segments. Missing keys → ``None``
    rather than KeyError; that's the v1.1 contract (operators can write
    ``$.maybe_missing == null`` to test for absence)."""
    cur: Any = state
    for seg in path:
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _eval(node: Any, state: dict[str, Any]) -> Any:  # noqa: PLR0912 — one branch per AST node
    if isinstance(node, _Lit):
        return node.value
    if isinstance(node, _JsonPath):
        return _lookup(node.path, state)
    if isinstance(node, _List):
        return [_eval(item, state) for item in node.items]
    if isinstance(node, _Not):
        return not _eval(node.operand, state)
    if isinstance(node, _BoolOp):
        # Short-circuit semantics so a malformed RHS isn't evaluated when
        # the LHS already decides the result.
        if node.op == "&&":
            return bool(_eval(node.left, state)) and bool(_eval(node.right, state))
        if node.op == "||":
            return bool(_eval(node.left, state)) or bool(_eval(node.right, state))
        raise ConditionEvalError(f"unknown boolean op {node.op!r}")
    if isinstance(node, _Cmp):
        left = _eval(node.left, state)
        right = _eval(node.right, state)
        try:
            if node.op == "==":
                return left == right
            if node.op == "!=":
                return left != right
            if node.op == "<":
                return left < right
            if node.op == "<=":
                return left <= right
            if node.op == ">":
                return left > right
            if node.op == ">=":
                return left >= right
            if node.op == "in":
                if not isinstance(right, list):
                    raise ConditionEvalError(
                        f"right operand of `in` must be a list; got {type(right).__name__}"
                    )
                return left in right
        except TypeError as exc:
            raise ConditionEvalError(f"type error in comparison {node.op!r}: {exc}") from exc
        raise ConditionEvalError(f"unknown comparison op {node.op!r}")
    raise ConditionEvalError(f"unknown AST node: {type(node).__name__}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledCondition:
    """A parsed expression ready to evaluate against a state dict.

    Construct via :func:`parse_condition`; call :meth:`evaluate` per
    routing decision. The :attr:`source` is the original string —
    useful for error messages downstream.
    """

    source: str
    _ast: Any  # opaque AST root; consumers shouldn't inspect

    def evaluate(self, state: dict[str, Any]) -> bool:
        """Evaluate the expression against ``state``. Returns a bool —
        the top-level expression's truthiness drives the routing
        decision. Sub-expressions can be any type."""
        try:
            return bool(_eval(self._ast, state))
        except ConditionEvalError:
            raise
        except Exception as exc:
            # Defensive: turn any unexpected runtime error into a typed one
            # so the compiler / runner only has one exception class to
            # catch for "the condition couldn't be evaluated."
            raise ConditionEvalError(f"runtime error in {self.source!r}: {exc}") from exc


def parse_condition(expr: str) -> CompiledCondition:
    """Parse a condition expression into a compiled, reusable form.

    Raises :class:`ConditionParseError` on malformed input. The caller
    typically calls this at workflow load (compile_workflow) so a typo
    in YAML fails fast rather than at routing time.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ConditionParseError(f"empty condition expression: {expr!r}")
    tokens = _tokenize(expr)
    ast = _Parser(tokens, source=expr).parse()
    return CompiledCondition(source=expr, _ast=ast)


__all__ = [
    "CompiledCondition",
    "ConditionEvalError",
    "ConditionParseError",
    "parse_condition",
]
