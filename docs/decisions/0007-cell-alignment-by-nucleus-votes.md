# 0007: Cell-scale alignment by votes of nucleus offsets

Date: 2026-11-09. Status: method and settings approved by the project owner (2026-11-09, M7-12). The interim assumption about consecutive sections (below) is to be checked with the lab.

## Context

Linking a cell's profiles across sections (M7-13) needs neighbouring sections aligned to 1 to 2 µm. After the registration (C5), neighbouring sections are still off by 5 to 70 µm locally. In M7-01, image correlation per window locked on in only part of the windows, with 5 to 33% of nuclei recurring. The plan (M7-12a) prescribed correlating maps of nuclei drawn from the cell files.

## Decision

1. **Votes instead of image correlation.** Around each node of a grid in the fixed slice, every nucleus votes for its offsets to the nuclei of the moving slice within the search range. Recurring nuclei vote for the same offset, so the histogram peaks at the local shift. This is the correlation of the two nuclei maps, computed exactly on the points.
2. **Coarse, then fine.**
   - Coarse: nodes every 400 µm, search ±100 µm (`align_search_um`).
   - Fine: nodes every 100 µm (`align_grid_um`), search ±10 µm around the coarse field; the peak is refined to the mean of its votes within 1.5 µm.
   - Refined in M7-13 (2026-11-09): two fine passes. Each moves every nucleus of k by the current field at its own position, then votes only for the remainder. With one constant shift per window, steep fields (2 µm per 100 µm) smeared the votes. That left errors of 1 to 2 µm, and 67% of true links found in one synthetic pair. With the passes, 96.9 to 99.9% are found.
3. **Lock rule.** A node locks when its peak has at least 3 votes and at least 2× (`align_min_peak_ratio`) the votes of the best other peak. Nuclei within 150 µm (`align_window_um`) of the node vote.
4. **Field.** Outliers among locked nodes are dropped; the other nodes are filled from the locked ones and get confidence 0.
5. **No recurrence.** If fewer than 10% of the coarse nodes lock, the pair shares no recognisable nuclei: the field is zero with confidence 0, and linking finds nothing there.

## Evidence

- **Synthetic:** sections cut from 3D nuclei with a smooth deformation and a registration error. The field matches the truth within 1 µm at 99 to 100% of the nodes (median 0.2 µm), also with half of the nuclei missed.
- **Real slices 1 to 10:**
  - 6 s per pair.
  - The five pairs 1→2, 2→3, 6→7, 8→9 and 9→10 align: 38 to 78% of nodes lock, and 9 to 20% of all nuclei have a partner within 1.5 µm (chance 0.8%).
  - Neighbouring nodes 100 µm apart agree within 0.3 to 0.6 µm (spike), so the shifts are real.

## Finding and interim assumption

- **Finding:** the pairs 3→4, 4→5, 5→6 and 7→8 share no nuclei at all.
  - This holds even with a search of ±300 µm.
  - Pair 7→8 is well registered (NCC 0.68).
  - No non-consecutive pair among slices 1 to 10 locks on either, so the slide order is consistent.
  - The most likely cause is missing sections between these slides.
- **Interim assumption (owner, 2026-11-09):** all sections are consecutive, with no gaps, until the lab confirms. The volume therefore keeps a uniform z spacing.
- **What the pipeline does meanwhile:** pairs without recurrence are reported as `no_recurrence` and get no links.

## Consequences

- C15 as implemented: `pair_{k}.json` also records the status, the shares of locked nodes and the recurrence with its chance level; links follow in M7-13.
- C9: three new fields (`align_window_um`, `align_search_um`, `align_min_peak_ratio`) next to `align_grid_um`.
- `brainrec cells` aligns every consecutive pair (resumable per pair) and prints a table per pair.
- If the lab confirms gaps, z positions in the volume need correcting (non-uniform spacing), which is a change of direction.
