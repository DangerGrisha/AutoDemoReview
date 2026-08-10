# steambot — automatic CS2 match detection + demo download

> **This package now has two layers.** The original **single-account PoC**
> (`python -m steambot …`, driven entirely by `.env`) is still here and is the
> right tool for debugging the protocol one step at a time. On top of it sits the
> **multi-user auto-import** the web app actually uses: subscriptions live in the
> database, players sign up on `/connect`, and reports are delivered on the site
> and (opt-in) over Steam chat. See [Multi-user auto-import](#multi-user-auto-import)
> below; the PoC sections that follow describe the layers both share.

**Scope of the original pass:** prove, for **one account (yours)**, that we can
(1) automatically detect a new match and (2) download its raw `.dem` file, end to
end.

This README doubles as the honest write-up you asked for: *does the
reverse-engineered GC protocol still work against current CS2?*

---

## TL;DR — the honest answer

| Layer | What it does | Status | Stack |
|------|---------------|--------|-------|
| **1. Sharecode chain** | detect the next match after a known one | ✅ **Works, reliable** | pure Python (Web API) |
| **2. GC demo-URL resolve** | turn a sharecode into a demo download URL | ⚠️ **Protocol works, but the *pure-Python library* is likely broken for CS2** | ValvePython (unmaintained) |
| **3. Download + decompress** | fetch the `.dem.bz2`, bunzip to `.dem` | ✅ **Works, reliable** | pure Python |

- **The protocol itself is alive.** CS2 uses the *same* Game-Coordinator message
  (`MatchListRequestFullGameInfo`) as CS:GO. The maintained Node library
  `node-globaloffensive` resolves sharecodes to demo URLs today (last release
  March 2026), and production trackers (Leetify, csstats.gg, CS Demo Manager) all
  run exactly this design at scale.
- **The pure-Python client is the weak link.** `ValvePython/csgo` last shipped in
  2020; it has an **open, unresolved issue #62 “Can't connect to GC” (Feb 2024)**
  and a failed attempt to update its protobufs for CS2 (#63). It still *installs
  and imports* on Python 3.13 (verified) and still exposes the right call, so we
  ship it as the in-stack option — **but expect Layer 2 to fail at the GC
  handshake.** If it does, that's not the protocol dying, it's this library being
  abandoned. Use the [fallback](#if-layer-2-fails-the-proven-fallback) below.

**What's already verified in this repo (no credentials needed):**
- Sharecode codec decodes the known Valve vector correctly
  (`python -m steambot selftest`, and `steambot/tests/test_sharecode.py`).
- The Web API endpoint is reachable and our request shape is correct (returns the
  expected auth error with a dummy key).
- `steam` + `csgo` install and import on Python 3.13.14, and the GC client is
  written against their real, introspected API.

**What only *you* can verify (needs your bot account + a played match):** whether
Layer 2's live GC handshake actually succeeds on the pure-Python path. Run
`python -m steambot login` to find out in ~30 seconds.

---

## How it works

```
   your Match Auth Code + a known sharecode
                 │
                 ▼
   ┌─────────────────────────────┐   Steam Web API, no login
   │ 1. GetNextMatchSharingCode  │   → "CSGO-xxxxx-..." (the next match) or n/a
   └─────────────────────────────┘
                 │  decode (pure math: base-57 → matchid/outcomeid/token)
                 ▼
   ┌─────────────────────────────┐   bot logs into the CS2 Game Coordinator
   │ 2. request_full_match_info  │   → demo URL in roundstatsall[-1].map
   └─────────────────────────────┘
                 │
                 ▼
   ┌─────────────────────────────┐
   │ 3. download replayNNN...bz2 │   → decompress → raw .dem on disk
   └─────────────────────────────┘
```

The three identities involved — keep them straight:
- **Bot account** (`STEAMBOT_USERNAME`): a throwaway Steam account that talks to
  the GC. Needs the free CS2 license (auto-requested). Does **not** need to be
  your friend.
- **You, the target** (`STEAMBOT_TARGET_STEAMID` + `STEAMBOT_MATCH_AUTH_CODE`):
  the Match Auth Code is a scoped, revocable token that only authorizes reading
  your match-sharing history. **It is not your Steam password.**
- **A Steam Web API key** (`STEAM_API_KEY`, shared with the web app): used only
  for the Layer-1 Web API call.

---

## Setup

1. **Install the extra dependencies** (separate from the main app):
   ```bash
   pip install -r requirements-steambot.txt
   ```

2. **Fill in `.env`** (copy from `.env.example`). The steambot block explains
   where each value comes from. The two you'll have to go fetch:
   - **Match Auth Code + seed sharecode:** the Steam help wizard —
     <https://help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128>
     generates the auth code *and* shows your most recent sharecode on the same page.
   - **Steam Web API key:** <https://steamcommunity.com/dev/apikey> (note: Steam
     now requires the requesting account to have Steam Guard on and a small
     purchase history to be issued a key).

3. **Make a bot account** (optional but recommended — don't use your main). Any
   free Steam account works; the tool adds the free CS2 license itself.

---

## Running it — test each layer independently

The CLI is split so you can see exactly where the chain works or breaks:

```bash
# Layer 0 — offline sanity: codec vs known vector + config check. No network.
python -m steambot selftest

# Layer 1 — one sharecode-chain call. No Steam login, no GC. Proves detection.
python -m steambot next

# Layer 2 handshake — one-time interactive login + GC connection check.
#   This is THE test of whether the pure-Python GC path works on your setup.
python -m steambot login

# Layers 2+3 for a specific match — decode a sharecode, resolve its URL, download.
python -m steambot resolve CSGO-xxxxx-xxxxx-xxxxx-xxxxx-xxxxx

# Full loop — watch the chain; download each new match as it appears.
python -m steambot poll --max-iterations 0
```

Recommended first run: `selftest` → `next` → `login`. If `next` returns a code or
a clean “caught up”, Layer 1 works. If `login` reaches “GC session established”,
you're in the lucky case where the pure-Python path works and `poll` will run
fully in Python. If `login` times out at the GC step, jump to the fallback.

---

## If Layer 2 fails: the proven fallback

If `python -m steambot login` reports *“GC did NOT become ready”* (the expected
ValvePython-on-CS2 failure), the rest of the pipeline is fine — only the GC
demo-URL step needs a different backend. Two maintained options, both of which
this design already isolates behind `GCClient.resolve_demo_url()`:

1. **Node `node-globaloffensive`** (recommended; battle-tested). A ~30-line
   Node helper logs in with `steam-user`, calls `csgo.requestGame(sharecode)`,
   reads `match.roundstatsall.at(-1).map`, and prints the URL. Reference
   implementation: `claabs/cs-demo-downloader` (targets CS2 Premier/comp/wingman).
   Our Python `poll` loop would call it via `subprocess` and keep everything else
   in Python.
2. **akiver `boiler-writter`** (C++ binary, actively maintained, used by CS Demo
   Manager). Takes the sharecode + bot credentials, connects to the GC, and
   writes the match info out; shell out to it from Python. This is what current
   Python demo tools (e.g. `SoulxSlayer/cs2-sharecode-cli`) do.

Say the word and I'll wire whichever you prefer into the same interface — it's a
single-file change (`gc_client.py`), no churn elsewhere.

---

## Known limits, quirks & rate limits (from the research pass)

- **Demo retention is short.** Valve prunes matchmaking demos after roughly
  1–2 weeks (no official SLA, shorter under load). After that the URL 404s. So the
  seed sharecode must be recent, and you should download promptly.
- **Demos aren't instant.** After a match ends it can take several minutes (up to
  ~an hour) for the compressed demo to appear on the replay server. Poll patiently.
- **The GC only returns your last 8 matches per mode**, independent of file
  retention. This PoC walks forward from a known code, so that's fine.
- **Rate limits are undocumented.** The Web API can return `429`/`503`; repeated
  calls with an invalid auth code are penalized. The GC throttles full-match-info
  requests — don't hammer it. Default poll interval is 90s; keep it sane.
- **Reliability is “needs monitoring”, not fire-and-forget.** Even on the
  maintained stacks, the GC session drops on its own (especially around Valve's
  weekly Tuesday updates), and a CS2 update can break a library's protobufs. A
  production version needs auto-reconnect, backoff, and library-update discipline.
- **Auth is a one-time hurdle.** A fresh bot account has email Steam Guard;
  `python -m steambot login` handles the first interactive login and stores a
  credential so later runs are unattended. For a mobile-authenticator bot, set
  `STEAMBOT_SHARED_SECRET` for fully automatic 2FA.

---

## Multi-user auto-import

What the web app runs. Same three layers as the PoC, but subscriptions come from
the database instead of `.env`, and the Game-Coordinator step moved to Node.

```
  player on /connect                     steambot/worker.py (python -m steambot.worker)
  ──────────────────                     ──────────────────────────────────────────────
  auth code + sharecode  ──► steam_links ──► 1. GetNextMatchSharingCode   (Web API)
                                             2. POST /resolve             (Node agent → GC)
                                             3. download + bunzip2
                                             4. web.ingest.ingest_demo    (same path as /upload)
                                             5. POST /message             (Node agent → Steam chat)
```

**Why the agent is Node.** `ValvePython/csgo` has been unmaintained since 2020 and
does not reliably reach the CS2 GC (see the honest status above). `steam-user` +
`globaloffensive` are maintained and cover *both* things we need — GC lookups and
chat — over one session. Everything else stays Python; the two talk over loopback
HTTP (`steambot/agent/agent.mjs`, port 8790, `X-Agent-Token`).

**Friend policy.** The bot **never sends invites**: players add it from `/connect`
and it accepts only SteamIDs the worker allow-lists. Outgoing invites from a bot
are a spam-flag trigger, and Steam caps friends at **250** — which is also why
chat is a delivery bonus, not the only channel. Note that a *limited* Steam
account (no ≥$5 purchase) can neither add friends nor send messages at all.

**Setup**

```bash
pip install -r requirements-steambot.txt        # httpx is all the worker needs
cd steambot/agent && npm install && cd ../..    # steam-user + globaloffensive + steam-totp
```

Fill in `.env`: `STEAM_API_KEY` (required — the sharecode chain is dead without
it), `STEAMBOT_USERNAME` / `STEAMBOT_PASSWORD` / `STEAMBOT_SHARED_SECRET`,
`STEAMBOT_AGENT_TOKEN` (any random string), and `STEAMBOT_PROFILE_URL` (the bot's
Steam profile, shown on `/connect`).

**Run** — two processes beside the web app:

```bash
node steambot/agent/agent.mjs         # keeps the Steam session + GC alive
python -m steambot.worker             # sweeps due subscriptions every minute
python -m steambot.worker --once      # single sweep (cron/Task Scheduler)
python -m steambot.worker --steamid 76561198…   # force one subscription now
curl http://127.0.0.1:8790/health -H "X-Agent-Token: …"   # is the GC up?
```

**The three outcomes the worker keeps apart** (conflating them is how this loop
breaks): *caught up* — nothing new; *waiting* — the match exists but Valve hasn't
published its demo yet, which takes minutes and sometimes ~an hour, so the cursor
must not move and the failure streak must not grow (bounded by
`MAX_PENDING_TRIES`, after which the match is skipped — Valve prunes demos after
1–2 weeks and a missing one would otherwise block the cursor forever); *broken* —
a revoked auth code, which does count as a failure and parks the subscription
after ten of them, telling the player to paste a fresh code.

## Files

| File | Purpose |
|------|---------|
| `worker.py` | multi-user sweep: detect → resolve → download → ingest → notify |
| `agent/agent.mjs` | Node Steam agent: GC resolve, chat, friend accepts (loopback HTTP) |
| `sharecode.py` | base-57 sharecode codec (decode/encode) — **verified** |
| `sharecode_api.py` | Layer 1: `GetNextMatchSharingCode` client — **verified shape** |
| `gc_client.py` | Layer 2: ValvePython GC resolve — **the uncertain layer** |
| `downloader.py` | Layer 3: download + bunzip2 |
| `config.py` | loads `.env`, keeps bot/target identities separate |
| `poc.py` | orchestrates the end-to-end flow with loud logging |
| `__main__.py` | CLI (`selftest` / `next` / `login` / `resolve` / `poll`) |
| `logutil.py` | numbered, colourised step logging |
| `tests/test_sharecode.py` | codec vs a known Valve vector + round-trip |
