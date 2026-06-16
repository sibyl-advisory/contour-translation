# Contour Translator

A **stack-agnostic** Foundry Python transform that reads Contour analyses
through the Foundry Contour HTTP API and produces a structured, board-by-board
breakdown of each analysis — including a human-readable description of what
each step (board) does, plus a normalized, OSDK-friendly "render spec" per
board.

It is designed to be dropped onto **any** Foundry stack: copy the two Python
files into a Foundry Python transforms repository, fill in a handful of
placeholder values, and build.

---

## What it produces

One output dataset (`contour_board_analysis` by convention) with **one row per
board (step) per analysis ref**. Notable columns:

| Column | Description |
| --- | --- |
| `analysis_rid`, `analysis_name` | The analysis the board belongs to. |
| `ref_name` | The Contour ref (query/tab) the board lives in. |
| `board_index`, `board_id`, `board_type`, `board_category` | Position, id, raw type, and a coarse class (`transform` / `visualization` / `input` / `other`). |
| `board_title` | The board's display title, if any. |
| `is_parameterized`, `is_disabled` | Whether the board uses a parameter / is disabled. |
| `logic_description` | **Human-readable** summary of the board's logic (e.g. ``Filter: `country` equals ['US','CA']``). |
| `render_spec_kind`, `is_renderable`, `render_spec_json` | A normalized, portable JSON spec for the board (see `contour_render_specs.py`). |
| `input_dataset_names`, `input_ref_names` | Upstream datasets / refs the board reads from. |
| `parameters_json` | Analysis-level parameter definitions. |
| `board_state_json`, `board_view_state_json` | Raw Contour JSON for anything not (yet) translated. |

---

## How it works

The transform authenticates to the Foundry REST API using a **Personal Access
Token (PAT)** stored as a secret on a Data Connection **Source**, then calls:

- `GET /contour/api/analyses/{analysisRid}` — analysis metadata + refs
- `GET /contour/api/refs/{refRid}` — ref name + head node id
- `GET /contour/api/refs/{refRid}/nodes/{headId}/board` — board snapshots
- `GET /compass/api/resources/{datasetRid}` — resolve dataset RIDs to names

It walks every board snapshot and translates the raw Contour `boardState` into
both a human-readable description and a stable render spec.

---

## Files

```
contour_translator.py     # main transform — API calls, board describers, output
contour_render_specs.py   # boardState -> normalized render-spec JSON (no config needed)
```

`contour_render_specs.py` is fully stack-agnostic and contains no RIDs, paths,
or secrets — it can be copied verbatim. `contour_translator.py` imports it; in a
Foundry repo both files live together under
`transforms-python/src/myproject/datasets/`.

---

## Setup

### 1. Add the files to a Foundry Python transforms repository
Copy both files into your transforms repo under
`transforms-python/src/myproject/datasets/`. `contour_translator.py` imports
`build_render_spec` from `contour_render_specs.py`, so keep them in the same
package.

### 2. Create a Source that points at your stack
In Data Connection, create a **REST / external system Source** whose base URL
is your Foundry stack, e.g. `https://<your-stack>.palantirfoundry.com`.

- Add a **secret** holding a Foundry **Personal Access Token (PAT)**. The PAT
  must belong to a user that can **read** the Contour analyses you want to
  translate. Note the secret's name.
- Enable the Source for **code repository usage** and **exports** so it can be
  used from a transform.

### 3. Import the Source into your repository
In the repo's **Libraries / source imports** panel, import the Source you
created so the transform can reference it.

### 4. Fill in the CONFIG block
Open `contour_translator.py` and replace every placeholder in the
`CONFIGURATION` section near the top. The shipped values are well-formed dummies
(all-zero UUIDs / a `REPLACE_ME` path) so the project parses out of the box —
but they point at nothing real, so the build fails until you replace them:

```python
FOUNDRY_STACK_SOURCE_RID = "ri.magritte..source.00000000-0000-0000-0000-000000000000"  # your Source RID
PAT_SECRET_NAME          = "REPLACE_ME_pat_secret_name"                                  # secret name on the Source
OUTPUT_DATASET           = "/REPLACE_ME/Your Project/contour_board_analysis"             # where to write output
ANALYSIS_RIDS = [
    "ri.contour.main.analysis.00000000-0000-0000-0000-000000000000",                     # your analysis RID(s)
]
```

> Don't forget to **import your Source** into the repository (Libraries /
> source imports panel) — otherwise the build will report a missing import.

### 5. Build
Build the output dataset. Each configured analysis becomes a set of rows in the
output. To add more analyses, append their RIDs to `ANALYSIS_RIDS`.

> **Auto-activation:** the transform is only **registered** once every
> placeholder has been replaced. With the shipped placeholders still in place,
> the pipeline is intentionally **empty** so the project builds cleanly out of
> the box (there is no real output dataset to create yet). As soon as you fill
> in your own values the transform activates automatically — no other code
> change needed.

---

## Notes & gotchas

- **Fail-fast on errors.** If any analysis returns a non-200 (e.g. HTTP 401/403
  from a rotated/insufficient PAT), the build raises rather than silently
  writing an empty dataset. The error message tells you which analysis failed
  and why.
- **PAT scope.** The translator can only read analyses the PAT's user can see.
  A `Contour:InsufficientPermission` (403) means the PAT user lacks access to
  that analysis.
- **Lightweight transform.** This runs as a single-node (`@lightweight`)
  pandas transform — no Spark profile required.
- **Unknown board types** are not lost: they are tagged `unsupported` in the
  render spec and their raw JSON is preserved in `board_state_json` /
  `board_view_state_json` so you can extend the translators later.
