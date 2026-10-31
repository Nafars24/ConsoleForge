# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/) once 1.0 is reached; until then, minor versions may change behaviour.

Versions 0.1.0 to 0.3.0 were internal development builds and were never published, so they carry no dates. They are listed because the fixes in them explain why some parts of the code look the way they do.

## [Unreleased]

Nothing yet.

## [0.3.1] - unreleased

### Added
- FLAC decoder (`flac.py`) and read support for the CHD codecs `cdfl` and `flac`. Every FLAC frame is checked against its CRC-8 and CRC-16, so a misparse raises an error instead of producing wrong data.
- `test_flac.py`, which compares the decoder with FFmpeg's FLAC encoder across 294 combinations of signal type, compression level, stereo mode and block size, and checks that the end-of-stream position is exact.

### Fixed
- CHDs created by `chdman createcd` were rejected as soon as they were opened, because the header lists `cdfl` and the reader tried to initialise every listed codec. Codecs are now created on first use, so an unsupported codec only fails if a hunk really needs it.
- Decoding a hunk that needs an unsupported codec now names the codec and the hunk, and points to `chdman extractcd` / `extractdvd` as a workaround.

### Notes
- `chdman` picks the smallest codec per hunk, so FLAC can appear even on data discs, for example for the zero-filled system area at the start of an ISO 9660 image. This is why FLAC support was needed to open ordinary data-disc CHDs.
- LZMA streams written by `chdman` have no end marker, unlike those written by Python's `lzma` module. The decoder was checked with the marker removed and does not depend on it.
- The `flac` codec's endianness prefix (`L` / `B`) is implemented from documentation and has only been tested against a synthetic stream, not a real `chdman createdvd` file.

## [0.3.0]

### Added
- Packaging for Windows: PyInstaller spec (GUI and CLI executables), Inno Setup installer script, an application icon, a build script and a GitHub Actions workflow that runs the tests and builds the release artifacts. See `BUILDING.md`.
- The UI server now shuts itself down when the browser tab has been gone for three minutes and no job is running. A **Quit** button stops it immediately.
- Settings are saved to a per-user folder (`%APPDATA%\ConsoleForge` on Windows) and restored on the next start.
- Unhandled errors in the UI launcher are written to `crash.log` in the same folder, since the windowed executable has no console.
- Optional native window through `pywebview`, with a fallback to the default browser.
- A single `version.py` as the source of the version number.

### Changed
- Resource and key-file lookups no longer depend on the script location, so they work in a frozen (PyInstaller) build. `wii_keys.txt` is searched in the working directory and next to the executable.

### Fixed
- The Wii common key could be written to `settings.json`: the UI sent its whole options object, including the key, and the server only removed a top-level `key` field. The client no longer sends it, and the server strips `key` at every nesting level. Found by `test_lifecycle.py`.
- The GitHub workflow looked for the installer in the wrong folder, which would have made artifact upload fail silently.

### Security
- Settings never contain the Wii key (see above).

## [0.2.2]

### Added
- A local web UI is now the default interface: a queue with platform badges, per-file progress, format cards with suggestions, an in-page file browser, an activity log, and dark and light themes. It needs no dependencies and works at phone widths. The Tkinter UI remains available as `gui-tk`.
- The server binds to `127.0.0.1` only, uses a random per-session token, and rejects requests whose `Host` header is not local.
- `test_web.py` for the server and API, and a browser-driven end-to-end check of the UI against the real server.

### Changed
- Format suggestions use the intersection across the queue rather than the union. Conversions that are unusual for a disc (for example Wii → CSO) are flagged on that row.

### Fixed
- The UI used the property `dir` both as a folder path (queue rows) and as a "is a directory" flag (file browser). With real data, queue rows showed `false` instead of the folder. The demo mode had hidden it.
- A worker thread that crashed before its job loop left the server permanently marked as running. Workers now always clear that state.

## [0.2.1]

### Added
- WBFS `scrub` mode: reads the game partition's file system table and drops clusters that are not used, and zeroes unused sectors inside blocks that are kept.
- Pure-Python AES-128, validated against the FIPS-197 and NIST SP 800-38A test vectors, and against the `cryptography` package when it is installed. `pycryptodome` is used automatically if present.
- Wii key handling through `--common-key`, `--korean-key`, `--vwii-key`, the `WII_COMMON_KEY` family of environment variables, or a `wii_keys.txt` file. Keys are never stored.
- Safeguards: every decrypted cluster is checked against its H0 hash; a partition that cannot be analysed is stored whole with a warning; the update partition is always dropped.
- Verification for scrubbed images: sectors the game uses must match the source exactly.

## [0.2.0]

First version under the name ConsoleForge.

### Added
- **CSO** and **ZSO** read and write. The block alignment shift is chosen automatically for images over 2 GB.
- LZ4 block codec with a pure-Python fallback, checked against a hand-built vector from the specification and against the specification's encoder rules. The `lz4` module is used when installed.
- **GCZ** read and write for GameCube and Wii, with per-block Adler-32.
- **WBFS** read and write in `full` and `game` modes, with optional splitting at 4 GiB − 32 KiB for FAT32.
- Platform detection from ISO 9660 contents and disc magic.
- Conversion between containers through a temporary ISO.
- A command line with `convert`, `extract`, `verify`, `info` and `detect`.

## [0.1.0]

Initial CHD tool, then called CHDForge.

### Added
- CHD v5 reader and writer in pure Python, including the Huffman-coded hunk map.
- ISO → CHD (DVD layout, or CD layout on request), BIN/CUE → CHD with multiple tracks, pregaps and postgaps, GDI → CHD, and extraction back to ISO, BIN/CUE or GDI.
- CD ECC regeneration (`cdlz` / `cdzl`), de-duplication of identical hunks, and a multi-threaded compressor.
- `verify` (per-hunk CRC-16 and header SHA-1) and `info`.

### Fixed
- The map decoder did not advance the data offset for uncompressed hunks.
- Corrupt compressed data surfaced as a raw `LZMAError` instead of a normal error, and `verify` raised instead of reporting a failure.
