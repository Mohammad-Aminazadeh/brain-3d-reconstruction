# 0006: Cell classes by appearance

Date: 2026-11-08. Status: approved by the project owner (2026-11-08). The class list is a starting point until the supervisor answers Q9 and the appearance groups (M7-18) have been reviewed.

## Context

The owner requires that all cells are not only detected but also classified into classes based on their looks, so that they can be told apart. Plan v3 (decision 0005) had coarse types (neuron, glia, vessel-wall cell) and an "unclassified" class for doubtful cells, both defined by labels only. The spike (M7-01) showed what the stain allows: neurons with a stained cell body, a pale nucleus and a dark nucleolus; small dark round nuclei; pale oval nuclei; elongated nuclei.

## Decision

1. **Every cell is classified.** It gets its most likely class and a confidence (the top class probability). The probabilities of all classes are stored. There is no "unclassified"; low-confidence cells can be hidden with the viewer's confidence slider and are listed for review. "Other" is a class for cells that fit none of the listed looks.
2. **Classes come from both data and experts.**
   - First, the cells' measured looks are grouped automatically (M7-18), and a gallery per group is shown.
   - The owner and the supervisor confirm or adjust the class list.
   - Then cells are labelled, and a classifier is trained on the labels (M7-11).
3. **First labelling round** (until Q9 is answered): neuron, astrocyte-like, oligodendrocyte-like, microglia-like, vessel-wall cell, other. Later (M9): dysmorphic neuron, balloon cell.

## Consequences

- New task M7-18 (appearance groups) between M7-02 and the owner's labelling.
- M7-02: the labelling guide uses the fine class list; about 100 to 200 examples per class are needed, so rare classes may get extra points outside the crops.
- M7-11 renamed "Cell classes by appearance": no "unclassified" threshold; probabilities for all classes; confidence per cell.
- C13 (cell file) stores all class probabilities and the confidence; C14 (type table) has no "unclassified" id.
- F16 and AC10 updated. Labelling takes longer than with the coarse list.
