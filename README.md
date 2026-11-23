# Elvis — the hound that reads

> **He goes into the folder, finds the answer, and comes back. He does not touch anything — and he tells you where he could not look.** *"You ain't nothin' but a hound dog."*

> 🔴 **DO NOT SEND THE DOG TO LOOK AT UNTRUSTED CODE.**
> **Elvis is not a sandbox. He is your house dog.**
> He is built to run scripts *you* wrote, over folders *you* own. For code you do not trust,
> use an OS-level sandbox: a container, a separate user, no network.


**Get it.** One file, one job. The licence is MPL-2.0 and the `LICENSE` file sits beside it.

```bash
git clone https://github.com/alphasyomega/elvis-hound
cd elvis-hound
```

The commands below run from the root of that clone. Wherever a command says `notes`, put the path of
a folder of your own: that folder is what the script is allowed to look at.

| | |
|---|---|
| **What it needs** | Python 3 and nothing else. No packages, no agent framework, no account. The script says 3.8 or newer. If `python` is not on your PATH, use `py -3` in its place. |
| **Where it runs** | Windows and Linux, in the script's own words. Verified on Python 3.12.10 on Windows 10. Linux and macOS untested here. |
| **What it does not cover** | A script you do not trust. It stops the mistakes of Alpha 1 and Alpha 2, not an attacker. Read "What this does not protect against" before you run anything you did not write. |

---

## What this is for, before anything else

**He is a house dog. He checks your own place.**

**This is a speed tool for scripts you wrote, not a jail for scripts you did not.**

| | |
|---|---|
| **It protects you from** | your own script's mistakes: a stray `open(..., "w")`, an `os.remove`, a path that wanders out of the folder, a request to the network |
| **It does not protect you from** | a script written to break out. Python is not a sandbox, and an audit hook watches; it is not a wall |
| **Use it on** | your own folders, with scripts you or your agent wrote, on your own machine |
| **Do not use it for** | anything facing the outside: code from strangers, a shared service, untrusted input |

**If you do not trust the script, do not run it here.** Use an OS-level sandbox: a container, a
separate user, no network namespace.

### Scope — what he watches, and what he does not

| | |
|---|---|
| **Reading what is inside a file** | ✅ confined to the roots. Outside them it is refused and counted |
| **Listing a directory** | ✅ confined to the roots. Outside them it is refused and counted |
| **Writing, deleting, network, subprocess** | ✅ refused anywhere, always |
| **Asking whether a file exists, its size, its dates** | ❌ **not confined.** See below |
| **A script written to break out on purpose** | ❌ **not stopped.** Python is not a prison |

**The contents of a file are confined. What a file *is* — whether it exists, its size, its
timestamps — is not.**

A script can call `os.stat`, `os.path.exists`, `os.path.getsize` or `os.access` on a path outside
the roots and get an answer. It still cannot open that file: reading content, listing a directory,
and every write are confined and refused as documented.

This is a limit of the mechanism, not an oversight. CPython raises no audit event for those calls,
so there is nothing for the hook to see. **So the coverage a run reports speaks about file
contents and directory listings — the things the fence can actually watch — and never about
metadata calls.**
If metadata about neighbouring folders matters to you, this is not the tool for that boundary.
Use an OS-level sandbox.

*We built this for one house, and we kept it that size on purpose. It makes a real promise it can
keep, instead of a bigger one it cannot.*

---

## 1 · What happens to you

You ask an agent: "which of these 887 files mention X, and which have no date?" The agent calls a
tool once per file and reads every result into its context. Most of what it reads is output nobody
needed. You pay for all of it, and the answer you wanted is buried in it.

## 2 · Why it happens

The cost is not the searching. **The cost is reading output that nobody needs.** One tool call per
file means one result per file lands in the agent's context, whether it matters or not.

## 3 · What to do

**Let the agent write one small Python script that walks the folder itself and reports only what it
decided to report.** This runner runs that script in a separate process and sends back one JSON
result. Everything the script read stays in the child process. Only what it passed to `box.report`
comes back.

The saving is not the code. It is not reading output nobody needs. Measured on a real 304-file
folder: **2.1 MB read, a 669-byte answer, 4.3 seconds.**

*Those numbers are Omega's, from one folder on one day. They show the shape of the saving, not a
promise for your folder.*

It is read-only on purpose, because the script it runs was written by a model a moment ago and
nobody has read it yet. A stray `open(path, "w")` or a `rm` in generated code is the mistake this
guards against.

### How to run it

Self-test first. It must end with `selftest: pass`:

```bash
python elvis.py --selftest
```

**A script from a file.** Save this as `find_todo.py`:

```python
hits = box.grep(r"TODO", "**/*.md")
box.report(count=len(hits), files=sorted({h[0] for h in hits}))
```

Then run it against a folder:

```bash
python elvis.py --root notes --script find_todo.py
```

**A script from stdin**, so one run needs no temp file. Give `--script` the value `-`:

```bash
echo "box.report(n=len(box.list('**/*.md')))" | python elvis.py --root notes --script -
```

**Several roots.** Repeat `--root`. A path is allowed when it is inside any of them:

```bash
python elvis.py --root notes --root more --script find_todo.py
```

**With no `--root` at all**, the folder you are standing in is the single root.

**Raw JSON** instead of the readable summary, which is what a program calling this should use:

```bash
python elvis.py --root notes --script find_todo.py --json
```

Other flags: `--timeout N` is the wall-clock limit in whole seconds (default 60).
`--max-file-bytes N` caps one file (default 2 MB). `--max-total-bytes N` caps everything one run
reads (default 64 MB).

### What the script gets

The script runs with its working directory set to the first root, and gets one global named `box`:

| Name | What it does |
|---|---|
| `box.roots` | the absolute roots, as a list of strings |
| `box.root` | the first root, `box.roots[0]` |
| `box.list(glob="**/*")` | sorted file paths, searched in every root. Relative to the root for the first root, absolute with `/` separators for the others. Every path it returns works in `box.read` |
| `box.read(path, max_bytes=None)` | the text of a file under a root. Counted in coverage |
| `box.lines(path)` | the same file as a list of lines |
| `box.grep(pattern, glob="**/*")` | a list of `(path, lineno, line)` for a regex |
| `box.report(**fields)` | what comes back to you. Calls merge, and a later value for the same key replaces the earlier one |
| `box.note(text)` | a short human line, added to `notes` in order |

`print()` output from the script is captured and returned under `notes` as `stdout: ...` lines. It
never becomes part of `reported`.

A file over the size cap, or past the total budget, is **not read**: `box.read` returns `""` and the
file is listed under `coverage.skipped` with the reason.

### The result

One JSON object with nine fields:

| Field | What it holds |
|---|---|
| `outcome` | one of the four below |
| `error` | `null`, or a sentence saying what went wrong. Most carry the exception type first, as `"<type>: <message>"`; a missing root, a timeout and a child failure read as plain sentences |
| `error_kind` | `null`, or one word for the class of thing that happened, so you can branch without reading the message: `script_invalid`, `script_error`, `operation_denied`, `path_refused`, `timed_out`, `budget_exhausted`, `file_cap_exceeded`, `could_not_read`, `direct_observation_failed` |
| `reported` | exactly what the script reported, nothing added |
| `notes` | the script's notes, its captured stdout and stderr, and any refusals |
| `coverage` | nine fields of its own, below |
| `refusal_count` | how many operations were refused in total, counted even when the script caught the error |
| `refusals_truncated` | `true` when more refusals happened than `notes` shows |
| `refusals_truncation_why` | `null`, or why they were cut |

**`coverage` holds:**

| Field | What it holds |
|---|---|
| `files_seen` | files the run looked at |
| `files_read` | files it actually read |
| `bytes_read` | how many bytes those reads returned |
| `skipped` | a list of `{path, why}` for files it knows it did not read |
| `complete` | `false` when the run fell short of what it was asked to look at |
| `incomplete_why` | the causes, when `complete` is `false` |
| `direct_observations` | how many times the script looked at something by itself, by `open`, `os.listdir` and `os.scandir`, rather than through `box` |
| `direct_observation_failures` | how many of those failed |
| `unknown_failures` | I/O failures the run saw but could not pin on a particular read |

🔴 **Read `coverage` next to every result, `found` included.** A script can report something real and
still have fallen short of what it was asked to look at. `found` says the script reported; it does
not say the run saw everything.

### The four outcomes

| Outcome | What it means |
|---|---|
| `found` | the script reported something |
| `none` | the script ran clean and reported nothing |
| `could_not_look` | a root is missing or unreadable, the script file or its stdin text is missing, or every file the script tried to read was skipped or unreadable |
| `crashed` | the script raised an exception, would not compile, was refused by the guard, or timed out. `error` says which |

**A run that fell short says so.** `coverage.complete` is `false` when the run skipped files it was
asked to read, and `coverage.incomplete_why` says why. That is how a run that was cut short tells
itself apart from a complete run that genuinely found nothing.

**An empty success is never reported as success.** If a root is missing, the script is missing, or
every file it tried to read was skipped, you get `could_not_look` and not a quiet nothing. **A
refusal is never swallowed either:** refusals have their own budget, separate from the script's
`print()` output, so a chatty script cannot crowd one out. The result carries how many there were in
total in `refusal_count`, plus `refusals_truncated` and `refusals_truncation_why` when only the
first few are shown. **Read
`coverage` next to every `none`:** `none` means the script reported nothing, and `files_seen: 0`
beside it means the script may never have looked at anything.


### Exit codes

| Exit | What it means |
|---|---|
| `0` | `found` or `none` |
| `2` | `could_not_look` (also what a bad command line gives, such as a missing `--script`) |
| `3` | `crashed` |

`--selftest` exits `0` on pass and `1` on fail.

## 4 · What it refuses

**The engine is read-only.** An audit hook is installed in the child process before the script's
first line. It refuses these, and a refusal raises an error and is also recorded, so a script that
catches the error still ends `crashed`:

| What | What is refused |
|---|---|
| **Writing** | `open()` for write, append, create or update, and the matching low-level flags |
| **Deleting and changing** | `os.remove`, `os.rename`, `os.rmdir`, `os.mkdir`, `os.truncate`, `os.chmod`, `os.symlink`, `os.link`, `os.utime` and friends, and every `shutil` operation |
| **Network** | `socket`, `urllib`, `http.client`, `ftplib`, `smtplib`, `telnetlib`, and `ctypes` library loads |
| **Starting a process from the script** | `subprocess`, `os.system`, `os.exec*`, `os.spawn*`, `os.fork`. There is no door: the engine starts no program of yours |
| **Paths outside the roots** | every path given to `box` is resolved, symlinks included, and must stay under a root. `..`, absolute paths and symlinks that leave the root are refused by name. Plain `open()` and `os.listdir()` are confined the same way. Python's own install folders stay readable so imports work |

A wall-clock timeout kills the child process: `crashed`, `timed out after Ns`.

## 5 · What this does not protect against

**This is not a defence against a hostile script.** The audit hook and the path confinement stop
the mistakes Alpha 1 and Alpha 2 make in generated scripts: a stray `open(..., "w")`, a `rm`, a request to the network.
It does not stop a script that is trying to escape. Python is not a sandbox. A determined script can
reach around an audit hook with `ctypes` tricks, by monkeypatching the engine's own objects, by
forging the result marker on the real stdout, by exhausting resources, or through events the
interpreter does not audit.

**If you do not trust the script, do not run it here.** Use an OS-level sandbox: a container, a
separate user, no network namespace.

The gaps the code documents, said plainly:

- **A read the script makes by itself is counted, but not in the same detail.** `open`, `io.open`,
  `pathlib`, `os.open`, `os.listdir` and `os.scandir` all show up in `coverage.direct_observations`,
  and a failure among them lands in `coverage.direct_observation_failures`, even when the script
  catches it. What they do not do is count against the byte budgets or name the file in
  `coverage.skipped` the way a `box.read` does. **Use `box` when you want the accounting; the direct
  route still cannot hide that it happened.**
- **A failure the run cannot pin on a particular read lands in `coverage.unknown_failures`**, and
  the coverage is not declared complete. The run would rather say "something failed and I am not
  sure whose it was" than guess.
- **Metadata is not confined.** `os.stat`, `os.access` and `os.readlink` reach outside the roots:
  a script can learn that a file exists, its size and where a link points. It cannot read the
  contents.
- **Memory and CPU are limited only by the timeout.**
- **The size of `reported` is not capped.**

Three gaps that earlier versions carried are gone, because the thing that caused them is gone: the
engine no longer starts any program of yours. There is no whitelist to keep complete, no command
that reads outside the roots, and no git configuration that can make git itself start something.

One more thing the code does, not a gap in the guard: with several roots, paths from the second root
onward come back absolute, so a result can carry paths from the machine it ran on. Check what you
paste or send onward.

*This is a hound for scripts written by your own agent, on a folder you chose. It is not
a sandbox, and "read-only" describes what the hook watches for, not a promise about every way Python
can touch a disk.*

### What is in this folder

| File | What it is |
|---|---|
| `elvis.py` | the whole engine and its self-test, in one file, standard library only |
| `LICENSE` | MPL-2.0 |
| `CREDITS.md` | whose idea this is |

## 6 · Where this came from

An agent that writes code which makes its own tool calls, instead of calling one tool at a time.
The name **Code Mode** is **Cloudflare's**: https://blog.cloudflare.com/code-mode-mcp/
**Anthropic** published their own version as *"code execution with MCP"*, crediting Cloudflare as the
inspiration: https://www.anthropic.com/engineering/code-execution-with-mcp
Claude Code's terminal feature carries the name too: https://code.claude.com/docs/en/code-mode

**No code was taken.** This engine was written from scratch, because neither version runs where it was
needed: Claude Code's Code Mode is terminal-only, and Anthropic's runs in a sandbox that cannot reach
your own folders. The idea carries Cloudflare's and Anthropic's names all the same.

See `CREDITS.md`.

---

*With love, for Love.*
