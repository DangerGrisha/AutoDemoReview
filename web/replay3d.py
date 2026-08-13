"""3D replay artifacts: extract3d invocation + per-match artifact storage.

The Rust CLI (rust/extract) re-parses the uploaded .dem and writes per-round
C2R3 blobs (rNN.3dr) plus manifest.json. Everything here is best-effort: any
failure just means the match has no 3D replay — the 2D report is unaffected.
Artifacts live in web/data/replay3d/<match_id>/ and existence of manifest.json
is the single source of truth (no DB column).
"""

import logging
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
