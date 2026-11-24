# Credits

**Built by [alphasyomega](https://github.com/alphasyomega) and Claude.**

Ideas carry the name of whoever had them, also when we did not take their code.

---

## Who found something and made this better

**Aya ([@ayakohi](https://github.com/ayakohi)), Cygnus and Assistant-Kun** tested this repository
against the promises in its own README and reported two real gaps, with the exact scripts that
produced them.

- **A database written from inside C code.** Python raises no alarm for it, so the read-only guard
  never heard the write. `sqlite3.connect` is refused today, and the limits on that page are worded
  honestly, because of this.
- **A run that opened not one file still answering `found`, with exit code 0.** The page already
  promised `could_not_look` for that case; the code did something else. It now answers what the
  page promised.

**Both are closed, and a self-test guards each one.** They also corrected our first guess at the
cause of the second: `box.grep` does go through `box.read`; the defect was the order in which the
outcome is decided.

**A reading like that is a gift. Thank you.**

---

## The one method this piece took from outside

**An agent that writes code which makes its own tool calls, instead of calling one tool at a time.**
The name **Code Mode** is **Cloudflare's** — they published the pattern under that name:
https://blog.cloudflare.com/code-mode-mcp/
**Anthropic** published their own version as *"code execution with MCP"*, and they credit
Cloudflare's Code Mode as the inspiration:
https://www.anthropic.com/engineering/code-execution-with-mcp
Claude Code's terminal feature carries the name Code Mode too:
https://code.claude.com/docs/en/code-mode
It is the whole idea of this piece: the agent writes one small script, the script does the walking,
and only what the script decided to report comes back.

**No code was taken.** This engine was written from scratch, because neither version runs where we
work: Claude Code's Code Mode is terminal-only, and Anthropic's runs in a sandbox that cannot reach
your own folders. **The idea carries Cloudflare's and Anthropic's names all the same.**

---

## Other pieces of the package

This piece is one of the set. The others carry their own credits in the full
package: **Aya ([@ayakohi](https://github.com/ayakohi))** with Cygnus, for decision hygiene
before building; and **Anne and Chadrien Solance ([@SolanceLab](https://github.com/SolanceLab))**
(the trail from a line of code back to the decision that put it there, and the
map over a folder).
