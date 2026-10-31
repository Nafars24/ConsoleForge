# GCZ

Notes on the GCZ container used by Dolphin for GameCube and Wii images. Source: `gcz.py`.

## Scope and provenance

This describes what ConsoleForge reads and writes, not an authoritative specification. The format belongs to Dolphin; where this document and Dolphin disagree, Dolphin wins. ConsoleForge was written from public documentation and from memory of the format, then tested. Each claim carries a status:

| Mark | Meaning |
|:-:|---|
| **T** | Covered by ConsoleForge's own tests (synthetic data). |
| **E** | Confirmed by an emulator or loader (reported by the author). |
| **X** | Confirmed against a file produced by another tool. |
| **?** | Not verified. Implemented from documentation or memory. |

All integers are **little-endian**.

## Overview

The image is cut into fixed-size blocks. Each block is compressed with zlib on its own, or stored raw if compression does not help. A table gives where each block starts, and a second table holds an Adler-32 checksum of each stored block, so damage is caught per block.

## Layout

```
0x00             header (32 bytes)
0x20             block pointers: blocks × u64
0x20 + 8n        block hashes:   blocks × u32
0x20 + 12n       block data
```

`n` is the number of blocks.

### Header

| Offset | Size | Field | Notes |
|---:|---:|---|---|
| 0 | 4 | magic | `0xB10BC001` (bytes `01 C0 0B B1`) |
| 4 | 4 | sub-type | 0 = GameCube, 1 = Wii |
| 8 | 8 | compressed data size | total bytes of the stored blocks (the data area only) |
| 16 | 8 | data size | size of the original image |
| 24 | 4 | block size | see below |
| 28 | 4 | number of blocks | `ceil(data size / block size)` |

### Block pointers

Each pointer is the block's offset **relative to the start of the data area**. Bit 63 set means the block is stored raw. A block's stored length is the next pointer's offset minus its own; for the last block it is the compressed data size minus its offset.

### Block hashes

One Adler-32 per block, computed over the bytes **as stored** (the compressed stream, or the raw block if the raw flag is set). The reader checks it for every block it reads.

### Blocks

- A compressed block is a standard zlib stream (with the two-byte zlib header), level 9 as written by ConsoleForge.
- A block is stored raw when its compressed form is not smaller than the block itself. The comparison used is `compressed length >= block size`. **?** (whether Dolphin uses exactly this threshold is from memory; either choice is valid for a reader)
- The last block is zero-padded to a full block before compression. The reader trims the output to the data size in the header.

### Sub-type and block size

ConsoleForge decides the sub-type from the image itself: the Wii magic `0x5D1C9EA3` (big-endian) at offset `0x18`, or the GameCube magic `0xC2339F3D` (big-endian) at `0x1C`. An image with neither is refused. **T**

The default block size is `0x4000` (16 KiB) for GameCube and `0x20000` (128 KiB) for Wii. The writer requires a power of two of at least `0x800`; the reader only requires that the block count in the header is consistent with the sizes. The defaults are from memory of common choices, not from a rule. **?**

## Reading

- Every block's stored bytes are checked against its Adler-32 before decoding. A mismatch raises an error that names the block. **T**
- A block that decodes to fewer bytes than expected raises an error.
- The last block may decode to more bytes than needed and is trimmed.

## Verification status

| Item | Status |
|---|:-:|
| Header, pointers and hashes parsed independently of the writer | **T** |
| Round trip for GameCube and Wii sub-types, several block sizes | **T** |
| Image that is not GameCube/Wii is refused | **T** |
| Corrupted data detected by the Adler-32 | **T** |
| File opens and plays in Dolphin | **E** |
| Files written by Dolphin read correctly | **?** |

For the Dolphin test, the sub-type and block size the author used were not recorded; add them to `COMPATIBILITY.md` when known.

## Limitations

- No scrubbing or junk removal: the image is stored as is. GCZ is lossless here.
- Whole-image checksums are not part of the format, so `--verify` compares the decompressed output with the source.
- Wii images are compressed as raw bytes. Dolphin's newer RVZ/WIA formats, which understand Wii partitions, are not implemented.

## References

Dolphin's disc image handling (the compressed-blob reader and writer), and public descriptions of the GCZ header. ConsoleForge is not affiliated with Dolphin.
