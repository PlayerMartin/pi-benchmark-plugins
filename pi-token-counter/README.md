# pi-token-counter

A Pi extension that counts input and output tokens for the current session.

- **Interactive (TUI):** live `tokens: ↑in ↓out` status in the footer, updated after every model response, plus a `/tokens` command that shows the full report (requests, input, output).
- **Headless (`pi -p "..."`):** prints the token report to the terminal right before Pi exits.
- **JSON mode:** the report goes to stderr so the JSON stream on stdout stays machine-readable.

Usage is accumulated from every assistant response, plus usage reported by tools that made nested model calls (e.g. subagents).

## Install

Copy (or symlink) this directory into one of Pi's extension locations:

```bash
# user-level (~/.pi/agent/extensions/)
cp -r pi-token-counter ~/.pi/agent/extensions/

# or project-level (<project>/.pi/extensions/)
mkdir -p .pi/extensions
cp -r pi-token-counter .pi/extensions/
```

No build step is required — Pi loads TypeScript extensions directly.

Or load it ad hoc with a flag:

```bash
pi --extension /path/to/pi-token-counter
```

## Headless usage

```bash
pi -p "explain this repo" --extension /path/to/pi-token-counter
```

Example output printed to the terminal before exit:

```
──── token usage ────
requests:     3
input:        12,345
output:       1,234
```

## Interactive usage

```
/tokens
```

shows the same report as a notification, and the footer shows a running
`tokens: ↑12,345 ↓1,234` counter while you work.
