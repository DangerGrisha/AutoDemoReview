"""3D replay artifacts: extract3d invocation + per-match artifact storage.

The Rust CLI (rust/extract) re-parses the uploaded .dem and writes per-round
C2R3 blobs (rNN.3dr) plus manifest.json. Everything here is best-effort: any
failure just means the match has no 3D replay — the 2D report is unaffected.
Artifacts live in web/data/replay3d/<match_id>/ and existence of manifest.json
is the single source of truth (no DB column).
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger("web.replay3d")

# Only extract3d's own outputs may be served; anything else in the directory
# (e.g. the assets/ radar copy) stays private. Also blocks path traversal.
ARTIFACT_RE = re.compile(r"^(manifest\.json|r\d{2}\.3dr)$")

# Map worlds converted via Source2Viewer + tools/prepare_map.py, under the
# viewer's asset root (everything Bevy loads must live below that root).
MAPS_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "maps"
# The textured maps from tools/prepare_map_textured.py, built from a local CS2 install
# and never committed, unlike the plain ones above.
MAPS_V2_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "maps_v2"
CS2_MODELS_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "models" / "cs2_v1"
CS2_SOUNDS_INDEX = config.WEB_DIR / "static" / "replay3d" / "assets" / "sounds_v1" / "index.json"
HUD_ICONS_DIR = config.WEB_DIR / "static" / "replay3d" / "assets" / "hud_v1"


def artifacts_dir(match_id):
    return config.REPLAY3D_DIR / str(int(match_id))


def has_replay(match_id):
    """True when extract3d artifacts exist for this match."""
    return (artifacts_dir(match_id) / "manifest.json").is_file()


def artifact_path(match_id, fname):
    """Whitelisted artifact path, or None (unknown name / missing file)."""
    if not ARTIFACT_RE.match(fname):
        return None
    path = artifacts_dir(match_id) / fname
    return path if path.is_file() else None


def cs2_models_enabled():
    """Show the real CS2 player models instead of the fallback soldier?

    They are built from a local CS2 install and never committed, so a fresh clone
    simply lacks them, and the viewer shows no players at all if told to load a
    missing file. REPLAY3D_CS2_MODELS=0 turns them off without deleting anything.
    """
    if os.environ.get("REPLAY3D_CS2_MODELS", "1") == "0":
        return False
    return all((CS2_MODELS_DIR / name).is_file() for name in ("t.glb", "ct.glb"))


def cs2_weapons_enabled():
    """Show the real CS2 weapons? They go on the real models' weapon bone, so they need
    those too, and REPLAY3D_CS2_MODELS=0 turns both off. A file missing from the set is
    not worth checking for: the viewer shows a simple weapon for whichever fails to load.
    """
    return cs2_models_enabled() and any((CS2_MODELS_DIR / "weapons").glob("*.glb"))


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


def extract(dem_path, match_id):
    """Run extract3d on `dem_path` into this match's artifact dir.

    Returns True when manifest.json landed. Never raises: logs and cleans up
    the partial output dir on any failure (missing binary, unsupported map,
    timeout, crash).
    """
    bin_path = Path(config.EXTRACT3D_BIN)
    if not bin_path.is_file():
        log.info("extract3d binary not found at %s; skipping 3D replay", bin_path)
        return False

    out_dir = artifacts_dir(match_id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # cwd=repo root lets the CLI's best-effort radar copy from
        # src/demoreview/assets/ resolve; harmless when it doesn't.
        proc = subprocess.run(
            [str(bin_path), str(dem_path), "--out-dir", str(out_dir)],
            cwd=str(config.REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=config.EXTRACT3D_TIMEOUT_S,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            log.warning("extract3d failed for match %s (rc=%s): %s",
                        match_id, proc.returncode, tail)
            raise RuntimeError("extract3d nonzero exit")
        if not has_replay(match_id):
            log.warning("extract3d wrote no manifest for match %s", match_id)
            raise RuntimeError("no manifest")
        log.info("extract3d ok for match %s -> %s", match_id, out_dir)
        return True
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        return False
