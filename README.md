# Brain 3D Reconstruction

Reconstruct a 3D volume of brain tissue from 140 serial, ultra-high-resolution 2D histology slices (Hamamatsu NDPI), and explore it interactively in a web browser, from the whole brain down to individual cells. Cells (later also vessels and other structures) are detected and shown as 3D objects inside the volume, which can be made transparent so that only these structures remain visible.

## Overview

Each slice is a scanned tissue section of about 3.8 GB (402 GB for all 140). The slices are physically shifted, rotated and slightly deformed relative to each other. The software:

1. reads the NDPI slices without loading them fully into memory,
2. finds the tissue in each slice and ignores the empty glass around it,
3. registers (aligns) consecutive slices by detecting and matching features of the tissue, so that anatomy lines up,
4. resamples them into a chunked, multi-resolution 3D volume (OME-Zarr) that contains only the tissue,
5. serves the volume to a browser-based viewer with zoom and level-of-detail streaming,
6. detects every nucleus in each slice, measures and classifies it, links the profiles of one cell across consecutive sections, and shows each cell as a 3D blob coloured by type (planned, milestone M7),
7. exports the cells as a table (CSV, QuPath) and as 3D density maps per cell type (planned, M7).

## Main functionality (planned)

- Read NDPI slices lazily, in order of the `N_H` number in the filename.
- Tissue detection per slice: the empty glass around the tissue is ignored everywhere; the volume contains only tissue.
- Automatic, feature-based slice-to-slice registration: tissue-boundary alignment, then features inside the tissue matched between neighbouring slices with a robust fit (similarity transform in the MVP; non-rigid and drift correction later).
- Registration quality control inside the tissue: metrics, overlays with tissue outlines, flagging (manual overrides later).
- Multi-resolution 3D volume with a precomputed pyramid down to about 1 µm/pixel (to be confirmed).
- Browser viewer: 3D and orthogonal slice views, zoom in and out.
- Native-resolution detail served on demand from the original slices (post-MVP).
- Resumable, per-slice batch processing through a CLI.
- **Cells in 3D (M7):**
  - nucleus detection with measured accuracy;
  - cell types with a probability;
  - one blob per cell, linked across sections;
  - one viewer layer per type, with the tissue layer fadeable to transparent;
  - cell table with CSV and QuPath export;
  - density maps per type;
  - details in [docs/decisions/0005](docs/decisions/0005-cells-in-3d.md).
- Later: vessels and other structures in 3D, with a non-rigid refinement of the alignment (M8); suspicious cells (M9).

## Current development status

**Phase: M4 quality control in progress (M3 done: registered, tissue-only volume of slices 1 to 10, reviewed by the owner). New goal since 2026-11-06: cells in 3D (milestone M7, after M4, before M5). Next task: M4-05 real QC review (owner).**

| Item | Status |
|---|---|
| Requirements | Drafted and approved (see [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)) |
| Architecture | Approved (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)) |
| Development rules | See [CLAUDE.md](CLAUDE.md) |
| NDPI facts | All 140 slides measured (see docs/REQUIREMENTS.md section 7) |
| Environment set up (M0) | Done: .venv, dependencies, package skeleton, lint and type checks |
| M1 data characterisation | Done (see docs/REQUIREMENTS.md section 7) |
| M2 unregistered volume | Done: slices 1 to 10 (19.5 GB, 11.5 min), checked in Neuroglancer |
| M3 registration | Done (2026-11-03): spike (M3-06), contracts (M3-00), transform model (M3-05), synthetic data (M3-02), pairwise registration (M3-07), work-dir layout (M3-01), tissue mask (M3-04), preprocessing stage (M3-03), register stage (M3-08), registered volume builder (M3-09), interim CLI (M3-10), transparent background (M3-12) and centre-first alignment with best effort for failed pairs (M3-13) done; real registered volume of slices 1 to 10 (M3-11, 12 min, 15.4 GB) reviewed by the owner; feature-based, tissue-only, similarity (see [docs/decisions/0003](docs/decisions/0003-tissue-only-feature-based-registration.md)) |
| M4 quality control | In progress: pair score (M4-01), overlay image (M4-02), QC report with flags (M4-03) and `brainrec qc` (M4-04) done; next: owner's review and thresholds (M4-05) |
| M7 cells in 3D | Planned (2026-11-06, decision 0005): spike, labelled cells, detection, 3D blobs, linking across sections, types, cell table, density maps. Open questions for the supervisor: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) section 6.2; section thickness still unknown |

## Usage (current)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # or: pip install -r requirements-lock.txt
cp config/project.example.yaml config/project.yaml          # adjust paths if needed
.venv/bin/brainrec --config config/project.yaml preprocess --slices 1-10  # tissue masks, 32x images
.venv/bin/brainrec --config config/project.yaml register --slices 1-10    # align, chain to slice 1
.venv/bin/brainrec --config config/project.yaml qc --slices 1-10          # scores, overlays, flags
.venv/bin/brainrec --config config/project.yaml volume --slices 1-10      # registered, tissue only
.venv/bin/brainrec --config config/project.yaml serve        # prints a viewer link; open it in Chrome
.venv/bin/pytest                                             # tests
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts && .venv/bin/mypy
```

- Each stage resumes: a second run skips finished slices and pairs. `--overwrite` redoes results made with other settings, or replaces a volume built from other inputs; nothing is replaced without it.
- `qc` prints one row per pair (status, inliers, residual, NCC, Dice) and the flagged pairs; the overlays (`qc/overlay_<k>.png`: fixed slice green, next slice magenta, grey where they agree) and `qc/report.json` are in the work directory. Changing `qc_min_ncc` or `qc_min_dice` re-evaluates the flags instantly.
- `register` never stops at a pair it cannot align reliably: it uses the best match of the tissue centre, prints `warning: pairs ... failed`, and QC flags those pairs. The centre is aligned most precisely; errors may grow towards the edge.
- `volume` needs the slices registered together in one `register` run; `volume --unregistered` stacks the whole scans without registration, as in M2.
- Mistakes such as a missing slice, a misspelled setting in the config or a missing step end with a one-line `error: ...` message that says what to do.

### Viewer controls (Neuroglancer)

- **Right-click** in any panel: move the current position there. All panels, including 3D, zoom around this position.
- **Ctrl + wheel**: zoom. **Wheel**: next/previous slice (z). **Shift + wheel**: 10 slices.
- 2D panels: left-drag pans. 3D panel: left-drag rotates, Shift + left-drag pans.
- The 3D panel shows the three 2D planes in space. They are sharp only where the 2D panels have loaded full-resolution data (their field of view around the current position). This is how Neuroglancer works, not a fault.
- `brainrec serve --layout 4panel` opens all four panels (heavier); the default is a single XY panel. Chrome needs hardware acceleration on (`chrome://gpu` shows "WebGL: Hardware accelerated").
- Everything outside the tissue (pure white, C11) is transparent, so the black panel shows through and the tissue stands out. This also applies to the unregistered M2 volume, whose glass is pure white too.
- The data is read from a USB hard disk (about 27 chunks/s), so full-resolution areas take a few seconds to fill in.

## Documentation

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): what the system must do and the decisions taken so far.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, interfaces and technology choices.
- [CLAUDE.md](CLAUDE.md): rules for AI-assisted development in this repository.
- [docs/MVP.md](docs/MVP.md): MVP scope, acceptance criteria and milestones.
- [tasks/BACKLOG.md](tasks/BACKLOG.md): work breakdown with task status.
- [tasks/IMPLEMENTATION_PLAN.md](tasks/IMPLEMENTATION_PLAN.md): order of implementation, critical path, contracts.
- [results/](results/README.md): result figures for presentations (compressed copies; originals on the work drive).
- [docs/decisions/](docs/decisions/): decision records (storage format, local viewer, tissue-only feature-based registration, centre-first alignment, cells in 3D).

## Data

The source NDPI files are stored on the external drive "My Passport" in `/Volumes/My Passport/3D FCD Alignment/` (read in place) and must never be modified. One sample slice is in `Data Sample/`. Generated data goes to a separate work directory outside this repository.
