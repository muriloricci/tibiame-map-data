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
    Split decompressed data into exactly 16 segments, in file order.

    Each segment is laid out as:

        [2-byte header][RLE stream][2-byte delimiter]

    except that the final segment's trailing delimiter is at the end of
    the file.  The header is a uint16-LE value, informational only; the
    script does not validate it against the decoded tiles, because
    observed values can exceed 1024 (for example 1033 or 1063).

    The RLE stream sums to exactly 1024 tiles for a well-formed segment
    (see decode_segment).  Segments are therefore located by counting
    tiles, not by scanning for zero-byte runs.

    Why not scan for zero runs?  The delimiter is exactly 2 bytes
    (0x00 0x00), but a segment header can itself start with a zero byte.
    For example, header 1024 is 0x00 0x04, so at a segment boundary the
    bytes are:

        delimiter 00 00 | header 00 04 | RLE ...

    That is a 3-byte zero run.  A scanner that consumes the whole run
    loses the header's low byte, reads the next RLE byte as the header
    (for example 0x04 0x62 = 25092), and decodes the rest of the segment
    shifted by one byte.  That is the inaccuracy reported in issue #1:
    most segments decode correctly, and only the segments whose header
    low byte is 0x00 are shifted.

    Empty segments follow the same layout: header 0x00 0x00 followed by
    64 bytes of 0xF0 (64 x 16 = 1024 tiles of colour 0).  The 4-byte zero
    run seen between empty segments is therefore the 2-byte delimiter
    plus the next segment's 2-byte 0x00 0x00 header, not a single
    4-byte delimiter.
    """
    segs = []
    i = 0
    for seg_idx in range(SEGS_PER_FILE):
        if i + 2 > len(data):
            label = Path(path).name if path else 'unknown'
            raise ValueError(
                f"{label}: segment {seg_idx} header truncated at offset {i}"
            )
        seg_start = i
        i += 2  # header, informational only
        total = 0
        while total < TILES_PER_SEG and i < len(data):
            total += (data[i] >> 4) + 1
            i += 1
        segs.append(data[seg_start:i])
        if seg_idx < SEGS_PER_FILE - 1:
            if data[i:i + 2] != b'\x00\x00':
                label = Path(path).name if path else 'unknown'
                raise ValueError(
                    f"{label}: segment {seg_idx} delimiter mismatch at "
                    f"offset {i}: expected 00 00, "
                    f"got {data[i:i + 2].hex(' ') or 'EOF'}"
                )
            i += 2
    # Trailing delimiter after the final segment, if present.
    if data[i:i + 2] == b'\x00\x00':
        i += 2
    if i != len(data):
        label = Path(path).name if path else 'unknown'
        print(f"  Warning: {label}: {len(data) - i} trailing byte(s) "
              f"after segment {SEGS_PER_FILE - 1}.")
    return segs


def decode_segment(seg: bytes) -> list:
    """
    Decode one RLE segment into exactly 1024 colour indices.

    Bytes 0-1: uint16-LE header (informational, skipped).  Empty
    segments carry header 0x0000 followed by 64 bytes of 0xF0.
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
