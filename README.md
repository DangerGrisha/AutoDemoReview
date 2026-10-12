# demoReview

A CS2 (Counter-Strike 2) demo analysis tool. It parses a single `.dem` file and
generates an **interactive, self-contained HTML report** (inline CSS/JS, no
external libraries), framed around one highlighted player (default `P1rat1C`):

- a **sticky header** (always visible): final score, total kills, and the
  player's K/D
- a **round jump bar** — one dot per round, green/red/grey by the player's kills
  vs deaths that round; click to smooth-scroll to the round
- **filter buttons**: all rounds / my deaths only / my multi-kills (2+) /
  vs your **nemesis** (auto-detected: the opponent who killed you most) — pure
  show/hide, no reload
- a **pro scoreboard** (sortable): K / D / A, **ADR**, **KAST%**, **HS%**, and an
  **HLTV 2.0-style rating** — click any column header to sort
- a **"your match" strip**: your rating, ADR, KAST, opening win %, clutches, etc.
- a **2D map heatmap** on the real radar: where you (or everyone) get kills vs die,
  plus an optional **grenade layer** — detonation dots per type (smoke / molotov /
  HE / flash / decoy) with a line back to where each was thrown from
- a **2D round replay**: canvas playback of all 10 players with play/scrub and
  jump-to-kill — you're the ringed green dot. Each player shows a **name tag** and
  their **held weapon** (the real gun/util — AK, M4A1‑S, AWP, DEAG, 🔪, 💣, …),
  updated frame-by-frame; **flashed** players wash toward white by how blind they are. A live
  **killfeed** stacks kills newest-on-top, each **death** leaves a red ✕ with the
  victim's name, and a **planted bomb** shows a 💣 with a 40 s countdown. Rounds
  hold ~2 s on the final frame so they don't cut off. **Grenades** fly along their
  real throw arc (the trail shows the direction) and detonate into timed effects:
  a smoke cloud, a molotov fire area, a flash ring, an HE burst, or a decoy
  marker, each lasting its real duration (toggle with **Nades on/off**)
- **round cards** framed as **"Your team" vs "Opponent"** with insight badges
  (**clutch won**, **3K+/ACE**), an **opening-duel indicator**, a collapsible
  **per-player economy breakdown**, players alive, and the kill list
- **filters**: all / my deaths / my multi-kills / clutch rounds / vs your
  auto-detected **nemesis**

Parsing is done with [`demoparser2`](https://github.com/LaihoE/demoparser), a fast
Rust-backed parser for Source 2 / CS2 demos.

> **Maps:** the heatmap and replay need a radar image + calibration; **de_mirage**
> ships in `src/demoreview/assets/` (sourced from [awpy](https://github.com/pnxenopoulos/awpy)).
> On other maps the report still renders — those two panels are just hidden.
> **Rating / KAST** are approximations of the (non-public) HLTV 2.0 formula.

> Score comes from the engine's native per-team counter (`team_rounds_total`), so
> it is correct across the halftime side swap (e.g. 13–11, not a raw CT/T tally).
> "Your team" follows the highlighted player through the swap. Buy value is
> sampled at each round's `buytime_ended` from `current_equip_value`; buy type is
> a heuristic (`<$1500` eco, `<$3500` force, else full).

## Setup

Requires Python 3.12 (installed via Homebrew: `brew install python@3.12`).

```bash
cd demoReview
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Put a CS2 demo file in the `demos/` folder, then:

```bash
python src/parse_demo.py demos/match.dem
# highlight a different player (rival auto-detected as their nemesis):
python src/parse_demo.py demos/match.dem SomePlayerName
# ...or pin a specific rival for the "vs" filter instead of the nemesis:
python src/parse_demo.py demos/match.dem SomePlayerName SomeRivalName
```

This writes `output/<demo-name>.html`. Open it in your browser:

```bash
open output/match.html      # macOS
```

The console just prints the final score and the report path; the full,
styled breakdown lives in the HTML file. The highlighted player defaults to
`P1rat1C` (pass a name as the second argument to change it).

## Where do I get a `.dem` file?

- Your own matches: CS2 → **Watch** → **Your Matches** → download, then locate the
  `.dem` under your Steam `csgo`/`replays` folder.
- A GOTV recording, or a demo from a service like FACEIT.

Drop it into `demos/` (files there are gitignored so demos aren't committed).

## World of Tanks Blitz (beta)

A second, independent analyzer for Wargaming's **WoT Blitz** replays lives in
`src/blitzreview/` (stdlib only, no CS2 code shared yet):

```bash
python src/review_blitz.py path/to/battle.wotbreplay [--json out.json] [--html out.html]
```

It prints the battle (map, mode, outcome, all 14 players with damage / kills /
shots / hp left, the kill feed with battle times), can dump the full battle
dict as JSON and write the HTML report. Replays without `battle_results.dat`
(recorder left early) still show the roster and kill feed.

On the site: sign in, open **Blitz** in the top bar and drag replays onto the
page (one or many at once). Each battle gets a report: your numbers with your
rank in the lobby, both teams, and a **2D replay** on the game's minimap --
every tank as the game's class mark (LT / MT / HT / TD) with its heading,
enemies only while they were spotted (then a dashed "last seen" outline), wrecks
where tanks died, trails, an arrow from killer to victim at every kill, the
Encounter base or the Supremacy points with who holds them and the capture
progress, a board with the tanks still in the battle per side (plus the
Supremacy score), a strip under the timeline showing which side had more tanks
when, play / scrub / speed, and the kill feed beside it (click a time to jump
there). Click a tank to see its whole route and its results; "★ Лучший в
команде" picks the teammate with the most damage. A battle uploaded before
a feature existed gets it when its replay is dropped again. Battles live in their own table
(`wotb_battles`, schema v4) and never mix into the CS2 matches or career. Windows/Steam replays live in
`%LOCALAPPDATA%\wotblitz\DAVAProject\replays`; there is no API to download them.

Tank and map names, tiers, minimaps and the minimap geometry come from
`src/blitzreview/assets/`, generated once from an installed client with
`tools/wotb_extract_assets.py` (measuring the minimaps needs Pillow).
The reverse-engineered format is documented in `docs/WOTB_FORMAT.md`.

## Project layout

```
demoReview/
├── demos/                    # your .dem files (gitignored)
├── output/                   # generated HTML reports (gitignored)
├── src/
│   ├── parse_demo.py         # thin shim -> demoreview.cli
│   └── demoreview/           # the package
│       ├── parsing.py        # collect(): events + ticks -> raw data
│       ├── stats.py          # ADR / KAST / rating / clutch / trade math
│       ├── maps.py           # radar calibration + world->pixel
│       ├── replay.py         # position sampling for the 2D replay
│       ├── render.py         # HTML / CSS / JS generation
│       └── assets/           # embedded radar image(s)
├── requirements.txt
└── README.md
```

You can also run it as a module: `python -m demoreview demos/match.dem`.

## Tests

```bash
python tests/run_tests.py          # everything, no dependencies needed
python tests/run_tests.py tests    # just the analysis suite
```

The runner needs nothing but the stdlib, so it works with the same interpreter
that runs the app. Every test file is also a plain pytest module, so
`python -m pytest` works where pytest is installed.

What is covered: the C2R3 reader (`tests/test_c2r3.py` — a reference encoder
written from the format spec, plus a parity check against a real artifact from
the Rust writer), the tactical facts engine (`tests/test_tactics.py` — above all
that every metric is ranked the way a human would rank it, which is the bug
class that silently praises a player for what is losing them the match), the
free coach (`tests/test_coachrules.py` — no finding may cite a round that isn't
in the fact sheet), report rendering and its link whitelist
(`tests/test_coachreport.py`), and the Steam sharecode codec
(`steambot/tests/`).
