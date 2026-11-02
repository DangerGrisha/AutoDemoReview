//! Which CS2 animation clip a player should be in right now. Pure logic, so the
//! decisions are unit-tested without a renderer; lib.rs::drive_player_anim feeds it
//! from the replay tracks and applies the answer to the Bevy `AnimationPlayer`.

pub const SECTORS: usize = 8;
/// Clockwise from where the player faces: `n` is forward, `e` is a strafe right.
const SECTOR_NAMES: [&str; SECTORS] = ["n", "ne", "e", "se", "s", "sw", "w", "nw"];
const SECTOR_WIDTH: f64 = std::f64::consts::TAU / SECTORS as f64;
/// Extra angle past a sector's edge before switching, so a heading right on the
/// boundary does not flip clips every frame.
const SECTOR_MARGIN: f64 = 10.0 * std::f64::consts::PI / 180.0;

/// Ground speed bands in Source units/s, with a gap so the sampled tracks' jitter
/// around a threshold does not flicker between clips. Shift-walking tops out near
/// 130 and running starts well above 200.
const WALK_ENTER: f64 = 25.0;
const WALK_EXIT: f64 = 15.0;
const RUN_ENTER: f64 = 150.0;
const RUN_EXIT: f64 = 135.0;

/// The ground speed each clip depicts, in Source units/s, which is how fast a planted
/// foot moves relative to the body. The clips carry no root motion and report a velocity
/// of 0, so this was measured from their foot bones, not read from the files. Running is
/// 142-178 depending on direction, walking 95-108, crouching 53-88.
const NOMINAL_RUN: f64 = 170.0;
const NOMINAL_WALK: f64 = 105.0;
const NOMINAL_CROUCH: f64 = 70.0;
/// Past this range a clip looks wrong (slow-motion legs, or a blur) and foot
/// sliding is the lesser evil.
const MIN_CLIP_RATE: f64 = 0.5;
const MAX_CLIP_RATE: f64 = 1.5;

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum WeaponClass {
    Rifle,
    Pistol,
    /// Knife and everything else held in a free hand (C4, grenades).
    Knife,
}

impl WeaponClass {
    pub const ALL: [WeaponClass; 3] = [WeaponClass::Rifle, WeaponClass::Pistol, WeaponClass::Knife];

    fn suffix(self) -> &'static str {
        match self {
            WeaponClass::Rifle => "rifle",
            WeaponClass::Pistol => "pistol",
            WeaponClass::Knife => "knife",
        }
    }
}

/// Clip set for a weaponTable tag. Unknown tags get the rifle stance, the most
/// common and the least wrong-looking if a new gun appears.
pub fn class_for_tag(tag: &str) -> WeaponClass {
    match tag {
        // "ZEUS X" is what `weapon_tag` in rust/extract makes of "Zeus x27".
        "GLOCK" | "USP" | "P2000" | "P250" | "FIVE7" | "TEC9" | "CZ" | "DUALS" | "DEAG" | "R8" | "ZEUS X" => {
            WeaponClass::Pistol
        }
        "🔪" | "💣" | "HE" | "FLASH" | "SMOKE" | "MOLLY" | "INC" | "DECOY" => WeaponClass::Knife,
        _ => WeaponClass::Rifle,
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, Default)]
pub enum Gait {
    #[default]
    Still,
    Walk,
    Run,
}

/// One player's locomotion state. It is kept between frames because both the gait
/// and the direction sector have hysteresis.
#[derive(Clone, Copy, Debug, Default)]
pub struct Locomotion {
    pub gait: Gait,
    pub sector: usize,
}

impl Locomotion {
    /// `heading_cw`: movement direction in radians, clockwise from the facing.
    pub fn update(&mut self, speed: f64, heading_cw: f64) {
        self.gait = match self.gait {
            Gait::Still if speed >= RUN_ENTER => Gait::Run,
            Gait::Still if speed >= WALK_ENTER => Gait::Walk,
            Gait::Walk if speed >= RUN_ENTER => Gait::Run,
            Gait::Walk if speed < WALK_EXIT => Gait::Still,
            Gait::Run if speed < WALK_EXIT => Gait::Still,
            Gait::Run if speed < RUN_EXIT => Gait::Walk,
            keep => keep,
        };
        if self.gait == Gait::Still {
            return; // a standing player has no meaningful heading: keep the last one
        }
        let center = self.sector as f64 * SECTOR_WIDTH;
        let off = wrap_pi(heading_cw - center).abs();
        if off > SECTOR_WIDTH / 2.0 + SECTOR_MARGIN {
            self.sector = sector_of(heading_cw);
        }
    }
}

fn wrap_pi(a: f64) -> f64 {
    let t = a.rem_euclid(std::f64::consts::TAU);
    if t > std::f64::consts::PI {
        t - std::f64::consts::TAU
    } else {
        t
    }
}

fn sector_of(heading_cw: f64) -> usize {
    (heading_cw.rem_euclid(std::f64::consts::TAU) / SECTOR_WIDTH).round() as usize % SECTORS
}

/// Movement heading from a Source-world velocity and yaw in degrees (Source yaw and
/// angles grow counter-clockwise seen from above; the sectors run clockwise).
pub fn heading_cw(dx: f64, dy: f64, yaw_deg: f64) -> f64 {
    yaw_deg.to_radians() - dy.atan2(dx)
}

/// How fast to play the clip so the feet roughly match the ground speed.
pub fn clip_rate(crouch: bool, gait: Gait, speed: f64) -> f32 {
    let nominal = match gait {
        Gait::Still => return 1.0,
        _ if crouch => NOMINAL_CROUCH,
        Gait::Walk => NOMINAL_WALK,
        Gait::Run => NOMINAL_RUN,
    };
    (speed / nominal).clamp(MIN_CLIP_RATE, MAX_CLIP_RATE) as f32
}

/// The clip name for a pose, exactly as in player_clips.txt. Crouching has a single
/// moving gait in CS2, so Walk and Run both land on the `crouch_*` clips.
pub fn clip_name(class: WeaponClass, crouch: bool, gait: Gait, sector: usize) -> String {
    let weapon = class.suffix();
    let dir = SECTOR_NAMES[sector % SECTORS];
    match (crouch, gait) {
        (false, Gait::Still) => format!("idle_{weapon}"),
        (true, Gait::Still) => format!("idle_crouch_{weapon}"),
        (true, _) => format!("crouch_{dir}_{weapon}"),
        (false, Gait::Walk) => format!("walk_{dir}_{weapon}"),
        (false, Gait::Run) => format!("run_{dir}_{weapon}"),
    }
}

/// How far either way `ground_motion` looks, in seconds.
const GROUND_MOTION_REACH_S: f64 = 0.125;

/// Ground velocity around sample `i`: (speed in Source units/s, dx, dy). Measured over a
/// stretch of time because a single step between samples is too noisy to steer clips
/// with, and in seconds because replays come at 16 and at 64 samples a second.
pub fn ground_motion(tr: &c2r3::Track, i: usize, n: usize, rate: f64) -> (f64, f64, f64) {
    let reach = ((GROUND_MOTION_REACH_S * rate).round() as usize).max(1);
    let (lo, hi) = (i.saturating_sub(reach), (i + reach).min(n - 1));
    if hi == lo {
        return (0.0, 0.0, 0.0);
    }
    let (dx, dy) = (tr.x[hi] - tr.x[lo], tr.y[hi] - tr.y[lo]);
    let dt = (hi - lo) as f64 / rate;
    ((dx * dx + dy * dy).sqrt() / dt, dx, dy)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Running along x at `speed` units/s, sampled `rate` times a second for 2 s.
    fn running(speed: f64, rate: f64) -> c2r3::Track {
        let n = (2.0 * rate) as usize;
        c2r3::Track {
            x: (0..n).map(|i| i as f64 / rate * speed).collect(),
            y: vec![0.0; n],
            ..Default::default()
        }
    }

    #[test]
    fn speed_reads_the_same_at_any_sample_rate() {
        for rate in [16.0, 64.0] {
            let tr = running(250.0, rate);
            let n = tr.x.len();
            let (speed, dx, dy) = ground_motion(&tr, n / 2, n, rate);
            assert!((speed - 250.0).abs() < 1e-6, "{rate} Hz: {speed}");
            assert!(dx > 0.0 && dy == 0.0);
        }
    }

    #[test]
    fn the_window_is_the_same_length_in_seconds_at_any_rate() {
        // one bad sample next to the point in question moves the reading no more at 64 Hz
        // than one does at 16 Hz, per second of data: the window holds 4x the samples
        let rate = 64.0;
        let mut tr = running(250.0, rate);
        let n = tr.x.len();
        tr.x[n / 2 + 1] += 40.0;
        let (speed, _, _) = ground_motion(&tr, n / 2, n, rate);
        assert!((speed - 250.0).abs() < 1e-6, "a jump inside the window does not count: {speed}");
    }

    #[test]
    fn the_ends_of_a_round_do_not_run_off_the_track() {
        let tr = running(250.0, 64.0);
        let n = tr.x.len();
        assert!((ground_motion(&tr, 0, n, 64.0).0 - 250.0).abs() < 1e-6);
        assert!((ground_motion(&tr, n - 1, n, 64.0).0 - 250.0).abs() < 1e-6);
        let one = c2r3::Track { x: vec![5.0], y: vec![5.0], ..Default::default() };
        assert_eq!(ground_motion(&one, 0, 1, 64.0).0, 0.0);
    }
    use std::collections::HashSet;
    use std::f64::consts::{FRAC_PI_2, PI};

    /// What tools/prepare_character.py keeps in the .glb. The viewer finds clips by
    /// name, so all that matters is that this list and `clip_name` agree.
    fn clip_list() -> impl Iterator<Item = &'static str> {
        include_str!("player_clips.txt").lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#'))
    }

    #[test]
    fn every_pose_has_a_clip_and_every_clip_a_pose() {
        let listed: HashSet<&str> = clip_list().collect();
        let mut named = HashSet::new();
        for class in WeaponClass::ALL {
            for crouch in [false, true] {
                for gait in [Gait::Still, Gait::Walk, Gait::Run] {
                    for sector in 0..SECTORS {
                        let n = clip_name(class, crouch, gait, sector);
                        assert!(listed.contains(n.as_str()), "{n} is not in player_clips.txt");
                        named.insert(n);
                    }
                }
            }
        }
        assert_eq!(named.len(), listed.len(), "player_clips.txt holds clips nothing plays");
        assert_eq!(clip_list().count(), 78);
    }

    #[test]
    fn heading_is_clockwise_from_the_facing() {
        // facing +X (yaw 0): moving +X is forward, moving -Y is to the player's right
        assert!(wrap_pi(heading_cw(1.0, 0.0, 0.0)).abs() < 1e-9);
        assert!((wrap_pi(heading_cw(0.0, -1.0, 0.0)) - FRAC_PI_2).abs() < 1e-9);
        assert_eq!(sector_of(heading_cw(0.0, -1.0, 0.0)), 2, "strafe right is `e`");
        assert_eq!(sector_of(heading_cw(-1.0, 0.0, 0.0)), 4, "backwards is `s`");
        // facing +Y (yaw 90): moving +Y is forward
        assert_eq!(sector_of(heading_cw(0.0, 1.0, 90.0)), 0);
    }

    #[test]
    fn sector_holds_near_a_boundary() {
        let mut l = Locomotion { gait: Gait::Run, sector: 0 };
        // 22.5 degrees is the n/ne edge: a little past it must not switch yet
        l.update(250.0, (22.5f64 + 5.0).to_radians());
        assert_eq!(l.sector, 0);
        l.update(250.0, (22.5f64 + 15.0).to_radians());
        assert_eq!(l.sector, 1);
        // and coming back across the edge stays in `ne` until clearly in `n`
        l.update(250.0, (22.5f64 - 5.0).to_radians());
        assert_eq!(l.sector, 1);
        l.update(250.0, 0.0);
        assert_eq!(l.sector, 0);
        // wrap-around: -10 degrees is still `n`
        l.update(250.0, (-10.0f64).to_radians());
        assert_eq!(l.sector, 0);
        l.update(250.0, PI);
        assert_eq!(l.sector, 4);
    }

    #[test]
    fn gait_has_hysteresis() {
        let mut l = Locomotion::default();
        l.update(20.0, 0.0);
        assert_eq!(l.gait, Gait::Still, "below walk entry");
        l.update(30.0, 0.0);
        assert_eq!(l.gait, Gait::Walk);
        l.update(20.0, 0.0);
        assert_eq!(l.gait, Gait::Walk, "above walk exit");
        l.update(160.0, 0.0);
        assert_eq!(l.gait, Gait::Run);
        l.update(140.0, 0.0);
        assert_eq!(l.gait, Gait::Run, "above run exit");
        l.update(100.0, 0.0);
        assert_eq!(l.gait, Gait::Walk);
        l.update(5.0, 0.0);
        assert_eq!(l.gait, Gait::Still);
    }

    #[test]
    fn standing_keeps_the_last_heading() {
        let mut l = Locomotion { gait: Gait::Run, sector: 2 };
        l.update(0.0, 0.0);
        assert_eq!((l.gait, l.sector), (Gait::Still, 2));
    }

    #[test]
    fn crouch_replaces_walk_and_run() {
        for gait in [Gait::Walk, Gait::Run] {
            assert_eq!(clip_name(WeaponClass::Rifle, true, gait, 0), "crouch_n_rifle");
        }
        assert_eq!(clip_name(WeaponClass::Knife, true, Gait::Still, 3), "idle_crouch_knife");
        assert_eq!(clip_name(WeaponClass::Pistol, false, Gait::Run, 7), "run_nw_pistol");
    }

    #[test]
    fn weapons_map_to_clip_sets() {
        assert_eq!(class_for_tag("AK"), WeaponClass::Rifle);
        assert_eq!(class_for_tag("AWP"), WeaponClass::Rifle);
        assert_eq!(class_for_tag("NEGEV"), WeaponClass::Rifle);
        assert_eq!(class_for_tag("MP9"), WeaponClass::Rifle);
        assert_eq!(class_for_tag("DEAG"), WeaponClass::Pistol);
        assert_eq!(class_for_tag("ZEUS X"), WeaponClass::Pistol);
        assert_eq!(class_for_tag("🔪"), WeaponClass::Knife);
        assert_eq!(class_for_tag("💣"), WeaponClass::Knife);
        assert_eq!(class_for_tag("SMOKE"), WeaponClass::Knife);
        assert_eq!(class_for_tag("something new"), WeaponClass::Rifle);
    }

    #[test]
    fn clip_rate_follows_speed_within_limits() {
        assert!((clip_rate(false, Gait::Run, NOMINAL_RUN) - 1.0).abs() < 1e-6);
        assert!((clip_rate(false, Gait::Walk, NOMINAL_WALK * 0.75) - 0.75).abs() < 1e-6);
        assert!((clip_rate(true, Gait::Walk, NOMINAL_CROUCH) - 1.0).abs() < 1e-6);
        assert_eq!(clip_rate(false, Gait::Walk, 1.0), MIN_CLIP_RATE as f32);
        assert_eq!(clip_rate(false, Gait::Still, 0.0), 1.0);
        assert_eq!(clip_rate(false, Gait::Run, 2000.0), 1.5);
    }
}
