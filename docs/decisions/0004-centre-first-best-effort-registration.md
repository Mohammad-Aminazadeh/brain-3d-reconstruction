# 0004: Centre-first alignment and best effort for failed pairs

Date: 2026-11-04. Status: approved by the project owner (design, 2026-11-04); the edge weight 0.2 is Proposed until QC on more slices confirms it (M4-05).

## Context

Two owner requirements, raised after the first real registered volume (M3-11):

1. **A failed pair must not stop the run.** Until now `register` stopped at the first pair that passed no C12 check, chained nothing after it and ended with an error, so the volume of the range could not be built. Over 140 slices at least one damaged section is likely.
2. **The tissue centre must align best.** "The centre must match perfectly; further towards the boundary, errors matter less and less (but minimise them everywhere)." Margins are often torn or folded; the interior holds the anatomy that matters.

Measured on real slices 1 to 10 (M3-13, read only):
- The C12 fallback, outline alignment, fits the damaged margins and is 0.9 to 1.3 mm off in the interior of pairs 4→5 and 5→6 (up to 3.3 mm), while the feature fit is right there.
- Correlation over the whole tissue is pulled down by damaged margins (0.31 on 4→5 and 5→6), while the interior agrees (0.37 to 0.43). Dice of the masks mostly measures the margins.

## Decision

**Centre weighting.** Every point inside the tissue gets a weight rising linearly with its depth (distance to the tissue edge): `reg_edge_weight` (Proposed 0.2) at the edge, 1 at the deepest point, the centre. It is used in:
- the least-squares refit of the feature fit, so the remaining error goes to the edge;
- the masked correlation (NCC) of registration's C12 check, of the choice between candidates, and of QC (`PairScore.ncc`).

**Candidates, best centre match wins.** Per pair: the fine passes run twice from the same coarse estimate, with and without centre weights. Of the fits that pass C12, the one with the highest centre-weighted correlation is kept. Both are needed: on the weak real pair 3→4 (about 80 inliers) the weighted refit alone led the second fine pass to a nearby solution that matches the centre worse (the right ventricle doubled); with the choice no real pair gets worse, and 5→6 improves.

**Retry.** If no fit passes, the matching is repeated once with a pass-1 window of `reg_retry_window_um` (2.5 mm, as in the spike), for starting estimates that are far off.

**Best effort, never silent.** If still no feature fit passes, the outline alignment and the outline estimate join the candidates and the best centre match is used:
- status `boundary` if its mask IoU is at least `boundary_min_iou` (a plausible fallback);
- otherwise status `failed`, still with that transform.

`register` registers and chains every pair of the range, warns about failed pairs and exits 0. QC always flags failed pairs. `RegistrationFailedError` is removed.

## Consequences

- One damaged section can no longer block the volume. A wrong best-effort transform offsets all later slices relative to the earlier ones; the QC flag shows where, and manual overrides (post-MVP) can correct it.
- Outline alignment is no longer accepted just because the outlines overlap: it must also match the centre at least as well as every other candidate.
- Registration takes about 15 s per real pair instead of 9 s (both fine-pass variants), about 35 min for 140 slices.
- New settings `reg_edge_weight` and `reg_retry_window_um`; the register stage version is 2, so existing pair results need `register --overwrite`. The pair file format (C5) is unchanged.
- `score_pair` takes the edge weight, so QC weights the tissue like registration. M4-01's synthetic score ranges still hold.
- Not done: a weight profile steeper than linear. With edge weight 0 the synthetic centre error drops only from 0.95 to 0.8 px, the rest is deformation a similarity transform cannot follow; non-rigid refinement (post-MVP) is the way to reduce it.
