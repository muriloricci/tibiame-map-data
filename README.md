# TibiaME Automap — File Format & Exporter

This document covers the reverse-engineered `.map` file format used by the TibiaME client to cache minimap data, and the Python script that converts those files into PNG images.

Both the Python script and this documentation were generated with Claude Sonnet 4.6.

This project is inspired by https://github.com/tibiamaps/tibia-map-data

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Script Usage](#script-usage)
3. [.map File Format](#map-file-format)
   - [File Header](#1-file-header-6-bytes-uncompressed)
   - [Directory & Coordinate System](#2-directory--coordinate-system)
   - [Decompressed Data Structure](#3-decompressed-data-structure)
   - [Segment Delimiters](#31-segment-delimiters)
   - [Segment Layout in the File](#32-segment-layout-in-the-file)
   - [Segment Format](#4-segment-format)
   - [Empty Segment](#41-empty-segment)
   - [Populated Segment](#42-populated-segment)
   - [RLE Encoding](#43-rle-encoding)
   - [Colour Palette](#5-colour-palette)
5. [How the Script Works](#how-the-script-works)

---

## Quick Start

```bash
pip install Pillow

# Stitch all floors into one PNG each (uses default TibiaME automap path)
python tibiame.py

# Stitch with custom path
python tibiame.py "C:\Users\YourName\AppData\Local\TibiaME\automap"

# Export one PNG per .map file (useful for debugging individual sectors)
python tibiame.py --per-file --scale 4 --out-dir debug
```

---

## Script Usage

```
python tibiame.py [automap_folder] [--scale N] [--out-dir DIR] [--per-file]
```

| Argument | Default | Description |
|---|---|---|
| `automap_folder` | `%LOCALAPPDATA%\TibiaME\automap` | Path to the automap root folder |
| `--scale N` | `1` | Pixels per tile in the output PNG |
| `--out-dir DIR` | `.` (current directory) | Directory to write output files |
| `--per-file` | off | Export one 128×128 PNG per `.map` file instead of stitching |

**Stitch mode** (default) — one PNG per floor, all sectors composited at their correct world coordinates:
```
floor-07-map.png
floor-08-map.png
...
```

**Per-file mode** (`--per-file`) — one PNG per `.map` file, named by world position:
```
floor-07/
  7_31744_31744.png
  7_31744_31872.png
  ...
floor-08/
  ...
```

The per-file output name encodes `<floor>_<worldX>_<worldY>`, making it easy to trace any incorrectly rendered sector back to its source file.

---

## .map File Format

The `.map` files are written by the TibiaME game client to cache minimap data that was originally received as network packets (protocol opcode `0x28`). Each file stores the minimap colour data for a 128×128 tile region of one floor of the game world.

### 1. File Header (6 bytes, uncompressed)

| Offset | Size | Type | Value | Description |
|--------|------|------|-------|-------------|
| 0 | 2 | `uint8[2]` | `0x65 0x00` | Magic bytes (always this value) |
| 2 | 2 | `uint16-LE` | variable | Size in bytes of the zlib-compressed block that follows |
| 4 | 2 | `uint8[2]` | `0x00 0x00` | Padding |
| 6 | N | `bytes` | — | zlib-compressed map data (N = value at offset 2) |

The zlib stream uses the standard deflate format (RFC 1950), typically with the `0x78 0x9C` header indicating default compression.

---

### 2. Directory & Coordinate System

Files are stored in a 5-level directory hierarchy under the automap root:

```
automap/
  <Z>/
    <Ax1>x<Ay1>/
      <Ax2>x<Ay2>/
        <Ax3>x<Ay3>/
          <Fx>x<Fy>.map
```

Each path component uses the format `NxM` (two integers separated by a literal `x`). The three folder levels (`Ax`/`Ay`) form a **base-4 positional number** encoding the block coordinate:

```
block_x = Ax1×16 + Ax2×4 + Ax3
block_y = Ay1×16 + Ay2×4 + Ay3

world_x = block_x × 1024 + (Fx − 1) × 128
world_y = block_y × 1024 + (Fy − 1) × 128
floor   = Z
```

**Key facts:**
- Each block is 1024 world tiles wide/tall.
- Within a block, `Fx` and `Fy` are **1-indexed** (values 1–8 are most common, but `Fx=0` also appears in practice, yielding a world offset of `−128` relative to the block boundary — these are valid world positions).
- Each `.map` file covers exactly **128×128 world tiles**.
- Adjacent files (e.g. `1x1.map` and `2x1.map`) are exactly 128 tiles apart with no gap or overlap.

---

### 3. Decompressed Data Structure

After zlib decompression, the data is a byte stream containing **16 segments** in file order. Each segment is followed by a 2-byte delimiter, and the file ends with a trailing delimiter. Segments are located as described in 3.1.

#### 3.1 Segment Delimiters

Each segment is followed by exactly **2 delimiter bytes** (`0x00 0x00`), and the file ends with a trailing delimiter after the 16th segment. A segment is located by reading its 2-byte header and then its RLE stream until exactly 1024 tiles have been decoded; only then are the next 2 bytes consumed as the delimiter.

A segment header can itself start with a zero byte. For example, header 1024 is `0x00 0x04`, so at a boundary the bytes are delimiter `00 00` + header `00 04` + RLE. A zero byte inside the RLE stream is RLE data, not a delimiter.

#### 3.2 Segment Layout in the File

There are always exactly **16 segments** per file (some may be empty). They are arranged **row-major** in a 4×4 grid, each segment covering a 32×32 tile block:

```
Segment index → (block_col, block_row):

  seg  0 → col 0, row 0   ┌──────┬──────┬──────┬──────┐
  seg  1 → col 1, row 0   │  0   │  1   │  2   │  3   │  ← each cell = 32×32 tiles
  seg  2 → col 2, row 0   ├──────┼──────┼──────┼──────┤
  seg  3 → col 3, row 0   │  4   │  5   │  6   │  7   │
  seg  4 → col 0, row 1   ├──────┼──────┼──────┼──────┤
  ...                     │  8   │  9   │ 10   │ 11   │
  seg 15 → col 3, row 3   ├──────┼──────┼──────┼──────┤
                          │ 12   │ 13   │ 14   │ 15   │
                          └──────┴──────┴──────┴──────┘
                           ↑ 4 × 32 = 128 tiles wide ↑
```

Combined, the 4×4 grid of 32×32 blocks = **128×128 tiles** per file.

---

### 4. Segment Format

#### 4.1 Empty Segment

Contains a **2-byte header `0x00 0x00`** followed by exactly **64 bytes**, all `0xF0` (66 bytes in total).

`0xF0` as an RLE byte means "16 tiles of colour 0". So 64 × 16 = **1024 tiles**, all colour 0 (void). This is the encoding for a sector that has never been visited.

#### 4.2 Populated Segment

| Offset | Size | Description |
|--------|------|-------------|
| 0–1 | 2 bytes | `uint16-LE` — count of **non-zero tiles** in this segment. Informational; used by the client for fast tile lookup. **Skipped during decoding.** |
| 2+ | variable | RLE-encoded tile stream |

#### 4.3 RLE Encoding

This encoding was confirmed from this C# network protocol source:

```csharp
// Network packet handler, opcode 0x28 (minimap data)
ReadUInt16(); // x
ReadUInt16(); // y
ReadByte();   // z

byte m4;
for (int var = 0; var < 1024; var += (m4 >> 4) + 1)
    m4 = ReadByte();
```

Each byte `m` in the RLE stream encodes a run of identically-coloured tiles:

```
upper nibble  (m >> 4)     =  run_length − 1   →  values 0..15, meaning 1..16 tiles
lower nibble  (m & 0x0F)   =  colour index      →  values 0..15
```

The loop accumulates tile positions (`var`) until it reaches 1024, filling the 32×32 block. If the final run extends past 1024, only the tiles up to position 1023 are used.

Tiles within a segment are in **row-major order**: position `t` → tile column `t % 32`, tile row `t // 32`.

---

### 5. Colour Palette

The lower nibble of each RLE byte is a 4-bit colour index (0–15) into a fixed minimap palette:

| Index | Hex | Colour |
|-------|-----|--------|
| 0 | `#000000` | black |
| 1 | `#999999` | gray |
| 2 | `#00CC00` | green |
| 3 | `#FFCC99` | beige |
| 4 | `#996633` | brown |
| 5 | `#FFFF00` | yellow |
| 6 | `#666666` | dark gray |
| 7 | `#006600` | dark green |
| 8 | `#3300CC` | blue |
| 9 | `#663300` | dark brown |
| 10 | `#FF3300` | red |
| 11 | `#FFFFFF` | white |

---

## How the Script Works

### Overview

```
automap/                    ← root folder
  7/                        ← floor Z
    1x1/3x3/3x3/            ← 3-level block path
      1x1.map               ← one 128×128 sector
      1x2.map
      ...
```

The script:
1. Walks the automap directory, grouping `.map` files by floor.
2. Decodes each file's world coordinates from its path.
3. Decodes the binary content of each file into a 128×128 colour grid.
4. In stitch mode: paints all grids onto a single canvas at their world positions.
5. In per-file mode: saves each grid as its own PNG named by world coordinates.

### Coordinate Decoding (`decode_coords`)

```python
bx = Ax1*16 + Ax2*4 + Ax3   # block X (base-4 from 3 folder levels)
by = Ay1*16 + Ay2*4 + Ay3   # block Y

world_x = bx * 1024 + (Fx - 1) * 128
world_y = by * 1024 + (Fy - 1) * 128
```

### File Decoding (`decode_file`)

```
decompress_map()   →   zlib.decompress(raw[6 : 6+comp_size])
       ↓
split_segments()   →   16 byte-strings, one per 32×32 block
       ↓                (sequential parse: header, RLE until 1024
                        tiles, then exactly 2 delimiter bytes)
decode_segment()   →   1024 colour indices via RLE
  each byte m:
    count = (m >> 4) + 1    # 1..16 tiles
    colour = m & 0x0F       # palette index 0..15
       ↓
assemble grid      →   row-major 4×4 placement of the 16 blocks
                        seg_idx % 4  = block column (0–3)
                        seg_idx // 4 = block row    (0–3)
                        tile t → wx = block_col*32 + t%32
                                 wy = block_row*32 + t//32
```

### Stitch Layout

Each file's 128×128 grid is painted at pixel offset `(world_x − min_wx, world_y − min_wy)` on the canvas. Files are processed in sorted world-coordinate order. Later files overwrite earlier ones where they overlap (no overlap should occur in well-formed data since each world position belongs to exactly one file).

### Output Filenames

| Mode | Pattern | Example |
|------|---------|---------|
| Stitch | `floor-{ZZ}-map.png` | `floor-07-map.png` |
| Per-file | `floor-{ZZ}/{Z}_{X}_{Y}.png` | `floor-07/7_31744_31744.png` |

Floor numbers are always zero-padded to 2 digits. Per-file names embed the exact world coordinates, making it straightforward to identify the source `.map` file for any given PNG.
