# Results (intermediate outputs for presentation)

Compressed copies (JPEG, quality 90, same pixel size) of curated result figures. Full-resolution PNG originals are on the work drive in `/Volumes/My Passport/3D Brain Reconstruction/results/`, in the same folder structure. Source slides were only read, never modified.

Colour code in all overlays: **green** = fixed (earlier) slice, **magenta** = moving (later) slice after alignment, **grey/white** = both agree. Outlines are the tissue masks in the same colours.

## M3-06_registration_spike (2026-10-19/20)

Real-data spike on slices 1 to 10 at 32x (about 7 µm/px), made with `scripts/spike_registration.py`. Findings: `docs/REQUIREMENTS.md` section 7, "Spike results".

| File | Shows |
|---|---|
| `contact_sheet.jpg` | Slices 1 to 10 with their first tissue masks (green). All slices have the same orientation. |
| `overlay_<k>.jpg` | Pair k-1 → k aligned with **global** feature matching (first attempt). Weak pairs (for example 6) are visibly off. |
| `matches_<k>.jpg` | The matched features (yellow lines) behind `overlay_<k>`: few matches, mostly on one vessel junction, which is why global matching is unreliable. |
| `overlay_guided_<k>.jpg` | Pair k-1 → k aligned with **guided** matching and a similarity fit, the method chosen for the project. Internal structures line up; remaining differences are tears, folds and compression (pairs 4 → 5 and 5 → 6). |
| `results.json` | All numbers: tissue areas, matches, inliers, residuals, scales, timings, resolution comparison. |

## M3-04_tissue_masks (2026-10-20)

| File | Shows |
|---|---|
| `mask_contact_sheet.jpg` | Slices 1 to 10 with the final tissue masks of M3-04 (chroma threshold with hysteresis; detached fragments kept; scanner edge and debris excluded). Checked and approved by the owner. |

## M4-05_qc_review (2026-11-07)

QC of the registered slices 1 to 10 (`brainrec qc --slices 1-10`) with the calibrated thresholds (NCC ≥ 0.40, Dice ≥ 0.93, Proposed). Calibration and verdicts: `tasks/BACKLOG.md`, M4-05; `docs/REQUIREMENTS.md` section 7.

| File | Shows |
|---|---|
| `contact_sheet.jpg` | The 9 QC overlays (registration level, 32x, about 7 µm/px, shown at about a quarter of their size) with NCC, Dice and flag. Interiors are grey in every pair; coloured areas are torn, folded or missing tissue at the margins. Flagged: 4→5 and 5→6, the damaged pairs. Full-size overlays: `qc/overlay_<k>.png` in the work directory. |
| `calibration.json` | NCC and Dice of every pair when its registered transform is deliberately shifted (25 to 400 µm, mean and maximum over 4 directions) or rotated about the tissue centre (0.5° to 5°). |

## M3-11_registered_volume (2026-11-03)

Taken from the registered, tissue-only volume of slices 1 to 10 (`brainrec volume --slices 1-10`), pyramid level 4 (64x of the scan, about 14 µm/px), so they show exactly what the viewer stacks in z. Match quality per pair: `tasks/BACKLOG.md`, M3-11.

| File | Shows |
|---|---|
| `contact_sheet.jpg` | The 10 planes of the volume (tiles at half size). Everything outside the tissue masks is background (dark grey here, transparent in the viewer); no glass, label or debris is kept. |
| `overlay_<k>.jpg` | Plane of slice k-1 (green) and slice k (magenta) in the volume. Internal structures line up; coloured areas are tissue present in only one section (tears, folds, lost pieces). |
