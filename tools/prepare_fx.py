"""Build the 3D replay's effect atlases from CS2's own particle textures.

    python tools/prepare_fx.py <raw export dir> <out dir>

`tools/convert_fx.ps1` exports the textures `tools/fx_sheets.txt` names with
Source2Viewer-CLI, which writes a sheet as one PNG per frame plus an `.mks` listing its
sequences, and then runs this. Each line becomes `<out dir>/<name>.ktx2`: the chosen
frames side by side, premultiplied by their alpha, BC3 with mips.

Premultiplied, because the viewer draws every effect with one blend, `one,
one-minus-source-alpha`: a sprite that should cover what is behind it keeps its alpha,
and one that should only add light, like fire or a muzzle flash, has it set to zero by
the viewer. That is one shader and one pipeline for every effect, and a mip of a
premultiplied picture has no dark fringe round its edges.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_map_textured as pmt  # noqa: E402

SHEET_LIST = Path(__file__).resolve().parent / "fx_sheets.txt"
# Must match COLUMNS in rust/viewer/src/fx.rs.
COLUMNS = 8


def read_sheet_list(path=SHEET_LIST):
    """[{name, textures, sequences, frames, cell}] in file order."""
    sheets = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 5:
            raise SystemExit(f"{path}:{number}: want 5 columns, got {len(parts)}")
        name, textures, sequences, frames, cell = parts
        sheets.append({
            "name": name,
            "textures": textures.split("+"),
            "sequences": None if sequences == "-" else [int(s) for s in sequences.split(",")],
            "frames": int(frames),
            "cell": int(cell),
        })
    return sheets


def _pow2(n):
    return 1 << max(0, int(n - 1).bit_length())


def layout(count):
    """(columns, rows) of an atlas holding `count` frames. Both sides are powers of two."""
    columns = min(COLUMNS, _pow2(count))
    return columns, _pow2(-(-count // columns))


def read_mks(path):
    """The sequences of a sheet, each a list of its frames' PNG files."""
    sequences = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("sequence"):
            sequences.append([])
        elif line.startswith("frame") and sequences:
            sequences[-1].append(Path(path).parent / line.split()[1])
    return sequences


def pick(frames, count):
    """`count` frames spread evenly over a sequence, keeping its first and last. A shorter
    sequence repeats some, so every sequence of a sheet lasts as many frames."""
    if not frames:
        raise SystemExit("a sequence has no frames")
    if count == 1:
        return [frames[0]]
    return [frames[round(i * (len(frames) - 1) / (count - 1))] for i in range(count)]


def sheet_frames(raw_dir, sheet):
    """The PNG files of every frame the atlas holds, in atlas order."""
    raw_dir = Path(raw_dir)
    if sheet["sequences"] is None:
        files = []
        for texture in sheet["textures"]:
            png = raw_dir / f"{texture}.png"
            if not png.exists():
                raise SystemExit(f"{png} is missing: was it exported?")
            files.append(png)
        return files
    if len(sheet["textures"]) != 1:
        raise SystemExit(f"{sheet['name']}: a sheet with sequences takes one texture")
    mks = raw_dir / f"{sheet['textures'][0]}.mks"
    if not mks.exists():
        raise SystemExit(f"{mks} is missing: was it exported?")
    sequences = read_mks(mks)
    files = []
    for s in sheet["sequences"]:
        if s >= len(sequences):
            raise SystemExit(f"{sheet['name']}: no sequence {s}, the sheet has {len(sequences)}")
        files += pick(sequences[s], sheet["frames"])
    return files


def premultiplied_frame(path, cell):
    """One frame stretched to the cell, as the engine stretches a frame over its quad,
    in linear light with its colour multiplied by its alpha."""
    with Image.open(path) as im:
        rgba = np.asarray(im.convert("RGBA").resize((cell, cell), Image.LANCZOS), np.float32) / 255.0
    rgb = pmt._srgb_to_linear(rgba[..., :3]) * rgba[..., 3:4]
    return np.dstack([rgb, rgba[..., 3]])


def build_atlas(raw_dir, sheet):
    """(linear premultiplied float atlas, frame count)."""
    files = sheet_frames(raw_dir, sheet)
    columns, rows = layout(len(files))
    cell = sheet["cell"]
    atlas = np.zeros((rows * cell, columns * cell, 4), np.float32)
    for i, f in enumerate(files):
        r, c = divmod(i, columns)
        atlas[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = premultiplied_frame(f, cell)
    return atlas, len(files)


def _to_bytes(linear):
    rgb = pmt._linear_to_srgb(np.clip(linear[..., :3], 0.0, 1.0))
    out = np.dstack([rgb, np.clip(linear[..., 3:4], 0.0, 1.0)])
    return (out * 255.0 + 0.5).astype(np.uint8)


def write_atlas(path, atlas):
    """BC3 sRGB with every mip down to one block high or wide."""
    levels = [pmt._encode_rgba8(_to_bytes(atlas), pmt.BC3_SRGB)]
    level = atlas
    while min(level.shape[:2]) >= 8:
        level = pmt._half(level)
        levels.append(pmt._encode_rgba8(_to_bytes(level), pmt.BC3_SRGB))
    h, w = atlas.shape[:2]
    pmt.write_ktx2(path, pmt.BC3_SRGB, w, h, levels)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("raw", help="Source2Viewer's export of the textures")
    ap.add_argument("out", help="where the .ktx2 files go")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for sheet in read_sheet_list():
        atlas, count = build_atlas(args.raw, sheet)
        target = out / f"{sheet['name']}.ktx2"
        write_atlas(target, atlas)
        h, w = atlas.shape[:2]
        print(f"{sheet['name']}: {count} frames, {w}x{h}, {target.stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
