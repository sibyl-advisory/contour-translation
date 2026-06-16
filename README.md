# Contour Translator

Read any Foundry Contour analysis through the Contour HTTP API and emit one structured, OSDK-friendly row per board.

## What this is

A stack-agnostic Foundry Python transforms template that reads a Contour analysis you have access to and produces one structured row per board (step) in it. Each row carries a human-readable description of the board's logic plus a normalized `render_spec_json` that downstream OSDK or application code can consume without parsing Contour's raw internal state. It lets you programmatically extract and inspect a Contour analysis's logic outside the Contour UI. The template is stack-agnostic: drop the two modules into a Foundry Python transforms repo, replace a few placeholders, and build.

## Architecture at a glance

One transform, one helper module, one output dataset. The transform authenticates to the Contour HTTP API with a Personal Access Token (PAT) read from a Data Connection source, walks every board in the analysis, and delegates each board's `boardState` to a translator that returns a normalized render spec.

```
Contour analysis  (Foundry Contour HTTP API, authenticated with a PAT)
    │
    ▼
contour_translator.py  ──(uses)──►  contour_render_specs.py
    │                                 (boardState → normalized render spec)
    ▼
output dataset (one row per board)
    ├── logic_description       (human-readable summary)
    ├── render_spec_kind / is_renderable / render_spec_json  (normalized, OSDK-friendly)
    ├── board_state_json        (raw)
    └── board_view_state_json   (raw)
```

The transform also calls the Compass API to resolve referenced dataset RIDs to human-readable names.

## What's in this repo

| Item | Type | Purpose |
|---|---|---|
| `contour_translator.py` | Python transform | Reads the Contour + Compass APIs; emits one row per board. Holds the CONFIG block. |
| `contour_render_specs.py` | Helper module | Per-board-type translators + `build_render_spec()`. No config; fully stack-agnostic. |
| Output dataset (you choose its path/RID via `OUTPUT_DATASET`) | Output dataset | One row per board with `render_spec_json`, raw state, and parameters. |

## Prerequisites

1. **A Contour analysis** you have read access to, and its RID (`ri.contour.main.analysis.<uuid>`).
2. **A Data Connection source** pointing at your Foundry stack's base URL, configured with:
   - An HTTPS connection to the stack.
   - A secret containing a Foundry **Personal Access Token (PAT)**. The PAT must belong to a user with read access to every analysis you intend to translate. You record this secret's *name* in the `PAT_SECRET_NAME` placeholder — **never put the token itself in the repo.**
   - The source's "allow import into code repositories" / export setting enabled, and the source imported into your transforms repo.
3. **A Foundry Python transforms repository** to host the two modules, and edit access to it.

## Configuration

All stack-specific values live in a single CONFIG block at the top of `contour_translator.py`. The shipped values are well-formed placeholders (all-zero UUIDs / a `REPLACE_ME` path) so the project parses out of the box but points at nothing real:

```python
FOUNDRY_STACK_SOURCE_RID = "ri.magritte..source.00000000-0000-0000-0000-000000000000"  # your Source RID
PAT_SECRET_NAME          = "REPLACE_ME_pat_secret_name"                                  # secret name on the Source
OUTPUT_DATASET           = "/REPLACE_ME/Your Project/contour_board_analysis"             # where to write output
ANALYSIS_RIDS            = ["ri.contour.main.analysis.00000000-0000-0000-0000-000000000000"]  # one or more
```

Replace each value with one from your own stack, import your source into the repo, and add as many analysis RIDs to `ANALYSIS_RIDS` as you want to translate in one build.

## Running the pipeline

1. Replace every placeholder in the CONFIG block and import your source.
2. Build the output dataset. The transform is `@lightweight` (single-node pandas), idempotent, and typically runs in under a minute.
3. **Auto-registration:** the transform only registers once *every* placeholder has been replaced. While any placeholder remains, the pipeline is intentionally empty so the project builds clean out of the box; filling in real values activates the transform automatically with no other code change.
4. If any analysis fails (e.g. 401/403), the build raises a clear error naming which RID(s) failed and distinguishing "PAT not recognized" from "PAT user lacks read access".

## Verifying the output

After a successful build the output dataset has one row per board. Columns to check: `render_spec_kind`, `is_renderable`, `render_spec_json`, `logic_description`, `board_state_json`. Unsupported board types appear as `render_spec_kind = "unsupported"`; translator bugs appear as `"error"` with the message inside `render_spec_json`. A quick sanity query:

```sql
SELECT render_spec_kind, COUNT(*) FROM <your_output_dataset> GROUP BY render_spec_kind
```

A well-supported analysis should show no `unsupported` or `error` rows.

## Adding support for a new board type

1. Open `contour_render_specs.py`.
2. Add a builder `_spec_<kind>(state)` returning a dict with at least `{"kind": "<kind>", ...}`.
3. Register it in `_BOARD_BUILDERS` (top-level board types) or in `_route_custom` (for `customBoardId`-driven custom boards).
4. Add the new kind to `RENDER_KINDS`, and to `_RENDERABLE_KINDS` if it should count as renderable.
5. Rebuild — the board type now produces a populated row instead of `unsupported`.

## Common errors and what they mean

| Error | Likely cause | Fix |
|---|---|---|
| ❌ `Default:Unauthorized` (HTTP 401) | PAT invalid, expired, or missing from the source secret | Regenerate the PAT and update the secret named in `PAT_SECRET_NAME` |
| ❌ `Contour:InsufficientPermission` (HTTP 403) on a specific analysis | The PAT user lacks read access to that analysis | Grant the PAT-owning user access, or regenerate the PAT under a user who has it |
| ⚠️ `render_spec_kind = "unsupported"` rows | Board type / `customBoardId` has no translator yet | Add one — see "Adding support for a new board type" |
| ⚠️ `render_spec_kind = "error"` rows | A translator threw at runtime; message is in `render_spec_json.error` | Fix the translator; the design is exception-safe so other boards keep working |
| ⚠️ Build succeeds but output is empty / transform didn't run | Placeholders not all replaced, so the transform isn't registered | Replace every value in the CONFIG block (see "Configuration") |

## Known limitations / out of scope

1. **Analysis boards only — no dashboard layout.** This template reads the analysis path's boards. The Contour HTTP endpoint that returns dashboard tile grid coordinates / dashboard-only cards is undocumented and not integrated here.
2. **Unsupported board types** are preserved (raw JSON kept in `board_state_json` / `board_view_state_json`) but not translated until you add a builder.
3. **One Foundry stack per build**, configured via the single source.

## Repository layout

This repo ships the two modules flat at the root. Drop them together into your Foundry transforms repo under `transforms-python/src/<your_package>/datasets/` — `contour_translator.py` imports `build_render_spec` from `contour_render_specs.py`, so they must live in the same package.

```
.
├── contour_translator.py     # Main reader: Contour API → output dataset (holds CONFIG)
├── contour_render_specs.py   # Per-board-type translators + build_render_spec()
└── README.md                 # ← this file
```
