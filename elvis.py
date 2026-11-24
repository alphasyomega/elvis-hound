#!/usr/bin/env python3
"""A read-only script runner for questions about a folder.

THE IDEA
    To answer "which of these 887 files mention X, and which have no date?" an
    agent usually calls a tool per file and reads every result. This engine lets
    the agent write ONE small Python script that walks the folder itself and
    returns ONLY what it decided to report. The saving is not the code: it is
    not reading output nobody needs.

USAGE
    python elvis.py [--root <folder>]... --script <file.py | -> [--timeout 60] [--json]
                        [--max-file-bytes N] [--max-total-bytes N]
    python elvis.py --selftest

    --script -   read the script text from stdin instead of a file, so one run
                 needs no temp file:  echo "box.report(n=len(box.list()))" | python elvis.py --script -
    --root       repeatable: every folder given is a root, and a path is allowed
                 when it is inside ANY of them. With no --root at all the current
                 working directory is the single root.

    Exit codes: 0 found/none, 2 could_not_look, 3 crashed (--selftest: 0 pass, 1 fail).

THE SCRIPT'S WORLD
    The script runs in a separate process (sys.executable -I) with its working
    directory set to the first root, and gets one global named `box`:

      box.roots                       the absolute roots, as a list of strings
      box.root                        box.roots[0]
      box.list(glob="**/*")           sorted file paths, searched in every root: relative to
                                      the root for the first root, absolute ('/' separated)
                                      for the others; every path it returns works in box.read
      box.read(path, max_bytes=None)  text of a file under root; counted in coverage
      box.lines(path)                 its lines, same accounting
      box.grep(pattern, glob="**/*")  [(path, lineno, line)] for a regex
      box.report(**fields)            what comes back to the caller; calls merge,
                                      a later value for the same key replaces the earlier
      box.note(text)                  a short human line, appended in order

    A file over the size cap, or past the total budget, is NOT read: box.read
    returns "" and the file is listed under coverage.skipped with its reason.
    A direct failure is listed there too. An I/O failure whose filename cannot
    be correlated with an observation is recorded as an unknown failure.
    The script's own print() output is captured and returned under notes as
    "stdout: ..." lines; it never becomes part of `reported`.

THE RESULT (one JSON object, nine fields)
    reported   exactly what the script reported, nothing added
    notes      the script's notes, its captured stdout/stderr, and any refusals
    coverage   files_seen, files_read, bytes_read, skipped [{path, why}], complete, incomplete_why,
               direct_observations {open, os.listdir, os.scandir},
               direct_observation_failures {open, os.listdir, os.scandir},
               unknown_failures [{path, why}]
    outcome    found | none | could_not_look | crashed
    error      null, or an explanatory error string (usually "<type>: <message>")
    error_kind null, or one of script_invalid, script_error, operation_denied,
               timed_out, budget_exhausted, could_not_read, direct_observation_failed,
               file_cap_exceeded, path_refused, unattributed_io_failure
    refusal_count  total refused operations
    refusals_truncated  whether all refusal notes fit in the separate refusal-note budget
    refusals_truncation_why  null, or why refusal notes were truncated

    found           the script reported anything
    none            it ran clean, looked at what it needed, and reported nothing
    could_not_look  root missing/unreadable, the script source could not be read,
                    or coverage is incomplete and nothing was reported
    crashed         exception, refusal, or timeout (error says which)
    The engine never returns an empty success that reads like "all clear".

GUARDRAILS (read-only, v0)
    1. An audit hook (sys.addaudithook, installed before the script's first line)
       refuses: open() for write/append/create/update, os.remove/rename/rmdir/mkdir
       and friends, shutil operations, subprocess/os.exec*/os.system/fork,
       socket/urllib/http, ctypes loads. A refusal raises AuditRefusal and is
       also recorded, so a script that catches the exception still ends "crashed".
    2. Every path given to box is resolved (symlinks included) and must stay under
       --root. '..', absolute paths and symlinks that leave the root are refused
       by name. Plain open()/os.listdir() by the script are confined the same
       way (stdlib and interpreter directories stay readable so imports work).
       With several roots, "under the root" means under ANY root.
    3. A wall-clock timeout kills the child process: crashed, "timed out after Ns".
    4. box.read caps one file (default 2 MB) and the total (default 64 MB).
    5. print() output is captured, not passed through.

HONEST LIMITS - READ THIS BEFORE TRUSTING IT
    An audit hook plus path confinement protects against OUR OWN MISTAKES in
    generated scripts (a stray open(..., "w"), a rm, a request to the network).
    It is NOT a defence against a hostile script that is trying to escape.
    Python is not a sandbox: a determined script can reach around an audit hook
    (ctypes tricks, monkeypatching the engine's own objects, forging the result
    marker on the real stdout, resource exhaustion, events the interpreter does
    not audit). If the script is not trustworthy, do not run it here; use an
    OS-level sandbox (container, separate user, no network namespace).
    Direct open()/io.open()/Path.open()/Path.read_text()/os.open(), os.listdir(),
    and os.scandir() calls inside a root are recorded by the audit hook in
    coverage.direct_observations; open includes all of those file-opening APIs.
    An OSError or UnicodeError raised by one of those observations is recorded
    in coverage.skipped and coverage.direct_observation_failures. The audit
    event happens before the operation finishes, so an observation without a
    matching exception is recorded as successful when the script ends. Exceptions are
    correlated by their filename and audit call site; uncorrelated filename-bearing I/O
    failures make coverage incomplete. Coverage is checked even when the outcome is
    found. Memory and CPU are limited
    only by the wall-clock timeout; the size of `reported` is not capped.

Standard library only; Python 3.8+; Windows and Linux.
"""
from __future__ import print_function

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = "@@CODE_MODE_RESULT@@"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_FILE = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL = 64 * 1024 * 1024
MAX_STDOUT_LINES = 200
MAX_NOTES = 200
MAX_REFUSALS = 200
MAX_LINE_CHARS = 500


class AuditRefusal(RuntimeError):
    """Raised by the audit hook when the script attempts a forbidden operation."""


class BoxRefusal(PermissionError):
    """Raised by box when a path would leave the root."""


def _record_refusal(refusals, refusal_count, reason):
    refusal_count[0] += 1
    if len(refusals) < MAX_REFUSALS:
        refusals.append(reason)


# --------------------------------------------------------------------------
# Child side: runs inside the separate process
# --------------------------------------------------------------------------

_BLOCKED_EXACT = frozenset([
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.truncate", "os.chmod",
    "os.chown", "os.lchown", "os.chflags", "os.lchmod", "os.symlink", "os.link",
    "os.utime", "os.mkfifo", "os.mknod", "os.startfile", "os.kill", "os.killpg",
    "os.exec", "os.system", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
    "subprocess.Popen", "urllib.Request", "pty.spawn", "webbrowser.open", "sqlite3.connect",
])
_BLOCKED_PREFIX = ("shutil.", "socket.", "http.client.", "ftplib.", "smtplib.",
                   "telnetlib.", "ctypes.dlopen", "ctypes.call_function")


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def _under(path, base):
    """True if `path` equals or is inside `base` (both already normalised)."""
    if path == base:
        return True
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:  # different drives on Windows
        return False


def _install_hook(roots_real, refusals, refusal_count, box_io, observations):
    import sysconfig
    write_bits = 0
    for name in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC", "O_EXCL"):
        write_bits |= getattr(os, name, 0)

    readable = list(roots_real)
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
              os.path.dirname(os.path.abspath(sys.executable)),
              os.path.dirname(os.__file__)):
        readable.append(_norm(p))
    try:
        for v in sysconfig.get_paths().values():
            readable.append(_norm(v))
    except Exception:
        pass
    readable = sorted(set(readable))
    devnull = _norm(os.devnull)

    def read_ok(path):
        real = _norm(path)
        if real == devnull:
            return True
        return any(_under(real, b) for b in readable)

    def observed(event, path):
        if box_io[0] or isinstance(path, int):
            return
        try:
            real = _norm(os.fsdecode(path))
        except Exception:
            return
        if any(_under(real, root) for root in roots_real):
            frame = sys._getframe(3)
            observations.append({"event": event, "path": os.fsdecode(path), "real": real,
                                 "site": (frame.f_code, frame.f_lineno),
                                 "failed": False, "why": None})

    def judge(event, args):
        if event == "open":
            path = args[0] if args else None
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            if isinstance(path, int):
                return None
            writing = False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                writing = True
            if isinstance(flags, int) and (flags & write_bits):
                writing = True
            if writing:
                return "refused: open for writing is not allowed (%r, mode %r)" % (path, mode)
            try:
                p = os.fsdecode(path)
            except Exception as e:
                return "refused: unreadable path for open: %s" % e
            if not read_ok(p):
                return "refused: reading outside the root is not allowed (%r)" % (p,)
            observed("open", path)
            return None
        if event in ("os.listdir", "os.scandir"):
            path = args[0] if args else None
            if path is None:
                return None
            if isinstance(path, int):
                return None
            try:
                p = os.fsdecode(path)
            except Exception as e:
                return "refused: unreadable path for listing: %s" % e
            if not read_ok(p):
                return "refused: listing a directory outside the root is not allowed (%r)" % (p,)
            observed(event, path)
            return None
        if event in _BLOCKED_EXACT or event.startswith(_BLOCKED_PREFIX):
            return "refused: %s is not allowed in read-only mode" % event
        return None

    def hook(event, args):
        reason = judge(event, args)
        if reason:
            _record_refusal(refusals, refusal_count, reason)
            raise AuditRefusal(reason)

    sys.addaudithook(hook)


def _install_observation_tracer(roots_real, observations, unknown_failures):
    """Correlate audit observations with I/O exceptions by path and call site."""
    seen = set()
    previous = sys.gettrace()
    def trace(frame, event, arg):
        if previous is not None:
            previous(frame, event, arg)
        if event != "exception" or not issubclass(arg[0], (OSError, UnicodeError)):
            return trace
        exc = arg[1]
        filename = getattr(exc, "filename", None)
        if id(exc) in seen:
            return trace
        seen.add(id(exc))
        site = (frame.f_code, frame.f_lineno)
        if not filename:
            if isinstance(exc, UnicodeError):
                for item in reversed(observations):
                    if not item["failed"] and item["event"] == "open":
                        item["failed"] = True
                        item["why"] = "%s: %s" % (type(exc).__name__, exc)
                        break
            return trace
        try:
            real = _norm(os.fsdecode(filename))
        except Exception:
            return trace
        if not any(_under(real, root) for root in roots_real):
            return trace
        matched_path = False
        for item in reversed(observations):
            if item["real"] == real:
                matched_path = True
            if (not item["failed"] and item["real"] == real
                    and item["site"] == site):
                item["failed"] = True
                item["why"] = "%s: %s" % (type(exc).__name__, exc)
                return trace
        if not matched_path and getattr(exc, "errno", None) is not None:
            unknown_failures.append({"path": os.fsdecode(filename),
                                     "why": "%s: %s" % (type(exc).__name__, exc)})
        return trace
    sys.settrace(trace)
    return trace


class _Capture(object):
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s if isinstance(s, str) else str(s))
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def lines(self):
        return "".join(self.parts).splitlines()


def _box_io(method):
    def guarded(self, *args, **kwargs):
        self._box_io[0] += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._box_io[0] -= 1
    return guarded


class _Box(object):
    def __init__(self, roots_real, max_file, max_total, refusals, refusal_count, box_io):
        import glob as _glob  # noqa: F401  (preload before the hook)
        self.roots = list(roots_real)
        self.root = self.roots[0]
        self._bases = [_norm(r) for r in self.roots]
        self._max_file = max_file
        self._max_total = max_total
        self.reported = {}
        self.notes = []
        self.seen = set()
        self.read_files = set()
        self.bytes_read = 0
        self.skipped = []
        self.attempted = 0  # reads attempted via box.read/grep
        self._refusals = refusals
        self._refusal_count = refusal_count
        self._box_io = box_io

    def _refuse(self, reason):
        _record_refusal(self._refusals, self._refusal_count, reason)
        raise BoxRefusal(reason)

    # -- paths --
    def _resolve(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            raise TypeError("path must be str or PathLike, got %s" % type(path).__name__)
        s = os.fsdecode(os.fspath(path))
        if not s:
            self._refuse("refused: empty path")
        if os.path.isabs(s):
            cand = s
        else:
            cand = os.path.join(self.root, s)
            for r in self.roots:  # a relative path means the first root that has it
                if os.path.lexists(os.path.join(r, s)):
                    cand = os.path.join(r, s)
                    break
        real = _norm(cand)
        if not any(_under(real, b) for b in self._bases):
            if os.path.isabs(s):
                why = "absolute path outside the root"
            elif ".." in s.replace("\\", "/").split("/"):
                why = "'..' leaves the root"
            else:
                why = "symlink leaves the root"
            self._refuse("refused %r: %s (%s)" % (s, why, ", ".join(self.roots)))
        return os.path.realpath(cand), s

    def _rel(self, real):
        # case-preserving (the normalised form is lower-cased on Windows).
        # First root: relative to it. Any other root: the absolute path, so the
        # name is unambiguous and box.read accepts it.
        if _under(_norm(real), self._bases[0]):
            return os.path.relpath(real, self.root).replace(os.sep, "/")
        return real.replace(os.sep, "/")

    def _skip(self, rel, why):
        self.skipped.append({"path": rel, "why": why})

    # -- API --
    @_box_io
    def list(self, glob="**/*"):
        import pathlib
        g = glob.replace("\\", "/")
        if os.path.isabs(g) or ".." in g.split("/"):
            self._refuse("refused glob %r: absolute or '..' patterns are not allowed" % glob)
        out = set()
        for k, root in enumerate(self.roots):
            for p in pathlib.Path(root).glob(g):
                try:
                    if not p.is_file():
                        continue
                except OSError as e:
                    rel = p.relative_to(root).as_posix() if k == 0 else p.as_posix()
                    self._skip(rel, "cannot examine: %s" % e)
                    continue
                real = _norm(str(p))
                rel = p.relative_to(root).as_posix() if k == 0 else p.as_posix()
                if not any(_under(real, b) for b in self._bases):
                    self._skip(rel, "symlink leaves the root")
                    continue
                out.add(rel)
        out = sorted(out)
        self.seen.update(out)
        return out

    def _read_bytes(self, path, max_bytes=None):
        """Returns bytes, or None when skipped (recorded). OSError is recorded and raised."""
        real, _ = self._resolve(path)
        rel = self._rel(real)
        self.seen.add(rel)
        self.attempted += 1
        if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                                      or max_bytes <= 0):
            raise ValueError("max_bytes must be a positive integer (booleans are not allowed)")
        cap = self._max_file if max_bytes is None else min(self._max_file, max_bytes)
        remaining = self._max_total - self.bytes_read
        if remaining <= 0:
            self._skip(rel, "total read budget of %d bytes exhausted" % self._max_total)
            return None
        limit = min(cap, remaining)
        try:
            with open(real, "rb") as f:
                data = f.read(limit + 1)
        except OSError as e:
            self._skip(rel, "cannot read: %s" % e)
            raise
        if len(data) > cap:
            self._skip(rel, "file grew past the per-file cap of %d" % cap)
            return None
        if len(data) > remaining:
            self._skip(rel, "total read budget of %d bytes exhausted" % self._max_total)
            return None
        self.bytes_read += len(data)
        self.read_files.add(rel)
        return data

    @_box_io
    def read(self, path, max_bytes=None):
        data = self._read_bytes(path, max_bytes)
        return "" if data is None else data.decode("utf-8", "replace")

    def lines(self, path):
        return self.read(path).splitlines()

    def grep(self, pattern, glob="**/*"):
        rx = re.compile(pattern)
        hits = []
        for rel in self.list(glob):
            try:
                text = self.read(rel)
            except OSError:
                continue  # already recorded under skipped
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append((rel, n, line))
        return hits

    def report(self, *args, **fields):
        for a in args:
            self.reported.update(dict(a))
        self.reported.update(fields)

    def note(self, text):
        if len(self.notes) < MAX_NOTES:
            self.notes.append(str(text)[:MAX_LINE_CHARS])

def _child_main(a):
    sys.dont_write_bytecode = True
    import traceback
    roots_real = [os.path.realpath(r) for r in (a.root or [os.getcwd()])]
    root_real = roots_real[0]
    refusals = []
    refusal_count = [0]
    box_io = [0]
    direct_observations = {"open": 0, "os.listdir": 0, "os.scandir": 0}
    direct_observation_failures = {"open": 0, "os.listdir": 0, "os.scandir": 0}
    observations = []
    unknown_failures = []
    box = _Box(roots_real, a.max_file_bytes, a.max_total_bytes, refusals, refusal_count, box_io)
    fname = "<stdin>" if a.script == "-" else a.script
    out, err = _Capture(), _Capture()
    result = {"outcome": None, "error": None, "error_kind": None}
    code = None
    try:
        if a.script == "-":
            src = sys.stdin.buffer.read().decode("utf-8", "replace")
        else:
            with open(a.script, "rb") as f:
                src = f.read().decode("utf-8", "replace")
        code = compile(src, fname, "exec")
        os.chdir(root_real)
    except SyntaxError as e:
        result.update(outcome="crashed", error="%s: %s" % (type(e).__name__, e),
                      error_kind="script_invalid")
    except OSError as e:
        result.update(outcome="could_not_look", error="%s: %s" % (type(e).__name__, e),
                      error_kind="could_not_read")
    except Exception as e:
        result.update(outcome="crashed", error="%s: %s" % (type(e).__name__, e),
                      error_kind="script_invalid")

    if code is not None:
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        normal_roots = [_norm(r) for r in roots_real]
        _install_hook(normal_roots, refusals, refusal_count, box_io, observations)
        script_builtins = dict(vars(__import__("builtins")))
        g = {"__name__": "__main__", "__file__": fname, "box": box,
             "__builtins__": script_builtins}
        _install_observation_tracer(normal_roots, observations, unknown_failures)
        try:
            exec(code, g)
        except SystemExit as e:
            if e.code not in (None, 0):
                result.update(outcome="crashed", error="SystemExit: %s" % (e.code,), error_kind="script_error")
        except BaseException as e:  # noqa: B902 - report anything the script raised
            kind = "crashed" if isinstance(e, (AuditRefusal, BoxRefusal)) or not isinstance(e, OSError) else "could_not_look"
            error_kind = "operation_denied" if isinstance(e, (AuditRefusal, BoxRefusal)) else \
                ("could_not_read" if isinstance(e, OSError) else "script_error")
            result.update(outcome=kind, error="%s: %s" % (type(e).__name__, e), error_kind=error_kind)
            at = None
            for frame, lineno in traceback.walk_tb(e.__traceback__):
                if frame.f_code.co_filename == fname:
                    at = lineno  # keep the innermost frame inside the script
            if at is not None:
                box.note("error at script line %s" % at)
        finally:
            sys.settrace(None)
            sys.stdout, sys.stderr = real_out, real_err

    notes = list(box.notes)
    for r in refusals:
        notes.append("REFUSED: " + r)
    if refusal_count[0] > len(refusals):
        notes.append("REFUSED: ... %d more refusals not shown" % (refusal_count[0] - len(refusals)))
    for stream, label in ((out, "stdout"), (err, "stderr")):
        ls = stream.lines()
        for ln in ls[:MAX_STDOUT_LINES]:
            notes.append("%s: %s" % (label, ln[:MAX_LINE_CHARS]))
        if len(ls) > MAX_STDOUT_LINES:
            notes.append("%s: ... %d more lines not shown" % (label, len(ls) - MAX_STDOUT_LINES))

    for item in observations:
        direct_observations[item["event"]] += 1
        if item["failed"]:
            direct_observation_failures[item["event"]] += 1
    direct_failures = [{"path": item["path"], "why": item["why"]}
                       for item in observations if item["failed"]]
    incomplete = list(box.skipped) + direct_failures + unknown_failures
    incomplete_why = []
    for item in box.skipped:
        why = item["why"]
        if "total read budget" in why:
            cause = "budget_exhausted"
        elif "cannot stat" in why or "cannot read" in why:
            cause = "could_not_read"
        elif "per-file cap" in why:
            cause = "file_cap_exceeded"
        else:
            cause = "path_refused"
        if cause not in incomplete_why:
            incomplete_why.append(cause)
    if any(direct_observation_failures.values()):
        incomplete_why.append("direct_observation_failed")
    if unknown_failures:
        incomplete_why.append("unattributed_io_failure")
    if refusal_count[0]:
        result.update(outcome="crashed", error_kind="operation_denied")
        if result["error"] is None:
            result["error"] = "refused operation: " + refusals[0]
    elif result["outcome"] is None:
        if not incomplete and not any(direct_observation_failures.values()) and not box.reported:
            result["outcome"] = "none"
        elif not box.read_files and incomplete:
            cause = ("could_not_read" if any(direct_observation_failures.values())
                     else (incomplete_why[0] if incomplete_why else "could_not_read"))
            coverage_detail = []
            if incomplete:
                coverage_detail.append("coverage.skipped")
            if any(direct_observation_failures.values()):
                coverage_detail.append("coverage.direct_observation_failures")
            result.update(outcome="could_not_look",
                          error="coverage incomplete (see %s)" % " and ".join(coverage_detail), error_kind=cause)
        elif box.reported:
            result["outcome"] = "found"
        else:
            cause = ("could_not_read" if any(direct_observation_failures.values())
                     else (incomplete_why[0] if incomplete_why else "could_not_read"))
            coverage_detail = []
            if incomplete:
                coverage_detail.append("coverage.skipped")
            if any(direct_observation_failures.values()):
                coverage_detail.append("coverage.direct_observation_failures")
            result.update(outcome="could_not_look",
                          error="coverage incomplete (see %s)" % " and ".join(coverage_detail), error_kind=cause)
    result.update(
        reported=box.reported,
        notes=notes,
        coverage={"files_seen": len(box.seen), "files_read": len(box.read_files),
                  "bytes_read": box.bytes_read, "skipped": incomplete,
                  "complete": not incomplete and not any(direct_observation_failures.values()),
                  "incomplete_why": incomplete_why,
                  "direct_observations": direct_observations,
                  "direct_observation_failures": direct_observation_failures,
                  "unknown_failures": unknown_failures},
        refusal_count=refusal_count[0], refusals_truncated=refusal_count[0] > len(refusals),
        refusals_truncation_why=("refusal note limit of %d reached" % MAX_REFUSALS
                                 if refusal_count[0] > len(refusals) else None),
    )
    # 17 Nov 2026: ensure_ascii=True is explicit and load-bearing. Every non-ASCII
    # character (U+2019, n-tilde, a lone surrogate from an undecodable file name)
    # becomes \uXXXX, so the result line is pure ASCII and survives any console
    # codepage or pipe decoding between the child and the parent.
    data = json.dumps(result, default=str, ensure_ascii=True)
    # 17 Nov 2026: force UTF-8 on the child's real stdout too (cp1252 on Windows
    # would raise or mangle on anything that slipped past), never raising.
    try:
        sys.__stdout__.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    sys.__stdout__.write("\n" + MARKER + data + "\n")
    sys.__stdout__.flush()


# --------------------------------------------------------------------------
# Parent side: spawn, time out, parse
# --------------------------------------------------------------------------

def _empty(outcome, error, notes=None):
    return {"reported": {}, "notes": notes or [],
            "coverage": {"files_seen": 0, "files_read": 0, "bytes_read": 0, "skipped": [],
                         "complete": False, "incomplete_why": ["could_not_read"],
                         "direct_observations": {"open": 0, "os.listdir": 0, "os.scandir": 0},
                         "direct_observation_failures": {"open": 0, "os.listdir": 0, "os.scandir": 0},
                         "unknown_failures": []},
            "outcome": outcome, "error": error, "error_kind": "could_not_read",
            "refusal_count": 0, "refusals_truncated": False, "refusals_truncation_why": None}


def run_script(root, script, timeout=DEFAULT_TIMEOUT,
               max_file_bytes=DEFAULT_MAX_FILE, max_total_bytes=DEFAULT_MAX_TOTAL,
               script_text=None):
    """Run `script` read-only against `root` and return the result dict.
    `root` is one folder or a list of them (None: the current directory); `script`
    is a file path, or "-" with the text in `script_text` (bytes or str)."""
    for name, value in (("max_file_bytes", max_file_bytes), ("max_total_bytes", max_total_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("%s must be a positive integer, not a boolean" % name)
    roots = [root] if isinstance(root, str) else list(root or [os.getcwd()])
    for r in roots:
        if not os.path.isdir(r):
            return _empty("could_not_look", "root does not exist or is not a folder: %s" % r)
        try:
            os.listdir(r)
        except OSError as e:
            return _empty("could_not_look", "root is not readable: %s" % e)
    from_stdin = script == "-"
    if from_stdin:
        if script_text is None or not len(script_text):
            return _empty("could_not_look", "no script text arrived on stdin")
        if isinstance(script_text, str):
            script_text = script_text.encode("utf-8")
    elif not os.path.isfile(script):
        return _empty("could_not_look", "script file not found: %s" % script)
    # 17 Nov 2026: "-X utf8" puts the child in UTF-8 mode (default encoding for
    # open(), stdio and file names) regardless of the Windows locale; -I alone
    # ignores PYTHONUTF8/PYTHONIOENCODING, so the flag is the only reliable switch.
    cmd = [sys.executable, "-I", "-X", "utf8", os.path.abspath(__file__), "--_child",
           "--script", "-" if from_stdin else os.path.abspath(script),
           "--max-file-bytes", str(max_file_bytes), "--max-total-bytes", str(max_total_bytes)]
    for r in roots:
        cmd += ["--root", os.path.realpath(r)]
    try:
        p = subprocess.run(cmd, input=script_text if from_stdin else None,
                           stdin=None if from_stdin else subprocess.DEVNULL,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, cwd=os.path.realpath(roots[0]))
    except subprocess.TimeoutExpired:
        res = _empty("crashed", "timed out after %ss" % timeout,
                      ["child process killed; coverage unavailable"])
        res["error_kind"] = "timed_out"
        return res
    # 17 Nov 2026: decode the child's bytes as UTF-8 explicitly (never the locale
    # codepage). "replace" is acceptable here only because the result line is
    # ASCII (ensure_ascii), so replacement can touch stray human output, never paths.
    stdout = p.stdout.decode("utf-8", "replace")
    idx = stdout.rfind(MARKER)
    if idx < 0:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        return _empty("crashed", "child exited with code %s without a result" % p.returncode,
                      ["child stderr: " + t for t in tail])
    try:
        return json.loads(stdout[idx + len(MARKER):].splitlines()[0])
    except ValueError as e:
        return _empty("crashed", "unreadable result from child: %s" % e)


def _render(res):
    lines = ["outcome: %s" % res["outcome"]]
    if res["error"]:
        lines.append("error:   %s" % res["error"])
    if res.get("error_kind"):
        lines.append("error kind: %s" % res["error_kind"])
    lines.append("reported: " + json.dumps(res["reported"], indent=2, default=str))
    for n in res["notes"]:
        lines.append("note: " + n)
    c = res["coverage"]
    lines.append("coverage: seen %d, read %d, %d bytes, skipped %d, complete %s"
                 % (c["files_seen"], c["files_read"], c["bytes_read"], len(c["skipped"]),
                    c.get("complete", False)))
    if not c.get("complete", False):
        lines.append("coverage incomplete: " + ", ".join(c.get("incomplete_why", [])))
    for s in c["skipped"]:
        lines.append("  skipped %s: %s" % (s["path"], s["why"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------

class _Tally(object):
    def __init__(self):
        self.results = []

    def check(self, name, cond, detail=""):
        ok = bool(cond)
        self.results.append((name, ok))
        print("  %-58s %s" % (name, "pass" if ok else ("FAIL  " + str(detail)).rstrip()))

    def close(self):
        bad = [n for n, ok in self.results if not ok]
        print("")
        if bad:
            print("  %d of %d FAILED: %s" % (len(bad), len(self.results), ", ".join(bad)))
            print("selftest: fail")
            return 1
        print("  all %d checks pass." % len(self.results))
        print("selftest: pass")
        return 0


def _selftest():
    import shutil
    t = _Tally()
    work = os.path.realpath(tempfile.mkdtemp(prefix="elvis_test_"))
    root = os.path.join(work, "tree")
    scripts = os.path.join(work, "scripts")
    os.makedirs(os.path.join(root, "sub"))
    os.makedirs(scripts)
    for rel, body in (("a.md", "Decision: x\n"), ("b.md", "nothing\n"),
                      ("sub/c.md", "Decision: y\n"), ("sub/d.txt", "hello\n")):
        with open(os.path.join(root, rel), "w") as f:
            f.write(body)
    counter = [0]

    def run(src, **kw):
        counter[0] += 1
        path = os.path.join(scripts, "s%02d.py" % counter[0])
        with open(path, "w") as f:
            f.write(src)
        return run_script(kw.pop("root", root), path, **kw)

    def err(r):
        return r["error"] or ""

    try:
        r = run("box.report(count=len(box.list('**/*')))")
        t.check("a. count files -> found, count == 4",
                r["outcome"] == "found" and r["reported"] == {"count": 4}, r)

        r = run("x = 1 + 1")
        t.check("b. reports nothing -> none",
                r["outcome"] == "none" and r["reported"] == {}, r)

        r = run("box.report(a=1)", root=os.path.join(work, "does-not-exist"))
        t.check("c. missing root -> could_not_look",
                r["outcome"] == "could_not_look" and "does not exist" in err(r), r)

        r = run("raise ValueError('boom')")
        t.check("d. script raises -> crashed, error attached",
                r["outcome"] == "crashed" and err(r) == "ValueError: boom", r)

        target = os.path.join(root, "created.txt")
        r = run("open(box.root + '/created.txt', 'w').write('x')")
        t.check("e. open(path, 'w') -> refused, crashed, file NOT created",
                r["outcome"] == "crashed" and "refused" in err(r) and not os.path.exists(target), r)

        target = os.path.join(root, "sqlite-test.db")
        r = run("import sqlite3\nsqlite3.connect('sqlite-test.db')")
        t.check("e0. sqlite3.connect -> refused, crashed, database NOT created",
                r["outcome"] not in ("found", "none") and r["refusal_count"] >= 1
                and "sqlite3.connect" in err(r) and not os.path.exists(target), r)
        if os.path.exists(target):
            os.remove(target)

        r = run("import subprocess\nsubprocess.Popen(['echo', 'hi'])")
        t.check("f. subprocess.Popen -> refused",
                r["outcome"] == "crashed" and "refused" in err(r), r)

        r = run("box.read('../../..')")
        t.check("g. read '../../..' -> refused by name",
                r["outcome"] == "crashed" and "'../../..'" in err(r) and "leaves the root" in err(r), r)

        t0 = time.time()
        r = run("import time\ntime.sleep(30)", timeout=2)
        t.check("h. sleeps past 2s timeout -> timed out",
                r["outcome"] == "crashed" and "timed out after 2s" in err(r) and time.time() - t0 < 15, r)

        r = run("for i in range(500):\n    print('noise', i)\nbox.report(ok=True)")
        stdout_notes = [n for n in r["notes"] if n.startswith("stdout: noise")]
        t.check("i. prints land under notes; reported holds only box.report",
                r["outcome"] == "found" and r["reported"] == {"ok": True}
                and len(stdout_notes) == MAX_STDOUT_LINES
                and any("more lines not shown" in n for n in r["notes"]), r["reported"])

        with open(os.path.join(root, "big.bin"), "w") as f:
            f.write("z" * 3000)
        r = run("n = 0\nfor p in box.list():\n    box.read(p)\n    n += 1\nbox.report(visited=n)",
                max_file_bytes=1000)
        sk = r["coverage"]["skipped"]
        t.check("j. file over per-file cap -> skipped with reason; run completes",
                r["outcome"] == "found" and r["reported"].get("visited") == 5
                and len(sk) == 1 and sk[0]["path"] == "big.bin" and "cap" in sk[0]["why"]
                and r["coverage"]["files_read"] == 4, r)
        os.remove(os.path.join(root, "big.bin"))

        # extras: guardrails the ten cases do not touch
        r = run("import os\nos.remove(box.root + '/a.md')")
        t.check("k. os.remove -> refused, file still there",
                r["outcome"] == "crashed" and os.path.exists(os.path.join(root, "a.md")), r)

        r = run("try:\n    open(box.root + '/swallow.txt', 'w')\nexcept Exception:\n    pass\nbox.report(fine=1)")
        t.check("l. swallowed refusal still ends crashed, no file",
                r["outcome"] == "crashed" and not os.path.exists(os.path.join(root, "swallow.txt")), r)

        r = run("import socket\nsocket.socket()")
        t.check("m. socket.socket -> refused",
                r["outcome"] == "crashed" and "refused" in err(r), r)

        outside = os.path.join(work, "secret.txt")
        with open(outside, "w") as f:
            f.write("secret")
        r = run("print(open(%r).read())" % outside)
        t.check("n. plain open() of a file outside root -> refused",
                r["outcome"] == "crashed" and "refused" in err(r), r)

        r = run("box.read(%r)" % outside)
        t.check("o. box.read of an absolute outside path -> refused",
                r["outcome"] == "crashed" and "absolute path outside the root" in err(r), r)

        r = run("box.read('nope.md')")
        t.check("p. reading a missing file -> could_not_look, not none",
                r["outcome"] == "could_not_look", r)

        # 17 Nov 2026: encoding cases q, r, s (non-ASCII paths, content, notes).
        curly, ntilde = u"\u2019", u"\u00f1"
        rel_dir = u"Compañía%ss birthday/a%so nuevo" % (curly, ntilde)
        rel_file = rel_dir + u"/archivo.md"
        body = u"Cumplea%sos de Compañía %s coraz\u00f3n \u2014 \u00bfqu\u00e9 tal? \u2713\n" % (ntilde, curly)
        os.makedirs(os.path.join(root, *rel_dir.split("/")))
        with io.open(os.path.join(root, *rel_file.split("/")), "w", encoding="utf-8", newline="") as f:  # 17 Nov 2026: newline="" so Windows does not turn LF into CRLF
            f.write(body)

        r = run("box.report(found=[p for p in box.list('**/archivo.md')])")
        got = r["reported"].get("found", [])
        opened = None
        if got:
            with io.open(os.path.join(root, got[0]), "r", encoding="utf-8") as f:
                opened = f.read()
        t.check("q. non-ASCII path from box.list OPENS the real file",
                got == [rel_file] and opened == body and u"\ufffd" not in got[0], (got, opened))

        r = run("box.report(text=box.read(%s))" % ascii(rel_file))
        text = r["reported"].get("text")
        t.check("r. non-ASCII file content from box.read intact, char by char",
                text == body and len(text) == len(body)
                and all(x == y for x, y in zip(text, body)), r)

        sline = u"nota %s %s \u00bfqu\u00e9?" % (curly, ntilde)
        r = run("box.note(%s)\nprint(%s)" % (ascii(sline), ascii(sline)))
        t.check("s. non-ASCII box.note and print() come back intact in notes",
                sline in r["notes"] and ("stdout: " + sline) in r["notes"], r["notes"])

        # 17 Nov 2026: stdin script and several roots. Each case below fails if its feature is reverted.
        me = os.path.abspath(__file__)

        def cli(extra, src, cwd):
            p = subprocess.run([sys.executable, me] + extra + ["--script", "-", "--json"],
                               input=src.encode("utf-8"), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=cwd, timeout=60)
            return json.loads(p.stdout.decode("utf-8", "replace"))

        r = cli(["--root", root], "box.report(n=len(box.list('**/*.md')))", work)
        t.check("t. --script - runs the script given on stdin",
                r["outcome"] == "found" and r["reported"] == {"n": 4}, r)

        root2 = os.path.join(work, "tree2")
        os.makedirs(root2)
        with open(os.path.join(root2, "e.txt"), "w") as f:
            f.write("from the second root\n")
        src2 = ("box.report(one=box.read('a.md'), two=box.read(%s),"
                " listed=[box.read(p) for p in box.list('**/e.txt')])"
                % ascii(os.path.join(root2, "e.txt").replace(os.sep, "/")))
        r = run(src2, root=[root, root2])
        t.check("u. two roots: a file from each root reads fine",
                r["outcome"] == "found" and r["reported"].get("one", "").strip() == "Decision: x"
                and r["reported"].get("two", "").strip() == "from the second root"
                and [x.strip() for x in r["reported"].get("listed", [])] == ["from the second root"], r)

        r = cli([], "box.report(root=box.root, roots=box.roots, n=len(box.list()))", root)
        t.check("v. no --root at all: the current directory is the root",
                r["outcome"] == "found" and r["reported"].get("roots") == [root]
                and r["reported"].get("n") == 5, r)

        r = run("box.report(roots=box.roots, same=(box.root == box.roots[0]))", root=[root, root2])
        t.check("w. box.roots holds both roots, box.root == box.roots[0]",
                r["reported"].get("roots") == [root, root2] and r["reported"].get("same") is True, r)

        r = run("box.read(%r)" % outside, root=[root, root2])
        t.check("x. a path outside every root is still refused",
                r["outcome"] == "crashed" and "outside the root" in err(r), r)

        r = run("try:\n    box.read('../secret.txt')\nexcept PermissionError:\n    pass")
        t.check("y. caught box path refusal is still crashed",
                r["outcome"] == "crashed" and r["refusal_count"] == 1
                and r["error_kind"] == "operation_denied", r)

        r = run("try:\n    box.read('../secret.txt')\nexcept PermissionError:\n    pass\nbox.report(ok=True)")
        t.check("z. caught box refusal remains visible beside a report",
                r["outcome"] == "crashed" and r["reported"] == {"ok": True}
                and r["refusal_count"] == 1, r)

        with open(os.path.join(root, "budget-only-a.txt"), "w") as f:
            f.write("a" * 20)
        with open(os.path.join(root, "budget-only-b.txt"), "w") as f:
            f.write("b" * 20)
        r = run("box.read('budget-only-a.txt')\nbox.read('budget-only-b.txt')", max_total_bytes=25)
        t.check("aa. exhausted budget without a report is could_not_look",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["incomplete_why"] == ["budget_exhausted"], r)

        with open(os.path.join(root, "cap-only.txt"), "w") as f:
            f.write("z" * 200)
        r = run("box.read('cap-only.txt')", max_file_bytes=100)
        t.check("ab. per-file cap without a report is could_not_look",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["incomplete_why"] == ["file_cap_exceeded"], r)

        r = run("box.read('cap-only.txt')\nbox.report(ok=True)", max_file_bytes=100)
        t.check("ac. a report after every attempted read is skipped cannot look",
                r["outcome"] == "could_not_look" and r["coverage"]["files_read"] == 0
                and not r["coverage"]["complete"]
                and r["coverage"]["incomplete_why"] == ["file_cap_exceeded"], r)

        blind_root = Path(work) / "all-skipped"
        blind_root.mkdir()
        for name in ("one.txt", "two.txt"):
            (blind_root / name).write_text("xxxxxx", encoding="utf-8")
        r = run("hits = box.grep('x')\nbox.report(hits=len(hits))",
                root=str(blind_root), max_file_bytes=5)
        t.check("ac0. reported grep after every read is skipped cannot look",
                r["outcome"] == "could_not_look"
                and {"found": 0, "none": 0, "could_not_look": 2}.get(r["outcome"], 3) == 2
                and r["coverage"]["files_read"] == 0
                and {item["path"] for item in r["coverage"]["skipped"]} == {"one.txt", "two.txt"}
                and r["reported"] == {"hits": 0}, r)

        partial_root = Path(work) / "partly-read"
        partial_root.mkdir()
        (partial_root / "readable.txt").write_text("x\n", encoding="utf-8")
        (partial_root / "skipped.txt").write_text("xxxxxx", encoding="utf-8")
        r = run("hits = box.grep('x')\nbox.report(hits=len(hits))",
                root=str(partial_root), max_file_bytes=5)
        t.check("ac1. reported grep with one read and one skip remains found",
                r["outcome"] == "found" and r["coverage"]["files_read"] == 1
                and [item["path"] for item in r["coverage"]["skipped"]] == ["skipped.txt"]
                and r["reported"] == {"hits": 1}, r)

        r = run("for p in box.list():\n    box.read(p)")
        t.check("ad. a complete clean scan with no report is none",
                r["outcome"] == "none" and r["coverage"]["complete"], r)

        direct = [
            "import subprocess\nsubprocess.Popen(['echo', 'hi'])",
            "import os\nos.system('echo hi')",
        ]
        rs = [run(d) for d in direct]
        t.check("ag. user script calling Popen/os.system directly is STILL refused",
                all(x["outcome"] == "crashed" and "refused" in err(x) for x in rs), rs)

        with open(os.path.join(root, "cap.txt"), "w") as f:
            f.write("z" * 200)
        r = run("box.read('cap.txt', max_bytes=1000)", max_file_bytes=100)
        t.check("ah. per-call max_bytes cannot raise the run per-file cap",
                r["coverage"]["skipped"] and "cap of 100" in r["coverage"]["skipped"][0]["why"], r)

        rn = run("box.read('a.md', -1)")
        rb = run("box.read('a.md', True)")
        t.check("ai. negative or boolean max_bytes is a clear call error",
                rn["outcome"] == "crashed" and rb["outcome"] == "crashed"
                and "positive integer" in err(rn) and "positive integer" in err(rb), (rn, rb))

        with open(os.path.join(root, "budget-a.txt"), "w") as f:
            f.write("a" * 20)
        with open(os.path.join(root, "budget-b.txt"), "w") as f:
            f.write("b" * 20)
        r = run("box.read('budget-a.txt')\nbox.read('budget-b.txt')", max_total_bytes=25)
        t.check("aj. exhausted total budget skips later reads and is incomplete",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["incomplete_why"] == ["budget_exhausted"]
                and any(x["path"] == "budget-b.txt" and "budget" in x["why"] for x in r["coverage"]["skipped"]), r)

        r = run("for i in range(250):\n    box.note(i)\ntry:\n    import subprocess\n    subprocess.Popen(['echo', 'no'])\nexcept Exception:\n    pass")
        t.check("ak. refusals survive full notes and report their total",
                r["outcome"] == "crashed" and r["refusal_count"] == 1
                and any(n.startswith("REFUSED:") for n in r["notes"]), r)

        r = run("if True print('broken')")
        t.check("al. invalid script syntax is crashed with exit-class data",
                r["outcome"] == "crashed" and r["error_kind"] == "script_invalid", r)

        r = run("try:\n    open('no-existe').read()\nexcept OSError:\n    pass")
        t.check("an. caught missing direct open is could_not_look, not none",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["direct_observation_failures"]["open"] == 1, r)

        r = run("import os\ntry:\n    os.listdir('no-existe')\nexcept OSError:\n    pass")
        t.check("an0. caught direct listdir failure is could_not_look, not none",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["direct_observation_failures"]["os.listdir"] == 1, r)

        r = run("import os\ntry:\n    list(os.scandir('no-existe'))\nexcept OSError:\n    pass")
        t.check("an0a. caught direct scandir failure is could_not_look, not none",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["direct_observation_failures"]["os.scandir"] == 1, r)

        with open(os.path.join(root, "bad-utf8.txt"), "wb") as f:
            f.write(b"\xff")
        r = run("try:\n    open('bad-utf8.txt', encoding='utf-8').read()\nexcept UnicodeDecodeError:\n    pass")
        t.check("an1. caught direct decoding failure is could_not_look, not none",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["direct_observations"]["open"] == 1
                and r["coverage"]["direct_observation_failures"]["open"] == 1, r)

        r = run("import pathlib\noriginal = pathlib.Path.is_file\ndef bad(self):\n    if self.name == 'a.md':\n        raise OSError('cannot examine')\n    return original(self)\npathlib.Path.is_file = bad\nbox.list()")
        t.check("an2. box.list examination failure is skipped and incomplete",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and any(x["path"] == "a.md" and "cannot examine" in x["why"]
                        for x in r["coverage"]["skipped"]), r)

        r = run("open('a.md').read()")
        t.check("ao. successful direct open is counted while a no-report run stays none",
                r["outcome"] == "none" and r["coverage"]["complete"]
                and r["coverage"]["direct_observations"]["open"] == 1, r)

        empty = os.path.join(root, "empty")
        os.makedirs(empty)
        r = run("import os\nos.listdir('empty')")
        t.check("ap. direct listdir of an empty root directory is counted",
                r["outcome"] == "none" and r["coverage"]["direct_observations"]["os.listdir"] == 1, r)

        r = run("import os\nlist(os.scandir('empty'))")
        t.check("aq. direct scandir of an empty root directory is counted",
                r["outcome"] == "none" and r["coverage"]["direct_observations"]["os.scandir"] == 1, r)

        r = run("box.report(text=open('a.md').read())")
        t.check("ar. direct open beside a report stays found",
                r["outcome"] == "found" and r["coverage"]["direct_observations"]["open"] == 1, r)

        r = run("n = 0\nfor line in open('a.md'):\n    n += 1\nbox.report(lines=n)")
        t.check("ar0. iterating an open file completely keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and not r["coverage"]["incomplete_why"], r)

        r = run("box.report(text=open('a.md').read())")
        t.check("ar1. open.read beside a report keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and not r["coverage"]["incomplete_why"], r)

        r = run("box.report(lines=open('a.md').readlines())")
        t.check("ar2. open.readlines beside a report keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and not r["coverage"]["incomplete_why"], r)

        r = run("with open('a.md') as f:\n    text = f.read()\nbox.report(text=text)")
        t.check("ar3. with open read beside a report keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and not r["coverage"]["incomplete_why"], r)

        r = run("box.report(text=open(file='a.md').read())")
        t.check("ar3a. open(file=...) is counted and keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and r["coverage"]["direct_observations"]["open"] == 1, r)

        r = run("import io\nbox.report(text=io.open('a.md').read())")
        t.check("ar3b. io.open is counted and keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and r["coverage"]["direct_observations"]["open"] == 1, r)

        r = run("import pathlib\nbox.report(text=pathlib.Path('a.md').read_text())")
        t.check("ar3c. Path.read_text is counted and keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and r["coverage"]["direct_observations"]["open"] == 1, r)

        r = run("import pathlib\nbox.report(text=pathlib.Path('a.md').open().read())")
        t.check("ar3d. Path.open is counted and keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and r["coverage"]["direct_observations"]["open"] == 1, r)

        r = run("import os\nfd = os.open('a.md', os.O_RDONLY)\nos.read(fd, 99)\nos.close(fd)\nbox.report(ok=True)")
        t.check("ar3e. os.open is counted and keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and r["coverage"]["direct_observations"]["open"] == 1, r)

        r = run("import pathlib\ntry:\n    pathlib.Path('no-existe').read_text()\nexcept OSError:\n    pass")
        t.check("ar3f. caught missing Path.read_text is could_not_look",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["direct_observation_failures"]["open"] == 1, r)

        r = run("import io\ntry:\n    io.open('no-existe')\nexcept OSError:\n    pass")
        t.check("ar3g. caught missing io.open is could_not_look",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["direct_observation_failures"]["open"] == 1, r)

        r = run("import os\ntry:\n    os.open('no-existe', os.O_RDONLY)\nexcept OSError:\n    pass")
        t.check("ar3h. caught missing os.open is could_not_look",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"]
                and r["coverage"]["direct_observation_failures"]["open"] == 1, r)

        r = run("import os\nbox.report(entries=os.listdir('.'))")
        t.check("ar4. os.listdir beside a report keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and not r["coverage"]["incomplete_why"], r)

        r = run("import os\nn = 0\nfor entry in os.scandir('.'):\n    n += 1\nbox.report(entries=n)")
        t.check("ar5. iterating scandir completely keeps coverage complete",
                r["outcome"] == "found" and r["coverage"]["complete"]
                and not r["coverage"]["incomplete_why"], r)

        r = run("x = 1")
        t.check("as. no observation and no report stays none with complete coverage",
                r["outcome"] == "none" and r["coverage"]["complete"]
                and not any(r["coverage"]["direct_observations"].values()), r)

        r = run("open('a.md').read()\ntry:\n    raise OSError('unrelated')\nexcept OSError:\n    pass")
        t.check("at. unrelated caught OSError does not fail a successful open",
                r["outcome"] == "none" and r["coverage"]["complete"]
                and not r["coverage"]["direct_observation_failures"]["open"], r)

        r = run("open('a.md').read()\ntry:\n    raise FileNotFoundError(2, 'manual', 'a.md')\nexcept OSError:\n    pass")
        t.check("au. manually raised filename does not fail a successful open",
                r["outcome"] == "none" and r["coverage"]["complete"]
                and not r["coverage"]["direct_observation_failures"]["open"], r)

        r = run("import os\ntry:\n    list(os.walk('no-existe'))\nexcept OSError:\n    pass")
        t.check("av. caught missing os.walk is could_not_look, not none",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"], r)

        r = run("import glob\ntry:\n    glob.glob('no-existe/*')\nexcept OSError:\n    pass")
        t.check("aw. caught missing glob is could_not_look, not none",
                r["outcome"] == "could_not_look" and not r["coverage"]["complete"], r)

        full = run("x = 1")
        short = run("box.read('cap.txt')", max_file_bytes=100)
        t.check("am. incomplete coverage differs from a complete no-result run",
                full["outcome"] == "none" and short["outcome"] == "could_not_look"
                and full["coverage"]["complete"] and not short["coverage"]["complete"], (full, short))
    except Exception as e:  # the test itself broke
        t.results.append(("selftest crashed", False))
        print("  selftest crashed: %s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return t.close()


def _positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer")
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run a small Python script read-only against a folder and return only what it reports.")
    ap.add_argument("--root", action="append",
                    help="folder the script may look at; repeat for several (default: the current directory)")
    ap.add_argument("--script", help="path to the script file, or - to read it from stdin (gets a global named box)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="wall-clock seconds (default 60)")
    ap.add_argument("--json", action="store_true", help="print the raw JSON result only")
    ap.add_argument("--max-file-bytes", type=_positive_int, default=DEFAULT_MAX_FILE,
                    help="positive per-file read cap in bytes (default: 2 MiB)")
    ap.add_argument("--max-total-bytes", type=_positive_int, default=DEFAULT_MAX_TOTAL,
                    help="positive total read budget in bytes for the whole run (default: 64 MiB)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a._child:
        _child_main(a)
        return 0
    # 17 Nov 2026: UTF-8 on the parent's own stdout/stderr BEFORE any print
    # (selftest included), with errors="replace" so printing a path never raises
    # on a cp1252 console. Moved up from just before the final print.
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if a.selftest:
        return _selftest()
    if not a.script:
        ap.error("--script is required (or use --selftest)")
    text = sys.stdin.buffer.read() if a.script == "-" else None
    res = run_script(a.root, a.script, a.timeout, a.max_file_bytes, a.max_total_bytes,
                     script_text=text)
    # 17 Nov 2026: stdout is already reconfigured above. --json keeps ensure_ascii
    # (default) so the JSON a caller captures is ASCII and codepage-proof.
    print(json.dumps(res, indent=None if a.json else 2, default=str) if a.json else _render(res))
    return {"found": 0, "none": 0, "could_not_look": 2}.get(res["outcome"], 3)


if __name__ == "__main__":
    sys.exit(main())
