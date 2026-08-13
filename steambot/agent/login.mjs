/**
 * One-off login helper: get a refresh token using the phone's Approve prompt.
 *
 * Why this exists: `steam-user`, when Steam offers BOTH a device code and a push
 * confirmation, emits `steamGuard` and waits for a typed code — it never polls
 * `PollAuthSessionStatus`, so tapping **Approve** on the phone does nothing (the
 * result is never collected). Confirmed in its own debug log: zero poll requests.
 * `steam-session` (already a dependency of steam-user) polls on its own, so we
 * use it directly, once, purely to obtain a refresh token.
 *
 * The token is written to `.steam-refresh-token`, which agent.mjs prefers over
 * the password — so this is needed once per account, not once per start.
 *
 *   node steambot/agent/login.mjs
 *   node steambot/agent/login.mjs --code 12345    # if you do have a code
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { EAuthSessionGuardType, EAuthTokenPlatformType, LoginSession } from 'steam-session';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..', '..');
const TOKEN_FILE = resolve(HERE, '.steam-refresh-token');

function loadEnv() {
  try {
    for (const line of readFileSync(resolve(REPO_ROOT, '.env'), 'utf8').split(/\r?\n/)) {
      const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
      if (!m) continue;
      const [, key, raw] = m;
      if (process.env[key] !== undefined) continue;
      let val = raw.trim();
      if ((val.startsWith('"') && val.endsWith('"')) ||
          (val.startsWith("'") && val.endsWith("'"))) val = val.slice(1, -1);
      process.env[key] = val;
    }
  } catch { /* no .env */ }
}
loadEnv();

const USERNAME = process.env.STEAMBOT_USERNAME || '';
const PASSWORD = process.env.STEAMBOT_PASSWORD || '';
if (!USERNAME || !PASSWORD) {
  console.error('STEAMBOT_USERNAME / STEAMBOT_PASSWORD are not set in .env');
  process.exit(2);
}

const codeArg = process.argv.indexOf('--code');
const CODE = codeArg > -1 ? (process.argv[codeArg + 1] || '').trim() : '';

const GUARD_NAMES = {
  [EAuthSessionGuardType.EmailCode]: 'a code emailed to you',
  [EAuthSessionGuardType.DeviceCode]: 'a code from the Steam mobile app',
  [EAuthSessionGuardType.DeviceConfirmation]: 'an Approve/Deny prompt in the Steam mobile app',
  [EAuthSessionGuardType.EmailConfirmation]: 'a confirmation link in your email',
};

// Steam's approval prompt expires after a couple of minutes. Rather than making
// the human race that window, each expiry starts a fresh attempt: a new prompt
// appears, so Approve can be tapped whenever — the next round picks it up.
const MAX_ATTEMPTS = Number(process.env.STEAMBOT_LOGIN_ATTEMPTS || 12);

/** One login attempt. Resolves 'ok' | 'timeout', rejects on a real failure. */
function attempt(n) {
  return new Promise((done, fail) => {
    const session = new LoginSession(EAuthTokenPlatformType.SteamClient);
    let settled = false;

    session.on('authenticated', () => {
      if (settled) return;
      settled = true;
      writeFileSync(TOKEN_FILE, session.refreshToken, { mode: 0o600 });
      console.log(`\nLogged in as ${session.accountName} (${session.steamID?.getSteamID64()})`);
      console.log(`Refresh token saved -> ${TOKEN_FILE}`);
      console.log('The agent will now start unattended: node steambot/agent/agent.mjs');
      done('ok');
    });

    session.on('timeout', () => {
      if (settled) return;
      settled = true;
      console.log(`[attempt ${n}] prompt expired — sending a fresh one…`);
      try { session.cancelLoginAttempt(); } catch { /* already gone */ }
      done('timeout');
    });

    session.on('error', (err) => {
      if (settled) return;
      settled = true;
      fail(err);
    });

    session.startWithCredentials({
      accountName: USERNAME,
      password: PASSWORD,
      ...(CODE && n === 1 ? { steamGuardCode: CODE } : {}),
    }).then((result) => {
      if (!result.actionRequired) {
        console.log('No second factor required; finishing…');
        return;
      }
      const actions = result.validActions || [];
      if (n === 1) {
        console.log('Steam wants a second factor. Available:');
        for (const a of actions) {
          console.log(`  - ${GUARD_NAMES[a.type] || `type ${a.type}`}${a.detail ? ` (${a.detail})` : ''}`);
        }
      }
      if (actions.some((a) => a.type === EAuthSessionGuardType.DeviceConfirmation)) {
        console.log(`[attempt ${n}] APPROVE prompt sent to the Steam mobile app — ` +
                    'tap it whenever; this keeps re-asking until you do.');
      } else if (!CODE) {
        settled = true;
        fail(new Error('Steam offered no push confirmation; re-run with --code XXXXX'));
      }
    }).catch((err) => {
      if (settled) return;
      settled = true;
      fail(err);
    });
  });
}

for (let n = 1; n <= MAX_ATTEMPTS; n++) {
  let outcome;
  try {
    outcome = await attempt(n);
  } catch (err) {
    console.error(`\nLogin failed: ${err.message}${err.eresult ? ` (eresult ${err.eresult})` : ''}`);
    process.exit(5);
  }
  if (outcome === 'ok') process.exit(0);
}
console.error(`\nGave up after ${MAX_ATTEMPTS} prompts — none were approved.`);
process.exit(4);
