#!/usr/bin/env python3
"""
UKKIN — the assembly, reconvened.
A room where many frontier models sit at one table and build one artifact together,
with a human in the gold seat. (The name is the oldest written word for "assembly":
the Sumerian sign UKKIN, U+1233A — a chamber with a figure standing inside it.)

Zero dependencies. Pure Python 3 standard library. Reads provider API keys from a .env
file next to this script (gitignored). A seat lights up only if its key is present.
The room was born as "The Salotto"; this is the same table under its true name.

Run:   python3 ukkin.py
Open:  http://localhost:8787

v2.2 (2026-07-17): THE CANVAS STORE — every press becomes a numbered, sha256-digested
version on disk; seats are told the current version and digest each turn; /store
endpoints expose history, any version, and diffs. Built because the second assembly
demanded it: "our substrate was never verifiable from within." Also: provider retry
guard, press-discipline prompt, host amendments via /gavel (the Gold Gavel).

Security model (see SECURITY.md):
- binds 127.0.0.1 only; the room is never reachable from the network
- POSTs are refused unless Host is local and any Origin header is local
- request bodies are capped at 1 MB
- keys live only in .env; /config never returns them; no telemetry of any kind
"""

import json, os, re, time, hashlib, difflib, urllib.request, urllib.error, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
SESS = os.path.join(HERE, "sessions")
STORE = os.path.join(SESS, "store")
PORT = int(os.environ.get("UKKIN_PORT", "8787"))
STATE_FILE = os.path.join(SESS, "room_state.json")
PROMPT_TRANSCRIPT_CAP = 30   # last N messages sent to providers; full history stays on disk
TURN_BUDGET = int(os.environ.get("UKKIN_TURN_BUDGET", "40"))  # the fire is finite; the host feeds it
DREAM_CAP = 12               # a dreaming run never exceeds this many turns
MAX_BODY = 1_000_000         # 1 MB request-body cap
DIGEST_SCHEME = "sha256(parent_hex + '\\n' + content)/utf-8"  # chained: recompute from parent + content

# ---------- COST ESTIMATE (rough; verify against your provider dashboards) ----------
# USD per 1,000,000 tokens, {in, out}. THESE ARE ESTIMATES AND THEY GO STALE.
# Update PRICES_AS_OF and the numbers when a provider changes them. The room never
# bills you; your provider dashboard is the only truth. Free-tier / local seats are 0.
PRICES_AS_OF = "2026-07"
PRICES = {
    "claude-opus-4-8":        {"in": 5.0,  "out": 25.0},
    "claude-opus-4-7":        {"in": 5.0,  "out": 25.0},
    "claude-sonnet-5":        {"in": 3.0,  "out": 15.0},
    "claude-haiku-4-5":       {"in": 1.0,  "out": 5.0},
    "claude-fable-5":         {"in": 10.0, "out": 50.0},
    "gpt-5.6-sol":            {"in": 5.0,  "out": 30.0},
    "gpt-5.6-terra":          {"in": 2.5,  "out": 15.0},
    "gpt-5.6-luna":           {"in": 1.0,  "out": 6.0},
    "grok-4.5":               {"in": 2.0,  "out": 6.0},
    "kimi-k3":                {"in": 3.0,  "out": 15.0},
    "deepseek-chat":          {"in": 0.28, "out": 1.10},
    "sonar-pro":              {"in": 3.0,  "out": 15.0},   # plus per-search fees, not modeled
    "gemini-3-flash-preview": {"in": 0.0,  "out": 0.0},    # free tier
    "gemini-flash-latest":    {"in": 0.0,  "out": 0.0},    # free tier
    "gemini-2.5-pro":         {"in": 1.25, "out": 10.0},
}
# a cheaper seat with a similar voice, when one exists
SWAPS = {
    "claude-opus-4-8":  ("claude-sonnet-5", "Sonnet 5 is a close voice at roughly 60% the cost"),
    "claude-fable-5":   ("claude-opus-4-8", "Opus 4.8 is about half the price"),
    "gpt-5.6-sol":      ("gpt-5.6-terra",   "the Terra tier is half the price"),
    "gpt-5.6-terra":    ("gpt-5.6-luna",    "the Luna tier is cheaper for lighter work"),
}

def load_env():
    env = dict(os.environ)
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env
ENV = load_env()

HOST_NAME = ENV.get("UKKIN_HOST", "Host")   # the human in the gold seat

SEATS = [
    {"id": "claude",  "name": "Claude",  "color": "#D97757", "provider": "anthropic",
     "model": ENV.get("ANTHROPIC_MODEL", "claude-opus-4-8"),   "key": "ANTHROPIC_API_KEY"},
    {"id": "chatgpt", "name": "ChatGPT", "color": "#10A37F", "provider": "openai",
     "model": ENV.get("OPENAI_MODEL", "gpt-5.6-terra"),        "key": "OPENAI_API_KEY",
     "base": "https://api.openai.com/v1"},
    {"id": "grok",    "name": "Grok",    "color": "#7C3AED", "provider": "openai",
     "model": ENV.get("XAI_MODEL", "grok-4.5"),                "key": "XAI_API_KEY",
     "base": "https://api.x.ai/v1"},
    {"id": "kimi",    "name": "Kimi",    "color": "#2563EB", "provider": "openai",
     "model": ENV.get("MOONSHOT_MODEL", "kimi-k3"),            "key": "MOONSHOT_API_KEY",
     "base": "https://api.moonshot.ai/v1"},
    {"id": "gemini",  "name": "Gemini",  "color": "#EA4335", "provider": "openai",
     "model": ENV.get("GEMINI_MODEL", "gemini-3-flash-preview"),       "key": "GOOGLE_API_KEY",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai"},
    {"id": "deepseek", "name": "DeepSeek", "color": "#4D6BFE", "provider": "openai",
     "model": ENV.get("DEEPSEEK_MODEL", "deepseek-chat"),      "key": "DEEPSEEK_API_KEY",
     "base": "https://api.deepseek.com"},
    {"id": "pplx",    "name": "Perplexity", "color": "#20808D", "provider": "openai",
     "model": ENV.get("PPLX_MODEL", "sonar-pro"),              "key": "PERPLEXITY_API_KEY",
     "base": "https://api.perplexity.ai",
     "role": "You are the table's RESEARCHER: when you speak, bring current facts and cite sources plainly. You verify and inform; you rarely rewrite the canvas."},
    {"id": "local",   "name": "Local",   "color": "#8B85A3", "provider": "openai",
     "model": ENV.get("OLLAMA_MODEL", "qwen3.6:27b"),          "key": "OLLAMA",
     "base": ENV.get("OLLAMA_BASE", "http://localhost:11434/v1"), "keyless": True},
]

_LIVE_CACHE = {}  # seat id -> (bool, monotonic_expiry): the Ollama probe is network I/O; don't run it every turn
def seat_live(s):
    if not s.get("keyless"):
        return bool(ENV.get(s["key"]))
    hit = _LIVE_CACHE.get(s["id"])
    now = time.monotonic()
    if hit and hit[1] > now:
        return hit[0]
    try:
        urllib.request.urlopen(s["base"].replace("/v1", "") + "/api/tags", timeout=1)
        live = True
    except Exception:
        live = False
    _LIVE_CACHE[s["id"]] = (live, now + 15)  # 15s TTL
    return live

def seat_by_id(sid):
    return next((s for s in SEATS if s["id"] == sid), None)

# ---------- shared room state (persisted) ----------
STATE = {"transcript": [], "canvas": "", "topic": "", "canvas_by": "",
         "canvas_version": 0, "canvas_digest": "", "turns_used": 0, "turn_budget": TURN_BUDGET,
         "pin": ""}
LOCK = threading.Lock()        # guards STATE reads/writes
TURN_LOCK = threading.Lock()   # serializes whole turns (one speaker at a time)

def save_state():
    os.makedirs(SESS, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(STATE, f)
    os.replace(tmp, STATE_FILE)

def load_state():
    global STATE
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and "transcript" in d:
            STATE.update(d)
            STATE.setdefault("canvas_by", "")
            STATE.setdefault("canvas_version", 0)
            STATE.setdefault("canvas_digest", "")
            STATE.setdefault("turns_used", 0)
            STATE.setdefault("turn_budget", TURN_BUDGET)
            STATE.setdefault("pin", "")
    except Exception:
        pass
load_state()

# ---------- THE CANVAS STORE (versioned, hash-chained) ----------
def canvas_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def chained_digest(parent, text):
    """The version digest commits to its parent, so any change to an ancestor
    breaks every descendant. Preimage is exactly: parent_hex + "\\n" + content."""
    return hashlib.sha256((parent + "\n" + text).encode("utf-8")).hexdigest()

def store_index():
    try:
        with open(os.path.join(STORE, "index.json")) as f:
            return json.load(f)
    except Exception:
        return []

def store_write(canvas, author):
    """Every press becomes a numbered version with a recomputable digest."""
    os.makedirs(STORE, exist_ok=True)
    idx = store_index()
    n = (idx[-1]["version"] + 1) if idx else 1
    parent = idx[-1]["sha256"] if idx else ""
    dig = chained_digest(parent, canvas)
    with open(os.path.join(STORE, f"v{n:04d}.txt"), "w", encoding="utf-8") as f:
        f.write(canvas)
    idx.append({"version": n, "sha256": dig, "parent": parent, "scheme": DIGEST_SCHEME,
                "author": author, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "chars": len(canvas)})
    tmp = os.path.join(STORE, "index.json.tmp")
    with open(tmp, "w") as f:
        json.dump(idx, f, indent=1)
    os.replace(tmp, os.path.join(STORE, "index.json"))
    return n, dig

def store_read(n):
    with open(os.path.join(STORE, f"v{int(n):04d}.txt"), encoding="utf-8") as f:
        return f.read()

def press_canvas_locked(canvas, author):
    """Single path for every canvas change: store first, then state."""
    n, dig = store_write(canvas, author)
    STATE["canvas"] = canvas
    STATE["canvas_by"] = author
    STATE["canvas_version"] = n
    STATE["canvas_digest"] = dig
    save_state()
    return n, dig

def room_system(seat, others):
    names = ", ".join(o["name"] for o in others) or "no one yet"
    role = (" SPECIAL ROLE: " + seat["role"]) if seat.get("role") else ""
    return role + (
        f"You are {seat['name']}, one voice at a round table called UKKIN (the assembly). "
        f"The human host is {HOST_NAME}. Also at the table: {names}. "
        "You are all here to CREATE ONE THING TOGETHER, not to give parallel monologues. "
        "There is a shared CANVAS (the work in progress) shown to you below; the room keeps "
        "every version in a store with a digest, so only pressed text changes the stone. "
        "Rules of the room: build on what others said by name, disagree kindly and specifically, "
        "keep each turn short (a few sentences), and if you propose wording for the work you MUST "
        "press the FULL updated canvas between <canvas> and </canvas> tags in that same message; "
        "speech alone changes nothing. Do not restate the whole transcript. If you have nothing to "
        "press, say so in one short sentence; brevity is the honest form of stopping. "
        "Be generous, be brief, make it beautiful. No emojis. Speak as yourself, in your own voice."
    )

def render_transcript_locked():
    lines = []
    if STATE["topic"]:
        lines.append(f"TOPIC: {STATE['topic']}")
    if STATE.get("pin"):
        lines.append(f"THE HOST'S STANDING NOTE (honor it in every turn, every session): {STATE['pin']}")
    lines.append("CANVAS (the work so far):\n" + (STATE["canvas"] or "(empty — start it)"))
    if STATE.get("canvas_version"):
        lines.append(f"CANVAS VERSION: v{STATE['canvas_version']} · {DIGEST_SCHEME}:"
                     f"{STATE.get('canvas_digest','')[:12]} · the store keeps every version; "
                     "claim no edit the store does not show.")
    tail = STATE["transcript"][-PROMPT_TRANSCRIPT_CAP:]
    if len(STATE["transcript"]) > len(tail):
        lines.append(f"\n--- CONVERSATION (last {len(tail)} of {len(STATE['transcript'])} turns) ---")
    else:
        lines.append("\n--- CONVERSATION ---")
    for m in tail:
        lines.append(f"{m['speaker']}: {m['text']}")
    return "\n".join(lines)

# ---------- provider adapters ----------
MAX_TOKENS = int(ENV.get("UKKIN_MAX_TOKENS", "4096"))
RETRY_CODES = (429, 500, 502, 503, 529)  # genuinely transient; 401 is a bad key, fail fast

def _post_json(url, headers, payload, timeout, retries=1):
    data = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if attempt < retries and e.code in RETRY_CODES:
                time.sleep(2)
                continue
            raise

def call_anthropic(seat, system, user_block):
    d = _post_json("https://api.anthropic.com/v1/messages",
        {"x-api-key": ENV.get(seat["key"], ""), "anthropic-version": "2023-06-01"},
        {"model": seat["model"], "max_tokens": MAX_TOKENS, "system": system,
         "messages": [{"role": "user", "content": user_block}]}, 120)
    return "".join(p.get("text", "") for p in d.get("content", []) if isinstance(p, dict))

def call_openai_compatible(seat, system, user_block):
    key = "ollama" if seat.get("keyless") else ENV.get(seat["key"], "")
    timeout = 600 if seat.get("keyless") else 120  # local models load slowly; let them
    url = seat["base"] + "/chat/completions"
    headers = {"authorization": f"Bearer {key}"}
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_block}]
    # OpenAI's reasoning family wants max_completion_tokens; everyone else still takes
    # max_tokens. Send the right one first, and swap on a 400 either way.
    tok = "max_completion_tokens" if "api.openai.com" in seat["base"] else "max_tokens"
    try:
        d = _post_json(url, headers, {"model": seat["model"], "messages": msgs,
                                      tok: MAX_TOKENS}, timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 400 and ("max_tokens" in body or "max_completion_tokens" in body):
            other = "max_tokens" if tok == "max_completion_tokens" else "max_completion_tokens"
            d = _post_json(url, headers, {"model": seat["model"], "messages": msgs,
                                          other: MAX_TOKENS}, timeout)
        else:
            raise urllib.error.HTTPError(e.url, e.code, body[:300], e.headers, None)
    msg = (d.get("choices") or [{}])[0].get("message", {})
    return msg.get("content") or ""

def estimate_cost(turns):
    """A rough, conservative estimate of what the live table will cost.
    Cost RISES as the canvas grows, so this is 'at the current size'; the room
    never bills you. ~4 chars per token; output assumes a seat may re-press the
    whole canvas plus a few sentences (the upper end, on purpose)."""
    live = [s for s in SEATS if seat_live(s)]
    with LOCK:
        ctx_chars = len(render_transcript_locked())
        canvas_chars = len(STATE.get("canvas", ""))
    per, priciest, unknown = [], None, []
    for s in live:
        others = [o for o in live if o["id"] != s["id"]]
        in_tok = (len(room_system(s, others)) + ctx_chars) / 4.0
        out_tok = min((300 + canvas_chars) / 4.0, MAX_TOKENS)
        price = PRICES.get(s["model"])
        if price is None:
            unknown.append(s["name"])
            per.append({"seat": s["name"], "model": s["model"], "known": False})
            continue
        cost = in_tok / 1e6 * price["in"] + out_tok / 1e6 * price["out"]
        row = {"seat": s["name"], "model": s["model"], "known": True, "per_turn": round(cost, 4),
               "free": price["in"] == 0 and price["out"] == 0}
        per.append(row)
        if not row["free"] and (priciest is None or cost > priciest["_c"]):
            priciest = {"seat": s["name"], "model": s["model"], "_c": cost}
    known = [p for p in per if p.get("known")]
    round_cost = sum(p["per_turn"] for p in known)          # one turn per live seat
    turn_avg = round_cost / len(known) if known else 0.0
    tip = None
    if priciest:
        sw = SWAPS.get(priciest["model"])
        if sw:
            tip = f"Your priciest seat is {priciest['seat']} ({priciest['model']}). Set its model to {sw[0]}: {sw[1]}."
        elif priciest["_c"] > 0.002:
            tip = f"Your priciest seat is {priciest['seat']}. Muting it, or leaning on DeepSeek and a local seat, runs the table for pennies."
    return {
        "as_of": PRICES_AS_OF, "live_seats": len(live), "turns": turns,
        "per_seat": per, "round_cost": round(round_cost, 4),
        "projected": round(turn_avg * turns, 2), "unknown": unknown, "tip": tip,
    }

def ask_seat(seat, dreaming=False):
    others = [o for o in SEATS if o["id"] != seat["id"] and seat_live(o)]
    system = room_system(seat, others)
    with LOCK:
        block = render_transcript_locked()
    if dreaming:
        block += (f"\n\nThis is a DREAMING round, {seat['name']}: drift and imagine. Offer one new idea "
                  "in a few sentences, wild but honest. Press the canvas only if a dream deserves the stone. "
                  "Never restate what the table already holds; if your dream is already there, name a fresh "
                  "one or say 'the fire is enough' in one line.")
    else:
        block += f"\n\nIt is your turn, {seat['name']}. Add to the work."
    if seat["provider"] == "anthropic":
        return call_anthropic(seat, system, block)
    return call_openai_compatible(seat, system, block)

CANVAS_RE = re.compile(r"<canvas>(.*?)</canvas>", re.S | re.I)  # non-greedy: take the LAST complete block
# strip the room's own injected footer only where it is injected: the leading line, once
STAMP_RE = re.compile(r"\A\s*CANVAS VERSION:[^\n]*\n?")

def sanitize_canvas(text):
    return STAMP_RE.sub("", text)
def extract_canvas(text):
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    matches = list(CANVAS_RE.finditer(text))
    if not matches:
        return text.strip(), None
    m = matches[-1]  # the last complete <canvas>...</canvas> block wins
    canvas = sanitize_canvas(m.group(1).strip()).strip()
    spoken = (text[:m.start()] + text[m.end():]).strip()
    if not canvas:            # an empty <canvas></canvas> never wipes the stone
        return spoken, None
    return spoken, canvas

def export_room():
    os.makedirs(SESS, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    path = os.path.join(SESS, f"ukkin_{stamp}.md")
    with LOCK:
        topic = STATE["topic"] or "Untitled"
        canvas = STATE["canvas"]
        by = STATE.get("canvas_by", "")
        ver = STATE.get("canvas_version", 0)
        dig = STATE.get("canvas_digest", "")
        turns = list(STATE["transcript"])
    lines = [f"# UKKIN — {topic}", f"_Exported {time.strftime('%Y-%m-%d %H:%M')}_", ""]
    head = "## The canvas" + (f" (v{ver}, last edited by {by}, {DIGEST_SCHEME}:{dig[:12]})" if ver else (f" (last edited by {by})" if by else ""))
    lines += [head, "", canvas or "(empty)", ""]
    lines += ["## The conversation", ""]
    for m in turns:
        lines.append(f"**{m['speaker']}:** {m['text']}")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return os.path.basename(path)

# ---------- HTTP ----------
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

def _is_local(hostport):
    if not hostport:
        return False
    h = hostport.strip().lower()
    if h.startswith("["):                       # bracketed IPv6, e.g. [::1]:8787
        end = h.find("]")
        if end == -1:
            return False
        rest = h[end + 1:]
        if rest and not rest.startswith(":"):   # anything after ]  must be a :port only
            return False
        h = h[1:end]
    elif h.count(":") == 1:                      # host:port
        h = h.split(":")[0]
    return h in LOCAL_HOSTS

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def _refuse_cross_origin(self):
        """DNS-rebinding / CSRF guard: the room only answers its own page."""
        if not _is_local(self.headers.get("host", "")):
            self._send(403, json.dumps({"error": "forbidden: non-local host"}))
            return True
        origin = self.headers.get("origin")
        if origin:
            hostpart = origin.split("//", 1)[-1]
            if not _is_local(hostpart):
                self._send(403, json.dumps({"error": "forbidden: cross-origin"}))
                return True
        return False

    def do_GET(self):
        if self._refuse_cross_origin():
            return
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/" or path.startswith("/index"):
            return self._send(200, open(os.path.join(HERE, "ukkin.html")).read(), "text/html; charset=utf-8")
        if path == "/config":
            seats = [{"id": s["id"], "name": s["name"], "color": s["color"],
                      "model": s["model"], "live": seat_live(s)} for s in SEATS]
            with LOCK:
                st = json.loads(json.dumps(STATE))
            return self._send(200, json.dumps({"seats": seats, "state": st, "host": HOST_NAME,
                                               "digest_scheme": DIGEST_SCHEME}))
        if path == "/state":
            with LOCK:
                return self._send(200, json.dumps(STATE))
        if path == "/estimate":
            try:
                turns = int(q.get("turns", ["40"])[0])
                turns = max(1, min(500, turns))
            except Exception:
                turns = 40
            return self._send(200, json.dumps(estimate_cost(turns)))
        if path == "/store/history":
            return self._send(200, json.dumps({"scheme": DIGEST_SCHEME, "versions": store_index()}))
        if path == "/store/version":
            try:
                n = int(q.get("n", ["0"])[0])
                content = store_read(n)
            except Exception:
                return self._send(404, json.dumps({"error": "no such version"}))
            idx = store_index()
            entry = next((e for e in idx if e["version"] == n), {})
            parent = entry.get("parent", "")
            return self._send(200, json.dumps({"version": n, "content": content,
                                               "parent": parent,
                                               "sha256": chained_digest(parent, content),
                                               "content_sha256": canvas_digest(content),
                                               "scheme": DIGEST_SCHEME}))
        if path == "/store/diff":
            try:
                a = int(q.get("a", ["0"])[0]); b = int(q.get("b", ["0"])[0])
                ta, tb = store_read(a).splitlines(), store_read(b).splitlines()
            except Exception:
                return self._send(404, json.dumps({"error": "no such versions"}))
            diff = list(difflib.unified_diff(ta, tb, fromfile=f"v{a}", tofile=f"v{b}", lineterm=""))
            return self._send(200, json.dumps({"a": a, "b": b, "diff": diff}))
        return self._send(404, "{}")

    def do_POST(self):
        if self._refuse_cross_origin():
            return
        try:
            n = int(self.headers.get("content-length", 0))
            if n > MAX_BODY:
                return self._send(413, json.dumps({"error": "body too large"}))
            data = json.loads(self.rfile.read(n) or "{}")
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception:
            return self._send(400, json.dumps({"error": "bad request body"}))
        if self.path == "/topic":
            with LOCK:
                STATE["topic"] = str(data.get("topic", ""))
                if data.get("canvas") is not None:
                    press_canvas_locked(str(data["canvas"]), f"{HOST_NAME} (host)")
                else:
                    save_state()
                body = json.dumps(STATE)   # snapshot inside the lock
            return self._send(200, body)
        if self.path == "/gavel":
            # THE GOLD GAVEL: the host amends the stone directly
            if data.get("canvas") is None:
                return self._send(400, json.dumps({"error": "gavel needs a canvas"}))
            with LOCK:
                ver, dig = press_canvas_locked(str(data["canvas"]), f"{HOST_NAME} (host)")
                return self._send(200, json.dumps({"canvas": STATE["canvas"],
                                                   "canvas_by": STATE["canvas_by"],
                                                   "canvas_version": ver, "canvas_digest": dig}))
        if self.path == "/pin":
            with LOCK:
                STATE["pin"] = str(data.get("text", ""))[:2000]
                save_state()
                return self._send(200, json.dumps({"pin": STATE["pin"]}))
        if self.path == "/budget":
            try:
                if "set" in data:
                    newb = max(1, min(500, int(data["set"])))
                else:
                    newb = None
                add = max(1, min(100, int(data.get("add", 0)))) if newb is None else 0
            except Exception:
                return self._send(400, json.dumps({"error": "budget needs a number"}))
            with LOCK:
                if newb is not None:
                    STATE["turn_budget"] = newb
                else:
                    STATE["turn_budget"] = STATE.get("turn_budget", TURN_BUDGET) + add
                save_state()
                return self._send(200, json.dumps({"turns_used": STATE["turns_used"],
                                                   "turn_budget": STATE["turn_budget"]}))
        if self.path == "/say":
            with LOCK:
                STATE["transcript"].append({"speaker": f"{HOST_NAME} (host)", "text": str(data.get("text", "")),
                                            "color": "#F4C430", "self": True})
                save_state()
                body = json.dumps(STATE)   # snapshot inside the lock
            return self._send(200, body)
        if self.path == "/turn":
            with LOCK:
                if STATE["turns_used"] >= STATE["turn_budget"]:
                    return self._send(429, json.dumps({"error": "The fire is low: the turn budget is spent. Feed it to continue.",
                                                       "budget_spent": True,
                                                       "turns_used": STATE["turns_used"],
                                                       "turn_budget": STATE["turn_budget"]}))
            seat = seat_by_id(str(data.get("seat", "")))
            if not seat or not seat_live(seat):
                return self._send(400, json.dumps({"error": "seat not live"}))
            dreaming = bool(data.get("dream"))
            with TURN_LOCK:  # one speaker at a time, ever
                try:
                    raw = ask_seat(seat, dreaming)
                except urllib.error.HTTPError as e:
                    detail = e.reason if isinstance(e.reason, str) else ""
                    return self._send(502, json.dumps({"error": f"{seat['name']} API {e.code}: {detail[:200]}"}))
                except Exception as e:
                    return self._send(502, json.dumps({"error": f"{seat['name']}: {e}"}))
                spoken, canvas = extract_canvas(raw)
                if not spoken and canvas is None:
                    return self._send(502, json.dumps({"error": f"{seat['name']} returned nothing"}))
                with LOCK:
                    STATE["transcript"].append({"speaker": seat["name"], "text": spoken, "color": seat["color"]})
                    STATE["turns_used"] = STATE.get("turns_used", 0) + 1
                    if canvas is not None:
                        press_canvas_locked(canvas, seat["name"])
                    else:
                        save_state()
                    return self._send(200, json.dumps({"speaker": seat["name"], "text": spoken,
                                                       "color": seat["color"], "canvas": STATE["canvas"],
                                                       "canvas_by": STATE["canvas_by"],
                                                       "canvas_version": STATE.get("canvas_version", 0),
                                                       "canvas_digest": STATE.get("canvas_digest", ""),
                                                       "pressed": canvas is not None,
                                                       "turns_used": STATE["turns_used"],
                                                       "turn_budget": STATE["turn_budget"]}))
        if self.path == "/export":
            try:
                fname = export_room()
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"saved": fname, "folder": "sessions/"}))
        if self.path == "/reset":
            with LOCK:
                STATE["transcript"] = []; STATE["canvas"] = ""; STATE["topic"] = ""
                STATE["canvas_by"] = ""; STATE["canvas_version"] = 0; STATE["canvas_digest"] = ""
                STATE["turns_used"] = 0; STATE["turn_budget"] = TURN_BUDGET
                # the pin survives on purpose: it is the host's standing note
                save_state()
                body = json.dumps(STATE)   # snapshot inside the lock
            return self._send(200, body)
        return self._send(404, "{}")

if __name__ == "__main__":
    import sys
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), H)   # bind first, announce second
    except OSError as e:
        print(f"\n  Could not open the room on port {PORT}: {e}.")
        print(f"  Something is already using it. Try a different port:")
        print(f"      UKKIN_PORT=8788 python3 ukkin.py")
        print(f"  or find what holds it:  lsof -ti :{PORT}\n")
        sys.exit(1)
    live = [s["name"] for s in SEATS if seat_live(s)]
    print(f"\n  UKKIN is convened at  http://localhost:{PORT}")
    print(f"  Seats lit: {', '.join(live) if live else 'none yet — add a key to .env, or start Ollama'}")
    if STATE["transcript"] or STATE["canvas"]:
        print(f"  Room restored: {len(STATE['transcript'])} turns, canvas "
              f"{'v' + str(STATE.get('canvas_version', 0)) if STATE.get('canvas_version') else 'present' if STATE['canvas'] else 'empty'}")
    print("  (Ctrl+C closes the room; the room remembers.)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  The room is closed. It remembers.\n")
