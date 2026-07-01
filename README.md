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
- a **2D map heatmap** on the real radar: where you (or everyone) get kills vs die
- a **2D round replay**: canvas playback of all 10 players with play/scrub and
  jump-to-kill — you're the ringed green dot
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
