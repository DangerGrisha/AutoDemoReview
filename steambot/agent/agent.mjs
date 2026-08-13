/**
 * Steam agent — the only process that talks to Steam directly.
 *
 * Why Node and not Python: the pure-Python Game Coordinator client
 * (ValvePython/csgo) has been unmaintained since 2020 and does not reliably
 * connect to the CS2 GC (see ../README.md). `steam-user` + `globaloffensive` are
 * maintained and give us BOTH halves we need — GC match lookups and Steam chat —
 * over one logged-in session. The Python worker keeps everything else (polling,
 * parsing, DB, reports) and drives this over loopback HTTP.
 *
 * Endpoints (all JSON):
 *   GET  /health          -> { ok, steamid, loggedOn, gcReady, friends }
 *   POST /resolve         { sharecode }        -> { url } | { url: null }
 *   POST /message         { steamid, text }    -> { sent: true }
 *   POST /friends/sync    { allow: [steamid] } -> { accepted, friends, pending }
 *
 * Friend policy, deliberately: the bot NEVER sends invites. It accepts incoming
 * ones only from SteamIDs the worker allow-lists (players who opted into chat on
 * /connect). Outgoing invites from a bot are a spam-flag trigger, and Steam caps
 * friends at 250 — so the list must not fill up with strangers.
 *
 * Logging in, in order of preference:
 *   1. a saved refresh token (`.steam-refresh-token`, written on first login) —
 *      unattended, valid for months, and needs no secrets on disk beyond itself;
 *   2. STEAMBOT_SHARED_SECRET — the mobile authenticator's secret, if you have it;
 *   3. password + a Steam Guard code typed once at the console (or passed as
 *      STEAMBOT_GUARD_CODE), after which (1) takes over.
 * Extracting shared_secret needs third-party tools that want your account
 * password, so (3) → (1) is the recommended path: type one code, never again.
 *
 * Env (from the repo-root .env):
 *   STEAMBOT_USERNAME, STEAMBOT_PASSWORD   — the bot account
 *   STEAMBOT_SHARED_SECRET                 — optional, for unattended 2FA
 *   STEAMBOT_GUARD_CODE                    — optional one-shot code (non-TTY setups)
 *   STEAMBOT_AGENT_TOKEN                   — shared secret with the worker
 *   STEAMBOT_AGENT_PORT                    — default 8790, loopback only
 */

import { createServer } from 'node:http';
import { createInterface } from 'node:readline';
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import GlobalOffensive from 'globaloffensive';
import SteamTotp from 'steam-totp';
import SteamUser from 'steam-user';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..', '..');

/** Minimal .env reader — the agent must not need a dependency just for this. */
function loadEnv() {
  try {
    const text = readFileSync(resolve(REPO_ROOT, '.env'), 'utf8');
    for (const line of text.split(/\r?\n/)) {
      const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
      if (!m) continue;
      const [, key, rawVal] = m;
      if (process.env[key] !== undefined) continue;         // real env wins
      let val = rawVal.trim();
      if ((val.startsWith('"') && val.endsWith('"')) ||
          (val.startsWith("'") && val.endsWith("'"))) val = val.slice(1, -1);
      process.env[key] = val;
    }
  } catch {
    /* no .env — rely on the real environment */
  }
}
loadEnv();

const USERNAME = process.env.STEAMBOT_USERNAME || '';
const PASSWORD = process.env.STEAMBOT_PASSWORD || '';
const SHARED_SECRET = process.env.STEAMBOT_SHARED_SECRET || '';
const TOKEN = (process.env.STEAMBOT_AGENT_TOKEN || '').trim();
const PORT = Number(process.env.STEAMBOT_AGENT_PORT || 8790);

if (!USERNAME || !PASSWORD) {
  console.error('STEAMBOT_USERNAME / STEAMBOT_PASSWORD are not set (see .env). Refusing to start.');
  process.exit(2);
}

const RESOLVE_TIMEOUT_MS = 30_000;
// The GC throttles match lookups; one in flight at a time, spaced out.
const GC_MIN_GAP_MS = 1_500;

const steam = new SteamUser();
const cs = new GlobalOffensive(steam);

// STEAMBOT_DEBUG=1 surfaces steam-user's own protocol log -- the only way to see
// WHICH second factor Steam is offering (device code / push confirmation / email)
// and whether a pending push is being polled.
if (process.env.STEAMBOT_DEBUG) {
  steam.on('debug', (msg) => console.log(`[debug] ${msg}`));
}

let gcReady = false;
let lastGcCall = 0;
// A Steam Guard callback waiting for a code delivered over HTTP (see /guard).
let pendingGuard = null;
const friends = new Set();          // SteamID64 strings we are friends with
let allowList = new Set();          // who may become a friend (set by the worker)

// ---- login ----

const TOKEN_FILE = resolve(HERE, '.steam-refresh-token');

function readToken() {
  try {
    return readFileSync(TOKEN_FILE, 'utf8').trim() || null;
  } catch {
    return null;
  }
}

function logOn() {
  const refreshToken = readToken();
  if (refreshToken) {
    console.log('[steam] logging on with the saved refresh token');
    steam.logOn({ refreshToken });
    return;
  }
  const details = { accountName: USERNAME, password: PASSWORD };
  if (SHARED_SECRET) details.twoFactorCode = SteamTotp.generateAuthCode(SHARED_SECRET);
  steam.logOn(details);
}

// steam-user hands us a refresh token after a password login; saving it is what
// makes every later start unattended without a shared_secret on disk.
steam.on('refreshToken', (token) => {
  try {
    writeFileSync(TOKEN_FILE, token, { mode: 0o600 });
    console.log(`[steam] saved refresh token -> ${TOKEN_FILE} (no Steam Guard code needed next time)`);
  } catch (err) {
    console.warn(`[steam] could not save the refresh token: ${err.message}`);
  }
});

steam.on('loggedOn', () => {
  console.log(`[steam] logged on as ${steam.steamID?.getSteamID64()}`);
  steam.setPersona(SteamUser.EPersonaState.Online);
  // CS2 is free; requesting the licence lets a fresh bot account reach the GC.
  steam.requestFreeLicense([730], () => {
    steam.gamesPlayed([730]);        // launching CS2 is what starts a GC session
  });
});

steam.on('steamGuard', (domain, callback, lastCodeWrong) => {
  if (lastCodeWrong) console.warn('[steam] that Steam Guard code was rejected');
  if (SHARED_SECRET && !lastCodeWrong) {
    callback(SteamTotp.generateAuthCode(SHARED_SECRET));
    return;
  }
  const preset = (process.env.STEAMBOT_GUARD_CODE || '').trim();
  if (preset && !lastCodeWrong) {
    console.log('[steam] using STEAMBOT_GUARD_CODE');
    callback(preset);
    return;
  }
  const where = domain ? `emailed to ${domain}` : 'from the Steam mobile app';
  if (!process.stdin.isTTY) {
    // No console to type into (backgrounded, service, or a non-interactive shell).
    // Rather than dying, park the callback and let the code arrive over loopback:
    //   curl -X POST http://127.0.0.1:PORT/guard -H "X-Agent-Token: …" \
    //        -H "content-type: application/json" -d '{"code":"XXXXX"}'
    pendingGuard = { callback, where };
    console.error(`[steam] Steam Guard code required (${where}). No console available — ` +
                  `waiting for POST /guard on port ${PORT}. Codes expire in ~30s, so send a fresh one.`);
    return;
  }
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  rl.question(`[steam] Steam Guard code (${where}): `, (code) => {
    rl.close();
    callback(code.trim());
  });
});

steam.on('error', (err) => {
  // Includes InvalidPassword / RateLimitExceeded / LoggedInElsewhere.
  console.error(`[steam] error: ${err.message} (eresult ${err.eresult})`);
  gcReady = false;
  // An expired/revoked refresh token can only be recovered by dropping it and
  // logging in with the password again — otherwise we'd retry it forever.
  if (err.eresult === SteamUser.EResult.InvalidPassword ||
      err.eresult === SteamUser.EResult.AccessDenied ||
      err.eresult === SteamUser.EResult.Expired) {
    try {
      unlinkSync(TOKEN_FILE);
      console.warn('[steam] discarded the saved refresh token; will use the password next try');
    } catch { /* nothing saved */ }
  }
  setTimeout(logOn, 60_000);
});

steam.on('disconnected', (eresult, msg) => {
  console.warn(`[steam] disconnected: ${msg || eresult}`);
  gcReady = false;
});

cs.on('connectedToGC', () => { gcReady = true; console.log('[gc] session established'); });
cs.on('disconnectedFromGC', (reason) => {
  gcReady = false;
  console.warn(`[gc] disconnected: ${reason}`);   // routine around Valve's updates
});

// ---- friends ----

function noteFriendship(sid64, relationship) {
  const isFriend = relationship === SteamUser.EFriendRelationship.Friend;
  if (isFriend) friends.add(sid64); else friends.delete(sid64);
}

steam.on('friendRelationship', (steamID, relationship) => {
  const sid64 = steamID.getSteamID64();
  if (relationship === SteamUser.EFriendRelationship.RequestRecipient) {
    if (allowList.has(sid64)) {
      steam.addFriend(steamID);                 // accepting, not inviting
      console.log(`[friends] accepted invite from ${sid64}`);
      friends.add(sid64);
      steam.chatMessage(steamID,
        "Hi! I'll message you a link to each match report once your match is parsed.");
    } else {
      console.log(`[friends] ignoring invite from ${sid64} (not on the allow-list)`);
    }
    return;
  }
  noteFriendship(sid64, relationship);
});

/** Accept anything already waiting when we log in (invites sent while offline). */
function acceptPending() {
  const accepted = [];
  const pending = [];
  for (const [sid64, rel] of Object.entries(steam.myFriends || {})) {
    if (rel === SteamUser.EFriendRelationship.Friend) { friends.add(sid64); continue; }
    if (rel !== SteamUser.EFriendRelationship.RequestRecipient) continue;
    if (allowList.has(sid64)) {
      steam.addFriend(sid64);
      friends.add(sid64);
      accepted.push(sid64);
    } else {
      pending.push(sid64);
    }
  }
  return { accepted, pending };
}

// ---- GC lookup ----

function resolveSharecode(sharecode) {
  return new Promise((ok, fail) => {
    if (!gcReady) return fail(new Error('no GC session (bot not connected to CS2 yet)'));
    const gap = Date.now() - lastGcCall;
    const wait = gap >= GC_MIN_GAP_MS ? 0 : GC_MIN_GAP_MS - gap;

    setTimeout(() => {
      lastGcCall = Date.now();
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        cs.removeListener('matchList', onList);
        fail(new Error('GC did not answer in time'));
      }, RESOLVE_TIMEOUT_MS);

      const onList = (matches) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        cs.removeListener('matchList', onList);
        const match = (matches || [])[0];
        // The demo URL rides on the LAST round-stats entry's `map` field — a
        // Valve quirk, not a mistake: `map` carries the replay URL there.
        const url = match?.roundstatsall?.at(-1)?.map || match?.roundstats_legacy?.map || null;
        // matchtime lets the worker tell "not published yet" from "pruned long
        // ago" — the replay CDN answers 502 (not 404) for both.
        ok({ url, matchid: String(match?.matchid ?? ''), matchtime: match?.matchtime ?? null });
      };

      cs.on('matchList', onList);
      try {
        cs.requestGame(sharecode);
      } catch (err) {
        settled = true;
        clearTimeout(timer);
        cs.removeListener('matchList', onList);
        fail(err);
      }
    }, wait);
  });
}

// ---- HTTP (loopback) ----

function readJson(req) {
  return new Promise((ok, fail) => {
    let body = '';
    req.on('data', (c) => {
      body += c;
      if (body.length > 1 << 20) fail(new Error('payload too large'));
    });
    req.on('end', () => {
      if (!body) return ok({});
      try { ok(JSON.parse(body)); } catch (e) { fail(e); }
    });
    req.on('error', fail);
  });
}

function send(res, code, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

const server = createServer(async (req, res) => {
  if (TOKEN && req.headers['x-agent-token'] !== TOKEN) {
    return send(res, 401, { error: 'bad or missing X-Agent-Token' });
  }
  const url = new URL(req.url, 'http://127.0.0.1');

  try {
    if (req.method === 'GET' && url.pathname === '/health') {
      return send(res, 200, {
        ok: true,
        steamid: steam.steamID?.getSteamID64() || null,
        loggedOn: Boolean(steam.steamID),
        gcReady,
        friends: friends.size,
        guardWaiting: pendingGuard ? pendingGuard.where : null,
      });
    }

    if (req.method === 'POST' && url.pathname === '/guard') {
      const { code } = await readJson(req);
      if (!code) return send(res, 400, { error: 'code is required' });
      if (!pendingGuard) return send(res, 409, { error: 'no Steam Guard code is being asked for' });
      const { callback } = pendingGuard;
      pendingGuard = null;                  // re-armed by the next steamGuard event
      callback(String(code).trim());
      return send(res, 200, { accepted: true });
    }

    if (req.method === 'POST' && url.pathname === '/resolve') {
      const { sharecode } = await readJson(req);
      if (!sharecode) return send(res, 400, { error: 'sharecode is required' });
      try {
        const info = await resolveSharecode(String(sharecode));
        return send(res, 200, info);
      } catch (err) {
        return send(res, 503, { error: String(err.message || err) });
      }
    }

    if (req.method === 'POST' && url.pathname === '/message') {
      const { steamid, text } = await readJson(req);
      if (!steamid || !text) return send(res, 400, { error: 'steamid and text are required' });
      if (!steam.steamID) return send(res, 503, { error: 'not logged on' });
      if (!friends.has(String(steamid))) {
        // Steam silently drops messages to non-friends; say so instead.
        return send(res, 409, { error: 'not friends with that account' });
      }
      steam.chatMessage(String(steamid), String(text));
      return send(res, 200, { sent: true });
    }

    if (req.method === 'POST' && url.pathname === '/friends/sync') {
      const { allow } = await readJson(req);
      allowList = new Set((allow || []).map(String));
      const { accepted, pending } = acceptPending();
      return send(res, 200, { accepted, pending, friends: [...friends] });
    }

    return send(res, 404, { error: 'unknown endpoint' });
  } catch (err) {
    return send(res, 500, { error: String(err.message || err) });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[agent] listening on http://127.0.0.1:${PORT}` +
              (TOKEN ? '' : '  (WARNING: STEAMBOT_AGENT_TOKEN is empty — any local process can drive the bot)'));
  logOn();
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    console.log(`[agent] ${sig} — logging off`);
    try { steam.logOff(); } catch { /* already down */ }
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000);
  });
}
