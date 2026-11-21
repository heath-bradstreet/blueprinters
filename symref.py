"""Symbol-reference index: a sharp backstop for module-level changes.

WHY THIS EXISTS
---------------
Per-test line coverage is unreliable for a changed line that sits OUTSIDE any
function body (see tools/astinfo.py): module-level code runs exactly once, at
import, during pytest collection, so coverage credits it to no test or to one
arbitrary test. The shipped ladder (tools/select_v2.py) therefore backstops such
a change. For a dialect module the backstop is the dialect family, which is
tight. For a NON-dialect module it falls through to per-test-FILE coverage --
and because every one of the 58 test files imports e.g.
``sqlglot/expressions/ddl.py``, that returns ~1617 of 1620 tests. Effectively
run-all, 36.8 s, on commits whose truth is often zero failing tests.

THE IDEA
--------
A module-level edit changes the value or the shape of a *named thing*: a
``class``, a module constant, a ``def`` statement, an ``import`` binding. Code
can only observe that change by **reading the name**. So:

    tests that can fail  subset of  tests that execute a line referencing the name

This module builds, offline from the pinned tree, the index that makes the right
hand side computable:

  * ``defs``        every module-level binding site, as (line span -> names)
  * ``name_refs``   per file, bare ``Name`` loads, keyed by name  (module scope)
  * ``attr_refs``   every ``.name`` attribute occurrence, file-global
  * ``str_refs``    every string constant equal to a known name, or to its lower
                    or upper case form (sqlglot sets ``cls.key =
                    cls.__name__.lower()`` in expressions/core.py and dispatches
                    generator methods through it, so the lowercased class name
                    is a real reference)
  * ``imports``     ``from M import N`` edges, so a rebinding in another module
                    continues the closure
  * ``modlevel``    which lines of each file are module level, so a reference
                    site that is ITSELF in the coverage blind spot can be
                    recognised and handled rather than silently trusted
  * ``test_owners`` for each tests/*.py, the line span of every test callable,
                    so a reference inside a test body names that one test

PYTHON SCOPING IS THE PRECISION
-------------------------------
A name bound at module level in module ``M`` is a global of ``M`` and of nothing
else. A bare ``Name`` reference in another file refers to *that* file's binding,
not M's. So references are resolved scope-aware:

    refs(M, N) = bare Name loads of N inside M itself
               + every ``.N`` attribute access anywhere   (could be ``m.N``)
               + every string constant naming N           (dynamic dispatch)
               + transitively, refs(F, N') for every F doing ``from M import N``
                 (including ``import *``), with N' the name F binds

Without the scoping, ``from sqlglot import exp`` in one module would drag in all
17,276 references to the name ``exp`` in the repo and the backstop would be no
better than run-all.

COMPLIANCE (docs/compliance.md, trap 5)
---------------------------------------
The index is a pure function of sqlglot at the pinned commit. It is keyed by
source path, line number and identifier; nothing in it is keyed by patch text,
diff shape or "kind of edit", and no number in it was fitted to
sample/answers.json. It is buildable by anyone with the pinned checkout and no
patches, which is the test the rule actually asks: *could the artefact have been
built before the commits existed?* Output is deterministic -- sorted keys
throughout, no timestamps, no paths outside the tree.

WHAT IT IS NOT
--------------
It is **not sound**, and the honest list of holes is:

  1. A name reached by a *computed* lookup -- ``getattr(exp, some_var)`` -- has
     no textual reference. The string-constant table covers the literal cases
     only.
  2. Enumeration: ``subclasses(exp, Expression)`` reaches a class without ever
     naming it. A newly added or reshaped class can break such a test.
  3. A reference site that is itself at module level (sqlglot's declarative
     tables: ``PROPERTIES_LOCATION = {exp.TriggerProperties: ...}``) is in the
     same coverage blind spot as the change, so the line query there is
     untrustworthy. ``resolve(..., propagate=True)`` continues the closure
     through those sites; ``propagate=False`` reports them to the caller, which
     must then widen some other way.
  4. A changed module-level line that binds no name at all (a bare call, a
     docstring, ``if TYPE_CHECKING:``) is *unresolvable*: it is reported as such
     and the caller must fall back.

So this is a *tightening* device to be used behind an explicit fallback, exactly
like line coverage itself. tools/symref_eval.py measures whether it holds
recall.

USAGE
-----
    python3 tools/symref.py build <pristine-tree> <out.json[.gz]>
    python3 tools/symref.py show  <index> <sqlglot/path.py> <line> [line...]

API
---
    idx = SymRef(path)
    res = idx.resolve("sqlglot/expressions/ddl.py", [79, 80], propagate=False)
    res.src          -> {file: set(lines)}   function-body reference lines
    res.src_modlevel -> {file: set(lines)}   module-level reference lines
    res.test         -> {file: set(lines)}   reference lines in tests/*.py
    res.unresolved   -> set(lines)           changed lines that bind no name
    idx.tests_for_locations(res, line_tests=..., tests_in_file=..., test_num=...)
"""

from __future__ import annotations

import ast
import gzip
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

INDEX_VERSION = 1

__all__ = [
    "SymRef", "Resolution", "SymRefIncomplete", "build_index", "write_index",
    "INDEX_VERSION",
]


class SymRefIncomplete(Exception):
    """The index found a reference it cannot turn into a set of tests.

    Raised rather than returning a smaller answer, because a safety net that
    silently drops evidence is the most dangerous artefact we could ship. The
    caller must widen (fall back) when it sees this.
    """


# ==========================================================================
# offline build
# ==========================================================================


def _module_name(rel: str) -> str:
    """'sqlglot/optimizer/scope.py' -> 'sqlglot.optimizer.scope'."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _Scan:
    """One file's AST walked once: definitions, references, imports."""

    def __init__(self, rel: str, source: str, module_table: dict[str, str]):
        self.rel = rel
        self.module_table = module_table
        self.pkg = _module_name(rel)
        self.is_pkg_init = rel.endswith("/__init__.py")
        # definitions: (start, end, frozenset(names))
        self.defs: list[tuple[int, int, frozenset]] = []
        self.name_refs: dict[str, set[int]] = {}
        self.attr_refs: dict[str, set[int]] = {}
        self.str_refs: dict[str, set[int]] = {}
        # (source_file_rel, imported_name, bound_name, line)
        self.imports: list[tuple[str, str, str, int]] = []
        self.test_owners: list[tuple[int, int, str]] = []
        # (class_name, [last component of each base expression])
        self.bases: list[tuple[str, list[str]]] = []
        self.ok = True

        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError:
            self.ok = False
            return
        self._walk_defs(tree, ())
        self._walk_refs(tree)
        if rel.startswith("tests/"):
            self._walk_test_owners(tree)

    # ---- definitions ----------------------------------------------------
    #
    # Only bindings that execute at import time matter here, because only those
    # lines reach this backstop. We descend into class bodies and into module
    # level if/try/for/with, but NOT into function bodies: a binding inside a
    # function is not a module-level line and its coverage is trustworthy.

    def _span(self, node: ast.AST) -> tuple[int, int]:
        start = node.lineno
        for dec in getattr(node, "decorator_list", []) or []:
            start = min(start, dec.lineno)
        return start, getattr(node, "end_lineno", None) or node.lineno

    def _walk_defs(self, node: ast.AST, classes: tuple) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                start, end = self._span(child)
                # Everything inside a class body is reachable only through the
                # class, so the class names ARE the handle. Deliberately we do
                # not credit a class attribute's own name: `arg_types` is
                # referenced in a hundred generic places and would make the
                # backstop useless, while `x.arg_types` necessarily references
                # whatever produced `x` -- which is the class.
                names = frozenset(classes + (child.name,))
                self.defs.append((start, end, names))
                self.bases.append(
                    (child.name, sorted({_base_name(b) for b in child.bases} - {""}))
                )
                self._walk_defs(child, classes + (child.name,))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start, end = self._span(child)
                # The signature/decorator lines are module level; the body is
                # not and never queries this index.
                #
                # A METHOD's own name is deliberately NOT a handle, for the same
                # reason a class attribute's name is not: `obj.table` requires
                # obtaining `obj`, which requires the class. Crediting the
                # method name instead makes generic names catastrophic --
                # `Context.table` drew in all 178 `.table` attribute references
                # across 38 files and selected 1,143 tests where the class name
                # alone selects a handful. Inheritance is what makes dropping it
                # safe, and inheritance edges are recorded below.
                names = classes if classes else (child.name,)
                self.defs.append((start, end, frozenset(names)))
                # do NOT descend: a nested def's lines are function-body lines
            elif isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    child.targets
                    if isinstance(child, ast.Assign)
                    else [child.target]
                )
                bound: set[str] = set()
                for t in targets:
                    bound |= _target_names(t)
                names = set(classes)
                if not classes:
                    # a module global: its own name is the handle
                    names |= bound
                if names:
                    self.defs.append(
                        (
                            child.lineno,
                            getattr(child, "end_lineno", None) or child.lineno,
                            frozenset(names),
                        )
                    )
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                self._record_import(child, classes)
            elif isinstance(
                child, (ast.If, ast.Try, ast.For, ast.While, ast.With)
            ):
                self._walk_defs(child, classes)
            elif hasattr(child, "body") and not isinstance(child, ast.expr):
                self._walk_defs(child, classes)

    def _record_import(self, node, classes: tuple) -> None:
        line = node.lineno
        end = getattr(node, "end_lineno", None) or line
        bound: set[str] = set()
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name.split(".")[0]))
        else:
            src = self._resolve_from(node)
            for a in node.names:
                if a.name == "*":
                    if src:
                        self.imports.append((src, "*", "*", line))
                    continue
                bound.add(a.asname or a.name)
                if src:
                    self.imports.append(
                        (src, a.name, a.asname or a.name, line)
                    )
        names = set(classes)
        if not classes:
            names |= bound
        if names:
            self.defs.append((line, end, frozenset(names)))

    def _resolve_from(self, node: ast.ImportFrom) -> str | None:
        """Map an ImportFrom to the rel path of the module it reads."""
        level = node.level or 0
        if level == 0:
            dotted = node.module or ""
        else:
            base = self.pkg.split(".") if self.pkg else []
            if not self.is_pkg_init:
                # a non-package module: its own name is not a package component
                pass
            hops = level - 1
            base = base[: len(base) - hops] if hops else base
            dotted = ".".join([p for p in base if p] + ([node.module] if node.module else []))
        return self.module_table.get(dotted)

    # ---- references -----------------------------------------------------

    def _walk_refs(self, tree: ast.AST) -> None:
        nr, ar, sr = self.name_refs, self.attr_refs, self.str_refs
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Store):
                    continue
                nr.setdefault(n.id, set()).add(n.lineno)
            elif isinstance(n, ast.Attribute):
                ar.setdefault(n.attr, set()).add(n.lineno)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                v = n.value
                if v and v.isidentifier():
                    sr.setdefault(v, set()).add(n.lineno)

    # ---- test ownership -------------------------------------------------

    def _walk_test_owners(self, tree: ast.AST) -> None:
        for cls in ast.iter_child_nodes(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in ast.iter_child_nodes(cls):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start, end = self._span(fn)
                    self.test_owners.append(
                        (start, end, f"{cls.name}::{fn.name}")
                    )


def _base_name(node: ast.AST) -> str:
    """Last component of a base-class expression: exp.Expression -> Expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):  # Generic[T]
        return _base_name(node.value)
    if isinstance(node, ast.Call):  # with_metaclass(...)
        return _base_name(node.func)
    return ""


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        out: set[str] = set()
        for e in node.elts:
            out |= _target_names(e)
        return out
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return set()  # Attribute / Subscript target: binds no module global


def _module_level_ranges(source: str, rel: str) -> list[tuple[int, int]]:
    """Run-length encode the lines that execute at import time."""
    try:
        import astinfo
    except ImportError:  # pragma: no cover
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import astinfo
    info = astinfo.classify_lines(source, None, filename=rel)
    hot = sorted(n for n, i in info.items() if i.module_level and i.is_code)
    out: list[tuple[int, int]] = []
    for n in hot:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def build_index(tree_root, verbose: bool = False) -> dict:
    """Walk the pinned tree and return the index as a plain JSON-able dict."""
    root = Path(tree_root)
    src_rel = sorted(
        str(p.relative_to(root)) for p in (root / "sqlglot").rglob("*.py")
    )
    test_rel = sorted(
        str(p.relative_to(root)) for p in (root / "tests").rglob("*.py")
    )
    all_rel = src_rel + test_rel
    module_table = {_module_name(r): r for r in all_rel}

    scans: dict[str, _Scan] = {}
    modlevel: dict[str, list[tuple[int, int]]] = {}
    unparseable: list[str] = []
    for rel in all_rel:
        source = (root / rel).read_text(encoding="utf-8", errors="replace")
        sc = _Scan(rel, source, module_table)
        if not sc.ok:
            unparseable.append(rel)
        scans[rel] = sc
        modlevel[rel] = _module_level_ranges(source, rel)

    # Only names bound at import time somewhere under sqlglot/ can ever be the
    # subject of a query, plus anything another module rebinds from one of
    # those. Pruning to that set is what keeps the index small.
    queryable: set[str] = set()
    for rel in src_rel:
        for _s, _e, names in scans[rel].defs:
            queryable |= names
    for rel in all_rel:
        for src, imported, bound, _line in scans[rel].imports:
            if src.startswith("sqlglot/"):
                queryable.add(bound)
                queryable.add(imported)
    queryable.discard("*")

    # String constants are matched against a name's exact / lower / upper form.
    # Several names can share a form ("Foo" and "FOO" both yield "FOO"), so the
    # map is one-to-many -- credit the reference to all of them. Keeping it
    # one-to-many also makes the build order-independent, which a set iteration
    # would not be.
    str_keys: dict[str, list[str]] = {}
    for n in sorted(queryable):
        for form in {n, n.lower(), n.upper()}:
            str_keys.setdefault(form, []).append(n)

    fidx = {rel: i for i, rel in enumerate(all_rel)}

    defs_out: dict[str, list] = {}
    name_refs_out: dict[str, dict[str, list[int]]] = {}
    attr_refs: dict[str, list[tuple[int, int]]] = {}
    str_refs: dict[str, list[tuple[int, int]]] = {}
    imports_out: dict[str, dict[str, list]] = {}
    test_owners_out: dict[str, list] = {}
    subclasses: dict[str, dict[str, list[str]]] = {}

    for rel in all_rel:
        sc = scans[rel]
        i = fidx[rel]
        if rel.startswith("sqlglot/") and sc.defs:
            defs_out[str(i)] = [
                [s, e, sorted(ns)] for s, e, ns in sorted(sc.defs)
            ]
        local = {
            n: sorted(ls) for n, ls in sc.name_refs.items() if n in queryable
        }
        if local:
            name_refs_out[str(i)] = dict(sorted(local.items()))
        for n, ls in sc.attr_refs.items():
            if n in queryable:
                attr_refs.setdefault(n, []).extend((i, l) for l in sorted(ls))
        for v, ls in sc.str_refs.items():
            for n in str_keys.get(v, ()):
                str_refs.setdefault(n, []).extend((i, l) for l in sorted(ls))
        # import edges, keyed by the file they READ
        for src, imported, bound, line in sc.imports:
            tbl = imports_out.setdefault(str(fidx[src]), {})
            tbl.setdefault(imported, []).append([i, bound, line])
        # A subclass inherits every member of its base, so a change inside the
        # base reaches code that only ever names the SUBCLASS.
        #
        # Scoped per file, exactly like the bare-name references: `class
        # ScopeType(Enum)` subclasses whatever `Enum` means *in that file*. A
        # name-global table was measured and is far too crude -- scope.py's
        # `from enum import Enum` then matched every Enum subclass in sqlglot
        # (TokenType, Dialects, ...), visiting 115 names and selecting 1,546 of
        # 1,620 tests. The import edges already walk the closure into any file
        # that got the base from here, so the scoped form loses nothing.
        for cname, bases in sc.bases:
            for b in bases:
                subclasses.setdefault(str(i), {}).setdefault(b, []).append(cname)
        if sc.test_owners:
            test_owners_out[str(i)] = [
                [s, e, name] for s, e, name in sorted(sc.test_owners)
            ]

    for tbl in imports_out.values():
        for k in tbl:
            tbl[k] = sorted(tbl[k], key=lambda r: (r[0], r[2], r[1]))
    imports_out = {
        k: dict(sorted(v.items())) for k, v in sorted(imports_out.items())
    }

    index = {
        "version": INDEX_VERSION,
        "files": all_rel,
        "defs": dict(sorted(defs_out.items(), key=lambda kv: int(kv[0]))),
        "modlevel": {
            str(fidx[r]): [[a, b] for a, b in modlevel[r]]
            for r in all_rel
            if modlevel[r]
        },
        "name_refs": dict(
            sorted(name_refs_out.items(), key=lambda kv: int(kv[0]))
        ),
        "attr_refs": {
            k: [[f, l] for f, l in sorted(set(v))]
            for k, v in sorted(attr_refs.items())
        },
        "str_refs": {
            k: [[f, l] for f, l in sorted(set(v))]
            for k, v in sorted(str_refs.items())
        },
        "imports": imports_out,
        "subclasses": {
            k: {b: sorted(set(cs)) for b, cs in sorted(v.items())}
            for k, v in sorted(subclasses.items(), key=lambda kv: int(kv[0]))
        },
        "test_owners": dict(
            sorted(test_owners_out.items(), key=lambda kv: int(kv[0]))
        ),
        "unparseable": sorted(unparseable),
    }
    if verbose:
        print(
            f"files={len(all_rel)} queryable_names={len(queryable)} "
            f"attr_names={len(attr_refs)} str_names={len(str_refs)} "
            f"unparseable={len(unparseable)}",
            file=sys.stderr,
        )
    return index


def write_index(index: dict, out_path) -> int:
    """Write deterministically. `.gz` suffix -> gzip with no mtime field."""
    out_path = Path(out_path)
    blob = json.dumps(index, separators=(",", ":"), sort_keys=False).encode()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix == ".gz":
        # filename="" and mtime=0: gzip otherwise stamps the output path and the
        # clock into the header, and the bytes must be reproducible.
        with open(out_path, "wb") as fh:
            with gzip.GzipFile(
                filename="", fileobj=fh, mode="wb", compresslevel=9, mtime=0
            ) as gz:
                gz.write(blob)
    else:
        out_path.write_bytes(blob)
    return out_path.stat().st_size


# ==========================================================================
# runtime lookup
# ==========================================================================


@dataclass
class Resolution:
    """Where a module-level change's names are referenced."""

    #: reference lines in sqlglot/*.py that sit inside a function body --
    #: coverage is trustworthy here, query it with line granularity
    src: dict = field(default_factory=dict)
    #: reference lines in sqlglot/*.py that are themselves module level --
    #: the SAME coverage blind spot; the caller must widen for these
    src_modlevel: dict = field(default_factory=dict)
    #: reference lines in tests/*.py
    test: dict = field(default_factory=dict)
    #: changed module-level lines that bind no name at all
    unresolved: set = field(default_factory=set)
    #: (file, name) pairs the closure visited, for explainability
    names: set = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not (self.src or self.src_modlevel or self.test)


class SymRef:
    """Read-only lookup over a built index."""

    __slots__ = (
        "files", "_fidx", "_defs", "_modlevel", "_name_refs", "_attr_refs",
        "_str_refs", "_imports", "_test_owners", "_subclasses", "version",
    )

    def __init__(self, path):
        path = Path(path)
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        d = json.loads(raw)
        self.version = d["version"]
        if self.version != INDEX_VERSION:
            raise ValueError(f"symref index version {self.version} unsupported")
        self.files = d["files"]
        self._fidx = {rel: i for i, rel in enumerate(self.files)}
        self._defs = {int(k): v for k, v in d["defs"].items()}
        self._modlevel = {int(k): v for k, v in d["modlevel"].items()}
        self._name_refs = {int(k): v for k, v in d["name_refs"].items()}
        self._attr_refs = d["attr_refs"]
        self._str_refs = d["str_refs"]
        self._imports = {int(k): v for k, v in d["imports"].items()}
        self._subclasses = {int(k): v for k, v in d["subclasses"].items()}
        self._test_owners = {int(k): v for k, v in d["test_owners"].items()}

    # ---- primitives -----------------------------------------------------

    def has_file(self, path: str) -> bool:
        return path in self._fidx

    def is_module_level(self, path: str, line: int) -> bool:
        i = self._fidx.get(path)
        if i is None:
            return True  # unknown file: fail safe towards "needs a backstop"
        for a, b in self._modlevel.get(i, ()):
            if a <= line <= b:
                return True
        return False

    def names_at(self, path: str, line: int) -> set:
        """Names bound by every import-time binding whose span covers `line`."""
        i = self._fidx.get(path)
        if i is None:
            return set()
        out: set = set()
        for start, end, names in self._defs.get(i, ()):
            if start <= line <= end:
                out.update(names)
        return out

    def test_ids_at(self, path: str, line: int) -> list:
        """'Class::test_x' suffixes whose body covers `line`; [] at module level."""
        i = self._fidx.get(path)
        if i is None:
            return []
        return [
            name
            for start, end, name in self._test_owners.get(i, ())
            if start <= line <= end
        ]

    # ---- the closure ----------------------------------------------------

    def resolve(
        self,
        path: str,
        lines,
        *,
        propagate: bool = False,
        max_names: int = 4096,
    ) -> Resolution:
        """Reference locations for the names bound at `lines` of `path`.

        `propagate=False` leaves reference sites that are themselves at module
        level in `Resolution.src_modlevel` for the caller to widen on.
        `propagate=True` continues the name closure through them, which is
        sound-er and much broader.
        """
        res = Resolution()
        frontier: list[tuple[str, str]] = []
        for line in lines:
            names = self.names_at(path, line)
            if not names:
                res.unresolved.add(line)
                continue
            for n in names:
                frontier.append((path, n))

        seen: set[tuple[str, str]] = set()
        seen_names: set[str] = set()
        while frontier:
            if len(seen) > max_names:
                # pathological fan-out: tell the caller to widen instead
                res.unresolved.add(-1)
                break
            key = frontier.pop()
            if key in seen:
                continue
            seen.add(key)
            fil, name = key

            # (a) bare Name loads of `name`, in that module's own scope only
            fi = self._fidx.get(fil)
            if fi is not None:
                for ln in self._name_refs.get(fi, {}).get(name, ()):
                    self._place(res, fil, ln, frontier, propagate)
                # (d) classes in THIS file that subclass this binding
                for cname in self._subclasses.get(fi, {}).get(name, ()):
                    frontier.append((fil, cname))

            if name not in seen_names:
                seen_names.add(name)
                # (b) `.name` attribute access, anywhere
                for f, ln in self._attr_refs.get(name, ()):
                    self._place(res, self.files[f], ln, frontier, propagate)
                # (c) string constants naming it (dynamic dispatch)
                for f, ln in self._str_refs.get(name, ()):
                    self._place(res, self.files[f], ln, frontier, propagate)

            # (e) `from <fil> import name` -- the name is rebound elsewhere, so
            #     continue the closure in the importing module's scope
            if fi is not None:
                tbl = self._imports.get(fi, {})
                for imported in (name, "*"):
                    for f, bound, ln in tbl.get(imported, ()):
                        dst = self.files[f]
                        self._place(res, dst, ln, frontier, propagate)
                        frontier.append((dst, name if bound == "*" else bound))

        res.names = seen
        return res

    def _place(self, res, fil: str, line: int, frontier, propagate: bool) -> None:
        if fil.startswith("tests/"):
            res.test.setdefault(fil, set()).add(line)
            return
        if self.is_module_level(fil, line):
            if propagate:
                more = self.names_at(fil, line)
                if more:
                    for n in more:
                        frontier.append((fil, n))
                    # still record the line: harmless, and it is the honest
                    # location of the reference
                    res.src.setdefault(fil, set()).add(line)
                    return
            res.src_modlevel.setdefault(fil, set()).add(line)
        else:
            res.src.setdefault(fil, set()).add(line)

    # ---- turning locations into tests -----------------------------------

    def tests_for_locations(
        self,
        res: Resolution,
        *,
        line_tests,
        tests_in_file,
        test_number,
        include_modlevel_lines: bool = True,
    ) -> set:
        """Map a Resolution onto test numbers.

        line_tests(path, lines)  -> set of test numbers whose coverage hits them
        tests_in_file(test_file) -> every test number defined in that file
        test_number(test_id)     -> number, or None
        """
        out: set = set()
        src = dict(res.src)
        if include_modlevel_lines:
            for f, ls in res.src_modlevel.items():
                src.setdefault(f, set()).update(ls)
        for f, ls in src.items():
            out |= line_tests(f, ls)
        for f, ls in res.test.items():
            for ln in ls:
                ids = self.test_ids_at(f, ln)
                if not ids:
                    # module level in a test file: the whole file is implicated
                    out |= self._widen_test_file(f, tests_in_file)
                    continue
                for suffix in ids:
                    n = test_number(f"{f}::{suffix}")
                    if n is None:
                        # inherited/base-class method, or a plain helper
                        # function: widen to the file rather than drop evidence
                        out |= self._widen_test_file(f, tests_in_file)
                    else:
                        out.add(n)
        return out

    @staticmethod
    def _widen_test_file(f: str, tests_in_file) -> set:
        """Every test in a referencing test module -- or refuse to answer.

        A shared module under tests/ can declare NO tests of its own and still
        be imported by hundreds (tests/helpers.py: 0 declared, 1377 importers).
        For those, "every test in this file" is the empty set, which would
        silently discard the reference. Refuse instead, so the caller falls
        back to its own per-test-FILE closure.
        """
        got = tests_in_file(f)
        if not got:
            raise SymRefIncomplete(
                f"{f} declares no tests of its own; its importers are not "
                "knowable from this index"
            )
        return set(got)


# ==========================================================================
# CLI
# ==========================================================================


def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "build":
        if len(argv) < 3:
            print("usage: symref.py build <pristine-tree> <out.json[.gz]>")
            return 2
        import time

        t0 = time.process_time()
        idx = build_index(argv[1], verbose=True)
        t1 = time.process_time()
        size = write_index(idx, argv[2])
        t2 = time.process_time()
        print(
            json.dumps(
                {
                    "out": argv[2],
                    "bytes": size,
                    "build_cpu_s": round(t1 - t0, 3),
                    "write_cpu_s": round(t2 - t1, 3),
                    "files": len(idx["files"]),
                    "attr_names": len(idx["attr_refs"]),
                    "str_names": len(idx["str_refs"]),
                    "unparseable": idx["unparseable"],
                },
                indent=2,
            )
        )
        return 0
    if cmd == "show":
        if len(argv) < 4:
            print("usage: symref.py show <index> <path.py> <line> [line...]")
            return 2
        idx = SymRef(argv[1])
        path = argv[2]
        lines = [int(x) for x in argv[3:]]
        for ln in lines:
            print(
                f"{path}:{ln}  module_level={idx.is_module_level(path, ln)}  "
                f"names={sorted(idx.names_at(path, ln))}"
            )
        for propagate in (False, True):
            res = idx.resolve(path, lines, propagate=propagate)
            print(f"\npropagate={propagate}  names_visited={len(res.names)}")
            print(f"  unresolved changed lines: {sorted(res.unresolved)}")
            for label, tbl in (
                ("src (function body)", res.src),
                ("src (module level)", res.src_modlevel),
                ("tests/", res.test),
            ):
                n = sum(len(v) for v in tbl.values())
                print(f"  {label}: {len(tbl)} files, {n} lines")
                for f, ls in sorted(tbl.items())[:12]:
                    print(f"      {f}: {sorted(ls)[:14]}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
