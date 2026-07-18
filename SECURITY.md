# UKKIN security model

UKKIN is a local-first application. The whole security posture follows from one
decision: **the room runs on your machine and never phones home.**

## What the room does

- Binds to `127.0.0.1` only. The server is unreachable from your network, your
  router, and the internet. There is no flag to change this; if you want remote
  access, put it behind a tunnel you control and understand.
- Sends prompts directly from your machine to the providers whose keys YOU put
  in `.env`, and to no one else. There is no UKKIN cloud, no telemetry, no
  analytics, no update check, no third-party requests of any kind.
- Persists room state to `sessions/` next to the script. Your transcripts and
  canvases are plain files you own, and they are gitignored by default.

## Hardening in the server

- **Cross-origin refusal.** Every request is checked: the `Host` header must be
  local, and any `Origin` header must be local, or the request is refused with
  403. This blocks the classic localhost attack where a malicious webpage you
  happen to visit fires requests at `http://localhost:8787` (CSRF against local
  servers, and DNS-rebinding variants that spoof the hostname).
- **Body caps.** Request bodies over 1 MB are refused (413) before parsing.
- **No dynamic paths.** The server serves exactly one page and fixed JSON
  endpoints. There is no file-path parameter anywhere, so there is nothing to
  traverse.
- **Key hygiene.** `/config` returns seat names, models, and live status,
  never keys. Keys are read from `.env` (gitignored, see `.env.example`) and
  appear only in outbound requests to the matching provider.
- **Safe rendering.** The UI builds DOM nodes with `textContent` exclusively.
  Model output is never interpreted as HTML, so a seat cannot inject script
  into your page.
- **Headers.** Responses carry `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store`.
- **Zero dependencies.** One Python file, standard library only. The supply
  chain is CPython itself. You can read every line the room runs on, the
  server is about 620 lines of standard-library Python, no dependencies to audit beyond it, and you should read it.

## What the room does NOT defend against

Honesty section. UKKIN is single-user, local software:

- **Other local processes.** Anything already running as your user can read
  `.env` and `sessions/`. That is true of every local tool that holds keys;
  use OS-level protections if your threat model includes local malware.
- **Multi-user hosting.** There is no auth layer because there are no users to
  separate. Do not port-forward the room to the internet.
- **Provider-side handling.** Once a prompt reaches Anthropic, OpenAI, xAI,
  Moonshot, Google, DeepSeek, or Perplexity, their data-handling terms apply.
  Choose your seats accordingly; the Local seat (Ollama) exists precisely so
  a fully offline table is possible.
- **Model output quality.** Seats can be wrong, and the canvas records whatever
  the table writes. The gold seat (you) is the review gate by design.

## Reporting

Found something? Email hello@ukkin.io with UKKIN in the subject line.
Plain-text report is fine. We will credit you in the README unless you prefer
otherwise.
