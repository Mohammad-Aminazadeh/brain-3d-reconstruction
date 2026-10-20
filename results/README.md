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
