# 0003: Tissue-only processing and feature-based registration

Date: 2026-10-19. Status: requirements confirmed by the project owner; technical details marked "Proposed" are fixed in task M3-00 (contracts) after the real-data spike (M3-06).

## Context

The project owner added two requirements:

1. **Tissue only (F12).** Tissue boundaries are clearly visible in every image. The empty glass around the tissue must be ignored in the alignment; only the 3D volume of the actual tissue matters.
2. **Feature-based alignment (F13).** Alignment must detect edges or features in each image, find the corresponding features in consecutive images, and align the images by them.

Until now the plan used the tissue mask only as an optional helper and aligned slices with intensity-based registration (SimpleITK, mutual information, angle sweep), and the M2 volume stores each slice's whole scanned area, glass included.

## Decision

**Tissue only**
- The tissue mask of each slice (from the registration image, 32x, about 7 µm/px) is **mandatory** and defines what counts as tissue for every later stage.
- Registration: features are detected, matched and scored **inside the masks only**. Debris, labels, dust and bubbles outside the main tissue are removed from the mask (small components dropped).
- Volume: each mask is warped with its slice's transform; voxels outside it are set to background (white, 255, the fill value, so all-background chunks are not stored). The canvas is the bounding box of the transformed tissue, not of the scanned area. (Proposed: the mask is enlarged by a safety margin of about 50 µm so tissue is never cut off.)
- QC: metrics inside the tissue only; overlays show the tissue outlines.

**Feature-based registration (per consecutive pair)**
1. **Boundary-based initial alignment** from the two masks: centroid and principal axes of the tissue shape (both 180° candidates tried). This uses the clearly visible tissue edges and handles large rotations.
2. **Feature detection inside the tissue**: SIFT keypoints and descriptors (OpenCV; rotation- and scale-invariant) on a contrast-normalised registration image, restricted to the mask. ORB is compared in the spike. (AKAZE is not available in the installed OpenCV 5.0 build.)
3. **Matching and robust fit**: nearest-neighbour matching with a ratio test, then RANSAC fitting of the transform (rigid; similarity if the spike shows differential shrinkage). The inlier count and residual (µm) are kept as quality measures.
4. **Fallback** when too few reliable matches are found: align the tissue boundaries (contour points, iterative closest point) instead.
5. **Optional refinement** with intensity-based registration (SimpleITK) inside the masks, only if the synthetic and real-data results show the feature-based fit is not accurate enough.

## Consequences

- The mask becomes a critical component: too tight cuts tissue away, too loose keeps glass or debris. Mitigations: safety margin, small-component removal, mask outlines on the contact sheet and QC overlays, owner review on real slices.
- Feature matching is rotation-invariant, so the planned 10° angle sweep is no longer needed in the main path.
- Consecutive sections do not contain the same cells (each section is a different physical layer), so matches must come from larger structures (vessels, layers, the tissue edge) at registration resolution. The spike measures whether real neighbouring sections yield enough reliable matches (risk R10).
- The volume becomes smaller and faster to view: background chunks are not stored.
- New acceptance criterion AC9 (tissue only). AC4 now tests the feature-based method. New risks R9 (mask quality) and R10 (feature matching on real sections).
- SimpleITK stays installed but is no longer the primary registration tool. OpenCV, already a dependency, provides the features and RANSAC.
- The M2 (unregistered) volume still contains glass; tissue-only output starts with the registered volume (task M3-09).

## Owner decisions (2026-10-19)

- **Edge margin accepted:** a background rim of up to about 50 µm along the tissue edge is acceptable for the MVP, so that tissue is never cut off.
- **Transparent background:** the background is shown transparent in the viewer, so empty space contrasts with the tissue. To make this unambiguous, background voxels are stored as pure white (255, 255, 255) and tissue voxels that are exactly pure white are stored as 254 (an invisible change). The Neuroglancer shader renders pure white as transparent (task M3-12). This takes effect with the masked, registered volume; in the unregistered M2 volume the glass is not pure white.
