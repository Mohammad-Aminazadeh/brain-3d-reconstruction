# Brain 3D Reconstruction

Reconstruct a 3D volume of brain tissue from 140 serial, ultra-high-resolution 2D histology slices (Hamamatsu NDPI), and explore it interactively in a web browser, from the whole brain down to individual cells.

## Overview

Each slice is a scanned tissue section of about 3.8 GB (402 GB for all 140). The slices are physically shifted, rotated and slightly deformed relative to each other. The software:

1. reads the NDPI slices without loading them fully into memory,
2. finds the tissue in each slice and ignores the empty glass around it,
3. registers (aligns) consecutive slices by detecting and matching features of the tissue, so that anatomy lines up,
4. resamples them into a chunked, multi-resolution 3D volume (OME-Zarr) that contains only the tissue,
5. serves the volume to a browser-based viewer with zoom and level-of-detail streaming.

## Main functionality (planned)

- Read NDPI slices lazily, in order of the `N_H` number in the filename.
- Tissue detection per slice: the empty glass around the tissue is ignored everywhere; the volume contains only tissue.
- Automatic, feature-based slice-to-slice registration: tissue-boundary alignment, then features inside the tissue matched between neighbouring slices with a robust fit (similarity transform in the MVP; non-rigid and drift correction later).
- Registration quality control inside the tissue: metrics, overlays with tissue outlines, flagging (manual overrides later).
- Multi-resolution 3D volume with a precomputed pyramid down to about 1 µm/pixel (to be confirmed).
- Browser viewer: 3D and orthogonal slice views, zoom in and out.
- Native-resolution detail served on demand from the original slices (post-MVP).
- Resumable, per-slice batch processing through a CLI.

## Current development status

**Phase: M2 (unregistered volume of slices 1 to 10, viewable in Neuroglancer). No registration yet.**

| Item | Status |
|---|---|
| Requirements | Drafted and approved (see [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)) |
| Architecture | Approved (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)) |
| Development rules | See [CLAUDE.md](CLAUDE.md) |
| NDPI facts | All 140 slides measured (see docs/REQUIREMENTS.md section 7) |
| Environment set up (M0) | Done: .venv, dependencies, package skeleton, lint and type checks |
| M1 data characterisation | Done (see docs/REQUIREMENTS.md section 7) |
| M2 unregistered volume | Done: slices 1 to 10 (19.5 GB, 11.5 min), checked in Neuroglancer |
| M3 registration | In progress: spike (M3-06), contracts (M3-00), transform model (M3-05), synthetic data (M3-02), pairwise registration (M3-07), work-dir layout (M3-01), tissue mask (M3-04), preprocessing stage (M3-03) and register stage (M3-08) done; feature-based, tissue-only, similarity (see [docs/decisions/0003](docs/decisions/0003-tissue-only-feature-based-registration.md)) |

## Usage (current)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # or: pip install -r requirements-lock.txt
cp config/project.example.yaml config/project.yaml          # adjust paths if needed
.venv/bin/brainrec --config config/project.yaml volume --slices 1-10
.venv/bin/brainrec --config config/project.yaml serve        # prints a viewer link; open it in Chrome
.venv/bin/pytest                                             # tests
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts && .venv/bin/mypy
```

### Viewer controls (Neuroglancer)

- **Right-click** in any panel: move the current position there. All panels, including 3D, zoom around this position.
- **Ctrl + wheel**: zoom. **Wheel**: next/previous slice (z). **Shift + wheel**: 10 slices.
- 2D panels: left-drag pans. 3D panel: left-drag rotates, Shift + left-drag pans.
- The 3D panel shows the three 2D planes in space. They are sharp only where the 2D panels have loaded full-resolution data (their field of view around the current position). This is how Neuroglancer works, not a fault.
- `brainrec serve --layout 4panel` opens all four panels (heavier); the default is a single XY panel. Chrome needs hardware acceleration on (`chrome://gpu` shows "WebGL: Hardware accelerated").
- The data is read from a USB hard disk (about 27 chunks/s), so full-resolution areas take a few seconds to fill in.

## Documentation

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): what the system must do and the decisions taken so far.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, interfaces and technology choices.
- [CLAUDE.md](CLAUDE.md): rules for AI-assisted development in this repository.
- [docs/MVP.md](docs/MVP.md): MVP scope, acceptance criteria and milestones.
- [tasks/BACKLOG.md](tasks/BACKLOG.md): work breakdown with task status.
- [tasks/IMPLEMENTATION_PLAN.md](tasks/IMPLEMENTATION_PLAN.md): order of implementation, critical path, contracts.
- [results/](results/README.md): result figures for presentations (compressed copies; originals on the work drive).
- [docs/decisions/](docs/decisions/): decision records (storage format, local viewer, tissue-only feature-based registration).

## Data

The source NDPI files are stored on the external drive "My Passport" in `/Volumes/My Passport/3D FCD Alignment/` (read in place) and must never be modified. One sample slice is in `Data Sample/`. Generated data goes to a separate work directory outside this repository.
