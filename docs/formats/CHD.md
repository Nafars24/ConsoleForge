# CHD (Compressed Hunks of Data), version 5

Notes on the CHD v5 layout as ConsoleForge reads and writes it. Source: `chdforge.py`.

## Scope and provenance

This document describes what ConsoleForge does, not an authoritative specification. The format is defined by MAME (`chd.h` / `chd.cpp`) and implemented by `libchdr`; where this document and those disagree, they win. ConsoleForge was written from public documentation and from memory of the format, then checked by testing. Each claim below carries a status:

| Mark | Meaning |
|:-:|---|
| **T** | Covered by ConsoleForge's own tests (synthetic data). Proves self-consistency, not compatibility. |
| **E** | Confirmed by an emulator or loader (reported by the author). |
| **X** | Confirmed against a file produced by another tool (`chdman`). |
| **?** | Not verified. Implemented from documentation or memory. |

All integers in the CHD header, map and metadata are **big-endian**.

## Concepts

A CHD stores a *logical* byte stream split into fixed-size **hunks**. Each hunk is stored raw or compressed with one of up to four codecs named in the header, or is a reference to an earlier identical hunk. A **map** says where each hunk lives. A chain of **metadata** entries describes what the data is (for example, CD tracks). The last hunk is zero-padded to a full hunk.

## File layout (as written by ConsoleForge)

```
0x000  header (124 bytes)
0x07C  hunk data, back to back, in hunk order (references take no space)
...    metadata entries (chained)
...    map (16-byte map header + bitstream)
```

The order of metadata and map is not fixed by the format; the header holds offsets. ConsoleForge's order is `header, hunks, metadata, map`. The compressed map requires hunk data to be contiguous from a start offset, which is why metadata is not interleaved with hunks. **E** (PCSX2, DuckStation read these files)

### Header (124 bytes)

| Offset | Size | Field | Written value |
|---:|---:|---|---|
| 0 | 8 | tag | `MComprHD` |
| 8 | 4 | header length | 124 |
| 12 | 4 | version | 5 |
| 16 | 16 | compressors (4 × u32 tags) | one codec tag, then three zeros |
| 32 | 8 | logical bytes | size of the logical stream |
| 40 | 8 | map offset | |
| 48 | 8 | metadata offset | 0 if there is no metadata |
| 56 | 4 | hunk bytes | see below |
| 60 | 4 | unit bytes | see below |
| 64 | 20 | raw SHA-1 | SHA-1 of the logical bytes (not the padding) |
| 84 | 20 | SHA-1 | raw SHA-1 combined with metadata hashes, see Checksums |
| 104 | 20 | parent SHA-1 | zeros (parent CHDs are not supported) |

**E** for the layout. `chdman` writes up to three codecs in the list (for example `cdlz, cdzl, cdfl`); ConsoleForge writes one. **X** for reading `chdman` headers.

### Map

The v5 compressed map is a 16-byte header followed by a bitstream.

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | length of the bitstream in bytes |
| 4 | 6 | offset of the first stored hunk (124 as written) |
| 10 | 2 | CRC-16 of the decoded 12-byte-per-hunk map |
| 12 | 1 | bits used for a compressed length |
| 13 | 1 | bits used for a "self" hunk index |
| 14 | 1 | bits used for a parent index (0 as written) |
| 15 | 1 | reserved |

Bitstream, most significant bit first:

1. A Huffman tree over 16 symbols (the compression types), maximum code length 8. Each symbol's code length is stored in 4 bits; a length of `1` is escaped by writing it twice (`1, 1`), because a lone `1` means "repeat count follows". Codes are canonical, longest codes first.
2. One symbol per hunk giving its compression type.
3. Per-hunk data: for types 0–3, a length (`lengthbits`) and a 16-bit CRC; for type 4, a 16-bit CRC; for type 5, a hunk index (`selfbits`).

Compression types: `0–3` = codec slot in the header, `4` = stored raw, `5` = same data as an earlier hunk, `6` = parent, `7`/`8` = run-length of the previous type (small/large), `9`/`10` = self reference to the last / last+1 hunk, `11–13` = parent variants.

The decoded map has 12 bytes per hunk: type (1), length (3), offset (6), CRC-16 (2). Raw hunks have length = hunk size. Self references have length 0 and CRC 0.

| Behaviour | Writer | Reader | Status |
|---|:-:|:-:|:-:|
| Types 0, 4, 5 | ✓ | ✓ | **E** (written); **X** (read, `chdman` file) |
| Run-length types 7 and 8 | no | ✓ | **?** (implemented from memory; unknown whether any test file exercised them) |
| Self shortcuts 9 and 10 | no | ✓ | **?** |
| Parent types | no | error | not supported |
| Uncompressed v5 map (no codec) | no | ✓ | **?** (no test) |

### Checksums

- **Per-hunk CRC-16** (CCITT, polynomial 0x1021, initial value 0xFFFF). ConsoleForge writes it over the *uncompressed* hunk. When reading, it accepts a match on either the uncompressed or the compressed data, because the author was not certain which one `chdman` uses. **T**, and **X** for reading.
- **Raw SHA-1**: SHA-1 of the logical bytes only.
- **SHA-1 (header)**: SHA-1 of the raw SHA-1 followed by the sorted list of `tag ‖ SHA-1(data)` for every metadata entry whose checksum flag is set. **?** — emulators do not check this field. `chdman verify` does. Whether ConsoleForge's value passes `chdman verify` has **not** been confirmed.

### Metadata

Each entry is a 16-byte header followed by its data:

| Size | Field |
|---:|---|
| 4 | tag (ASCII) |
| 1 | flags (bit 0 = include in the header SHA-1) |
| 3 | data length |
| 8 | offset of the next entry, 0 for the last |

Tags written: `CHT2` (CD track), `CHGD` (GD-ROM track), `DVD ` (DVD marker), and the private `FGLB` (below). Unknown tags are ignored when reading. **E**

## Disc kinds

### DVD / raw data

- Unit size 2048, default hunk size 16384, codec `lzma`, or `zlib` on request. **E** (PCSX2)
- One metadata entry: tag `DVD `, flags 1, data a single `0x00` byte. The payload is from memory. **?** — PCSX2 does not need it; `chdman extractdvd` may.
- The logical size is the exact file size. If the ISO is not a multiple of 2048 bytes, ConsoleForge warns and proceeds. **T**

### CD

- One logical frame is 2448 bytes: 2352 bytes of sector data and 96 bytes of subcode. One hunk is 8 frames = **19584** bytes; unit size 2448. **E**
- ConsoleForge stores subcode as zeros (`SUBTYPE:NONE`). Discs with stored subcode are not supported.
- Each track is padded with zero frames up to a multiple of 4 frames. Metadata `FRAMES` counts the real frames; the padding is implied. **E** (multi-track PS1 disc in DuckStation) for the write side.
- Track types and sector data size:

| Type | Bytes per sector in the source |
|---|---:|
| `MODE1` | 2048 |
| `MODE1_RAW`, `MODE2_RAW`, `AUDIO` | 2352 |
| `MODE2`, `MODE2_FORM_MIX` | 2336 |
| `MODE2_FORM1` | 2048 |
| `MODE2_FORM2` | 2324 |

  Tracks shorter than 2352 bytes are stored at the start of the 2352-byte data area, followed by zeros. For a `MODE1` ISO this means extraction returns the original 2048-byte sectors. **E** / **T**

- Metadata string, NUL-terminated, one entry per track:
  `TRACK:n TYPE:t SUBTYPE:NONE FRAMES:f PREGAP:p PGTYPE:t PGSUB:NONE POSTGAP:g`
- **Pregaps.** `PREGAP` from a CUE `PREGAP` command is virtual: not stored, `PGTYPE` is the track type. A pregap from `INDEX 00` data that is present in the BIN is stored inside the track's frames, with `PGTYPE` prefixed by `V`. This reading of the `V` prefix is from memory. **?** (round-trips in tests, **E** only for the single multi-track disc the author tried)
- If a track's first `INDEX 01` is after the start of its file and there is no `INDEX 00`, the leading data is treated as pregap so that no bytes are lost.

### GD-ROM (Dreamcast)

- Same frame layout as CD. Track entries use `CHGD`:
  `TRACK:n TYPE:t SUBTYPE:NONE FRAMES:f PAD:p PREGAP:… PGTYPE:… PGSUB:… POSTGAP:…`
  `PAD` is written as the padding to a multiple of 4 frames. Whether this is the meaning other implementations expect is **?**; the author converted a GDI and ran it in Flycast, but the outcome is not recorded.
- GDI track start LBAs are not representable in the standard fields. ConsoleForge stores them in a **private** entry `FGLB` (flags 0, so it is excluded from the header SHA-1): `TRACK:n LBA:l`. Other tools ignore it; ConsoleForge uses it to rebuild the exact GDI. **T**
- Without `FGLB`, extraction guesses: track 1 at LBA 0, each next track after the previous one, and track 3 moved to LBA 45000 if it would start earlier. **?**

## Codecs

| Tag | Read | Write | Structure |
|---|:-:|:-:|---|
| `lzma` | ✓ | ✓ | Raw LZMA1 stream, no header. |
| `zlib` | ✓ | ✓ | Raw deflate (no zlib header). |
| `cdlz` | ✓ | ✓ | CD hunk: LZMA for sector data, deflate for subcode. |
| `cdzl` | ✓ | ✓ | CD hunk: deflate for both. |
| `cdfl` | ✓ | – | CD hunk: FLAC for sector data, deflate for subcode. |
| `flac` | ✓ | – | Plain data as FLAC audio. |
| `huff`, `avhu`, other | error | – | Not supported. |

**LZMA parameters.** `lc=3, lp=0, pb=2`. The dictionary size is derived from the amount of data per call, as the reference does: the smallest `2<<i` or `3<<i` (i from 11) that is at least that size. For a CD hunk (18816 bytes of sector data) this is 24576; for a 16384-byte DVD hunk it is 16384. Python's `lzma` module always writes an end-of-payload marker; `chdman` does not. The decoder works without the marker (it stops at the known output size), checked by truncating a Python-written stream by up to five bytes, which removes the marker. **T**; **X** for streams written by `chdman`.

**`cdlz` / `cdzl` hunk payload** (8 frames):

| Part | Size |
|---|---|
| ECC bitmap | `ceil(frames / 8)` bytes, bit `i` of byte `i/8` set if frame `i` had its sync and ECC removed |
| Length of the sector-data block | 2 bytes (3 if the hunk were ≥ 65536 bytes) |
| Sector data, `frames × 2352` bytes, compressed | LZMA (`cdlz`) or deflate (`cdzl`) |
| Subcode, `frames × 96` bytes, compressed | raw deflate |

When a sector starts with the 12-byte sync pattern and its P/Q parity matches the one recomputed from the sector, the sync (12 bytes) and the parity (bytes `0x81C–0x92F`, 276 bytes) are zeroed before compression and the bitmap bit is set; the reader restores them. The parity is the standard CD-ROM Reed–Solomon product code over GF(2^8) (polynomial 0x11D): 86 columns × 24 rows for P, 52 × 43 for Q, computed over bytes from offset 12. The encoder only removes data it can regenerate, so a wrong parity implementation cannot corrupt data written by ConsoleForge, only fail to save space. **T**. Restoration of ECC in files written by `chdman` has **not** been exercised on real data (**?**).

**`cdfl`.** The sector-data block is a sequence of FLAC frames, with no `fLaC` marker and no STREAMINFO, covering `frames × 588` stereo 16-bit samples. The decoded samples are written back as **big-endian** 16-bit words, followed by the raw-deflate subcode starting at the byte after the last FLAC frame. Verified: the decoder against FFmpeg's FLAC encoder, 294 signal/level/stereo-mode/block-size combinations, including exact end-of-stream position (**T**); a real `chdman` CHD whose hunk 0 uses `cdfl` was extracted correctly (**X**). That hunk is the all-zero system area of an ISO, so it cannot show whether the byte order is right for non-zero data. The byte order for real audio is **?**.

**`flac`.** First byte `L` or `B` gives the byte order of the output words, then FLAC frames for `size / 4` stereo samples. From memory. **T** (synthetic only), **?** against a real file.

## Reading tolerances

- Codecs are created when a hunk needs them, not when the file is opened. A header that lists `cdfl` does not stop a file whose hunks only use `cdlz`. **X**
- CRC-16 mismatches, decompression failures and short reads raise an error that names the hunk.
- Version 1–4 files, parent CHDs and unknown map types are rejected with an error.

## Open questions

1. **Audio byte order.** ConsoleForge stores audio sectors exactly as they are in the source BIN and returns them unchanged on extraction. Some implementations may store CD audio byte-swapped in the CHD. A ConsoleForge CHD with a data and an audio track played in DuckStation without noise (**E**), but whether a `chdman`-made CHD's audio extracts byte-for-byte equal to the original BIN has **not** been tested. Decisive test: `chdman createcd` a CUE that has audio, extract with ConsoleForge, compare the audio BIN's hash with the original.
2. Does `chdman verify` accept the header SHA-1 that ConsoleForge writes? Not tested.
3. Does `chdman extractdvd` accept the `DVD ` metadata payload? Not tested.
4. Do any real files use map run-length or self-shortcut types? Unknown; the reader implements them from memory.

## Not implemented

Parent/delta CHDs, versions 1–4, subcode data, `huff` and `avhu` codecs, writing `cdfl`/`flac` (audio is compressed with the same codecs as data, so CHDs of audio-heavy discs come out larger than `chdman`'s).

## References

MAME `chd.h` and `chd.cpp`; the `libchdr` library; the FLAC format specification (RFC 9639); ECMA-130 for the CD sector and ECC layout. ConsoleForge is not affiliated with any of them.
