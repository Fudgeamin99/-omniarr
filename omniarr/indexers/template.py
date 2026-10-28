"""A small Go text/template interpreter covering what Cardigann definitions use.

Supports: {{ .Field.Path }}, $vars, string/number literals, pipelines, parentheses,
if / else if / else / end, range (with $i, $e :=), with, variable assignment,
trim markers {{- -}}, comments, and functions: and or not eq ne lt le gt ge len index
join re_replace print printf html urlquery.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any
from urllib.parse import quote_plus

from .netregex import compile_net, net_replacement


class TemplateError(Exception):
    pass


# ------------------------------------------------------------------ lexing --
_ACTION = re.compile(r"\{\{(-\s)?(.*?)(\s-)?\}\}", re.S)


def _segments(src: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _ACTION.finditer(src):
        text = src[pos:m.start()]
        if m.group(1):
            text = text.rstrip()
        if out and out[-1][0] == "trim_next":
            out.pop()
            text = text.lstrip()
        out.append(("text", text))
        body = m.group(2).strip()
        out.append(("action", body))
        if m.group(3):
            out.append(("trim_next", ""))
        pos = m.end()
    tail = src[pos:]
    if out and out[-1][0] == "trim_next":
        out.pop()
        tail = tail.lstrip()
    out.append(("text", tail))
    return [s for s in out if not (s[0] == "text" and s[1] == "")]


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<raw>`[^`]*`)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<assign>:=|=)
  | (?P<var>\$[A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)
  | (?P<field>(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)+|\.)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<lp>\()
  | (?P<rp>\))
  | (?P<pipe>\|)
  | (?P<comma>,)
""", re.X)


def _unquote(s: str) -> str:
    body = s[1:-1]
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(n, "\\" + n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _lex(expr: str) -> list[tuple[str, str]]:
    toks, pos = [], 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise TemplateError(f"bad token in {{{{ {expr} }}}} at {pos}")
        kind = m.lastgroup
        pos = m.end()
        if kind == "ws":
            continue
        toks.append((kind, m.group(kind)))
    return toks


# ---------------------------------------------------------------- parsing --
class Node:
    pass


class Text(Node):
    def __init__(self, s): self.s = s


class Action(Node):
    def __init__(self, pipe, decl=None):
        self.pipe, self.decl = pipe, decl


class If(Node):
    def __init__(self):
        self.branches: list[tuple[Any, list]] = []
        self.else_: list = []


class Range(Node):
    def __init__(self, pipe, keyvar, valvar):
        self.pipe, self.keyvar, self.valvar = pipe, keyvar, valvar
        self.body: list = []
        self.else_: list = []


class With(Node):
    def __init__(self, pipe):
        self.pipe = pipe
        self.body: list = []
        self.else_: list = []


def _parse_pipeline(tokens: list) -> Any:
    """pipeline := command ('|' command)* ; command := operand+"""
    cmds, cur, depth = [], [], 0
    for t in tokens:
        if t[0] == "lp":
            depth += 1
        elif t[0] == "rp":
            depth -= 1
        if t[0] == "pipe" and depth == 0:
            cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    cmds.append(cur)
    return [_parse_command(c) for c in cmds if c]


def _parse_command(tokens: list) -> list:
    args, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t[0] == "lp":
            depth, j = 1, i + 1
            while j < len(tokens) and depth:
                if tokens[j][0] == "lp":
                    depth += 1
                elif tokens[j][0] == "rp":
                    depth -= 1
                j += 1
            args.append(("pipe", _parse_pipeline(tokens[i + 1:j - 1])))
            i = j
            continue
        if t[0] == "str":
            args.append(("lit", _unquote(t[1])))
        elif t[0] == "raw":
            args.append(("lit", t[1][1:-1]))
        elif t[0] == "num":
            args.append(("lit", float(t[1]) if "." in t[1] else int(t[1])))
        elif t[0] in ("field", "var"):
            args.append((t[0], t[1]))
        elif t[0] == "ident":
            if t[1] in ("true", "false"):
                args.append(("lit", t[1] == "true"))
            elif t[1] == "nil":
                args.append(("lit", None))
            else:
                args.append(("ident", t[1]))
        elif t[0] == "comma":
            pass
        else:
            raise TemplateError(f"unexpected {t}")
        i += 1
    return args


def _split_decl(tokens):
    """Detect '$a, $b := pipe' or '$a := pipe'."""
    for idx, t in enumerate(tokens):
        if t[0] == "assign":
            names = [x[1] for x in tokens[:idx] if x[0] == "var"]
            return names, tokens[idx + 1:]
    return None, tokens


def parse(src: str) -> list:
    root: list = []
    stack: list[tuple[Any, list]] = [(None, root)]
    for kind, body in _segments(src):
        container = stack[-1][1]
        if kind == "text":
            container.append(Text(body))
            continue
        if body.startswith("/*"):
            continue
        word = body.split(None, 1)[0] if body else ""
        rest = body[len(word):].strip()
        if word == "if":
            node = If()
            node.branches.append((_parse_pipeline(_lex(rest)), []))
            container.append(node)
            stack.append((node, node.branches[-1][1]))
        elif word == "else":
            owner = stack[-1][0]
            stack.pop()
            if rest.startswith("if "):
                if not isinstance(owner, If):
                    raise TemplateError("else if without if")
                owner.branches.append((_parse_pipeline(_lex(rest[3:])), []))
                stack.append((owner, owner.branches[-1][1]))
            else:
                if isinstance(owner, If):
                    stack.append((owner, owner.else_))
                elif isinstance(owner, (Range, With)):
                    stack.append((owner, owner.else_))
                else:
                    raise TemplateError("else without if")
        elif word == "end":
            if len(stack) == 1:
                raise TemplateError("unexpected end")
            stack.pop()
        elif word == "range":
            names, toks = _split_decl(_lex(rest))
            keyvar = valvar = None
            if names:
                if len(names) == 1:
                    valvar = names[0]
                else:
                    keyvar, valvar = names[0], names[1]
            node = Range(_parse_pipeline(toks), keyvar, valvar)
            container.append(node)
            stack.append((node, node.body))
        elif word == "with":
            node = With(_parse_pipeline(_lex(rest)))
            container.append(node)
            stack.append((node, node.body))
        else:
            names, toks = _split_decl(_lex(body))
            container.append(Action(_parse_pipeline(toks), names[0] if names else None))
    if len(stack) != 1:
        raise TemplateError("missing {{ end }}")
    return root


# -------------------------------------------------------------- evaluation --
def truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0
    if isinstance(v, (str, list, tuple, dict, set)):
        return len(v) > 0
    return bool(v)


def to_str(v: Any) -> str:
    if v is None:
        return ""
    if v is True:
        return "True"
    if v is False:
        return "False"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (list, tuple)):
        return "[" + " ".join(to_str(x) for x in v) + "]"
    return str(v)


def _lookup(base: Any, path: list[str]) -> Any:
    cur = base
    for p in path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            cur = getattr(cur, p, None)
    return cur


def _eq(a, b) -> bool:
    if type(a) is not type(b):
        return to_str(a) == to_str(b)
    return a == b


def _cmp(a, b):
    try:
        return (float(a) > float(b)) - (float(a) < float(b))
    except (TypeError, ValueError):
        sa, sb = to_str(a), to_str(b)
        return (sa > sb) - (sa < sb)


def _printf(fmt, *args):
    py = re.sub(r"%v", "%s", to_str(fmt))
    try:
        return py % tuple(args)
    except (TypeError, ValueError):
        return py


FUNCS = {
    "not": lambda a: not truthy(a),
    "eq": lambda a, *bs: any(_eq(a, b) for b in bs),
    "ne": lambda a, b: not _eq(a, b),
    "lt": lambda a, b: _cmp(a, b) < 0,
    "le": lambda a, b: _cmp(a, b) <= 0,
    "gt": lambda a, b: _cmp(a, b) > 0,
    "ge": lambda a, b: _cmp(a, b) >= 0,
    "len": lambda a: len(a) if a is not None else 0,
    "index": lambda a, *ks: _index(a, ks),
    "join": lambda a, sep="": to_str(sep).join(to_str(x) for x in (a or [])) if isinstance(a, (list, tuple))
    else to_str(a),
    "re_replace": lambda s, pat, rep: compile_net(to_str(pat)).sub(net_replacement(to_str(rep)), to_str(s)),
    "print": lambda *a: "".join(to_str(x) for x in a),
    "printf": _printf,
    "html": lambda a: _html.escape(to_str(a)),
    "urlquery": lambda *a: quote_plus("".join(to_str(x) for x in a)),
    "lower": lambda a: to_str(a).lower(),
    "upper": lambda a: to_str(a).upper(),
}


def _index(a, keys):
    for k in keys:
        if a is None:
            return None
        if isinstance(a, dict):
            a = a.get(k)
        else:
            try:
                a = a[int(k)]
            except (IndexError, ValueError, TypeError):
                return None
    return a


class _Ctx:
    def __init__(self, data: dict):
        self.root = data
        self.vars: list[dict] = [{"$": data}]

    def var(self, name: str) -> Any:
        parts = name.split(".")
        for scope in reversed(self.vars):
            if parts[0] in scope:
                return _lookup(scope[parts[0]], parts[1:])
        return None


def _eval_arg(arg, ctx: _Ctx, dot: Any) -> Any:
    kind, val = arg
    if kind == "lit":
        return val
    if kind == "field":
        if val == ".":
            return dot
        return _lookup(dot, val.strip(".").split("."))
    if kind == "var":
        return ctx.var(val)
    if kind == "pipe":
        return _eval_pipe(val, ctx, dot)
    if kind == "ident":
        fn = FUNCS.get(val)
        if fn is None:
            raise TemplateError(f"unknown function {val}")
        return fn()
    raise TemplateError(f"bad arg {arg}")


def _eval_cmd(cmd: list, ctx: _Ctx, dot: Any, piped: list) -> Any:
    head = cmd[0]
    if head[0] == "ident":
        name = head[1]
        if name in ("and", "or"):
            args = cmd[1:]
            vals_iter = (_eval_arg(a, ctx, dot) for a in args)
            last = None
            extra = piped
            for v in list(vals_iter) + extra:
                last = v
                if name == "and" and not truthy(v):
                    return v
                if name == "or" and truthy(v):
                    return v
            return last
        fn = FUNCS.get(name)
        if fn is None:
            raise TemplateError(f"unknown function {name}")
        args = [_eval_arg(a, ctx, dot) for a in cmd[1:]] + piped
        return fn(*args)
    if len(cmd) == 1 and not piped:
        return _eval_arg(head, ctx, dot)
    return _eval_arg(head, ctx, dot)


def _eval_pipe(pipe: list, ctx: _Ctx, dot: Any) -> Any:
    val = None
    for i, cmd in enumerate(pipe):
        val = _eval_cmd(cmd, ctx, dot, [val] if i > 0 else [])
    return val


def _render(nodes: list, ctx: _Ctx, dot: Any, out: list) -> None:
    for n in nodes:
        if isinstance(n, Text):
            out.append(n.s)
        elif isinstance(n, Action):
            v = _eval_pipe(n.pipe, ctx, dot)
            if n.decl:
                ctx.vars[-1][n.decl] = v
            else:
                out.append(to_str(v))
        elif isinstance(n, If):
            for cond, body in n.branches:
                if truthy(_eval_pipe(cond, ctx, dot)):
                    _render(body, ctx, dot, out)
                    break
            else:
                _render(n.else_, ctx, dot, out)
        elif isinstance(n, With):
            v = _eval_pipe(n.pipe, ctx, dot)
            if truthy(v):
                _render(n.body, ctx, v, out)
            else:
                _render(n.else_, ctx, dot, out)
        elif isinstance(n, Range):
            seq = _eval_pipe(n.pipe, ctx, dot)
            items = list(seq.items()) if isinstance(seq, dict) else list(enumerate(seq or []))
            if not items:
                _render(n.else_, ctx, dot, out)
                continue
            for k, v in items:
                scope = {}
                if n.keyvar:
                    scope[n.keyvar] = k
                if n.valvar:
                    scope[n.valvar] = v
                ctx.vars.append(scope)
                try:
                    _render(n.body, ctx, v, out)
                finally:
                    ctx.vars.pop()


_cache: dict[str, list] = {}


def render(src: Any, data: dict) -> str:
    if src is None:
        return ""
    src = str(src) if not isinstance(src, str) else src
    if "{{" not in src:
        return src
    tree = _cache.get(src)
    if tree is None:
        tree = parse(src)
        if len(_cache) > 5000:
            _cache.clear()
        _cache[src] = tree
    out: list[str] = []
    _render(tree, _Ctx(data), data, out)
    return "".join(out)
