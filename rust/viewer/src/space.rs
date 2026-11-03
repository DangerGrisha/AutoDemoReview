//! Which parts of a map are solid, from `tools/prepare_space.py`: a grid of 16-unit
//! cells, in the map file's frame (Hammer units, y up, z = -Hammer y), which is the
//! scene's divided by its scale.
//!
//! A smoke floods the open cells around it and a molotov the floor around it, so both
//! stop at walls and run further down a corridor than across a square, as in CS2. A
//! bullet stops at the first solid cell along its line.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashSet};

use bevy::asset::io::Reader;
use bevy::asset::{AssetLoader, LoadContext};
use bevy::prelude::*;

const MAGIC: &[u8; 4] = b"C2SP";
/// Must match VERSION in tools/prepare_space.py.
const VERSION: u32 = 1;
const HEADER: usize = 36;

pub type Cell = [i32; 3];

pub struct SpaceGrid {
    origin: Vec3,
    cell: f32,
    dims: [i32; 3],
    bits: Vec<u8>,
}

/// A cell waiting in a flood, nearest first.
struct Queued {
    key: f32,
    cell: Cell,
}

impl PartialEq for Queued {
    fn eq(&self, other: &Self) -> bool {
        self.key == other.key
    }
}
impl Eq for Queued {}
impl PartialOrd for Queued {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Queued {
    fn cmp(&self, other: &Self) -> Ordering {
        other.key.total_cmp(&self.key)
    }
}

const FACES: [Cell; 6] = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]];

impl SpaceGrid {
    /// Takes the file by value: de_vertigo's grid is 23 MB, and a copy would double that
    /// in the wasm's memory, which never shrinks back.
    pub fn parse(mut bytes: Vec<u8>) -> Result<Self, String> {
        if bytes.len() < HEADER || &bytes[..4] != MAGIC {
            return Err("not a space file".into());
        }
        let u = |at: usize| u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap());
        let f = |at: usize| f32::from_le_bytes(bytes[at..at + 4].try_into().unwrap());
        if u(4) != VERSION {
            return Err(format!("space file version {}, want {VERSION}", u(4)));
        }
        let origin = Vec3::new(f(8), f(12), f(16));
        let cell = f(20);
        let dims = [u(24), u(28), u(32)];
        if !(cell > 0.0) || dims.iter().any(|&d| d == 0 || d > 4096) {
            return Err("space file has no cells".into());
        }
        let count = dims.iter().map(|&d| d as usize).product::<usize>();
        bytes.drain(..HEADER);
        let bits = bytes;
        if bits.len() != count.div_ceil(8) {
            return Err(format!("space file holds {} bytes of cells, want {}", bits.len(), count.div_ceil(8)));
        }
        Ok(SpaceGrid { origin, cell, dims: dims.map(|d| d as i32), bits })
    }

    pub fn cell_size(&self) -> f32 {
        self.cell
    }

    fn index(&self, c: Cell) -> Option<usize> {
        let [nx, ny, nz] = self.dims;
        if c[0] < 0 || c[1] < 0 || c[2] < 0 || c[0] >= nx || c[1] >= ny || c[2] >= nz {
            return None;
        }
        Some(((c[0] * ny + c[1]) * nz + c[2]) as usize)
    }

    /// Outside the grid is open: there is no map there to stop anything.
    pub fn solid(&self, c: Cell) -> bool {
        self.index(c).is_some_and(|i| self.bits[i / 8] >> (i % 8) & 1 == 1)
    }

    fn inside(&self, c: Cell) -> bool {
        self.index(c).is_some()
    }

    pub fn cell_of(&self, p: Vec3) -> Cell {
        let q = (p - self.origin) / self.cell;
        [q.x.floor() as i32, q.y.floor() as i32, q.z.floor() as i32]
    }

    pub fn centre(&self, c: Cell) -> Vec3 {
        self.origin + (Vec3::new(c[0] as f32, c[1] as f32, c[2] as f32) + 0.5) * self.cell
    }

    /// The open cell a grenade lying at `p` is in: it rests on the floor, whose cell is
    /// solid, so this looks up first.
    fn open_near(&self, p: Vec3) -> Option<Cell> {
        let c = self.cell_of(p);
        if !self.inside(c) {
            return None;
        }
        for dy in [0, 1, 2, 3] {
            let up = [c[0], c[1] + dy, c[2]];
            if !self.solid(up) && self.inside(up) {
                return Some(up);
            }
        }
        FACES.iter().map(|f| [c[0] + f[0], c[1] + f[1], c[2] + f[2]]).find(|&n| self.inside(n) && !self.solid(n))
    }

    /// The open cells a smoke at `start` fills, nearest first, with how far each is.
    /// It takes as many cells as half an ellipsoid of `radius` across and `up` high has, so
    /// on open ground it is that dome and in a corridor it runs further. Below the start
    /// counts `down` for every `radius`, so smoke spills down a ledge but not far.
    pub fn flood_smoke(&self, start: Vec3, radius: f32, up: f32, down: f32) -> Vec<(Cell, f32)> {
        let Some(first) = self.open_near(start) else { return Vec::new() };
        let k = self.cell;
        let distance = |c: Cell| {
            let d = (Vec3::new(c[0] as f32, c[1] as f32, c[2] as f32)
                - Vec3::new(first[0] as f32, first[1] as f32, first[2] as f32))
                * k;
            let vertical = if d.y > 0.0 { d.y * radius / up } else { -d.y * radius / down };
            (d.x * d.x + d.z * d.z + vertical * vertical).sqrt()
        };
        // the dome's cells, counted on the grid itself so the two agree
        let (rx, ry) = ((radius / k).ceil() as i32, (up / k).ceil() as i32);
        let mut budget = 0usize;
        for x in -rx..=rx {
            for y in 0..=ry {
                for z in -rx..=rx {
                    if distance([first[0] + x, first[1] + y, first[2] + z]) <= radius {
                        budget += 1;
                    }
                }
            }
        }
        let limit = radius * 2.5;
        let mut out = Vec::with_capacity(budget);
        let mut seen = HashSet::new();
        let mut heap = BinaryHeap::new();
        heap.push(Queued { key: 0.0, cell: first });
        seen.insert(first);
        while let Some(Queued { key, cell }) = heap.pop() {
            out.push((cell, key));
            if out.len() >= budget {
                break;
            }
            for f in FACES {
                let n = [cell[0] + f[0], cell[1] + f[1], cell[2] + f[2]];
                // a smoke never stands much taller than on open ground, so it does not
                // climb a wall to spill over into the street behind
                let rise = (n[1] - first[1]) as f32 * k;
                if rise > up * 1.3 || rise < -down * 2.0 {
                    continue;
                }
                if !self.inside(n) || self.solid(n) || !seen.insert(n) {
                    continue;
                }
                let d = distance(n);
                if d <= limit {
                    heap.push(Queued { key: d, cell: n });
                }
            }
        }
        out
    }

    /// Can a player stand in this open cell: is the one under it solid?
    fn standing(&self, c: Cell) -> bool {
        self.inside(c) && !self.solid(c) && self.solid([c[0], c[1] - 1, c[2]])
    }

    /// The floor a molotov at `start` burns on: standing cells reached across the floor,
    /// up or down a step at a time, nearest first, as many as a disc of `radius` holds.
    /// Each comes as (cell, horizontal distance, height of the floor in it).
    pub fn flood_floor(&self, start: Vec3, radius: f32) -> Vec<(Cell, f32, f32)> {
        let Some(mut first) = self.open_near(start) else { return Vec::new() };
        for _ in 0..6 {
            if self.standing(first) {
                break;
            }
            first[1] -= 1;
        }
        if !self.standing(first) {
            return Vec::new();
        }
        let k = self.cell;
        let distance = |c: Cell| {
            let (dx, dz) = ((c[0] - first[0]) as f32 * k, (c[2] - first[2]) as f32 * k);
            (dx * dx + dz * dz).sqrt()
        };
        let budget = (std::f32::consts::PI * (radius / k).powi(2)).round().max(1.0) as usize;
        let limit = radius * 2.0;
        // a floor at the grenade's own level is where it lies; elsewhere, the top of the cell below
        let floor = |c: Cell| {
            if c[1] == first[1] {
                start.y
            } else {
                self.origin.y + c[1] as f32 * k
            }
        };
        let mut out = Vec::with_capacity(budget);
        let mut seen = HashSet::new();
        let mut heap = BinaryHeap::new();
        heap.push(Queued { key: 0.0, cell: first });
        seen.insert(first);
        while let Some(Queued { key, cell }) = heap.pop() {
            out.push((cell, key, floor(cell)));
            if out.len() >= budget {
                break;
            }
            for (dx, dz) in [(1, 0), (-1, 0), (0, 1), (0, -1)] {
                for dy in [0, 1, -1] {
                    let n = [cell[0] + dx, cell[1] + dy, cell[2] + dz];
                    // a step up needs head room over the current cell
                    if dy == 1 && self.solid([cell[0], cell[1] + 1, cell[2]]) {
                        continue;
                    }
                    if !self.standing(n) || seen.contains(&n) {
                        continue;
                    }
                    let d = distance(n);
                    if d <= limit {
                        seen.insert(n);
                        heap.push(Queued { key: d, cell: n });
                    }
                    break;
                }
            }
        }
        out
    }

    /// How far along `dir` (a unit vector) from `from` the first solid cell is, ignoring
    /// any in the first `skip` units: a player's eye can share a cell with the wall beside it.
    pub fn ray(&self, from: Vec3, dir: Vec3, max: f32, skip: f32) -> Option<f32> {
        let k = self.cell;
        let mut c = self.cell_of(from);
        let step = [dir.x, dir.y, dir.z].map(|d| if d > 0.0 { 1 } else if d < 0.0 { -1 } else { 0 });
        let p = (from - self.origin) / k;
        let mut next = [0.0f32; 3];
        let mut delta = [f32::INFINITY; 3];
        for a in 0..3 {
            let d = dir[a];
            if d != 0.0 {
                let edge = if d > 0.0 { p[a].floor() + 1.0 } else { p[a].floor() };
                next[a] = (edge - p[a]) * k / d;
                delta[a] = k / d.abs();
            } else {
                next[a] = f32::INFINITY;
            }
        }
        let mut t = 0.0;
        while t <= max {
            if t >= skip && self.solid(c) {
                return Some(t);
            }
            let a = if next[0] < next[1] && next[0] < next[2] {
                0
            } else if next[1] < next[2] {
                1
            } else {
                2
            };
            t = next[a];
            next[a] += delta[a];
            c[a] += step[a];
        }
        None
    }
}

#[derive(Asset, TypePath)]
pub struct SpaceAsset(pub SpaceGrid);

#[derive(Default, TypePath)]
pub struct SpaceLoader;

impl AssetLoader for SpaceLoader {
    type Asset = SpaceAsset;
    type Settings = ();
    type Error = std::io::Error;

    async fn load(
        &self,
        reader: &mut dyn Reader,
        _settings: &(),
        _load_context: &mut LoadContext<'_>,
    ) -> Result<SpaceAsset, Self::Error> {
        let mut bytes = Vec::new();
        reader.read_to_end(&mut bytes).await?;
        SpaceGrid::parse(bytes)
            .map(SpaceAsset)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
    }

    fn extensions(&self) -> &[&str] {
        &["c2space"]
    }
}

/// The current map's grid, while it loads and after. None: no grid for this map, and
/// smoke is a plain dome.
#[derive(Resource, Default)]
pub struct MapSpace(pub Option<Handle<SpaceAsset>>);

impl MapSpace {
    pub fn get<'a>(&self, assets: &'a Assets<SpaceAsset>) -> Option<&'a SpaceGrid> {
        self.0.as_ref().and_then(|h| assets.get(h)).map(|a| &a.0)
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A grid of `dims` cells of 16 with these cells solid, as prepare_space.py writes it.
    pub(crate) fn grid(dims: [u32; 3], solid: impl Fn(i32, i32, i32) -> bool) -> SpaceGrid {
        let count = (dims[0] * dims[1] * dims[2]) as usize;
        let mut bits = vec![0u8; count.div_ceil(8)];
        for x in 0..dims[0] as i32 {
            for y in 0..dims[1] as i32 {
                for z in 0..dims[2] as i32 {
                    if solid(x, y, z) {
                        let i = ((x * dims[1] as i32 + y) * dims[2] as i32 + z) as usize;
                        bits[i / 8] |= 1 << (i % 8);
                    }
                }
            }
        }
        let mut bytes = MAGIC.to_vec();
        bytes.extend(VERSION.to_le_bytes());
        for v in [0.0f32, 0.0, 0.0, 16.0] {
            bytes.extend(v.to_le_bytes());
        }
        for d in dims {
            bytes.extend(d.to_le_bytes());
        }
        bytes.extend(bits);
        SpaceGrid::parse(bytes).unwrap()
    }

    #[test]
    fn reads_what_prepare_space_writes() {
        let g = grid([4, 3, 2], |x, y, z| (x, y, z) == (1, 2, 1));
        assert!(g.solid([1, 2, 1]));
        assert!(!g.solid([1, 2, 0]));
        assert!(!g.solid([9, 9, 9]), "outside is open");
        assert_eq!(g.cell_of(Vec3::new(17.0, 40.0, 1.0)), [1, 2, 0]);
        assert_eq!(g.centre([1, 2, 0]), Vec3::new(24.0, 40.0, 8.0));
    }

    #[test]
    fn a_broken_file_is_refused() {
        assert!(SpaceGrid::parse(b"C2SP".to_vec()).is_err());
        let mut bytes = MAGIC.to_vec();
        bytes.extend(2u32.to_le_bytes());
        bytes.extend([0u8; 28]);
        assert!(SpaceGrid::parse(bytes).is_err(), "another version");
    }

    /// A floor at y = 0 over a 60 x 60 field, with walls round a corridor 3 cells wide.
    fn field_and_corridor() -> SpaceGrid {
        grid([60, 20, 60], |x, y, z| {
            y == 0 || ((2..58).contains(&x) && z >= 30 && (z == 30 || z == 34) && y < 15)
        })
    }

    #[test]
    fn smoke_on_open_ground_is_a_dome_of_its_radius() {
        let g = grid([60, 20, 60], |_, y, _| y == 0);
        let start = Vec3::new(30.0 * 16.0, 16.0, 30.0 * 16.0);
        let cells = g.flood_smoke(start, 144.0, 112.0, 56.0);
        assert!(cells.len() > 400, "{}", cells.len());
        let far = cells.iter().map(|(_, d)| *d).fold(0.0, f32::max);
        assert!((120.0..=170.0).contains(&far), "reaches {far}");
        assert!(cells.iter().all(|(c, _)| c[1] >= 1), "never into the floor");
        assert!(cells.windows(2).all(|w| w[0].1 <= w[1].1), "nearest first");
    }

    #[test]
    fn smoke_in_a_corridor_runs_further_and_stays_inside() {
        let g = field_and_corridor();
        // in the corridor between z = 30 and z = 34
        let start = Vec3::new(30.0 * 16.0, 16.0, 32.0 * 16.0 + 8.0);
        let cells = g.flood_smoke(start, 144.0, 112.0, 56.0);
        let far = cells.iter().map(|(_, d)| *d).fold(0.0, f32::max);
        assert!(far > 250.0, "a corridor takes it further: {far}");
        assert!(cells.iter().all(|(c, _)| c[2] > 30 && c[2] < 34 || c[1] >= 15), "through no wall");
    }

    #[test]
    fn fire_spreads_over_the_floor_and_down_a_step() {
        // floor at y = 0 for x < 30, a step down to y = -1... modelled as floor at y = 2 for x < 30
        let g = grid([60, 10, 60], |x, y, _| if x < 30 { y == 2 } else { y == 1 });
        let start = Vec3::new(28.0 * 16.0, 3.0 * 16.0 + 1.0, 30.0 * 16.0);
        let floor = g.flood_floor(start, 150.0);
        assert!(floor.len() > 200, "{}", floor.len());
        assert!(floor.iter().any(|(c, _, _)| c[1] == 2), "down the step");
        assert!(floor.iter().all(|(c, _, _)| g.standing(*c)));
        let own = floor.iter().find(|(c, _, _)| c[1] == 3).unwrap();
        assert_eq!(own.2, start.y, "the grenade's own floor is where it lies");
        let lower = floor.iter().find(|(c, _, _)| c[1] == 2).unwrap();
        assert_eq!(lower.2, 32.0, "a lower one is the top of the cell below");
    }

    #[test]
    fn fire_does_not_climb_a_wall() {
        let g = grid([40, 10, 40], |x, y, _| y == 0 || (x == 20 && y < 8));
        let start = Vec3::new(18.0 * 16.0, 17.0, 20.0 * 16.0);
        let floor = g.flood_floor(start, 150.0);
        assert!(floor.iter().all(|(c, _, _)| c[0] < 20), "stays on its side");
    }

    #[test]
    fn a_ray_stops_at_the_wall_and_skips_its_own_cell() {
        let g = grid([40, 4, 4], |x, _, _| x == 30 || x == 0);
        let from = Vec3::new(8.0, 24.0, 24.0); // in cell x = 0, which is solid
        let hit = g.ray(from, Vec3::X, 1000.0, 24.0).unwrap();
        assert!((hit - 472.0).abs() < 0.01, "{hit}");
        assert!(g.ray(from, Vec3::X, 400.0, 24.0).is_none(), "out of reach");
        let back = g.ray(Vec3::new(600.0, 24.0, 24.0), -Vec3::X, 1000.0, 0.0).unwrap();
        assert!((back - (600.0 - 496.0)).abs() < 0.01, "{back}");
    }
}
