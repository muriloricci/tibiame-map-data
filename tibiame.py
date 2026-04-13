#!/usr/bin/env python3
"""
TibiaME automap -> PNG exporter
=================================

Two modes:

  1. Stitch (default) -- one PNG per floor, all sectors stitched at world coordinates.
         python tibiame.py
         python tibiame.py --scale 2 --out-dir maps

  2. Per-file         -- one 128x128 PNG per .map file, named by world coordinate + floor.
         python tibiame.py --per-file
         python tibiame.py --per-file --scale 4 --out-dir debug

Automap root defaults to %LOCALAPPDATA%\\TibiaME\\automap.

Stitch output  : floor-07-map.png, floor-08-map.png, ...
Per-file output: <out-dir>/floor-<ZZ>/<floor>_<worldX>_<worldY>.png
                 e.g.  debug/floor-07/7_31744_31744.png
"""

import zlib, struct, os, re, sys, argparse
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Pillow not found. Install with: pip install Pillow")
    sys.exit(1)


# ── Palette ───────────────────────────────────────────────────────────────────

PALETTE = {
    0:  (  0,   0,   0),   # #000000  black
    1:  (153, 153, 153),   # #999999  gray
    2:  (  0, 204,   0),   # #00CC00  green
    3:  (255, 204, 153),   # #FFCC99  beige
    4:  (153, 102,  51),   # #996633  brown
    5:  (255, 255,   0),   # #FFFF00  yellow
    6:  (102, 102, 102),   # #666666  dark gray
    7:  (  0, 102,   0),   # #006600  dark green
    8:  ( 51,   0, 204),   # #3300CC  blue
    9:  (102,  51,   0),   # #663300  dark brown
    10: (255,  51,   0),   # #FF3300  red
    11: (255, 255, 255),   # #FFFFFF  white
}

VOID_COLOR = PALETTE[0]


# ── Constants ─────────────────────────────────────────────────────────────────

SEGS_PER_FILE = 16
TILES_PER_SEG = 1024
SEG_GRID      = 4
SEG_SIZE      = 32
FILE_TILES    = 128
WORLD_STEP    = 128


# ── Default path ──────────────────────────────────────────────────────────────

def default_automap_path() -> str:
    local = os.environ.get('LOCALAPPDATA', '')
    if not local:
        local = str(Path.home() / 'AppData' / 'Local')
    return os.path.join(local, 'TibiaME', 'automap')


# ── Decoding ──────────────────────────────────────────────────────────────────

def decompress_map(path: str) -> bytes:
    raw = Path(path).read_bytes()
    comp_size = struct.unpack_from('<H', raw, 2)[0]
    return zlib.decompress(raw[6 : 6 + comp_size])


def split_segments(data: bytes, path: str = '') -> list:
    """
    Split decompressed data into exactly 16 segments.

    Segments are delimited by zero-byte runs of length >= 2, BUT only after
    the first 2 bytes of each segment have been consumed.  Those first 2 bytes
    are the uint16-LE non-zero-tile-count header; their high byte is often
    0x00, and if the following RLE byte is also 0x00 the naive scanner would
    see a false delimiter inside the header.

    The fix: track how many bytes have been consumed in the current segment
    (seg_byte).  A zero-run is only treated as a real delimiter once seg_byte
    >= 2, i.e. after the 2-byte header has been safely passed.

    This handles all three file layouts seen in practice:
      A) file starts with [0x00 0x00 ...] — leading delimiter, seg_start set
         after it, seg_byte resets to 0.
      B) file starts with [non-zero byte, 0x00, non-zero ...] — no leading
         delimiter, header high byte is 0x00 but is followed by a non-zero
         RLE byte so no false split would occur anyway.
      C) file starts with [non-zero, 0x00, 0x00, ...] — no leading delimiter,
         header high byte 0x00 followed immediately by 0x00 RLE byte; without
         the seg_byte guard this produces a false split at offset 1.
    """
    segs      = []
    i         = 0
    seg_start = None
    seg_byte  = 0    # bytes consumed in the current segment

    while i < len(data):
        if data[i] == 0x00:
            j = i
            while j < len(data) and data[j] == 0x00:
                j += 1
            run = j - i

            if run >= 2 and seg_byte >= 2:
                # Real delimiter — only valid after we've passed the 2-byte header
                if seg_start is not None:
                    segs.append(data[seg_start:i])
                seg_start = j
                seg_byte  = 0
                i         = j
            else:
                # Single zero (RLE data) or we're still in the header — not a delimiter
                seg_byte += run
                i        += run
        else:
            if seg_start is None:
                seg_start = i
            seg_byte += 1
            i        += 1

    if seg_start is not None and seg_start < len(data):
        segs.append(data[seg_start:])

    segs = [s for s in segs if len(s) > 0]

    if len(segs) != SEGS_PER_FILE:
        label = Path(path).name if path else 'unknown'
        print(f"  Warning: {label}: expected {SEGS_PER_FILE} segments, "
              f"got {len(segs)}. Output may be incorrect.")

    while len(segs) < SEGS_PER_FILE:
        segs.append(bytes([0xF0] * 64))

    return segs[:SEGS_PER_FILE]


def decode_segment(seg: bytes) -> list:
    """
    Decode one RLE segment into exactly 1024 colour indices.

    Bytes 0-1: uint16-LE count of non-zero tiles (informational, skipped).
    Bytes 2+:  RLE stream — each byte m encodes:
                 upper nibble (m >> 4)   = run_length - 1  (1..16 tiles)
                 lower nibble (m & 0x0F) = colour index    (0..15)
    """
    tiles, var, i = [], 0, 2
    while var < TILES_PER_SEG and i < len(seg):
        m     = seg[i]
        count = (m >> 4) + 1
        color = m & 0x0F
        tiles.extend([color] * count)
        var  += count
        i    += 1
    while len(tiles) < TILES_PER_SEG:
        tiles.append(0)
    return tiles[:TILES_PER_SEG]


def decode_file(path: str) -> list:
    """
    Decode a .map file into a flat list of 16384 colour indices (128x128, row-major).

    16 segments arranged row-major in a 4x4 grid of 32x32 blocks:
        seg  0 -> block (col=0, row=0)   seg  1 -> (col=1, row=0)  ...
        seg  4 -> block (col=0, row=1)   ...
        seg 15 -> block (col=3, row=3)
    """
    data    = decompress_map(path)
    segs    = split_segments(data, path)
    decoded = [decode_segment(s) for s in segs]

    grid = [0] * (FILE_TILES * FILE_TILES)
    for seg_idx, tiles in enumerate(decoded):
        bc = seg_idx % SEG_GRID
        br = seg_idx // SEG_GRID
        for t, color in enumerate(tiles):
            wx = bc * SEG_SIZE + (t % SEG_SIZE)
            wy = br * SEG_SIZE + (t // SEG_SIZE)
            grid[wy * FILE_TILES + wx] = color
    return grid


def grid_to_image(grid: list, scale: int = 1) -> Image.Image:
    img = Image.new('RGB', (FILE_TILES * scale, FILE_TILES * scale), VOID_COLOR)
    px  = img.load()
    for idx, color_idx in enumerate(grid):
        tx    = idx % FILE_TILES
        ty    = idx // FILE_TILES
        color = VOID_COLOR if color_idx == 0 else PALETTE.get(color_idx, (255, 0, 255))
        if scale == 1:
            px[tx, ty] = color
        else:
            for dy in range(scale):
                for dx in range(scale):
                    px[tx * scale + dx, ty * scale + dy] = color
    return img


# ── Coordinate decoding ───────────────────────────────────────────────────────

_XY = re.compile(r'^(\d+)x(\d+)$')

def decode_coords(path: str, automap_root: str):
    rel   = os.path.relpath(os.path.abspath(path), os.path.abspath(automap_root))
    parts = Path(rel).parts
    if len(parts) != 5:
        return None
    try:
        z      = int(parts[0])
        levels = [_XY.match(parts[k]) for k in (1, 2, 3)]
        fm     = _XY.match(Path(parts[4]).stem)
        if not all(levels) or not fm:
            return None
        ax = [int(m.group(1)) for m in levels]
        ay = [int(m.group(2)) for m in levels]
        fx, fy = int(fm.group(1)), int(fm.group(2))
    except (ValueError, IndexError):
        return None
    bx = ax[0] * 16 + ax[1] * 4 + ax[2]
    by = ay[0] * 16 + ay[1] * 4 + ay[2]
    return (bx * 1024 + (fx - 1) * WORLD_STEP,
            by * 1024 + (fy - 1) * WORLD_STEP,
            z)


# ── Discovery ─────────────────────────────────────────────────────────────────

def find_all_floors(automap_root: str) -> dict:
    all_floors = {}
    if not os.path.isdir(automap_root):
        return all_floors
    for entry in os.scandir(automap_root):
        if not entry.is_dir():
            continue
        try:
            floor_z = int(entry.name)
        except ValueError:
            continue
        files = {}
        for dirpath, _, filenames in os.walk(entry.path):
            for fn in filenames:
                if not fn.lower().endswith('.map'):
                    continue
                full   = os.path.join(dirpath, fn)
                coords = decode_coords(full, automap_root)
                if coords and coords[2] == floor_z:
                    files[(coords[0], coords[1])] = full
        if files:
            all_floors[floor_z] = files
    return all_floors


# ── Stitch mode ───────────────────────────────────────────────────────────────

def render_floor_stitched(files: dict, floor: int, scale: int) -> Image.Image | None:
    if not files:
        return None
    all_wx = [wx for wx, _ in files]
    all_wy = [wy for _, wy in files]
    min_wx, max_wx = min(all_wx), max(all_wx)
    min_wy, max_wy = min(all_wy), max(all_wy)
    cw = (max_wx - min_wx) + FILE_TILES
    ch = (max_wy - min_wy) + FILE_TILES

    print(f"  World X : {min_wx} - {max_wx + FILE_TILES - 1}  ({cw} tiles wide)")
    print(f"  World Y : {min_wy} - {max_wy + FILE_TILES - 1}  ({ch} tiles tall)")
    print(f"  Canvas  : {cw * scale} x {ch * scale} px")

    img = Image.new('RGB', (cw * scale, ch * scale), VOID_COLOR)
    px  = img.load()
    ok  = errors = 0

    for (wx, wy), path in sorted(files.items()):
        off_x = wx - min_wx
        off_y = wy - min_wy
        try:
            grid = decode_file(path)
        except Exception as e:
            print(f"  Error: {Path(path).name}: {e}")
            errors += 1
            continue

        for idx, color_idx in enumerate(grid):
            tx    = idx % FILE_TILES
            ty    = idx // FILE_TILES
            color = VOID_COLOR if color_idx == 0 else PALETTE.get(color_idx, (255, 0, 255))
            x = (off_x + tx) * scale
            y = (off_y + ty) * scale
            if scale == 1:
                px[x, y] = color
            else:
                for dy in range(scale):
                    for dx in range(scale):
                        px[x + dx, y + dy] = color

        ok += 1
        if ok % 200 == 0:
            print(f"  {ok}/{len(files)} sectors rendered...")

    print(f"  Done: {ok} rendered, {errors} errors")
    return img


# ── Per-file mode ─────────────────────────────────────────────────────────────

def export_per_file(all_floors: dict, scale: int, out_dir: str) -> None:
    """
    Export one PNG per .map file.
    Output: <out_dir>/floor-<ZZ>/<floor>_<worldX>_<worldY>.png
    """
    total = sum(len(f) for f in all_floors.values())
    done  = 0
    for floor in sorted(all_floors.keys()):
        files     = all_floors[floor]
        floor_out = os.path.join(out_dir, f'floor-{floor:02d}')
        os.makedirs(floor_out, exist_ok=True)
        print(f"Floor {floor:02d}: exporting {len(files)} file(s) -> {floor_out}/")
        for (wx, wy), path in sorted(files.items()):
            out_path = os.path.join(floor_out, f'{floor}_{wx}_{wy}.png')
            try:
                grid = decode_file(path)
                img  = grid_to_image(grid, scale)
                img.save(out_path)
            except Exception as e:
                print(f"  Error: {Path(path).name}: {e}")
                continue
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{total} files exported...")
        print(f"  Floor {floor:02d} done.")
    print(f"\nPer-file export complete: {done}/{total} files.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='TibiaME automap exporter -- stitch floors or export one PNG per file')
    ap.add_argument(
        'automap', nargs='?', default=None,
        help='Path to the automap root folder '
             '(default: %%LOCALAPPDATA%%\\TibiaME\\automap)')
    ap.add_argument('--scale',    type=int, default=1, help='Pixels per tile (default: 1)')
    ap.add_argument('--out-dir',  default='.', help='Output directory (default: current dir)')
    ap.add_argument('--per-file', action='store_true',
                    help='Export one 128x128 PNG per .map file instead of stitching. '
                         'Output: <out-dir>/floor-<ZZ>/<floor>_<worldX>_<worldY>.png')
    args = ap.parse_args()

    automap_root = args.automap or default_automap_path()
    print(f"Automap folder : {automap_root}")

    if not os.path.isdir(automap_root):
        print(f"Error: folder not found: {automap_root}")
        print("Pass the path as an argument, e.g.:")
        print(f'  python tibiame.py "C:\\Users\\YourName\\AppData\\Local\\TibiaME\\automap"')
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    all_floors = find_all_floors(automap_root)
    if not all_floors:
        print("No .map files found.")
        sys.exit(0)

    floors = sorted(all_floors.keys())
    total  = sum(len(f) for f in all_floors.values())
    print(f"Found {len(all_floors)} floor(s): {floors}  ({total} files total)\n")

    if args.per_file:
        print(f"Mode: per-file  (scale={args.scale}, out={args.out_dir}/floor-<ZZ>/)\n")
        export_per_file(all_floors, args.scale, args.out_dir)
    else:
        print(f"Mode: stitch  (scale={args.scale}, out={args.out_dir}/floor-<ZZ>-map.png)\n")
        for floor in floors:
            out = os.path.join(args.out_dir, f'floor-{floor:02d}-map.png')
            print(f"Floor {floor:02d}: {len(all_floors[floor])} sector(s) -> {os.path.basename(out)}")
            img = render_floor_stitched(all_floors[floor], floor, args.scale)
            if img:
                img.save(out)
                print(f"  Saved {out}  ({img.width}x{img.height} px)\n")
            else:
                print(f"  Skipped (no data)\n")


if __name__ == '__main__':
    main()
