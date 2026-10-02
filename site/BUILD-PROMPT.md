# Launch prompt for one build

Paste verbatim into a fresh agent, replacing `<model>` with `fable`, `opus` or `sonnet`.

---

You are building one of three independent versions of a public landing page for Henk, a
personal homelab agent. Work only from `/home/wsl/Coding/henk/site/BRIEF.md` and the
files it names (`henk-public.json` beside it, and `README.md` and `NORTH-STAR.md` in
`/home/wsl/Coding/henk`). Do not open `/home/wsl/Coding/henk/site/builds/` except your
own directory, `/home/wsl/Coding/henk/site/builds/<model>/`. Do not look at buzz.xyz or
any other site; the brief carries what matters from it.

Read the brief in full first. Then, before writing code, write a compact plan in your
head: palette, display face, creature drawing, copy, motion timing, and the one thing a
visitor will remember. Check it against the brief's "register" and "content inventory"
and against the fingerprint rule: if a similar prompt for a different product would land
in the same place, change it. Then build, following your plan.

Deliver into your directory exactly: `index.html` (self-contained, see the quality
floor), `NOTES.md` (about ten lines: what you replaced from the defaults and why, what
you cut, what you would do with another hour), and `shots/desktop-ok.png`,
`shots/phone-ok.png`, `shots/desktop-incident.png` at 1440×900 and 375×812. Take the
screenshots with the Playwright MCP tools (`browser_navigate` to the `file://` path,
`browser_resize`, `browser_take_screenshot` with a filename inside your `shots/`
directory; the Playwright root allows paths under `/home/wsl/Coding`). Look at every
screenshot yourself and fix what is wrong before finishing: the name at display scale,
the typing moment visible, both page colours correct, no horizontal scroll at 375px.
Preview the incident state with the mechanism you built (for example `?state=incident`;
with `file://` URLs a query string may not pass, so a `#incident` hash is an acceptable
alternative, say which in NOTES.md).

Do not commit, push, or touch any file outside your directory. Copy the mock JSON into
your directory as `henk-public.json` so the relative fetch works there too. When done,
reply with the list of files you produced and the three screenshot paths, nothing more.
