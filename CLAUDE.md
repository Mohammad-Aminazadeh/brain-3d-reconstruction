# CLAUDE.md

Rules for AI-assisted development in this repository. Read [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before starting any task.

## Project in one paragraph

Reconstruct a 3D volume from 140 ultra-high-resolution NDPI brain slices (about 3.8 GB each, on an external drive) and explore it in a browser. Python staged pipeline plus a thin tile server. Machine: macOS, Apple M4 Pro, 24 GB RAM.

## Development rules

- Follow the approved architecture. Do not add components, services or dependencies that it does not contain without asking.
- Do not change an interface (signature, file format, directory layout) without first explaining the consequences, and wait for approval.
- Implement one assigned task at a time. Do not start the next task unasked.
- Avoid unrelated code modifications: no drive-by refactors, renames or reformatting.
- Prefer simple solutions; no premature optimisation; no microservices.
- Do not commit or push without explicit permission.
- Do not install dependencies, or create files outside the task scope, without permission.
- Report blockers, failed tests and uncertainty immediately and plainly.

## Data safety

- Never modify, move or delete source NDPI files (the sample in `Data Sample/` and everything on "My Passport").
- Never load a full slice at full resolution; use pyramid levels or tiles.
- Write generated data only to the configured work directory, never into the repository.
- Do not read all 140 files in tests. Tests use synthetic data or small crops.
- Check free disk space before writing large outputs (internal disk has about 26 GB free).
- The work drive is exFAT with 512 KB blocks and macOS adds a `._` file per written file: keep the number of output files low (shards, not one file per chunk).
- Never replace an existing generated volume silently; require an explicit overwrite.

## Architecture constraints

- Stages communicate only through files described in `store`; no stage imports another stage's internals.
- Dependencies point downward only (see the dependency diagram in `docs/ARCHITECTURE.md`); no cycles.
- Transforms are stored separately from pixels. Registration runs on low-resolution images.
- All stages are resumable per slice and record status in the manifest.
- Voxel size (including Z spacing) comes from configuration, never hard-coded.

## Coding conventions

- Python 3.12+, type annotations on all public functions, `pathlib.Path` for paths.
- Format and lint with ruff; type-check with mypy (once configured).
- Small, single-purpose functions; docstrings on public interfaces describing inputs, outputs and units (pixels vs. µm).
- No global state; pass configuration explicitly (pydantic models).
- Use `logging`, not `print`, in library code.
- Match the style of the surrounding code.

## Testing requirements

- Every new function or behaviour comes with tests (pytest) in `tests/`.
- Registration is tested with the synthetic slice generator (known shift/rotation, check recovery).
- Never claim that tests passed unless they were executed in this session. Quote the actual result; if tests were not run, say so.
- If a test fails, report it with the output. Do not weaken or delete a test to make it pass.

## Documentation requirements

- Keep README.md, `docs/REQUIREMENTS.md` and `docs/ARCHITECTURE.md` consistent with the implementation; update them in the same task as the change.
- Record significant decisions as short notes in `docs/decisions/`.
- Update the "Current development status" in README.md when a milestone is reached.
- Label assumptions as "Proposed" until the project owner confirms them.
