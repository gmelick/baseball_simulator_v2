"""
test_check_file_integrity.py
============================
Unit tests for ``scripts/check_file_integrity.py`` — the SIM-315 file-integrity
guard that flags ``.py`` files corrupted by the Cowork/OneDrive file bridge
(NUL bytes or truncated / un-parseable source).

These tests are pure stdlib + ``tmp_path`` — no external services, no third-party
deps — so they run in the default ``tests/unit/`` lane.

They assert the guard's contract:

  * a clean, valid ``.py`` file passes (``check_file`` returns None);
  * a file containing a NUL byte (``\\x00``) is flagged;
  * a syntactically-broken / truncated file is flagged;
  * a non-UTF-8 file is flagged;
  * directory walking yields ``.py`` files and prunes excluded dirs
    (``__pycache__`` etc.);
  * ``main()`` returns 0 on a clean tree and non-zero (1) when offenders exist;
  * explicit non-.py path args are ignored.

Owned by QA / DevOps.
"""

from __future__ import annotations

import importlib.util
import pathlib

# ---------------------------------------------------------------------------
# Import the script-under-test by file path (scripts/ is not an importable
# package — no __init__.py — so we load it directly from disk).
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_file_integrity.py"

_spec = importlib.util.spec_from_file_location("check_file_integrity", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
cfi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfi)


# ---------------------------------------------------------------------------
# check_file — per-file checks
# ---------------------------------------------------------------------------
def test_clean_file_passes(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("import os\n\n\ndef add(a, b):\n    return a + b\n", encoding="utf-8")
    assert cfi.check_file(str(f)) is None


def test_null_byte_is_flagged(tmp_path):
    f = tmp_path / "nul.py"
    # A NUL byte spliced into otherwise-valid source — the bridge's signature.
    f.write_bytes(b"x = 1\n\x00\ny = 2\n")
    offender = cfi.check_file(str(f))
    assert offender is not None
    assert "NUL byte" in offender.reason
    assert offender.path == str(f)


def test_null_byte_caught_even_when_rest_parses(tmp_path):
    # Source that would parse fine if the NUL were stripped — proves the NUL
    # check runs before (and independently of) the ast.parse check.
    f = tmp_path / "nul_parsable.py"
    f.write_bytes(b"value = 42\x00\n")
    offender = cfi.check_file(str(f))
    assert offender is not None
    assert "NUL byte" in offender.reason


def test_truncated_file_is_flagged(tmp_path):
    # Mid-statement truncation — exactly what a bridge cut-off produces.
    f = tmp_path / "truncated.py"
    f.write_text("def compute(\n", encoding="utf-8")
    offender = cfi.check_file(str(f))
    assert offender is not None
    assert "parse" in offender.reason.lower()


def test_syntax_error_is_flagged(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def f(:\n    pass\n", encoding="utf-8")
    offender = cfi.check_file(str(f))
    assert offender is not None
    assert "parse" in offender.reason.lower()


def test_non_utf8_is_flagged(tmp_path):
    f = tmp_path / "latin1.py"
    # Invalid UTF-8 byte sequence (0xff is never valid as a UTF-8 lead byte).
    f.write_bytes(b"x = '\xff\xfe'\n")
    offender = cfi.check_file(str(f))
    assert offender is not None
    assert "UTF-8" in offender.reason


# ---------------------------------------------------------------------------
# iter_python_files — discovery + pruning
# ---------------------------------------------------------------------------
def test_iter_python_files_walks_and_prunes(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "b.py").write_text("b = 2\n", encoding="utf-8")
    (sub / "notes.txt").write_text("ignore me\n", encoding="utf-8")
    # Excluded dir with a .py inside — must be pruned.
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "c.py").write_text("c = 3\n", encoding="utf-8")

    found = {pathlib.Path(p).name for p in cfi.iter_python_files([str(tmp_path)])}
    assert found == {"a.py", "b.py"}


def test_iter_python_files_ignores_non_py_file_args(tmp_path):
    txt = tmp_path / "readme.txt"
    txt.write_text("hello\n", encoding="utf-8")
    assert list(cfi.iter_python_files([str(txt)])) == []


def test_iter_python_files_accepts_explicit_py_file(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert list(cfi.iter_python_files([str(f)])) == [str(f)]


# ---------------------------------------------------------------------------
# main — exit codes
# ---------------------------------------------------------------------------
def test_main_returns_zero_on_clean_tree(tmp_path, capsys):
    (tmp_path / "ok.py").write_text("ok = True\n", encoding="utf-8")
    rc = cfi.main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_main_returns_nonzero_when_offenders_exist(tmp_path, capsys):
    (tmp_path / "good.py").write_text("good = 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_bytes(b"def broken(\n")  # truncated
    (tmp_path / "nul.py").write_bytes(b"z = 0\x00\n")  # NUL byte

    rc = cfi.main([str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "bad.py" in out
    assert "nul.py" in out
    # The clean file should NOT be listed as an offender.
    assert "good.py" not in out


def test_main_explicit_file_args(tmp_path, capsys):
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"x = (\n")  # truncated tuple/call
    rc = cfi.main([str(bad)])
    assert rc == 1
    assert "bad.py" in capsys.readouterr().out
