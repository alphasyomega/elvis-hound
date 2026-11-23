# Credits

**Built by [alphasyomega](https://github.com/alphasyomega) and Claude.**

Ideas carry the name of whoever had them, also when we did not take their code.

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
