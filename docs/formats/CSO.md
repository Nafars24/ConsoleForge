# CSO and ZSO

Notes on the block-compressed ISO containers used on PSP (CSO) and PS2 (CSO, ZSO). Source: `cso.py`, `lz4block.py`.

## Scope and provenance

This describes what ConsoleForge reads and writes, not an authoritative specification. These formats have no single owning project; behaviour was taken from public descriptions of the container and from memory, then tested. Each claim carries a status:

| Mark | Meaning |
|:-:|---|
| **T** | Covered by ConsoleForge's own tests (synthetic data). |
| **E** | Confirmed by an emulator or loader (reported by the author). |
| **X** | Confirmed against a file produced by another tool. |
| **?** | Not verified. Implemented from documentation or memory. |

All integers are **little-endian**.

## Overview

The image is cut into fixed-size blocks (2048 bytes, the ISO sector size). Each block is compressed on its own, so a reader can seek to any sector. CSO compresses blocks with raw deflate; ZSO uses LZ4. The two share one layout and differ only in the magic and the codec.

## Layout

```
0x00   header (24 bytes)
0x18   index: (blocks + 1) × u32
...    block data, padded to the alignment
```

### Header

| Offset | Size | Field | Written value |
|---:|---:|---|---|
| 0 | 4 | magic | `CISO` (CSO) or `ZISO` (ZSO) |
| 4 | 4 | header size | 24 |
| 8 | 8 | uncompressed size in bytes | exact size of the source |
| 16 | 4 | block size | 2048 |
| 20 | 1 | version | 1 |
| 21 | 1 | index shift (alignment) | 0, 1 or 2; see below |
| 22 | 2 | reserved | 0 |

### Index

`blocks = ceil(size / block size)`. There are `blocks + 1` entries. For block `i`:

- bit 31 set: the block is stored **plain** (uncompressed);
- bits 0–30: the block's file offset divided by `1 << shift`.

The block's stored length is the next entry's offset minus its own, so the final entry marks the end of the data. Offsets are multiples of `1 << shift`, and the writer pads with zero bytes after each block to keep them so. The data area itself starts at the first aligned offset after the index.

**T** for the layout, parsed independently of the writer in the tests. **E** for files with shift 0 (PPSSPP for CSO, PCSX2 for ZSO).

### Alignment shift

A 31-bit offset can address 2 GiB. For larger images the offset is stored shifted right, trading a little padding for range. ConsoleForge picks the smallest shift for which the worst case (every block stored plain, plus padding) still fits:

| Image size | Shift |
|---|---:|
| up to about 2 GiB | 0 |
| up to about 4 GiB | 1 |
| up to about 8 GiB | 2 |

A shift above 0 is covered by tests with a forced shift (**T**). Whether a **real** image over 2 GiB, written with a shift, plays in PCSX2 or OPL is **?**.

### Compression and the "plain" rule

| | CSO | ZSO |
|---|---|---|
| Codec | raw deflate (no zlib header), level 1–9 (default 9) | LZ4 block format, no frame |
| Stored plain when | compressed length ≥ block size | compressed length + 12 ≥ block size |

The 12-byte margin for ZSO is from memory of existing tools and is **?**. Storing a block plain that could have been compressed is always valid, so a wrong margin can only cost space, not correctness. A reader that decodes only plain blocks and blocks it can decompress does not depend on it.

## LZ4 handling

LZ4 block data has no length marker: a decoder stops when the output reaches the expected size. When blocks are padded for alignment, trailing zero bytes follow the compressed data, so the reader first finds the true end of the block by walking the LZ4 sequences, then decompresses only that slice. **T**

ConsoleForge has two LZ4 paths:

| Path | Used when | Status |
|---|---|:-:|
| Pure Python (`lz4block.py`) | the `lz4` module is not installed | **T**; the encoder is checked against the LZ4 block-format rules (last five bytes are literals, last match starts at least 12 bytes before the end) and the decoder against a hand-built vector |
| `lz4` module (high-compression mode on write) | the module is installed | **?** — the module was not available in the environment where the tests ran, so this path has never been exercised by the author's tests |

The pure-Python path is correct but slow. Which path produced the ZSO file the author tested in PCSX2 is not recorded.

## Reading

- The last block is trimmed to the size in the header.
- A block whose decoded length is shorter than expected raises an error that names the block.
- The version byte is **not** checked. Files from tools that use a different version, or other relatives of the format (for example DAX or JSO), are not supported and may fail or be misread. **?**

## Verification status

| Item | Status |
|---|:-:|
| CSO round trip, sizes multiple of 2048, odd sizes, tiny inputs | **T** |
| ZSO round trip, same sizes, with and without alignment padding | **T** |
| Index parsed independently of the writer | **T** |
| Corrupted block detected | **T** |
| CSO plays in PPSSPP | **E** |
| ZSO plays in PCSX2 | **E** |
| ZSO on real OPL hardware | **?** |
| Images over 2 GiB (shift > 0) in a real player | **?** |
| `lz4` module path | **?** |
| CSO/ZSO files written by other tools read correctly | **?** |

## Limitations

- 2048-byte blocks only on write. Other block sizes are accepted when reading if they are a power of two of at least 512.
- No CSO versions other than the one written.
- No checksum in the format, so `--verify` compares the decompressed output with the source image rather than checking a stored hash.

## References

Public descriptions of the CISO container as used by PSP tools and PPSSPP; the OPL ZSO convention; the LZ4 block format description. ConsoleForge is not affiliated with any of them.
