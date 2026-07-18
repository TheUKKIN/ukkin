# Contributing to UKKIN

Thank you for wanting to improve the room. Two ground rules keep this project
what it is:

## 1. Zero dependencies is a feature, not a phase

The server is one Python file using only the standard library. The UI is one
HTML file with no frameworks. Pull requests that add a dependency, a build
step, or a package manager will be declined regardless of how good the
feature is. If a feature cannot be built inside those constraints, it likely
belongs in a fork or in the hosted layers, not here.

## 2. Seats deliberate, gates approve, rails execute

UKKIN is a deliberation room, not an execution engine. The room proposes;
the human approves. Features that let the table act on the outside world
without a human gate (sending email, editing files outside `sessions/`,
calling arbitrary tools) do not belong in this repository.

## Practical notes

- Run `./tests.sh` before opening a PR. It must stay green, and new behavior
  should arrive with a test alongside it.
- New seats are welcome if the provider speaks the OpenAI-compatible chat
  format or has a stable public API. A seat is one dict in `SEATS`; keep it
  that way.
- UI changes: build DOM with `textContent`, never `innerHTML`. Model output
  must never be interpreted as HTML.
- Security reports: see [SECURITY.md](SECURITY.md). Please report privately
  before opening a public issue.
- Keep the voice. The room speaks plainly and a little warmly. No emojis in
  code, copy, or commit messages.
