# 0002: Serve Neuroglancer locally

Date: 2026-10-19. Status: accepted for the MVP (M2).

## Context

The public Neuroglancer page (https://neuroglancer-demo.appspot.com) would have to fetch data
from `http://127.0.0.1`. Browsers restrict a public site from reading local addresses (Chrome asks
for permission or blocks it), and the viewer would depend on an external website.

## Decision

`brainrec serve` starts the viewer with the `neuroglancer` Python package, which serves the
Neuroglancer client from 127.0.0.1 (data port + 1). The public link is still printed as a fallback.
The OME channel axis `c` is mapped to the Neuroglancer channel dimension `c^` in the viewer state,
so the shader can combine the three channels into RGB.

## Consequences

- The viewer works offline and no data leaves the machine.
- One more dependency (`neuroglancer`).
- The `serve` process must stay running while viewing.
