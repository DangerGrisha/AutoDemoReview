"""3D replay artifacts: extract3d invocation + per-match artifact storage.

The Rust CLI (rust/extract) re-parses the uploaded .dem and writes per-round
C2R3 blobs (rNN.3dr) plus manifest.json. Everything here is best-effort: any
failure just means the match has no 3D replay — the 2D report is unaffected.
Artifacts live in web/data/replay3d/<match_id>/ and existence of manifest.json
is the single source of truth (no DB column).
"""

import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import brotli

from . import config

log = logging.getLogger("web.replay3d")

# Only extract3d's own outputs may be served; anything else in the directory
# (e.g. the assets/ radar copy) stays private. Also blocks path traversal.
ARTIFACT_RE = re.compile(r"^(manifest\.json|r\d{2}\.3dr)$")

# What a replay should hold, kept equal to DATA_VERSION in rust/extract/src/lib.rs (a test
# checks). Older replays get replaced on a re-upload and by tools/rebuild_replays.py.
DATA_VERSION = 2

# A replay being built, next to the final directories. ARTIFACT_RE and the numeric match id
# keep it unreachable.
BUILD_PREFIX = ".build-"

# A round is compressed when first asked for, while someone waits: 33 ms for 148 KB at 9
# against 436 ms for 131 KB at 10, on a 446 KB round.
BROTLI_QUALITY = 9
COMPRESSIONS = {
    "br": (".br", lambda data: brotli.compress(data, quality=BROTLI_QUALITY)),
    "gzip": (".gz", lambda data: gzip.compress(data, 6)),
}

# Map worlds converted via Source2Viewer + tools/prepare_map.py, under the
# viewer's asset root (everything Bevy loads must live below that root).
MAPS_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "maps"
# The textured maps from tools/prepare_map_textured.py, built from a local CS2 install
# and never committed, unlike the plain ones above.
MAPS_V2_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "maps_v2"
CS2_MODELS_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "models" / "cs2_v1"
# The page's assets are cached for a week by URL, so a rebuild that changes the bone tree
# gets new names. tools/prepare_character.py and rust/viewer/src/scene.rs hold the same.
CS2_MODEL_FILES = ("t_rig2.glb", "ct_rig2.glb")
CS2_SOUNDS_INDEX = config.WEB_DIR / "static" / "replay3d" / "assets" / "sounds_v1" / "index.json"
HUD_ICONS_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "hud_v1"


def artifacts_dir(match_id):
    return config.REPLAY3D_DIR / str(int(match_id))


def has_replay(match_id):
    """True when extract3d artifacts exist for this match."""
    return (artifacts_dir(match_id) / "manifest.json").is_file()


def data_version(match_id):
    """What this match's replay holds, or None without a readable one. A manifest from
    before there were versions is 1."""
    try:
        manifest = json.loads((artifacts_dir(match_id) / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return manifest.get("dataVersion", 1) if isinstance(manifest, dict) else None


def is_stale(match_id):
    """True when extracting again would give this match more than it has."""
    version = data_version(match_id)
    return version is None or version < DATA_VERSION


def artifact_path(match_id, fname):
    """Whitelisted artifact path, or None (unknown name / missing file)."""
    if not ARTIFACT_RE.match(fname):
        return None
    path = artifacts_dir(match_id) / fname
    return path if path.is_file() else None


def compressed_artifact(path, accepted):
    """`(compressed copy, encoding)` of an artifact for a client accepting these encodings,
    or None to send it as it is. The copy sits next to the file and is made on first
    request, so old replays gain from this too, and a rebuilt replay starts without any.
    """
    for encoding, (suffix, compress) in COMPRESSIONS.items():
        if encoding not in accepted:
            continue
        copy = path.with_name(path.name + suffix)
        tmp_name = None
        try:
            if not copy.is_file() or copy.stat().st_mtime < path.stat().st_mtime:
                fd, tmp_name = tempfile.mkstemp(suffix=".tmp", dir=str(path.parent))
                with os.fdopen(fd, "wb") as out:
                    out.write(compress(path.read_bytes()))
                os.replace(tmp_name, copy)
            return copy, encoding
        except Exception as exc:  # noqa: BLE001 -- the raw file still works
            log.warning("could not compress %s: %s", path, exc)
            if tmp_name is not None:
                Path(tmp_name).unlink(missing_ok=True)
            return None
    return None


def cs2_models_enabled():
    """Show the real CS2 player models instead of the fallback soldier?

    They are built from a local CS2 install and never committed, so a fresh clone
    simply lacks them, and the viewer shows no players at all if told to load a
    missing file. REPLAY3D_CS2_MODELS=0 turns them off without deleting anything.
    """
    if os.environ.get("REPLAY3D_CS2_MODELS", "1") == "0":
        return False
    return all((CS2_MODELS_DIR / name).is_file() for name in CS2_MODEL_FILES)


def cs2_weapons_enabled():
    """Show the real CS2 weapons? They go on the real models' weapon bone, so they need
    those too, and REPLAY3D_CS2_MODELS=0 turns both off. A file missing from the set is
    not worth checking for: the viewer shows a simple weapon for whichever fails to load.
    """
    return cs2_models_enabled() and any((CS2_MODELS_DIR / "weapons").glob("*.glb"))


def cs2_viewmodel_enabled():
    """Show the real arms and weapon in first person? Each side's arms must be there. A
    weapon's rig missing from the set is not worth checking for: whoever holds it keeps
    the still weapon.
    """
    return cs2_weapons_enabled() and all(
        (CS2_MODELS_DIR / "viewmodel" / name).is_file() for name in ("arms_t.glb", "arms_ct.glb"))


def cs2_sounds_enabled():
    """Play the real CS2 sounds? Built from a local CS2 install and never committed, like
    the models. Without them, or with REPLAY3D_CS2_SOUNDS=0, the page synthesizes gunfire.
    """
    return os.environ.get("REPLAY3D_CS2_SOUNDS", "1") != "0" and CS2_SOUNDS_INDEX.is_file()


def hud_icons_enabled():
    """Show CS2's weapon icons in the kill feed and the player list instead of short
    text labels? Also built from a local CS2 install. A tag without an icon stays text.
    """
    return any(HUD_ICONS_DIR.glob("*.svg"))


def map_asset_rel(map_name):
    """Asset-root-relative path of the converted map world, or None.

    The page injects this into the manifest as `mapAsset`; the viewer falls
    back to the radar greybox when it is absent.
    """
    if not map_name:
        return None
    for fname in ("world.glb", "world.gltf"):
        if (MAPS_DIR / map_name / fname).is_file():
            return f"maps/{map_name}/{fname}"
    return None


# Textured maps fit to show by default: within 120 MB and 60 FPS, checked on a real match.
# The others build too, but weigh up to 409 MB or have no match to check them on (the table
# in docs/REPLAY3D.md), so they stay on the plain map unless asked for with
# REPLAY3D_CS2_MAPS=all.
TEXTURED_MAPS_READY = {"de_mirage", "de_nuke"}


def map_v2_rel(map_name):
    """Asset-root-relative path of the textured map, or None. The page falls back to the
    plain map without it, or with REPLAY3D_CS2_MAPS=0.
    """
    flag = os.environ.get("REPLAY3D_CS2_MAPS", "1")
    if not map_name or flag == "0":
        return None
    if flag != "all" and map_name not in TEXTURED_MAPS_READY:
        return None
    if (MAPS_V2_DIR / map_name / "world.c2map").is_file():
        return f"maps_v2/{map_name}/world.c2map"
    return None


def _sweep_old_builds():
    """Remove what a killed server left behind. Older than any extract may run, so never
    the build of a running one."""
    cutoff = time.time() - 2 * config.EXTRACT3D_TIMEOUT_S
    for path in config.REPLAY3D_DIR.glob(f"{BUILD_PREFIX}*"):
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _swap_in(new_dir, out_dir):
    """Make `new_dir` the replay. Raises with the old one still in place when that fails,
    as it does on Windows while a file of the old one is being sent."""
    old_dir = None
    if out_dir.exists():
        old_dir = new_dir.with_name(new_dir.name + ".old")
        os.replace(out_dir, old_dir)
    try:
        os.replace(new_dir, out_dir)
    except OSError:
        if old_dir is not None:
            os.replace(old_dir, out_dir)
        raise
    if old_dir is not None:
        shutil.rmtree(old_dir, ignore_errors=True)


def extract(dem_path, match_id):
    """Run extract3d on `dem_path` and put the result in this match's artifact dir.

    Returns True when a new replay is in place. Never raises: logs and cleans up on any
    failure (missing binary, unsupported map, timeout, crash). The output is built aside
    and moved in whole, so a failed rebuild leaves an older replay as it was.
    """
    bin_path = Path(config.EXTRACT3D_BIN)
    if not bin_path.is_file():
        log.info("extract3d binary not found at %s; skipping 3D replay", bin_path)
        return False

    new_dir = None
    try:
        _sweep_old_builds()
        new_dir = Path(tempfile.mkdtemp(prefix=f"{BUILD_PREFIX}{int(match_id)}-",
                                        dir=str(config.REPLAY3D_DIR)))
        # cwd=repo root lets the CLI's best-effort radar copy from
        # src/demoreview/assets/ resolve; harmless when it doesn't.
        proc = subprocess.run(
            [str(bin_path), str(dem_path), "--out-dir", str(new_dir)],
            cwd=str(config.REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=config.EXTRACT3D_TIMEOUT_S,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            log.warning("extract3d failed for match %s (rc=%s): %s",
                        match_id, proc.returncode, tail)
            return False
        if not (new_dir / "manifest.json").is_file():
            log.warning("extract3d wrote no manifest for match %s", match_id)
            return False
        _swap_in(new_dir, artifacts_dir(match_id))
        log.info("extract3d ok for match %s", match_id)
        return True
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        log.warning("extract3d failed for match %s: %s", match_id, exc)
        return False
    finally:
        if new_dir is not None:
            shutil.rmtree(new_dir, ignore_errors=True)
