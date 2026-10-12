# World of Tanks Blitz replay format (`.wotbreplay`)

Reverse-engineered notes behind `src/blitzreview/`. Verified on client versions
**11.7.1** and **11.20.0** (Wargaming EU client; Lesta's Tanks Blitz was not
examined). Wargaming publishes no schema. Field numbers come from
[eigenein/wotbreplay-parser](https://github.com/eigenein/wotbreplay-parser)
(MIT) where noted, and from cross-checks on our replays. Those cross-checks are
pinned in `tests/test_wotb_*.py`.

Confidence levels:
- **sure**: checked against independent data in every fixture
- **likely**: plausible values, but not cross-checked
- **guess**: a name only

There is **no API** that downloads replays. The game writes them only to the
player's device:
- Windows/Steam: `%LOCALAPPDATA%\wotblitz\DAVAProject\replays`
- Mobile: inside the app sandbox, reachable only through the in-game "share" button

The client prunes old replays.

## Container

A replay is a zip archive with three members:

| member | contents |
|---|---|
| `meta.json` | ~330 bytes of JSON (fields below) |
| `battle_results.dat` | final results of all 14 players. **Missing** when the recorder quit before they arrived (1 of our 13 replays) |
| `data.wotreplay` | packet stream, 1–5 MB uncompressed |

`meta.json` fields:

| field | meaning |
|---|---|
| `version` | client version, e.g. `"11.20.0"` |
| `mapName` | map key, e.g. `"canyon"` |
| `mapId` | numeric map id, e.g. `27` |
| `playerName`, `dbid` | recorder's nickname and account id (as a string) |
| `arenaUniqueId` | battle id (string); shared by every player's replay of the same battle |
| `battleStartTime` | unix seconds (string) |
| `battleDuration` | seconds, float. The length of the whole battle, not of the recording |
| `playerVehicleName` | recorder's tank, internal key (`"Object_704"`) |
| `vehicleCompDescriptor` | recorder's tank id |
| `arenaBonusType` | `1` = random battle |
| `camouflageId` | — |

## `battle_results.dat`

The file is a Python-2 pickle, protocol 2, of the tuple
`(arena_unique_id, protobuf_bytes)`. Its opcodes are:

```
PROTO 2 | LONG1 arena_id | BINSTRING payload | TUPLE2 | BINPUT | STOP
```

**Never unpickle it.** A pickle can carry arbitrary code. `battle_results.unwrap`
reads exactly these opcodes and rejects anything else, which includes every
pickle that needs a global.

### Top level

| # | field | type | confidence | notes |
|---|---|---|---|---|
| 1 | mode_map | uint | sure | low 16 bits = map id; bits 16–23 = mode: 0 encounter (Встречный бой), 1 supremacy (Превосходство) |
| 2 | timestamp | int64 | sure | ≈ `battleStartTime` |
| 3 | winner_team | uint | sure | absent on a draw. Checked: a fully destroyed team is never the winner |
| 8 | author | msg | sure | the recorder's own totals |
| 9 | room_type | uint | sure (eigenein) | `1` in all our replays. **This is not the recorder's team**; reading it as the team flipped half the outcomes |
| 137 | free_xp | uint | likely (eigenein) | |
| 201 | players | repeated msg | sure | `{1 account_id, 2 info}` |
| 301 | results | repeated msg | sure | `{1 entity_id, 2 info}` |

The entity id in `#301.#1` **equals the vehicle entity id in `data.wotreplay`**,
so results join the packet stream exactly.

### Author (`#8`)

| # | field | confidence |
|---|---|---|
| 1 | hp_left, int32 (−2 = auto-destroyed for inactivity) | sure |
| 2 | credits, including premium and awards | likely |
| 3 | xp, including premium | likely |
| 4 | shots | sure |
| 5 | hits | sure |
| 6 | splashes | likely |
| 7 | penetrations | sure |
| 8 | damage | sure |
| 101 | account_id | sure |
| 102 | team — this is the recorder's team | sure |
| 103 | tank_id | sure |

### Player info (`#201.#2`)

| # | field | confidence |
|---|---|---|
| 1 | nickname | sure |
| 2 | platoon id: shared by platoon mates, absent for solo players | sure |
| 3 | team | sure |
| 5 | clan tag | sure |
| 7 | avatar | sure |
| 4 | unknown small uint | — |
| 8 | unknown uint | — |
| 9 | rank | likely (eigenein) |

### Result info (`#301.#2`)

| # | field | confidence |
|---|---|---|
| 1 | hp_left, int32. ≤ 0 = destroyed; overkill shows up as −N | sure: `max_hp − damage_received == max(hp_left, 0)` |
| 2 | credits | likely |
| 3 | base xp | likely |
| 4 | shots | sure |
| 5 | hits | sure |
| 7 | penetrations | sure |
| 8 | damage dealt | sure |
| 9 | assisted damage, kind A | likely: spotting vs tracking not yet told apart |
| 10 | assisted damage, kind B | likely: same |
| 11 | damage received | sure |
| 12 | hits received | sure: `#12 == #13 + #15` |
| 13 | non-penetrating hits received | sure |
| 15 | penetrations received | sure |
| 17 | enemies damaged | likely |
| 18 | kills | sure: equals the number of victims whose `#25` names this vehicle (168/168) |
| 25 | killer's entity id | sure: present exactly when hp_left ≤ 0 |
| 32 | victory points earned (supremacy) | guess (eigenein) |
| 33 | victory points seized (supremacy) | guess (eigenein) |
| 101 | account_id | sure |
| 102 | team | sure |
| 103 | tank_id | sure |
| 107 | mm_rating, float | likely: display = 3000 + 10·value. Often absent |
| 117 | damage blocked | likely (eigenein) |
| 125 | max HP | sure. Written by 11.20, missing in 11.7 |

Unknown: 16, 23, 24, 27/28 (repeated `{1,2}` pairs, possibly achievements),
29–31, 36, 105, 106, 116, 119, 124, 126.

## `data.wotreplay`

Header:

```
magic 0x12345678 u32le | rest_size u32le (= file size − 8) | header_size u32le
header: u8 len + client hash (32 hex chars) | u8 len + version | u8 (= 10, unknown)
```

Packets follow until EOF:

```
size u32le | type u32le | clock f32le | payload[size]
```

`clock` counts seconds from the **start of the recording**, not from the start
of the battle. All our fixtures split into packets byte for byte.

| type | meaning | layout |
|---|---|---|
| 0 | base player create | Avatar entity id u32, u16, u32, pascal nickname, arena id u64, map id u32, then a pickle (`vehNames`, `accountDatabaseIds`, `battleLevel`, …; no globals). The analyzer reads only the first u32 |
| 5 | entity create | starts with the entity id u32; rest not decoded |
| 7 | entity property | `entity u32, property u32, size u32, value`. Property ids 0–10 seen; not decoded yet |
| 8 | entity method | `entity u32, method u32, size u32, args` |
| 10 | position, 49 bytes | `entity u32, space u32, vehicle u32, x y z f32, error xyz f32, yaw pitch roll f32, flag u8`. x/z = ground plane in metres (maps are about ±250 m), y = height, yaw in radians. Sent about 10 times a second per visible vehicle |
| 39 | 28 bytes, ~8k per battle | f32s that look like the recorder's camera or aim point; not decoded |
| 0xFFFFFFFF | end marker | |

**Visibility:** allies are tracked for the whole battle. Enemies appear in the
stream only while spotted, and an enemy never spotted has no position at all.

**Recording end:** the stream stops when the recorder leaves the battle (often
right after dying). The battle itself, and `battleDuration`, can run longer.

### Arena updates: entity method 48 on the Avatar

This is BigWorld's `updateArena(updateType: u8, blob)`. The blob carries a
packed length (one byte, or `0xFF` + u24le), followed by protobuf.

| type | content (top-level field in brackets) | used |
|---|---|---|
| 1 | vehicle list `[1]`: repeated `{1 entity id, 2 compact descriptor, 3 nickname, 4 team, 5 ?, 7 account id, 8 clan tag, 9 ?, 10 platoon id, 17 avatar}` | roster |
| 3 | period `[3]`: `{1 period, 2 length f64, 3 length s}`. Periods: 1 waiting (60 s), 2 pre-battle countdown (7 s), 3 battle (420 s), 4 after | battle start = clock of period 3 |
| 4 | `[4]` list of entity ids (statistics?) | — |
| 5 | `[5]` `{1 {1 entity id, 2 kills}}`, sent right after each kill | — |
| 6 | vehicle killed `[6]`: `{1 victim, 2 killer, 3 ?}` | kill feed |
| 7 | avatar ready `[7]` `{1 entity id}`, one per player | — |
| 8 | `[8]` `{1 team, 2 1}`, seen in encounter battles (base?) | — |
| 15, 16, 17 | per-vehicle `{1 entity, …}` (effects? visibility?) | — |
| 18 | `[17]` `{5 "regular", 9, 10 300, 11 1000, 12}` battle settings | — |
| 23 | tutorial hints | — |

Compact descriptor: the first two bytes (u16le) are the **tank id**. This
matched battle results on every vehicle. The rest of the descriptor (modules)
is not decoded.

## Tank ids

```
tank_id = (index << 8) | (nation << 4) | 1
```

- `index` is the vehicle's `<id>` in `Data/XML/item_defs/vehicles/<nation>/list.xml`. All indices are ≤ 255.
- `nation` is the id in `vehicles/common/nations.xml`: ussr 0, germany 1, usa 2, china 3, france 4, uk 5, japan 6, other 7, european 8.

Example: Object 704 = ussr index 32 → `0x2001` = 8193.

## Client files (offline assets)

The Steam client lives at `steamapps/common/World of Tanks Blitz/Data`. Its
files are packed as DVPL:

```
payload | original u32 | packed u32 | crc32(payload) u32 | kind u32 | "DVPL"
```

`kind` is 0 for stored, 1 or 2 for an LZ4 block.

`tools/wotb_extract_assets.py` turns them into committed JSON plus minimaps:

| source | output |
|---|---|
| `XML/item_defs/vehicles/*/list.xml` + `Strings/{ru,en}.yaml` | `vehicles.json`: tier, class from tags, ru/en names |
| `maps.yaml` | `maps.json`: id, scene name, modes, assault respawn points |
| `Gfx/UI/BattleScreenHUD/minimap/<key>/MiniMapSmall@2x.packed.webp` | `minimaps/<key>.webp`, 512×512 |

Not done yet:
- **Map bounds.** Needed to place world coordinates on the minimap; not in `maps.yaml`. Candidates: the scene `.sc2`, or `blitz/<map>.mkm` (a 1024² 2-bit grid).
- **Map geometry** for a future 3D view: `3d/Maps/<scene>/landscape/*_height.heightmap` plus `*_colormap`.

## Open questions

- Event methods: shots, damage, health changes. Entity method ids 0–48 have been seen. Decoded events will be checked against the result totals.
- Assist kinds A and B: which one is spotting and which is tracking.
- Game mode for replays without battle results. The period-1 update has a field 5 that matches the mode, but it is not present in every replay.
