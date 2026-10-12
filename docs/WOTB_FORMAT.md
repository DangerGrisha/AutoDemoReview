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
| 5 | entity create | `entity u32, type u16, space u32, vehicle u32, position 3f, direction 3f, size u32`, then the properties: `count u8` and `count` × (index u8, value). Read only for vehicles (type 2), see [fight events](#fight-events-vehicles-and-the-avatar) |
| 7 | entity property | `entity u32, property u32, size u32, value` |
| 8 | entity method | `entity u32, method u32, size u32, args` |
| 10 | position, 49 bytes | `entity u32, space u32, vehicle u32, x y z f32, error xyz f32, yaw pitch roll f32, flag u8`. x/z = ground plane in metres, x east and z north (the playable area is about ±230…285 m), y = height. yaw = hull heading in radians: 0 faces +z, positive turns toward +x, i.e. clockwise on the minimap (`yaw = atan2(dx, dz)` of the travel direction, median error 0.1° over 5.8k moving samples). Sent about 10 times a second per visible vehicle |
| 39 | 28 bytes, ~8k per battle | f32s that look like the recorder's camera or aim point; not decoded |
| 0xFFFFFFFF | end marker | |

**Visibility:** allies are tracked for the whole battle (the longest silence in
13 battles: 1.1 s). Enemies appear in the stream only while spotted -- silences
of seconds to minutes -- and an enemy never spotted has no position at all.
Every kill (102 of them) has a position of the victim at most 0.13 s before
it, enemies included, so the death spot is always known; wrecks keep sending
positions afterwards and slide a few metres at most. The killer is almost
always in the stream at the moment of the kill too: 42 of 43 kills by enemies
in 13 battles, often after only about 2 s of visibility. The game seems to
reveal a shooter at the shot.

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
| 6 | vehicle killed `[6]`: `{1 victim, 2 killer, 3 ?, 4 ?}` | kill feed |
| 7 | avatar ready `[7]` `{1 entity id}`, one per player | — |
| 8 | Encounter base `[8]`: `{1 team, 2 base id (1), 3 capture points scored AGAINST that team (0–100), 4 1 while the team's own tanks stand in the base}`. Both teams' records come together, every 0.5–1 s while someone captures. **sure**: on Rift a lone ally rolled within 25 m of the base twice and the progress counted up in the enemy team's record, then dropped to 0 when the ally backed out to 27 m. `arena.py` credits field 3 to the other team. The game's tip says the bar fills "до 100" | base capture |
| 12 | Supremacy point `[11]`: `{1 point id (A = 0), 2 owner team, 3 capturing team, 4 capture progress %, 5 ?, 6 ?}`. Each update is the point's whole state, and protobuf leaves zeros out, so an empty message means point A, neutral, untouched. Captures take about 15 s. **sure**: every capture by the recorder's team (the only side that is always visible) starts with one of its tanks inside the point's 15 m | point owners |
| 13 | Supremacy score `[12]`: `{1 team, 2 victory points}`. **sure**: a team's points drop exactly when it loses a tank, within 1 s, and by 30 or 40 (`gameplay_settings.yaml`: 40 for a top-tier tank, 30 for a lower one); the winner of a battle decided on points ended at exactly 1000 | score |
| 15, 16, 17 | per-vehicle `{1 entity, …}` (effects? visibility?) | — |
| 18 | battle settings `[17]`: `{5 "regular", 9 ?, 10 victory points at the start (300), 11 victory points that win (1000), 12 ?}`. Sent in Encounter battles too | Supremacy goal |
| 23 | tutorial hints | — |

The capture updates also tell the **mode** of a replay without
`battle_results.dat`: Supremacy battles send point states and scores from the
start of the battle, Encounter battles the base records. This matched the
results on every replay that has them.

Compact descriptor: the first two bytes (u16le) are the **tank id**. This
matched battle results on every vehicle. The rest of the descriptor (modules)
is not decoded.

### Fight events: vehicles and the Avatar

`events.py` reads these. The indexes are the same in 11.7.1 and 11.20.0.
Vehicle methods and properties arrive only while the vehicle is in view, so
the recorder's own team is complete and enemies are not.

Vehicle entity (entity type 2 in the create packet):

| what | layout | confidence |
|---|---|---|
| create, properties | `0 u8, 1 u8, 2 gun angles u16, 3 health i16`, then 4 u16, 5 public info (nickname, compact descriptor, ...), more not read | sure: health equals the results' max HP for every ally at the start (11.20) |
| property 2 | gun angles u16: turret yaw relative to the hull in the top 10 bits, `(v >> 6) / 1023 · 2π − π`, same sense as the hull yaw; gun pitch in the low 6 bits (not used). Sent about 10 times a second | sure: hull yaw + turret yaw points along the tracer of the shot: median error 0.25°, 0.7° at the 90th percentile, over 919 shots in 13 battles |
| property 3 | health i16 | sure. Always followed by method 1 with the same value, so only creates and method 1 are read |
| method 0 | shooting: burst size u8 (1, or 3 for burst guns) | sure: for every tank of the recorder's team the sizes add up to its shots |
| method 1 | health changed: `new health i16 (negative = overkill), attacker u32, reason u8`. Reasons: 0 shell, 1 fire (ticks every 0.5 s, falling: 60, 55, 50), 2 ramming (both tanks hurt in the same instant), 3 a fall -- the attacker is the vehicle itself; all five in our battles came after the tank dropped 5–8 m within a second | sure: the drops on a tank of the recorder's team add up to its damage received; reasons 1 and 2 likely, 3 sure |
| method 8 | hit: `attacker u32, victim u32, 1 u8, hit point u64, effects u8, damage factor? u8, u8, u8`. The point's low byte is the kind: 0 ricochet the shell flew on from, 1 ricochet, 2 not pierced, 3 pierced, 4 pierced with a critical hit | sure: the hits on a tank of the recorder's team add up to its hits received; kind 3 always came with damage (581/581), kinds 1 and 2 never (127), kind 4 did 79 of 99 times, kind 0 18 of 72 times |
| method 6 | 29 bytes, `energy f, point 3f, normal 3f, u8` | likely: a crash into the scenery |
| method 2 | 8 bytes, 2 floats; seen when a tank died | — |

The results' penetrations are not a plain count of kinds 3 and 4: some kind 0
and kind 2 hits count too. The report therefore shows what a hit did -- its
damage, or "ricochet", "not pierced", "crit" -- instead of a pen count of
its own.

Avatar (the recorder's entity):

| method | layout | confidence |
|---|---|---|
| 29 | tracer: `shooter u32, shot id u32, effects u8, start 3f, velocity 3f, gravity f`. One per shell the recorder saw fired; a ricochet starts a second tracer with the same shot id where it bounced | sure: hull + turret yaw points along it; for the recorder's team one tracer per shell |
| 20 | tracer stop: `shot id u32, end point 3f`. Mostly in the same packet bundle as the tracer -- the server sends the whole flight at once. A stop without a tracer is a hidden shooter's shell landing in view | sure: a shell that hit ends 1.5 m from its victim's position (median; 4.2 m at the 99th percentile) |
| 27 | `shot id u32, u32, u8, end point 3f, direction 3f, u8`: the shell exploding on the ground or an object | likely |
| 19 | `the recorder's vehicle u32, 1 u8, enemy u32, u32, u8` | guess: the recorder spotted that enemy |
| 5, 8 | the recorder's health u16 + flag, and a hit direction f + flag, at each hit on them | likely |
| 13, 35 | `vehicle u32, u8, f` / `vehicle u32, f, f, u8` | guess: reload timers |
| 38 | `victim u32, ...` after each of the recorder's hits | guess: the damage log |
| 46 | `vehicle u32, account u32, u32, protobuf {1 {1 x f, 2 y f, 3 z f}}` | guess: a minimap ping |
| 39 | `0000` every 10 s | — |

**Out of view.** An enemy hit -- or burning -- while unseen comes back with
less health at its next create packet; `events.py` records that as `unseen`.
On Rift an enemy hit by a Panzer 58 lost 249 out of view -- most likely
burning, since every hit the results give the Panzer 58 is in view -- and the
Panzer 58's damage in the results is exactly its seen damage plus those 249. Over
all recordings that run to the end of the battle, the results' damage minus
the damage seen equals the unseen health drops, to the point.

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
| `maps.yaml` | `maps.json`: id, scene name, modes, variant `tag`, assault respawn points |
| `3d/Maps/<scene>.sc2` (see below) | `maps.json`: `world` (terrain square), `border` (playable area), `base` (Encounter: x, z, radius), `points` (Supremacy: id, x, z, radius) |
| `Gfx/UI/BattleScreenHUD/minimap/<key>/MiniMapSmall@2x.packed.webp` | `minimaps/<key>.webp`, 512×512; `maps.json`: `minimap_crop` |

### Scenes (`.sc2`) and the minimap

A scene is a DAVA engine file (`blitzreview/dava.py` reads it):

```
"SFV2" | version i32 (48) | node count i32
version tags: "KA" format 1, empty
descriptor: size u32 | size bytes
string table: "KA" 0x0002 | count u32 | count × (u16 len + utf-8) | count × u32 id
body: count u32, then (u32 key id, variant) items: #dataNodes, #hierarchy, ...
```

After the string table every key, string, fast name and file path is a u32 id.
Keyed archives are `"KA" | u16 format` with format 0x0102 (`count u32`, then
`id, variant` pairs) or 0xFF02 (empty, nothing follows). A variant is a type
byte plus payload (DAVA `VariantType` numbering: 1 bool … 8 keyed archive with
a byte size in front, 19 AABBox3, 27 variant vector …). All 36 scenes of the
11.20 client parse to their last byte.

Entities sit in `#hierarchy` (children in their own `#hierarchy`); components
name their kind in `comp.typename`. Two of them place the minimap:

- the **Landscape** render object's `bbox` (6 floats): the terrain is
  600 × 600 m around the origin on every map;
- **MapBorderComponent** `mbc.rect` (4 floats: x0, y0, x1, y1): the playable
  area. Scenes with map variants (`milbase` … `milbase_05`) carry one border per
  variant; its LabelComponent label (`mlb4`) is the variant's `tags` in
  `maps.yaml`. Scene y is the replay's z.

**Minimap geometry**, checked three ways: the tracks of all 13 local replays
follow the roads on the picture; the variant minimaps grey out everything
outside the variant's border, and that edge (a thin white line) sits where the
transform puts the border, within 1–2 px; the recorder's team starts on the
`maps.yaml` spawn points. The minimap shows the whole terrain square, x to
the right and z up, and fills only the top-left **505 × 505** pixels of the
512 × 512 texture (the rest is transparent; its 6 × 6 grid is 100 m a cell):

```
u = left + (x − x0) / (x1 − x0) · (right − left)      # world = x0 z0 x1 z1
v = top  + (z1 − z) / (z1 − z0) · (bottom − top)      # crop  = left top right bottom
```

**Capture zones** are entities with a CustomPropertiesComponent whose
`cpc.properties.archive` has a `type`:
- `controlpoint`: the Encounter base;
- `strategicpoint`: a Supremacy point, with `baseID` (the point id of arena update 12, shown as A, B, C … in the game), `radius` 15 and its scoring settings.

The position is the TransformComponent's `tc.worldTranslation`: x, y (the replay's z), height. It is the same frame as the spawn points.

Variant scenes label their zones like their borders. An untagged map uses the unlabelled ones.

Every map has its Encounter base, and the 30 maps played in Supremacy have 3 or 4 points. Himmelsdorf and Plant have four, and all four are live: the battles send states for all of them.

Most bases carry no radius, so the game uses a default. It is **25 m**, measured on Rift: the capture started at 25 m from the centre and stopped at 27 m. Variant bases that state a radius say 15 m.

**Terrain heights** (the 3D view). The Landscape object's `hmap` names a
`.heightmap` file next to the scene (`landscape/canyon_height.heightmap`):

```
size u32 (512) | tile u32 (16) | size × size u16le, stored TILE BY TILE
```

The values come in (size / tile)² tiles of `tile` × `tile`, tiles in
row-major order, each tile row by row. Untiled, row j runs along y (the
replay's z, south to north) and column i along x, from the bbox's minimum
corner: `height = z0 + value / 65535 · (z1 − z0)` over the bbox's z range
(0–100 m on Canyon, 0–150 on Erlenberg). **sure**: on all 11 maps we have
replays of, a tank's height in the position packets is the terrain's under it
to a few centimetres (median 1–6 cm, 90 % within 15 cm; the rest stand on
bridges and rocks). Read row by row without untiling, nothing correlates.

`tools/wotb_extract_assets.py` resamples each scene's heightmap to 160 × 160
(3.8 m apart) and writes `assets/terrain/<scene>.bin`: decimetres as i16le,
row by row from the south, each row's first value as is and the rest as steps,
zlib-compressed -- about 17 KB a map. `*_colormap.dx11.dds` (2048² DXT3) is
the bare ground's colour; the 3D view drapes the minimap over the relief
instead, because it shows the buildings and roads.

Not done yet:
- Buildings and objects for 3D: the `.scg` geometry next to each scene.
- `blitz/<scene>.mkm` (`kkm` header, a 1024² 2-bit grid) and `.lka`: probably terrain kinds and the ground-sound map; not needed so far.

## Open questions

- Which hits the results count as penetrations (kinds 0 and 2 sometimes do), and what the hit's effects byte, "damage factor" and the last two bytes mean.
- Avatar methods 19, 13, 35, 38, 46 (guesses above) and vehicle properties 0, 1, 4, 7, 8, 9.
- Assist kinds A and B: which one is spotting and which is tracking.
- Point state fields 5 and 6 (seen while a capture stalls and while it runs faster, maybe the crew skill "Стремительный захват"), and kill field 4.
