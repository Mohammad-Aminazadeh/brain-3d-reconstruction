# 0001: Volume storage format

Date: 2026-10-19. Status: accepted for the MVP (M2).

## Context

- The work directory is on My Passport: exFAT with 512 KB allocation blocks. macOS adds a 4 KB
  `._` file next to every file it writes there, and each of those also occupies 512 KB.
- The first M2 build used Zarr v2 with one file per 512 x 512 chunk. For 10 slices, level 0
  held 33,685 chunk files (14.3 GB of data) plus 33,689 `._` files, occupying 44.7 GB on disk.
  Listing one directory took over 2 minutes. At 140 slices this would be roughly 900k files.
- Neuroglancer's Zarr reader supports blosc, gzip, zlib and zstd, but not JPEG.

## Decision

- OME-Zarr 0.5 on Zarr v3. Axes (c, z, y, x), uint8 RGB, white (255) fill.
- 512 x 512 chunks (the unit the viewer fetches) grouped into 4096 x 4096 shards (one file each).
- zstd level 3, lossless.
- Each band of rows read from a slide is aligned to the shard grid, so every shard file is written once.

## Consequences

- About 64x fewer files. Disk waste from block rounding and `._` files becomes negligible.
- Lossless only, so about 2 GB per slice and about 280 GB for 140 slices at the 4x base level.
  A lossy codec would need a different viewer format (for example Neuroglancer "precomputed" with
  JPEG), which is a separate decision.
- Reading one 4096-row band of the widest slice needs about 1.5 GB of RAM.
