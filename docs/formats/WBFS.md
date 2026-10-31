# WBFS, and how scrubbing works

Notes on the WBFS container used for Wii images and on ConsoleForge's partition analysis. Source: `wbfs.py`, `wii.py`, `aes.py`.

## Scope and provenance

This describes what ConsoleForge reads and writes, not an authoritative specification. WBFS comes from `libwbfs` and is read by Dolphin and USB loaders; where this document disagrees with them, they win. ConsoleForge was written from public documentation and from memory, then tested. Each claim carries a status:

| Mark | Meaning |
|:-:|---|
| **T** | Covered by ConsoleForge's own tests (synthetic data). |
| **E** | Confirmed by an emulator or loader (reported by the author). |
| **X** | Confirmed against a file produced by another tool. |
| **?** | Not verified. Implemented from documentation or memory. |

WBFS fields are **big-endian**. This document uses "WBFS block" for a 2 MiB unit of the container and "sector" for a 0x8000-byte unit of the Wii disc.

## Overview

A WBFS file holds one disc as a list of 2 MiB blocks plus a table saying which block of the container holds each 2 MiB range of the disc. Ranges the table marks as absent are not stored, which is how unused space is saved. The container does not compress anything.

## Layout (as written by ConsoleForge)

The file is a sequence of 2 MiB blocks. **Block 0** is the header block; blocks `1…N` hold disc data in the order they were written.

### Block 0

| Offset | Size | Content |
|---:|---:|---|
| 0x000 | 4 | magic `WBFS` |
| 0x004 | 4 | `n_hd_sec`: size of the file in 512-byte sectors |
| 0x008 | 1 | `hd_sec_sz_s` = 9 (512-byte sectors) |
| 0x009 | 1 | `wbfs_sec_sz_s` = 21 (2 MiB blocks) |
| 0x00A | 2 | padding |
| 0x00C | 500 | disc table: byte 0 is 1 (a disc is present), the rest 0 |
| 0x200 | 0x100 | copy of the first 0x100 bytes of the disc (game ID, title) |
| 0x300 | 4483 × 2 | `wlba` table, one `u16` per 2 MiB of disc |
| 0x100000 | 16 | private note: `FORGESZ\0` + the original image size as `u64` |
| end − ⌈blocks/8⌉ | | free-block bitmap |

- The `wlba` table has `ceil(143432 × 2 × 0x8000 / 2 MiB) = 4483` entries, enough for a dual-layer disc. Entry `i` is the block number (≥ 1) that holds disc bytes `[i × 2 MiB, (i+1) × 2 MiB)`, or 0 if that range is not stored. **T** (parsed independently of the writer); **E** (Dolphin reads files written this way).
- The private note records the exact size of the original image. Other tools ignore that part of block 0. **T**
- `n_hd_sec` is written as the file size divided by 512. That the value should equal the file size is from memory. **?**
- The free-block bitmap marks every block as used (all bits 0). Which bit value means "free" is from memory. Nothing in ConsoleForge reads it, and a loader that only plays discs does not need it. **?**

### Data blocks

Block `k` of the container (k ≥ 1) holds disc bytes starting at `i × 2 MiB` for the `i` whose `wlba[i] = k`. ConsoleForge numbers the stored blocks 1, 2, 3, … in disc order. The last block is zero-padded to 2 MiB. **T**

A range with `wlba = 0` reads as zeros in `libwbfs`; ConsoleForge's reader does the same. ConsoleForge never relies on a loader reading a missing range that a game uses, because it only omits ranges the game does not read. Behaviour of every loader for missing ranges is **?**.

### Split files

For FAT32, ConsoleForge can cut the stream at 4 GiB − 32 KiB (`0xFFFF8000` bytes). The parts are named `GAME.wbfs`, `GAME.wbf1`, `GAME.wbf2`, … (the last character of the extension is replaced). The reader joins parts it finds next to the first one. **T**. Whether USB loaders accept these parts is **?**.

## Reading

- Only the first disc slot is read. A file with more than one disc is refused.
- The original size comes from the private note when present. For files from other tools it is guessed: the end of the last stored block, rounded to a standard Wii size (4,699,979,776 bytes if it fits, else 8,511,160,320), otherwise the end of the block. **T** (note present and absent); guesses against real third-party files are **?**.
- Fewer table entries are read if the block is too small to hold all 4483.

## Modes

| Mode | Stored | Needs a key |
|---|---|:-:|
| `full` | every block | no |
| `game` | blocks that overlap `[0, 0x50000)` or a non-update partition | no |
| `scrub` | blocks that contain a used sector, with unused sectors zeroed | yes |

`full` reproduces the source exactly. `game` and `scrub` change the image by design: removed data reads as zeros.

**`game`** reads only the unencrypted partition table (below). It drops partitions of type 1 (the update partition) and anything outside the partitions. It is **E** (a Wii image converted in this mode, with no key, runs in Dolphin).

**`scrub`** also removes unused clusters inside the game partition. Details follow.

## Wii disc structure used by `scrub`

All offsets are in bytes from the start of the disc unless noted.

### Disc header and partition table

- The disc header flags are at `0x60` (hash verification disabled) and `0x61` (encryption disabled).
- The partition table is at `0x40000`: four groups of `(count: u32, table offset >> 2: u32)`. Each table is `count` entries of `(partition offset >> 2: u32, type: u32)`. Type 1 is the update partition. A count above 16 is treated as corrupt.
- ConsoleForge requires every partition offset and its data offset to be multiples of `0x8000`; otherwise it refuses to scrub.

### Partition header

| Offset in partition | Size | Content |
|---:|---:|---|
| 0x1BF | 16 | encrypted title key |
| 0x1DC | 8 | title ID |
| 0x1F1 | 1 | common-key index (0 common, 1 Korean, 2 vWii) |
| 0x2B8 | 4 | data offset `>> 2` |
| 0x2BC | 4 | data size `>> 2` |

### Decryption

- **Title key** = AES-128-CBC-decrypt(common key for the index, IV = title ID followed by 8 zero bytes, encrypted title key).
- Partition data is divided into **clusters** of `0x8000` bytes: a `0x400`-byte hash block and `0x7C00` bytes of data.
- The hash block is AES-128-CBC with a zero IV. The data is AES-128-CBC with the IV taken from bytes `0x3D0–0x3DF` of the **encrypted** cluster.
- A logical offset `L` inside the partition's decrypted data maps to cluster `L / 0x7C00`, byte `L % 0x7C00`.
- The first 31 × 20 bytes of the decrypted hash block (**H0**) are the SHA-1 of each consecutive `0x400` bytes of the cluster's data. ConsoleForge checks this for every cluster it decrypts. A wrong key or wrong IV handling fails this check at once.

The AES implementation matches FIPS-197 and NIST SP 800-38A test vectors and the `cryptography` package on random inputs (**T**). The cluster layout, IV choice and H0 check work on a synthetic encrypted disc built to the same description (**T**). On a retail disc they are **?**.

### Finding what the game uses

Read from the decrypted partition data:

1. `boot.bin` and `bi2.bin`: bytes `0 … 0x2440`.
2. Boot info: DOL offset at `0x420`, FST offset at `0x424`, FST size at `0x428` (each `>> 2`).
3. Apploader: `0x20 + size + trailer`, with the size at `0x2454` and the trailer size at `0x2458`, starting at `0x2440`.
4. DOL: a `0x100`-byte header holding 18 section offsets (from `0x00`) and 18 section sizes (from `0x90`); the DOL ends at the largest `offset + size`, at least `0x100`.
5. FST: the entry count is at offset 8 of the first 12-byte entry. Each entry is `(type: u8, name offset: u24, offset: u32, length: u32)`. For files (type 0) the offset is `>> 2`.

Every byte range found this way is converted to clusters. The result is a map of used sectors for the whole disc:

- bytes `[0, 0x50000)` (disc header, partition tables, region data);
- for each kept partition, its header area `[partition offset, partition offset + data offset)`;
- for each used cluster, its `0x8000` bytes.

Update partitions are never marked. Blocks with any marked sector are stored; inside those blocks, unmarked sectors are written as zeros.

### Safety rules

- If a partition cannot be analysed (key missing for its index, H0 mismatch, FST nonsense, a range outside the partition), the whole partition is stored unchanged and a warning is emitted. Nothing in a partition is dropped on a guess. **T**
- The mode needs a key; without one, `scrub` is refused and the default falls back to `game`.
- Keys come from the command line, environment variables or `wii_keys.txt`. They are kept in memory and never written to disk.

### Verification

`--verify` recomputes the used-sector map from the source and requires every used sector in the WBFS to equal the source exactly, and every block that holds used sectors to be present. **T**

## Verification status

| Item | Status |
|---|:-:|
| Header block layout, `wlba` table, block numbering, parsed independently | **T** |
| `full`: bit-exact round trip | **T** |
| `game` mode file runs in Dolphin | **E** |
| Split files read back | **T** |
| `scrub` on a synthetic encrypted disc: every file recovered intact through the FST after scrubbing | **T** |
| Wrong key or wrong key index: partition kept whole, with a warning | **T** |
| `scrub` on a retail disc, and the resulting game running | **?** |
| `full` file in a USB loader; split files in a USB loader | **?** |
| WBFS files from other tools read correctly | **?** |
| Korean and vWii discs | **?** |

## Known risks

- **Data outside the FST.** A few games read data that no file entry lists. Scrubbing removes it. If a scrubbed game misbehaves, redo it in `game` mode.
- **Layout assumptions from memory.** The size field and free-block bitmap are the weakest parts of the container description above; they matter to tools that manage a whole WBFS drive, not to playing a single file.
- **Single disc.** Multi-disc WBFS drives are not supported.

## Not implemented

Multi-disc WBFS partitions, GameCube images, junk-data regeneration, WIA/RVZ.

## References

`libwbfs` and the WBFS support in Dolphin; public documentation of the Wii disc, partition and file-system layout. ConsoleForge is not affiliated with any of them.
