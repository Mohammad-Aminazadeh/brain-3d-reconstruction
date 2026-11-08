# Labelling guide: cells in the label crops

For the person who marks cells (owner or pathologist). Your marks are the ground truth that measures how accurately the software finds and classifies cells, and they train its classifier. Time needed: about 5 to 7 hours for all 20 crops (about 1500 cells).

> **Class list status (2026-11-08):** confirmed by the owner after the automatic appearance groups (M7-18). The supervisor's answer to Q9 may still change it.
>
> **Reference pictures:** example cells of each look are in `/Volumes/My Passport/3D Brain Reconstruction/spike/M7-18/group_0.jpg` to `group_7.jpg`. They show groups found automatically, not checked classes. See the table below for which group shows which class.

## What you get

All files are in the work directory `/Volumes/My Passport/3D Brain Reconstruction/labels/`:
- `crops_overview.jpg`: the 20 crops (250 × 250 µm each) from 10 slides across the block.
- `qupath/<slide file>.geojson`: the crop squares of each slide, to drop onto that slide in QuPath.
- `annotations/`: empty. Your exported labels go here.

You label on the **original slides** at full resolution. The source slides are only opened, never changed.

## The classes

Choose the class by the cell's **appearance**. Always pick the most likely class; use **Other** only if a nucleus fits none of the others. Do not leave a cell without a class.

| Class (exact name in QuPath) | Looks like (this stain) |
|---|---|
| **Neuron** | Either a large, round, **pale** nucleus with a **dark nucleolus**, or a nucleus inside a purple stained cell body (often triangular, with a process); often both. Reference groups 7 and 5 |
| **Astrocyte-like** | Medium, round to oval, **pale** nucleus with fine, evenly spread chromatin; no visible cell body. Reference group 6 |
| **Oligodendrocyte-like** | **Small, round, dark** nucleus with dense chromatin, **often but not always** with a clear halo; often in rows (white matter) or next to a neuron. Reference groups 3 (with halo) and 0 |
| **Microglia-like** | Small, **elongated**, rod-, comma- or bean-shaped dark nucleus, often irregular, **not** on a vessel. Reference groups 2 and 4 |
| **Vessel-wall** | Flat, elongated nucleus lying **along a vessel** (endothelial cells, pericytes). It looks like a microglia-like nucleus; the vessel next to it decides. Reference groups 2 and 4 |
| **Other** | A nucleus that fits none of the above (for example a lymphocyte), or one you cannot place at all |

Medium round nuclei between "small, dark" and "pale" (reference group 1) are the hardest cases: decide between Oligodendrocyte-like and Astrocyte-like by darkness and chromatin.

Do **not** mark red blood cells (no nucleus), empty holes, debris or out-of-focus smudges that are not recognisable nuclei.

## Rules

1. **Every nucleus inside a square:** mark each nucleus whose **centre** lies inside the square, including those cut by the square's edge.
2. **One point per nucleus,** placed on the centre of the **nucleus** (not of the cell body).
3. **Sizes:** for about 10 nuclei of each class, spread over different crops, also **outline the nucleus** (the nucleus border, not the cell body) with the brush or polygon tool, and give the outline the same class. These measure the size accuracy. An outlined nucleus needs no extra point.
4. **Rare classes:** if a class (for example Microglia-like or Vessel-wall) has fewer than about 100 examples in the squares, you may add extra points for it **outside** the squares on the same slides. They are used for training the classifier only.
5. **Blurred areas:** mark what you can still recognise as a nucleus; choose the most likely class.

## Steps in QuPath (version 0.4 or later)

1. Create a project (File → Project → Create project) and add the 10 slides listed in `qupath/`. They are on My Passport in `3D FCD Alignment/`.
2. Once per project, create the six classes with the exact names above: in the **Annotations** tab, open the **⋮** menu next to the class list, choose **Add class**, and repeat for each class.
3. Open a slide and drag its file `qupath/<slide file>.geojson` onto the image. Two locked squares named `crop NN` appear. Do not move or delete them.
4. Choose the **Points** tool (shortcut `.`). In the **Points** panel, add one point set per class and set its class (right-click → **Set class**). Select a set, then click the nuclei of that class.
5. For the outlines, use the **Brush** or **Polygon** tool, then set the class of the outline (right-click → **Set class**).
6. When a slide is done, export: **File → Export objects as GeoJSON** → **All objects**. Save it in `labels/annotations/` with the slide's file name plus `.geojson`, for example `250_23_II16_1_H - 2024-11-11 15.08.23.ndpi.geojson`. That is the same name as the file in `qupath/`.

## Checks before you hand over

- Every square has its points, and every point has a class.
- Each class has some outlines (about 10 in total per class).
- One export file per slide in `labels/annotations/`, ten files in all.

The software reports class names it does not know and points without a class, so a slip is caught. You then fix it in QuPath and export again.
