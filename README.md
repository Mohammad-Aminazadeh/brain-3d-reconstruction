# Brain 3D Reconstruction

Reconstruct a 3D volume of brain tissue from 140 serial, ultra-high-resolution 2D histology slices (Hamamatsu NDPI), and explore it interactively in a web browser, from the whole brain down to individual cells.

## Overview

Each slice is a scanned tissue section of about 3.8 GB (about 530 GB for all 140). The slices are physically shifted, rotated and slightly deformed relative to each other. The software:

1. reads the NDPI slices without loading them fully into memory,
2. registers (aligns) consecutive slices so that anatomy lines up,
3. resamples them into a chunked, multi-resolution 3D volume (OME-Zarr),
4. serves the volume to a browser-based viewer with zoom and level-of-detail streaming.

## Main functionality (planned)

- Read NDPI slices lazily, in order of the `N_H` number in the filename.
- Automatic slice-to-slice registration (rigid first, non-rigid later), with drift correction.
- Registration quality control: metrics, overlays, flagging and manual overrides.
- Multi-resolution 3D volume with a precomputed pyramid down to about 1 µm/pixel (to be confirmed).
- Browser viewer: 3D and orthogonal slice views, zoom in and out.
- Native-resolution detail served on demand from the original slices (post-MVP).
- Resumable, per-slice batch processing through a CLI.

## Current development status

**Phase: M0 complete (environment and skeleton). No application functionality yet.**

| Item | Status |
|---|---|
| Requirements | Drafted and approved (see [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)) |
| Architecture | Approved (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)) |
| Development rules | See [CLAUDE.md](CLAUDE.md) |
| NDPI facts | Slice 1: 139776 x 102144 px at 220 nm/px (reported by owner); pyramid levels not yet inspected |
| Environment set up (M0) | Done: .venv, dependencies, package skeleton, 1 smoke test |
| Implementation | Not started |

## Documentation

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): what the system must do and the decisions taken so far.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, interfaces and technology choices.
- [CLAUDE.md](CLAUDE.md): rules for AI-assisted development in this repository.

## Data

The source NDPI files are stored on the external drive "My Passport" in `/Volumes/My Passport/3D FCD Alignment/` (read in place) and must never be modified. One sample slice is in `Data Sample/`. Generated data goes to a separate work directory outside this repository.
