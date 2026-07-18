#!/usr/bin/env bash
# UKKIN test suite. Zero dependencies beyond python3 + curl (both ship with macOS).
# Runs against a COPY of the app in a temp dir on a test port; your real room is untouched.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${UKKIN_TEST_PORT:-8799}"
BASE="http://127.0.0.1:$PORT"
TMP="$(mktemp -d)"
PASS=0; FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL  $1  ->  $2"; }
check(){ # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$2] got [$3]"; fi
}

cleanup(){ [ -n "${SRV:-}" ] && { kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; }; rm -rf "$TMP"; }
trap cleanup EXIT

# refuse to run against a stale server: results would be lies
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  say "ABORT: port $PORT is already in use (stale test server?). Kill it first:"
  say "  kill \$(lsof -ti :$PORT)"
  exit 2
fi

say "UKKIN tests (temp dir: $TMP, port: $PORT)"
cp "$HERE/ukkin.py" "$HERE/ukkin.html" "$TMP/"

# ---------- 0. python syntax + unit tests (no server) ----------
python3 - "$TMP" <<'EOF'
import sys, os, py_compile
tmp = sys.argv[1]
py_compile.compile(os.path.join(tmp, "ukkin.py"), doraise=True)
sys.path.insert(0, tmp)
import ukkin

# extract_canvas: plain text, canvas present, None, greedy multi-tag, non-string
s, c = ukkin.extract_canvas("just words"); assert (s, c) == ("just words", None), (s, c)
s, c = ukkin.extract_canvas("hi <canvas>THE WORK</canvas>"); assert s == "hi" and c == "THE WORK", (s, c)
s, c = ukkin.extract_canvas(None); assert (s, c) == ("", None), (s, c)
s, c = ukkin.extract_canvas("a <canvas>x</canvas> b <canvas>y</canvas> c")
assert c == "y", c   # non-greedy: the LAST complete block wins
s, c = ukkin.extract_canvas(12345); assert s == "12345" and c is None, (s, c)

# _is_local: the cross-origin guard's core
assert ukkin._is_local("localhost:8787")
assert ukkin._is_local("127.0.0.1:8787")
assert ukkin._is_local("LOCALHOST")
assert not ukkin._is_local("evil.example.com")
assert not ukkin._is_local("localhost.evil.com:80")
assert not ukkin._is_local("")
assert ukkin._is_local("[::1]:8787"), "bracketed IPv6 loopback must be local"
assert ukkin._is_local("::1")
assert not ukkin._is_local("[::1].evil.com")

# empty <canvas></canvas> must never wipe the stone
sp, cv = ukkin.extract_canvas("done <canvas>  </canvas>"); assert cv is None, (sp, cv)
# last complete block wins, non-greedy
sp, cv = ukkin.extract_canvas("a <canvas>one</canvas> b <canvas>two</canvas>"); assert cv == "two", cv
# chained digest actually chains
assert ukkin.chained_digest("aa", "x") != ukkin.chained_digest("bb", "x"), "parent must change the digest"

# stamp sanitizer: strip the room's LEADING injected footer, but never corrupt mid-content text
s, c = ukkin.extract_canvas("ok <canvas>CANVAS VERSION: v9 junk\nreal work</canvas>")
assert "CANVAS VERSION" not in c and c.strip() == "real work", ("leading strip", c)
s, c = ukkin.extract_canvas("ok <canvas>real work\nCANVAS VERSION: v9 is legit content here\nmore</canvas>")
assert "CANVAS VERSION: v9 is legit content here" in c and "more" in c, ("mid-content preserved", c)

# cost estimate: computes, never fabricates, flags unknown models
est = ukkin.estimate_cost(40)
assert est["turns"] == 40 and est["round_cost"] >= 0, est
assert isinstance(est["per_seat"], list)
# a model with no price on file must be reported unknown, not guessed
ukkin.SEATS.append({"id":"zz","name":"Ztest","color":"#000","provider":"openai","model":"no-such-model-xyz","key":"ZZ_TEST_KEY"})
ukkin.ENV["ZZ_TEST_KEY"] = "x"
est2 = ukkin.estimate_cost(10)
assert "Ztest" in est2["unknown"], est2["unknown"]
ukkin.SEATS.pop(); ukkin.ENV.pop("ZZ_TEST_KEY", None)

# seat table sanity: unique ids, colors present, exactly one keyless local seat
ids = [s["id"] for s in ukkin.SEATS]
assert len(ids) == len(set(ids)), "duplicate seat ids"
assert all(s.get("color") for s in ukkin.SEATS)
assert sum(1 for s in ukkin.SEATS if s.get("keyless")) == 1
print("UNIT-OK")
EOF
if [ $? -eq 0 ]; then ok "syntax + unit tests (extract_canvas, _is_local, seat table)"; else bad "unit tests" "python exited nonzero"; fi

# ---------- start server ----------
# note: no ( cd && ... ) subshell — kill must reach python itself, not a wrapper
# whose death would orphan it. ukkin.py resolves its files from __file__, not cwd.
UKKIN_PORT="$PORT" UKKIN_HOST=Tester python3 "$TMP/ukkin.py" >/dev/null 2>&1 &
SRV=$!
for i in $(seq 1 30); do curl -s -o /dev/null "$BASE/config" && break; sleep 0.2; done

# ---------- 1. basic endpoints ----------
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")
check "GET / is 200" "200" "$code"
body=$(curl -s "$BASE/")
case "$body" in *UKKIN*) ok "page carries the UKKIN brand";; *) bad "brand" "UKKIN not in page";; esac

cfg=$(curl -s "$BASE/config")
case "$cfg" in *'"seats"'*) ok "/config returns seats";; *) bad "/config" "$cfg";; esac
case "$cfg" in *'"host": "Tester"'*|*'"host":"Tester"'*) ok "/config carries UKKIN_HOST";; *) bad "host name" "$cfg";; esac
case "$cfg" in *API_KEY*|*sk-*) bad "key leak" "/config contains key material";; *) ok "/config leaks no key material";; esac

# ---------- 2. room flow ----------
curl -s -X POST "$BASE/topic" -H 'content-type: application/json' -d '{"topic":"test topic"}' >/dev/null
st=$(curl -s "$BASE/state")
case "$st" in *'test topic'*) ok "topic persists to state";; *) bad "topic" "$st";; esac
curl -s -X POST "$BASE/say" -H 'content-type: application/json' -d '{"text":"hello table"}' >/dev/null
st=$(curl -s "$BASE/state")
case "$st" in *'hello table'*) ok "host message lands in transcript";; *) bad "say" "$st";; esac
case "$st" in *'Tester (host)'*) ok "host speaker uses UKKIN_HOST";; *) bad "host speaker" "$st";; esac

# ---------- 3. input guards ----------
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/turn" -H 'content-type: application/json' -d '{"seat":"nope"}')
check "unknown seat is 400" "400" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/topic" -H 'content-type: application/json' -d 'not json')
check "malformed JSON is 400" "400" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/topic" -H 'content-type: application/json' -d '[1,2,3]')
check "non-object JSON is 400" "400" "$code"
python3 -c "print('{\"topic\":\"'+ 'x'*1100000 +'\"}')" > "$TMP/big.json"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/topic" -H 'content-type: application/json' --data-binary "@$TMP/big.json")
check "oversized body is 413" "413" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/nowhere")
check "unknown path is 404" "404" "$code"

# ---------- 4. cross-origin / rebinding guards ----------
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/reset" -H 'Origin: https://evil.example.com')
check "foreign Origin is 403" "403" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/reset" -H 'Host: evil.example.com')
check "foreign Host is 403" "403" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/reset" -H 'Origin: http://localhost:'"$PORT")
check "local Origin is allowed" "200" "$code"

# ---------- 5. restart persistence ----------
curl -s -X POST "$BASE/topic" -H 'content-type: application/json' -d '{"topic":"survives restart"}' >/dev/null
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
UKKIN_PORT="$PORT" python3 "$TMP/ukkin.py" >/dev/null 2>&1 &
SRV=$!
for i in $(seq 1 30); do curl -s -o /dev/null "$BASE/config" && break; sleep 0.2; done
st=$(curl -s "$BASE/state")
case "$st" in *'survives restart'*) ok "state survives a server restart";; *) bad "persistence" "$st";; esac

# ---------- 5b. THE CANVAS STORE ----------
curl -s -X POST "$BASE/topic" -H 'content-type: application/json' -d '{"topic":"store test","canvas":"first stone"}' >/dev/null
curl -s -X POST "$BASE/gavel" -H 'content-type: application/json' -d '{"canvas":"second stone, amended"}' >/dev/null
hist=$(curl -s "$BASE/store/history")
case "$hist" in *'"sha256"'*) ok "store history returns digests";; *) bad "store history" "$hist";; esac
v1=$(curl -s "$BASE/store/version?n=1")
case "$v1" in *'first stone'*) ok "store serves version 1 content";; *) bad "store v1" "$v1";; esac
python3 - "$BASE" <<'PYEOF2'
import json, sys, hashlib, urllib.request
base = sys.argv[1]
d = json.load(urllib.request.urlopen(base + "/store/version?n=1"))
# the digest is CHAINED: sha256(parent + "\n" + content); v1 parent is empty
chained = hashlib.sha256((d["parent"] + "\n" + d["content"]).encode("utf-8")).hexdigest()
assert chained == d["sha256"], "chained digest mismatch"
h = json.load(urllib.request.urlopen(base + "/store/history"))
assert h["versions"][0]["sha256"] == chained, "index digest mismatch"
diff = json.load(urllib.request.urlopen(base + "/store/diff?a=1&b=2"))
assert any("second stone" in l for l in diff["diff"]), "diff missing change"
print("STORE-OK")
PYEOF2
if [ $? -eq 0 ]; then ok "digests recompute independently; diffs show changes"; else bad "store integrity" "python exited nonzero"; fi
python3 - "$BASE" <<'PYEOF3'
import json, sys, urllib.request
base = sys.argv[1]
h = json.load(urllib.request.urlopen(base + "/store/history"))["versions"]
assert h[0].get("parent", None) == "", "v1 parent must be empty"
for prev, cur in zip(h, h[1:]):
    assert cur["parent"] == prev["sha256"], f"chain broken at v{cur['version']}"
print("CHAIN-OK")
PYEOF3
if [ $? -eq 0 ]; then ok "the chain holds: every version records its predecessor's digest"; else bad "hash chain" "python exited nonzero"; fi
gav=$(curl -s "$BASE/state")
case "$gav" in *'"canvas_version": 2'*|*'"canvas_version":2'*) ok "gavel amendment became version 2";; *) bad "gavel version" "$gav";; esac

# ---------- 5c. THE FIRE (turn budget) ----------
st=$(curl -s "$BASE/state")
case "$st" in *'"turn_budget"'*) ok "state carries a turn budget";; *) bad "budget in state" "$st";; esac
b=$(curl -s -X POST "$BASE/budget" -H 'content-type: application/json' -d '{"add":20}')
case "$b" in *'"turn_budget": 60'*|*'"turn_budget":60'*) ok "feeding the fire adds 20 turns";; *) bad "feed fire" "$b";; esac
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/budget" -H 'content-type: application/json' -d '{"add":"nope"}')
check "bad budget value is 400" "400" "$code"

# ---------- 5d. THE PIN and THE DIAL ----------
curl -s -X POST "$BASE/pin" -H 'content-type: application/json' -d '{"text":"no em dashes, ever"}' >/dev/null
curl -s -X POST "$BASE/reset" >/dev/null
st=$(curl -s "$BASE/state")
case "$st" in *'no em dashes, ever'*) ok "the pin survives Clear the room";; *) bad "pin persistence" "$st";; esac
b=$(curl -s -X POST "$BASE/budget" -H 'content-type: application/json' -d '{"set":80}')
case "$b" in *'"turn_budget": 80'*|*'"turn_budget":80'*) ok "the dial sets an absolute fire";; *) bad "budget set" "$b";; esac

# ---------- 6. export ----------
resp=$(curl -s -X POST "$BASE/export")
case "$resp" in *'"saved"'*) ok "export writes a session file";; *) bad "export" "$resp";; esac
ls "$TMP/sessions/"ukkin_*.md >/dev/null 2>&1 && ok "export file is named ukkin_*.md" || bad "export name" "no ukkin_*.md in sessions/"

say ""
say "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
