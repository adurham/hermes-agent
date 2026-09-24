"""Guard: a ``cli.py`` class-body copy of a mixin method must stay call-compatible.

``HermesCLI``'s class body always wins over its mixin bases, so a stale copy of a
method in ``cli.py`` silently shadows the live mixin definition. When upstream
changes a mixin's call contract — e.g. ``_confirm_and_apply_cli_model_switch``
gaining a fifth ``reasoning_effort`` slot — the shadowing copy keeps the old
signature and every call site breaks at runtime with a ``TypeError`` (the
2026-09 sync shipped exactly that bug on the ``/model --reasoning`` paths,
where the failure was swallowed by a broad ``except`` and looked like
"✗ Model selection failed: …").

This guard is pure AST (no app imports) and cheap: it parses ``cli.py`` and the
``hermes_cli/cli_*.py`` mixin modules, and for every method name defined in
BOTH a ``HermesCLI`` base mixin class and the ``HermesCLI`` class body asserts
the ``cli.py`` copy accepts a SUPERSET of the mixin's call contract:

* every mixin parameter name is accepted by the ``cli.py`` copy (by name, or
  positionally for the mixin's positional-only parameters); and
* the ``cli.py`` copy accepts at least the mixin's positional arity (or takes
  ``*args``).

A violation means the shadowing copy would reject a call the mixin's callers
legitimately make.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _REPO_ROOT / "cli.py"
_MIXIN_DIR = _REPO_ROOT / "hermes_cli"
_TARGET_CLASS = "HermesCLI"


class _Signature:
    """Parameter view of one function definition."""

    def __init__(self, node: ast.FunctionDef) -> None:
        args = node.args
        self.path = node
        self.posonly = [a.arg for a in args.posonlyargs]
        self.args = [a.arg for a in args.args]
        self.vararg = args.vararg.arg if args.vararg else None
        self.kwonly = [a.arg for a in args.kwonlyargs]
        self.kwarg = args.kwarg.arg if args.kwarg else None

    @property
    def positional(self) -> list[str]:
        return self.posonly + self.args

    def names(self) -> set[str]:
        return set(self.positional) | set(self.kwonly)

    def accepts_name(self, name: str) -> bool:
        return name in self.names() or self.kwarg is not None

    def accepts_positional_arity(self, count: int) -> bool:
        return self.vararg is not None or len(self.positional) >= count

    def describe(self) -> str:
        parts = list(self.positional)
        if self.vararg:
            parts.append(f"*{self.vararg}")
        parts.extend(self.kwonly)
        if self.kwarg:
            parts.append(f"**{self.kwarg}")
        return f"({', '.join(parts)})"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class_defs(tree: ast.Module) -> dict[str, ast.ClassDef]:
    """Top-level classes by name (nested helper classes do not participate in the MRO)."""
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    """Regular methods only — this guard compares synchronous call contracts."""
    return {
        item.name: item
        for item in cls.body
        if isinstance(item, ast.FunctionDef)
    }


def _mixin_classes_in_mro() -> dict[str, ast.ClassDef]:
    """Mixins actually inherited by HermesCLI, found across hermes_cli/cli_*.py.

    Only base classes listed in ``class HermesCLI(...)`` are compared: a nested
    helper class such as ``_Panel`` shares method names (``__init__``) with the
    CLI without ever shadowing it.
    """
    cli_tree = _parse(_CLI_PATH)
    base_names = set()
    for cls in _class_defs(cli_tree).values():
        if cls.name == _TARGET_CLASS:
            base_names = {b.id for b in cls.bases if isinstance(b, ast.Name)}
    assert base_names, f"could not find base classes of {_TARGET_CLASS} in {_CLI_PATH}"

    found: dict[str, ast.ClassDef] = {}
    for path in sorted(_MIXIN_DIR.glob("cli_*.py")):
        if path.resolve() == _CLI_PATH.resolve():
            continue
        for name, cls in _class_defs(_parse(path)).items():
            if name in base_names:
                assert name not in found, f"{name} defined in two mixin modules"
                found[name] = cls
    missing = sorted(base_names - set(found))
    assert not missing, f"mixin base(s) not found under {_MIXIN_DIR}: {missing}"
    return found


def _violations() -> list[str]:
    cli_tree = _parse(_CLI_PATH)
    cli_cls = _class_defs(cli_tree).get(_TARGET_CLASS)
    assert cli_cls is not None, f"{_TARGET_CLASS} not found in {_CLI_PATH}"
    cli_methods = _methods(cli_cls)

    problems: list[str] = []
    for mixin_name, mixin_cls in sorted(_mixin_classes_in_mro().items()):
        for method_name, mixin_fn in _methods(mixin_cls).items():
            cli_fn = cli_methods.get(method_name)
            if cli_fn is None:
                continue  # no shadow: the mixin's definition is the one that runs
            mixin_sig, cli_sig = _Signature(mixin_fn), _Signature(cli_fn)
            missing: list[str] = []
            for name in mixin_sig.names():
                if name in cli_sig.names() or cli_sig.kwarg is not None:
                    continue
                if name in mixin_sig.posonly and cli_sig.accepts_positional_arity(
                    mixin_sig.positional.index(name) + 1
                ):
                    continue
                missing.append(name)
            if missing:
                problems.append(
                    f"{_TARGET_CLASS}.{method_name} (cli.py:{cli_fn.lineno}) does not accept "
                    f"parameter(s) {missing} declared by {mixin_name}.{method_name} "
                    f"({mixin_sig.describe()}); cli.py copy is {cli_sig.describe()}"
                )
            if not cli_sig.accepts_positional_arity(len(mixin_sig.positional)):
                problems.append(
                    f"{_TARGET_CLASS}.{method_name} (cli.py:{cli_fn.lineno}) does not accept the "
                    f"positional arity of {mixin_name}.{method_name} "
                    f"({len(mixin_sig.positional)} positional slots needed, "
                    f"{len(cli_sig.positional)} available); cli.py copy is {cli_sig.describe()}"
                )
    return problems


def test_cli_class_body_copies_accept_every_mixin_call_contract() -> None:
    problems = _violations()
    assert not problems, (
        "cli.py HermesCLI class-body copies shadow mixin methods with an incompatible "
        "signature (update the cli.py copy, or delete it so the mixin wins):\n  - "
        + "\n  - ".join(problems)
    )


def test_guard_detects_an_incompatible_shadow(monkeypatch, tmp_path) -> None:
    """Self-check: the comparison logic actually flags a missing parameter/slot."""
    mixin_src = "class _Mixin:\n    def _shadowed(self, self_ok, added=''):\n        pass\n"
    cli_src = (
        "class HermesCLI(_Mixin):\n"
        "    def _shadowed(self, self_ok):\n"
        "        pass\n"
    )
    mixin_file = tmp_path / "cli_fake_mixin.py"
    cli_file = tmp_path / "cli.py"
    mixin_file.write_text(mixin_src, encoding="utf-8")
    cli_file.write_text(cli_src, encoding="utf-8")

    monkeypatch.setattr("tests.hermes_cli.test_cli_shadow_signature_guard._CLI_PATH", cli_file)
    monkeypatch.setattr("tests.hermes_cli.test_cli_shadow_signature_guard._MIXIN_DIR", tmp_path)
    problems = _violations()
    assert problems and "_shadowed" in problems[0] and "added" in problems[0]
