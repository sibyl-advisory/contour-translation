"""
Contour Analysis Reader  (stack-agnostic template)
==================================================

Reads Contour analyses via the Foundry Contour HTTP API and produces a
structured dataset describing every board (step) in each analysis — its type,
configuration, whether it uses parameters, and a human-readable description of
its logic, plus a normalized OSDK-friendly "render spec" per board.

------------------------------------------------------------------------------
HOW TO USE THIS ON YOUR OWN STACK
------------------------------------------------------------------------------
1. Create a Data Connection REST/external "Source" that points at your own
   Foundry stack's base URL (e.g. https://<your-stack>.palantirfoundry.com),
   and store a Foundry Personal Access Token (PAT) as a secret on it.
   The PAT must belong to a user that can read the Contour analyses you want
   to translate.  Enable the source for code-repository usage and exports.
2. Import that Source into this repository (Libraries / source imports).
3. Fill in EVERY <PLACEHOLDER> in the CONFIGURATION block below.
4. Build the output dataset.

See README.md for a step-by-step walkthrough.
------------------------------------------------------------------------------
"""

from transforms.api import transform_pandas, Output, lightweight
from transforms.external.systems import external_systems, Source
import pandas as pd
import json
import logging

from myproject.datasets.contour_render_specs import build_render_spec

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION — replace every <PLACEHOLDER> to run on your own stack
# ============================================================================

# NOTE: the values below are well-formed PLACEHOLDERS (all-zero UUIDs / a
# REPLACE_ME path) so the project parses out-of-the-box. They do NOT point at
# anything real — replace each one with a value from your own stack before
# building, otherwise the build will fail with "resource not found".

# (1) The Data Connection Source RID that points at YOUR Foundry stack's REST
#     API. This is the external system the transform connects through to call
#     /contour/api/... and /compass/api/...
#     Remember to also IMPORT this Source into the repository (Libraries panel).
FOUNDRY_STACK_SOURCE_RID = "ri.magritte..source.00000000-0000-0000-0000-000000000000"

# (2) The name of the secret (on the Source above) that holds a Foundry
#     Personal Access Token with read access to the Contour analyses.
PAT_SECRET_NAME = "REPLACE_ME_pat_secret_name"

# (3) Where to write the output. A Foundry path or a dataset RID both work.
OUTPUT_DATASET = "/REPLACE_ME/Your Project/contour_board_analysis"

# (4) The Contour analysis RID(s) you want to read. Add as many as you like.
#     Find the RID in the Contour URL or via the resource's details panel.
ANALYSIS_RIDS = [
    "ri.contour.main.analysis.00000000-0000-0000-0000-000000000000",
]

API_TIMEOUT = 15  # seconds per API request


# Board types that perform data transformations
TRANSFORM_BOARD_TYPES = {
    "filter", "expression", "combine-columns", "find-and-replace",
    "pivot", "unpivot", "aggregate", "join", "union", "sort",
    "column-selection", "rename-columns", "cast-columns",
    "convert-types", "transpose", "sample", "limit",
    "flatten-arrays", "split-column", "deduplicate", "group-by",
    "pivot-table", "custom",
}

# Board types that are visualizations / display
VISUALIZATION_BOARD_TYPES = {
    "chart", "table", "map", "markdown", "details", "histogram",
}


# ============================================================================
# CONTOUR API HELPERS
# ============================================================================


class ContourApiError(RuntimeError):
    """Raised when a Contour API request returns non-200."""


def _raise_for_status(resp, what):
    if resp.status_code == 200:
        return
    snippet = (resp.text or "")[:200]
    raise ContourApiError(
        f"{what} returned HTTP {resp.status_code}. "
        f"Likely cause: the PAT secret on the Foundry source has rotated, "
        f"or the requested resource is not accessible to that PAT. "
        f"Body: {snippet}"
    )


def fetch_analysis(client, base_url, analysis_rid):
    """Fetch analysis metadata including all refs. Raises on non-200."""
    resp = client.get(
        f"{base_url}/contour/api/analyses/{analysis_rid}",
        timeout=API_TIMEOUT,
    )
    _raise_for_status(resp, f"fetch_analysis({analysis_rid})")
    return resp.json()


def fetch_ref_details(client, base_url, ref_rid):
    """Fetch ref metadata: name and head node ID. Raises on non-200."""
    resp = client.get(
        f"{base_url}/contour/api/refs/{ref_rid}",
        timeout=API_TIMEOUT,
    )
    _raise_for_status(resp, f"fetch_ref_details({ref_rid})")
    ref_data = resp.json()
    ref_name = ref_data.get("name", ref_rid)
    head_id = ref_data.get("head", {}).get("id")
    return ref_name, head_id


def fetch_board_snapshots(client, base_url, ref_rid, head_id):
    """Fetch all board snapshots for a ref's head node. Raises on non-200."""
    resp = client.get(
        f"{base_url}/contour/api/refs/{ref_rid}/nodes/{head_id}/board",
        timeout=API_TIMEOUT,
    )
    _raise_for_status(resp, f"fetch_board_snapshots({ref_rid}/{head_id})")
    return resp.json()


def resolve_dataset_name(client, base_url, dataset_rid):
    """Resolve a dataset RID to its human-readable name via the Compass API."""
    try:
        resp = client.get(
            f"{base_url}/compass/api/resources/{dataset_rid}",
            timeout=API_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("name", dataset_rid)
    except Exception as e:
        logger.debug("Could not resolve dataset name for %s: %s", dataset_rid, e)
    return None


def resolve_dataset_names_batch(client, base_url, dataset_rids):
    """
    Resolve multiple dataset RIDs to names. Returns dict of rid -> name.
    Falls back to individual lookups if batch fails.
    """
    rid_to_name = {}
    for rid in dataset_rids:
        name = resolve_dataset_name(client, base_url, rid)
        if name:
            rid_to_name[rid] = name
    return rid_to_name


def extract_parameters(analysis_data):
    """
    Extract analysis-level parameter definitions from the analysis response.
    Returns a list of dicts with parameter id, name, type, default value.
    """
    params = []
    parameters = analysis_data.get("parameters", analysis_data.get("parameterDefinitions", []))
    if isinstance(parameters, dict):
        for pid, pdef in parameters.items():
            params.append({
                "parameter_id": pid,
                "parameter_name": pdef.get("name", pdef.get("label", pid)),
                "parameter_type": pdef.get("type", pdef.get("@type", "unknown")),
                "default_value": str(pdef.get("defaultValue", pdef.get("default", ""))),
            })
    elif isinstance(parameters, list):
        for pdef in parameters:
            pid = pdef.get("id", pdef.get("parameterId", ""))
            params.append({
                "parameter_id": pid,
                "parameter_name": pdef.get("name", pdef.get("label", pid)),
                "parameter_type": pdef.get("type", pdef.get("@type", "unknown")),
                "default_value": str(pdef.get("defaultValue", pdef.get("default", ""))),
            })
    return params


# ============================================================================
# PARAMETER DETECTION
# ============================================================================


def has_parameter(obj):
    """Recursively check if any 'parameterId' key exists in a nested dict/list."""
    if isinstance(obj, dict):
        if "parameterId" in obj:
            return True
        return any(has_parameter(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_parameter(item) for item in obj)
    return False


# ============================================================================
# BOARD DESCRIBERS — one function per board type
# ============================================================================


def describe_filter(state):
    """Describe a filter board's logic."""
    fg = state.get("filterGroup", {})
    operation = fg.get("operation", "AND")
    parts = []
    for ft in fg.get("filterTypes", []):
        ftype = ft.get("type", "unknown")

        if ftype == "alpha-filter":
            cols = [c.get("name", "?") for c in ft.get("columns", [])]
            vals = [t["value"] for t in ft.get("terms", []) if t.get("type") == "concrete"]
            col_s = ", ".join(f"`{c}`" for c in cols)
            # Truncate at 50. Curated whitelists were getting cut off, hiding
            # which values were included.
            val_s = ", ".join(f"'{v}'" for v in vals[:50])
            if len(vals) > 50:
                val_s += f" ... (+{len(vals) - 50} more)"
            mode = "contains" if ft.get("contains") else "equals"
            parts.append(f"{col_s} {mode} [{val_s}]")

        elif ftype == "null-filter":
            col = ft.get("column", {}).get("name", "?")
            parts.append(f"remove nulls from `{col}`")

        elif ftype == "date-filter":
            col = ft.get("column", {}).get("name", "?")
            comp = ft.get("comparison", "")
            parts.append(f"`{col}` {comp}")

        elif ftype == "numeric-filter":
            col = ft.get("column", {}).get("name", "?")
            mn, mx = ft.get("min"), ft.get("max")
            if mn is not None and mx is not None:
                parts.append(f"`{col}` between {mn} and {mx}")
            elif mn is not None:
                parts.append(f"`{col}` >= {mn}")
            elif mx is not None:
                parts.append(f"`{col}` <= {mx}")

        else:
            parts.append(ftype)

    joiner = f" {operation} "
    return f"Filter: {joiner.join(parts)}" if parts else "Filter (empty)"


def describe_expression(state):
    """Describe an expression (computed column) board."""
    exprs = state.get("expressions", [])
    if not exprs:
        return "Expression (empty)"
    parts = [
        f"`{e.get('columnName', '?')}` = {e.get('expression', '?')}"
        for e in exprs
    ]
    return "Computed columns: " + "; ".join(parts)


def describe_combine_columns(state):
    """Describe a combine-columns board."""
    cols = state.get("inputColumns", [])
    delim = state.get("delimiter", "")
    col_s = ", ".join(f"`{c}`" for c in cols)
    return f"Combine columns [{col_s}] with delimiter '{delim}'"


def describe_find_and_replace(state):
    """Describe a find-and-replace board."""
    cols = state.get("columns", [])
    find = state.get("regexToFind", "")
    replace = state.get("replacement", "")
    col_s = ", ".join(f"`{c}`" for c in cols)
    return f"In [{col_s}]: replace '{find}' -> '{replace}'"


def describe_pivot(state):
    """Describe a pivot board."""
    group_cols = state.get("groupByColumns", [])
    pivot_col = state.get("pivotColumn", {}).get("name", "?")
    value_col = state.get("valueColumn", {}).get("name", "?")
    agg = state.get("aggregation", "?")
    group_s = ", ".join(f"`{c}`" for c in group_cols) if group_cols else "none"
    return f"Pivot: group by [{group_s}], pivot on `{pivot_col}`, {agg}(`{value_col}`)"


def describe_unpivot(state):
    """Describe an unpivot board."""
    cols = state.get("columns", [])
    col_s = ", ".join(f"`{c}`" for c in cols[:5])
    if len(cols) > 5:
        col_s += f" ... (+{len(cols) - 5} more)"
    return f"Unpivot columns: [{col_s}]"


def describe_aggregate(state):
    """Describe an aggregate / group-by board."""
    group_cols = state.get("groupByColumns", state.get("groupColumns", []))
    aggs = state.get("aggregations", [])
    group_s = ", ".join(f"`{c}`" for c in group_cols) if group_cols else "none"
    agg_parts = []
    for a in aggs[:5]:
        func = a.get("function", a.get("type", "?"))
        col = a.get("column", a.get("columnName", "?"))
        alias = a.get("alias", a.get("outputName", ""))
        part = f"{func}(`{col}`)"
        if alias:
            part += f" as `{alias}`"
        agg_parts.append(part)
    if len(aggs) > 5:
        agg_parts.append(f"... (+{len(aggs) - 5} more)")
    return f"Group by [{group_s}]: {', '.join(agg_parts)}" if agg_parts else f"Group by [{group_s}]"


def describe_join(state):
    """Describe a join board."""
    join_type = state.get("joinType", "?")
    conditions = state.get("joinConditions", state.get("conditions", []))
    cond_parts = []
    for c in conditions:
        left = c.get("leftColumn", c.get("left", "?"))
        right = c.get("rightColumn", c.get("right", "?"))
        cond_parts.append(f"`{left}` = `{right}`")
    cond_s = ", ".join(cond_parts) if cond_parts else "?"
    return f"{join_type} join on {cond_s}"


def describe_union(state):
    """Describe a union board."""
    mode = state.get("unionType", state.get("mode", "union"))
    return f"Union ({mode})"


def describe_sort(state):
    """Describe a sort board."""
    sorts = state.get("sortColumns", state.get("sorts", []))
    parts = []
    for s in sorts:
        col = s.get("column", s.get("columnName", "?"))
        direction = s.get("direction", s.get("order", "asc"))
        parts.append(f"`{col}` {direction}")
    return f"Sort by: {', '.join(parts)}" if parts else "Sort (empty)"


def describe_column_selection(state):
    """Describe a column-selection board."""
    hidden = state.get("hiddenColumns", [])
    shown = state.get("shownColumns", state.get("selectedColumns", []))
    if hidden:
        cols = ", ".join(f"`{c}`" for c in hidden[:10])
        extra = f" ... (+{len(hidden) - 10} more)" if len(hidden) > 10 else ""
        return f"Hide columns: [{cols}{extra}]"
    if shown:
        cols = ", ".join(f"`{c}`" for c in shown[:10])
        extra = f" ... (+{len(shown) - 10} more)" if len(shown) > 10 else ""
        return f"Show columns: [{cols}{extra}]"
    return "Column selection"


def describe_rename_columns(state):
    """Describe a rename-columns board."""
    renames = state.get("renames", state.get("columnRenames", {}))
    if isinstance(renames, dict):
        parts = [f"`{old}` -> `{new}`" for old, new in list(renames.items())[:5]]
    elif isinstance(renames, list):
        parts = [f"`{r.get('from', '?')}` -> `{r.get('to', '?')}`" for r in renames[:5]]
    else:
        parts = []
    return f"Rename: {', '.join(parts)}" if parts else "Rename columns"


def describe_cast_columns(state):
    """Describe a cast-columns / convert-types board."""
    casts = state.get("casts", state.get("conversions", []))
    if isinstance(casts, list):
        parts = [
            f"`{c.get('column', '?')}` -> {c.get('type', c.get('targetType', '?'))}"
            for c in casts[:5]
        ]
    elif isinstance(casts, dict):
        parts = [f"`{col}` -> {typ}" for col, typ in list(casts.items())[:5]]
    else:
        parts = []
    return f"Cast types: {', '.join(parts)}" if parts else "Cast columns"


def describe_limit(state):
    """Describe a limit / sample board."""
    n = state.get("limit", state.get("count", state.get("n", "?")))
    return f"Limit to {n} rows"


def describe_deduplicate(state):
    """Describe a deduplicate board."""
    cols = state.get("columns", state.get("deduplicateColumns", []))
    col_s = ", ".join(f"`{c}`" for c in cols) if cols else "all columns"
    return f"Deduplicate on [{col_s}]"


def describe_dataset_board(state):
    """Describe a dataset input board."""
    rid = state.get("datasetRid", state.get("rid", "?"))
    branch = state.get("branch", "")
    desc = f"Input dataset: {rid}"
    if branch:
        desc += f" (branch: {branch})"
    return desc


def describe_object_set(state):
    """Describe an object-set board."""
    obj_type = state.get("objectTypeId", state.get("objectType", "?"))
    return f"Object set: {obj_type}"


# ============================================================================
# CUSTOM BOARD DESCRIBERS
# ============================================================================


def describe_custom_join(internal_state, external_sets):
    """
    Describe a custom join-columns board.

    Extracts join type, join conditions, and the source being joined to.
    """
    # Determine join type
    combine_type = internal_state.get("combineType", "INTERSECTION")
    join_type_map = {
        "INTERSECTION": "Inner",
        "LEFT_OUTER": "Left",
        "RIGHT_OUTER": "Right",
        "FULL_OUTER": "Full outer",
        "ANTI": "Anti",
        "CROSS": "Cross",
    }
    join_type = join_type_map.get(combine_type, combine_type)

    # Extract join conditions
    match_conditions = internal_state.get("matchConditions", [])
    cond_parts = []
    for mc in match_conditions:
        src_col = mc.get("sourceColumn", {}).get("name", "?")
        join_col = mc.get("joinedColumn", {}).get("name", "?")
        cond_parts.append(f"`{src_col}` = `{join_col}`")

    conditions_str = ", ".join(cond_parts) if cond_parts else "?"

    # Identify what we're joining to
    join_target = ""
    incoming = internal_state.get("incomingSetWithDescription", {})
    lat_set = incoming.get("latitudeSet", {})
    identifier = lat_set.get("identifier", "")
    if identifier:
        join_target = f" -> {identifier}"

    # Check external sets for ref references
    if external_sets and not join_target:
        for ext_name, ext_val in external_sets.items():
            if isinstance(ext_val, dict):
                ref_rid = ext_val.get("refRid", {}).get("rid", "")
                if ref_rid:
                    join_target = f" -> ref:{ref_rid}"
                    break

    return f"{join_type} join on [{conditions_str}]{join_target}"


def describe_custom_calculation(internal_state):
    """
    Describe a custom calculation board.

    Extracts aggregation type and column for each calculation.
    """
    calc_map = internal_state.get("calculationResultConfigMap", {})
    if not calc_map:
        return "Calculation (empty)"

    parts = []
    for calc_id, config in list(calc_map.items())[:10]:
        agg_type = config.get("selectedAggregateType", "?")
        col = config.get("selectedColumn", {}).get("name", "?")
        parts.append(f"{agg_type}(`{col}`)")

    if len(calc_map) > 10:
        parts.append(f"... (+{len(calc_map) - 10} more)")

    return f"Calculation: {', '.join(parts)}"


def _extract_col_name(col):
    """Extract a column name from a string or a column info dict."""
    if isinstance(col, dict):
        return col.get("name", col.get("identifier", str(col)[:40]))
    return str(col)


def describe_custom_bulk_column_editor(internal_state):
    """
    Describe a bulk-column-editor board.

    Extracts renamed columns, removed columns, and kept columns.
    Handles both dict and list formats for renamedColumns.
    """
    parts = []

    # Renamed columns — can be a dict {old: new} or a list of {from, to} objects
    renamed = internal_state.get("renamedColumns", {})
    if renamed:
        if isinstance(renamed, dict):
            rename_parts = [f"`{old}` -> `{new}`" for old, new in list(renamed.items())[:8]]
            rename_count = len(renamed)
        elif isinstance(renamed, list):
            rename_parts = [
                f"`{r.get('from', r.get('oldName', '?'))}` -> `{r.get('to', r.get('newName', '?'))}`"
                for r in renamed[:8]
            ]
            rename_count = len(renamed)
        else:
            rename_parts = []
            rename_count = 0

        if rename_count > 8:
            rename_parts.append(f"... (+{rename_count - 8} more)")
        if rename_parts:
            parts.append(f"Rename: {', '.join(rename_parts)}")

    # Removed columns — can be a list of strings or column info dicts
    removed = internal_state.get("removedColumns", [])
    if removed:
        rem_names = [_extract_col_name(c) for c in removed[:8]]
        rem_cols = ", ".join(f"`{n}`" for n in rem_names)
        extra = f" ... (+{len(removed) - 8} more)" if len(removed) > 8 else ""
        parts.append(f"Remove: [{rem_cols}{extra}]")

    # Kept columns (only show if no removed — otherwise it's redundant)
    if not removed:
        kept = internal_state.get("keptColumns", [])
        if kept:
            kept_names = [_extract_col_name(c) for c in kept[:8]]
            kept_cols = ", ".join(f"`{n}`" for n in kept_names)
            extra = f" ... (+{len(kept) - 8} more)" if len(kept) > 8 else ""
            parts.append(f"Keep: [{kept_cols}{extra}]")

    # Dedup flag
    if internal_state.get("removeDuplicateRows"):
        parts.append("+ deduplicate rows")

    return f"Column editor: {'; '.join(parts)}" if parts else "Column editor (no changes)"


def describe_custom_join_rows(internal_state, external_sets):
    """
    Describe a join-rows (union/append) board.

    Extracts the combine type and the source being unioned with.
    """
    combine_type = internal_state.get("combineType", "UNION")
    combine_map = {
        "UNION": "Union all",
        "INTERSECTION": "Intersect",
        "SUBTRACT": "Subtract",
    }
    combine_str = combine_map.get(combine_type, combine_type)

    # Identify what we're combining with
    source = ""
    incoming = internal_state.get("incomingSetWithDescription", {})
    lat_set = incoming.get("latitudeSet", {})
    identifier = lat_set.get("identifier", "")
    if identifier:
        source = f" with {identifier}"

    if external_sets and not source:
        for ext_name, ext_val in external_sets.items():
            if isinstance(ext_val, dict):
                ref_rid = ext_val.get("refRid", {}).get("rid", "")
                if ref_rid:
                    source = f" with ref:{ref_rid}"
                    break

    return f"{combine_str}{source}"


def describe_custom_sort_columns(internal_state):
    """
    Describe a sort-columns board.

    Extracts column sort specifications.
    """
    sorts = internal_state.get("columnsToSort", [])
    parts = []
    for s in sorts[:8]:
        col = s.get("column", s.get("columnName", "?"))
        if isinstance(col, dict):
            col = col.get("name", "?")
        direction = s.get("direction", s.get("order", "asc"))
        parts.append(f"`{col}` {direction}")

    if len(sorts) > 8:
        parts.append(f"... (+{len(sorts) - 8} more)")

    limit_info = ""
    if internal_state.get("applyLimit"):
        limit_info = " (with row limit)"

    return f"Sort: {', '.join(parts)}{limit_info}" if parts else "Sort (empty)"


def describe_custom(state):
    """
    Route custom boards to specific describers based on customBoardId.
    Falls back to generic description for unknown custom board types.
    """
    custom_board_id = state.get("customBoardId", "")
    internal_state = state.get("internalState", {})
    external_sets = state.get("externalSetsByName", {})

    if custom_board_id == "join-columns":
        return describe_custom_join(internal_state, external_sets)
    elif custom_board_id == "calculation":
        return describe_custom_calculation(internal_state)
    elif custom_board_id == "bulk-column-editor":
        return describe_custom_bulk_column_editor(internal_state)
    elif custom_board_id == "join-rows":
        return describe_custom_join_rows(internal_state, external_sets)
    elif custom_board_id == "sort-columns":
        return describe_custom_sort_columns(internal_state)
    elif custom_board_id in ("chart", "markdown"):
        # Visualization / documentation — not data logic
        return f"[Visualization: {custom_board_id}]"
    elif custom_board_id:
        # Unknown custom board type — show the ID and top-level internal keys
        int_keys = sorted(internal_state.keys())[:8] if internal_state else []
        key_str = f" (internal keys: {', '.join(int_keys)})" if int_keys else ""
        return f"Custom [{custom_board_id}]{key_str}"
    else:
        return describe_generic("custom", state)


# ============================================================================
# PIVOT-TABLE DESCRIBER
# ============================================================================


def _unwrap_column(col_wrapper):
    """
    Render a column wrapper into a human-readable label.

    Handles wrappers we see in practice:
      - BasicColumnInfoV2 → just the name
      - AliasedColumnV1 → name (or aliased identifier if it differs)
      - TimeBucketColumnInfoV1 → `name (by month, tz=America/Chicago)`
      - ExpressionColumnInfoV2 → `(expression)`
    Returns "?" when nothing parseable is found.
    """
    if not isinstance(col_wrapper, dict):
        return "?"

    cls = col_wrapper.get("@class", "")

    # AliasedColumnV1 wraps an inner column under "column"; use the inner column
    # name but surface a non-trivial alias if it differs.
    if "AliasedColumn" in cls:
        inner = _unwrap_column(col_wrapper.get("column", {}))
        alias = col_wrapper.get("identifier")
        if alias and alias != inner.strip("`"):
            return f"{inner} as `{alias}`"
        return inner

    # TimeBucketColumnInfoV1: column + timeBucket + timeZone
    if "TimeBucket" in cls or col_wrapper.get("timeBucket") is not None:
        inner = _unwrap_column(col_wrapper.get("column", col_wrapper))
        bucket = col_wrapper.get("timeBucket", "?")
        tz = col_wrapper.get("timeZone")
        suffix = f"by {bucket.lower()}" if isinstance(bucket, str) else "by ?"
        if tz:
            suffix += f", tz={tz}"
        return f"{inner} ({suffix})"

    # ExpressionColumnInfoV2: surface the expression text if it's flat
    if "Expression" in cls:
        expr = col_wrapper.get("expression")
        if isinstance(expr, str):
            return f"`({expr})`"
        if isinstance(expr, dict):
            return f"`(expr {expr.get('type','?')})`"
        return "`(expression)`"

    # BasicColumnInfoV2 / fallback
    name = col_wrapper.get("name") or col_wrapper.get("identifier")
    return f"`{name}`" if name else "?"


def describe_pivot_table(state):
    """
    Describe a pivot-table board.

    Extracts row groupings, column groupings, and aggregation functions
    including custom expression aggregates. Time-bucketed columns are
    surfaced as e.g. ``registration_date (by month, tz=America/Chicago)``
    rather than the bare ``?`` placeholder, so dashboards' date dimensions
    are visible without inspecting raw board JSON.
    """
    row_aggs = state.get("rowAggregates", [])
    col_aggs = state.get("columnAggregates", [])
    aggregates = state.get("aggregates", [])

    row_cols = [_unwrap_column(ra.get("column", {})) for ra in row_aggs]
    row_str = ", ".join(row_cols) if row_cols else "none"

    col_cols = [_unwrap_column(ca.get("column", {})) for ca in col_aggs]
    col_str = ", ".join(col_cols) if col_cols else "none"

    agg_parts = []
    for agg in aggregates[:10]:
        agg_type_tag = agg.get("@type", "")

        if "Expression" in agg_type_tag:
            expr = agg.get("expression", "?")
            col_name = agg.get("columnName", "?")
            agg_parts.append(f"EXPR(`{col_name}` = {expr})")
        else:
            func = agg.get("type", "?")
            col_label = _unwrap_column(agg.get("column", {}))
            agg_parts.append(f"{func}({col_label})")

    if len(aggregates) > 10:
        agg_parts.append(f"... (+{len(aggregates) - 10} more)")

    agg_str = ", ".join(agg_parts) if agg_parts else "none"

    return f"Pivot-table: rows=[{row_str}], cols=[{col_str}], aggs=[{agg_str}]"


# ============================================================================
# STARTING BOARD DESCRIBER (with ref resolution)
# ============================================================================


def describe_starting_board(state, ref_rid_to_name=None, dataset_rid_to_name=None):
    """
    Describe a starting board, resolving dataset RIDs to names and
    ref RIDs to ref names where possible.
    """
    if ref_rid_to_name is None:
        ref_rid_to_name = {}
    if dataset_rid_to_name is None:
        dataset_rid_to_name = {}

    desc = state.get("startingSetDescription", {})
    desc_type = desc.get("type", "")

    if desc_type == "initialwithtransaction":
        # Direct dataset reference
        dataset_rid = desc.get("identifier", "?")
        txn_rid = desc.get("transactionRid", "")
        dataset_name = dataset_rid_to_name.get(dataset_rid, "")
        name_part = f" ({dataset_name})" if dataset_name else ""
        txn_part = f" @txn:{txn_rid[-12:]}" if txn_rid else ""
        return f"Input dataset: {dataset_rid}{name_part}{txn_part}"

    elif desc_type == "refIdentified":
        # Reference to another ref/tab
        ref_rid = desc.get("refRid", {}).get("rid", "?")
        ref_name = ref_rid_to_name.get(ref_rid, "")
        name_part = f" ({ref_name})" if ref_name else ""
        return f"Input from ref: {ref_rid}{name_part}"

    elif desc_type == "initial":
        dataset_rid = desc.get("identifier", "?")
        dataset_name = dataset_rid_to_name.get(dataset_rid, "")
        name_part = f" ({dataset_name})" if dataset_name else ""
        return f"Input dataset: {dataset_rid}{name_part}"

    else:
        return f"Starting board ({desc_type}): {json.dumps(desc, default=str)[:200]}"


def describe_histogram(state):
    """
    Describe a histogram board.

    Beyond the static groupBy + aggregate, surface ``selection`` info if the
    user has clicked bars to apply an interactive filter (selectionBars,
    selectionMode). Histograms in Contour double as filters — this is the
    only way to see whether end users have an interactive filter applied
    that is invisible from the static analysis state.
    """
    gb_col = _unwrap_column(state.get("groupBy", {}))
    agg_type = state.get("aggregateType", "?")
    agg_col_wrapper = state.get("aggregate")
    agg_label = (
        f"{agg_type}({_unwrap_column(agg_col_wrapper)})"
        if agg_col_wrapper else agg_type
    )

    # Interactive filter state: are bars selected to filter downstream?
    sel_bars = state.get("selectionBars") or []
    sel_mode = state.get("selectionMode")
    selection = ""
    if sel_bars:
        # selectionBars items are typically {"value": "X"} dicts
        vals = [b.get("value") if isinstance(b, dict) else str(b) for b in sel_bars[:50]]
        more = f" ... (+{len(sel_bars)-50} more)" if len(sel_bars) > 50 else ""
        selection = f"; selection={sel_mode or 'KEEP'} [{', '.join(repr(v) for v in vals)}{more}]"

    return f"Histogram: groupBy={gb_col}, agg={agg_label}{selection}"


def describe_table(state):
    """Describe a table board (mostly displays parent data; surface key flags)."""
    flags = []
    if state.get("isDisabled"):
        flags.append("disabled")
    if state.get("limitNumberOfComputedColumns"):
        flags.append("col-limited")
    pcols = state.get("prioritizedColumnsForComputation") or []
    if pcols:
        names = [_unwrap_column(c) for c in pcols[:5]]
        more = f" +{len(pcols)-5}" if len(pcols) > 5 else ""
        flags.append(f"prioritized=[{', '.join(names)}{more}]")
    return "Table" + (f" ({'; '.join(flags)})" if flags else "")


def describe_chart(state):
    """Describe a chart board: chart type + main encodings."""
    ct = state.get("chartType") or state.get("type") or "?"
    layers = state.get("layers", [])
    encodings = []
    for layer in layers[:3]:
        x = layer.get("x") or layer.get("xAxis")
        y = layer.get("y") or layer.get("yAxis")
        if x or y:
            encodings.append(
                f"x={_unwrap_column(x or {})}, y={_unwrap_column(y or {})}"
            )
    enc_str = "; ".join(encodings) if encodings else ""
    return f"Chart [{ct}]" + (f" {enc_str}" if enc_str else "")


def describe_markdown(state):
    """Describe a markdown board: surface the first line as the title hint."""
    md = state.get("markdown") or state.get("content") or ""
    if isinstance(md, str) and md.strip():
        first_line = md.strip().splitlines()[0][:120]
        return f"Markdown: {first_line!r}"
    return "Markdown (empty)"


def describe_details(state):
    """Describe a details board: which columns are shown."""
    cols = state.get("columns") or state.get("displayedColumns") or []
    if cols:
        names = [_unwrap_column(c) for c in cols[:5]]
        more = f" +{len(cols)-5}" if len(cols) > 5 else ""
        return f"Details: cols=[{', '.join(names)}{more}]"
    return "Details"


def describe_generic(board_type, state):
    """Fallback description for any unrecognized board type."""
    keys = sorted(state.keys())[:8] if state else []
    if keys:
        return f"{board_type} (config keys: {', '.join(keys)})"
    return board_type


# ---- Registry mapping board type -> describer function ----
# Note: 'custom', 'pivot-table', and 'starting' use specialized functions
# that need extra context (ref mapping, dataset names), so they are
# handled separately in describe_board().
BOARD_DESCRIBERS = {
    "filter": describe_filter,
    "expression": describe_expression,
    "combine-columns": describe_combine_columns,
    "find-and-replace": describe_find_and_replace,
    "pivot": describe_pivot,
    "unpivot": describe_unpivot,
    "aggregate": describe_aggregate,
    "group-by": describe_aggregate,
    "join": describe_join,
    "union": describe_union,
    "sort": describe_sort,
    "column-selection": describe_column_selection,
    "rename-columns": describe_rename_columns,
    "cast-columns": describe_cast_columns,
    "convert-types": describe_cast_columns,
    "limit": describe_limit,
    "sample": describe_limit,
    "deduplicate": describe_deduplicate,
    "dataset": describe_dataset_board,
    "object-set": describe_object_set,
    # Visualization boards — important: histogram captures interactive
    # selection state, which is the only way to see filter clicks that are
    # otherwise invisible from the static analysis state.
    "histogram": describe_histogram,
    "table": describe_table,
    "chart": describe_chart,
    "markdown": describe_markdown,
    "details": describe_details,
}


def describe_board(board_type, board_state, ref_rid_to_name=None, dataset_rid_to_name=None):
    """
    Get a human-readable description of any board type.
    For custom, pivot-table, and starting boards, uses enhanced describers
    that resolve references.
    """
    # Specialized describers that need extra context
    if board_type == "custom":
        try:
            return describe_custom(board_state)
        except Exception as e:
            return f"custom (parse error: {e})"

    if board_type == "pivot-table":
        try:
            return describe_pivot_table(board_state)
        except Exception as e:
            return f"pivot-table (parse error: {e})"

    if board_type == "starting":
        try:
            return describe_starting_board(board_state, ref_rid_to_name, dataset_rid_to_name)
        except Exception as e:
            return f"starting (parse error: {e})"

    # Standard describers
    describer = BOARD_DESCRIBERS.get(board_type)
    if describer:
        try:
            return describer(board_state)
        except Exception as e:
            return f"{board_type} (parse error: {e})"
    return describe_generic(board_type, board_state)


def classify_board(board_type):
    """Classify a board as 'transform', 'visualization', 'input', or 'other'."""
    if board_type in TRANSFORM_BOARD_TYPES:
        return "transform"
    if board_type in VISUALIZATION_BOARD_TYPES:
        return "visualization"
    if board_type in {"dataset", "object-set", "streaming", "starting"}:
        return "input"
    return "other"


# ============================================================================
# DATASET RID COLLECTOR — gather all dataset RIDs for batch resolution
# ============================================================================


def collect_dataset_rids(snapshots):
    """
    Scan all board snapshots and collect dataset RIDs referenced
    in starting boards and custom join boards.
    """
    rids = set()
    for snap in snapshots:
        board_state = snap.get("boardState", {})
        board_type = snap.get("boardType", "")

        # Starting boards
        if board_type == "starting":
            desc = board_state.get("startingSetDescription", {})
            identifier = desc.get("identifier", "")
            if identifier and identifier.startswith("ri.foundry.main.dataset."):
                rids.add(identifier)

        # Custom join boards reference datasets
        if board_type == "custom":
            internal = board_state.get("internalState", {})
            incoming = internal.get("incomingSetWithDescription", {})
            lat_set = incoming.get("latitudeSet", {})
            identifier = lat_set.get("identifier", "")
            if identifier and identifier.startswith("ri.foundry.main.dataset."):
                rids.add(identifier)

    return rids


# ============================================================================
# MAIN ANALYSIS FUNCTION
# ============================================================================


def analyze_contour(client, base_url, analysis_rid):
    """
    Read a full Contour analysis and return a list of board records.

    For each board (step) in each ref of the analysis, produces a record with:
        - analysis_rid / analysis_name
        - ref_name
        - board_index, board_id, board_type, board_category
        - board_title
        - is_parameterized, is_disabled
        - logic_description  (human-readable summary)
        - render_spec_kind / is_renderable / render_spec_json
        - input_dataset_names (comma-separated dataset names referenced)
        - input_ref_names    (comma-separated ref names referenced)
        - parameters_json    (analysis-level parameter definitions)
        - board_state_json / board_view_state_json (raw JSON for inspection)
    """
    analysis_data = fetch_analysis(client, base_url, analysis_rid)
    if not analysis_data:
        return [], []

    analysis_name = analysis_data.get("name", analysis_rid)
    refs = analysis_data.get("refs", [])
    if not refs:
        logger.warning("No refs found for analysis %s", analysis_rid)
        return [], []

    # Extract analysis-level parameters
    parameters = extract_parameters(analysis_data)
    parameters_json = json.dumps(parameters, default=str) if parameters else "[]"

    # ---- Phase 1: Build ref RID -> ref name mapping ----
    ref_rid_to_name = {}
    ref_rid_to_head = {}
    for ref in refs:
        ref_rid = ref.get("rid", ref) if isinstance(ref, dict) else ref
        ref_name, head_id = fetch_ref_details(client, base_url, ref_rid)
        if ref_name:
            ref_rid_to_name[ref_rid] = ref_name
        if head_id:
            ref_rid_to_head[ref_rid] = head_id

    # ---- Phase 2: Fetch all board snapshots and collect dataset RIDs ----
    ref_snapshots = {}
    all_dataset_rids = set()
    for ref_rid, head_id in ref_rid_to_head.items():
        board_data = fetch_board_snapshots(client, base_url, ref_rid, head_id)
        if board_data:
            snapshots = board_data.get("snapshots", [])
            ref_snapshots[ref_rid] = snapshots
            all_dataset_rids.update(collect_dataset_rids(snapshots))

    # ---- Phase 3: Resolve dataset names ----
    dataset_rid_to_name = {}
    if all_dataset_rids:
        logger.info("Resolving %d dataset names...", len(all_dataset_rids))
        dataset_rid_to_name = resolve_dataset_names_batch(
            client, base_url, all_dataset_rids
        )

    # ---- Phase 4: Build board records ----
    records = []
    for ref_rid, snapshots in ref_snapshots.items():
        ref_name = ref_rid_to_name.get(ref_rid, ref_rid)

        for idx, snap in enumerate(snapshots):
            board_type = snap.get("boardType", "unknown")
            board_state = snap.get("boardState", {})
            board_id = snap.get("id", "")
            is_disabled = board_state.get("isDisabled", False)
            is_parameterized = has_parameter(board_state)

            # Extract title from view state
            title = None
            view_state = snap.get("boardViewState")
            if not isinstance(view_state, dict):
                view_state = None
            if view_state is not None:
                title = view_state.get("boardTitle")

            # Build human-readable description (with resolved references)
            logic_desc = describe_board(
                board_type, board_state, ref_rid_to_name, dataset_rid_to_name
            )

            # Build normalized OSDK-friendly render spec.
            # build_render_spec is exception-safe (returns kind="error" on
            # failure), so it can never break the build.
            render_spec = build_render_spec(
                board_type, board_state, view_state, title
            )
            try:
                render_spec_json = json.dumps(
                    render_spec, separators=(",", ":"), default=str
                )
            except Exception:
                render_spec_json = json.dumps(
                    {"specVersion": "1", "kind": "error",
                     "title": title, "isRenderable": False,
                     "error": "render_spec serialization failed"}
                )
            render_spec_kind = render_spec.get("kind", "error")
            is_renderable = bool(render_spec.get("isRenderable", False))

            # Collect referenced dataset RIDs and ref names for this board
            input_dataset_rids = []
            input_ref_names = []

            if board_type == "starting":
                desc = board_state.get("startingSetDescription", {})
                desc_type = desc.get("type", "")
                if desc_type in ("initialwithtransaction", "initial"):
                    ds_rid = desc.get("identifier", "")
                    if ds_rid:
                        ds_name = dataset_rid_to_name.get(ds_rid, ds_rid)
                        input_dataset_rids.append(ds_name)
                elif desc_type == "refIdentified":
                    rrid = desc.get("refRid", {}).get("rid", "")
                    rname = ref_rid_to_name.get(rrid, rrid)
                    input_ref_names.append(rname)

            if board_type == "custom":
                internal = board_state.get("internalState", {})
                incoming = internal.get("incomingSetWithDescription", {})
                lat_set = incoming.get("latitudeSet", {})
                ds_rid = lat_set.get("identifier", "")
                if ds_rid and ds_rid.startswith("ri.foundry.main.dataset."):
                    ds_name = dataset_rid_to_name.get(ds_rid, ds_rid)
                    input_dataset_rids.append(ds_name)

                ext_sets = board_state.get("externalSetsByName", {})
                for ext_name, ext_val in ext_sets.items():
                    if isinstance(ext_val, dict):
                        rrid = ext_val.get("refRid", {}).get("rid", "")
                        if rrid:
                            rname = ref_rid_to_name.get(rrid, rrid)
                            input_ref_names.append(rname)

            # Serialize board state to JSON for raw inspection
            try:
                state_json = json.dumps(board_state, separators=(",", ":"), default=str)
            except Exception:
                state_json = "{}"

            # boardViewState carries things not present in boardState — most
            # importantly the markdown body for custom markdown cards, and
            # axis/title overrides for charts. We persist it raw alongside
            # boardState so consumers downstream of the render_spec_json can
            # still recover anything we didn't translate.
            try:
                view_state_json = (
                    json.dumps(view_state, separators=(",", ":"), default=str)
                    if view_state is not None else None
                )
            except Exception:
                view_state_json = None

            records.append({
                "analysis_rid": analysis_rid,
                "analysis_name": analysis_name,
                "ref_name": ref_name,
                "board_index": idx,
                "board_id": board_id,
                "board_type": board_type,
                "board_category": classify_board(board_type),
                "board_title": title,
                "is_parameterized": is_parameterized,
                "is_disabled": is_disabled,
                "logic_description": logic_desc,
                "render_spec_kind": render_spec_kind,
                "is_renderable": is_renderable,
                "render_spec_json": render_spec_json,
                "input_dataset_names": ", ".join(input_dataset_rids) if input_dataset_rids else None,
                "input_ref_names": ", ".join(input_ref_names) if input_ref_names else None,
                "parameters_json": parameters_json,
                "board_state_json": state_json,
                "board_view_state_json": view_state_json,
            })

    return records, parameters


# ============================================================================
# OUTPUT SCHEMA (used when no data is returned)
# ============================================================================

EMPTY_COLUMNS = {
    "analysis_rid": pd.Series(dtype="str"),
    "analysis_name": pd.Series(dtype="str"),
    "ref_name": pd.Series(dtype="str"),
    "board_index": pd.Series(dtype="int64"),
    "board_id": pd.Series(dtype="str"),
    "board_type": pd.Series(dtype="str"),
    "board_category": pd.Series(dtype="str"),
    "board_title": pd.Series(dtype="str"),
    "is_parameterized": pd.Series(dtype="bool"),
    "is_disabled": pd.Series(dtype="bool"),
    "logic_description": pd.Series(dtype="str"),
    "render_spec_kind": pd.Series(dtype="str"),
    "is_renderable": pd.Series(dtype="bool"),
    "render_spec_json": pd.Series(dtype="str"),
    "input_dataset_names": pd.Series(dtype="str"),
    "input_ref_names": pd.Series(dtype="str"),
    "parameters_json": pd.Series(dtype="str"),
    "board_state_json": pd.Series(dtype="str"),
    "board_view_state_json": pd.Series(dtype="str"),
}


# ============================================================================
# TRANSFORM
# ============================================================================


def _run_translation(foundry_stack: Source) -> pd.DataFrame:
    """
    Read Contour analyses and produce a structured board-by-board breakdown.

    Each row in the output represents a single board (step) in a Contour
    analysis, with its type, human-readable logic description, parameter usage,
    resolved dataset names, ref dependencies, and raw state.
    """
    enabled_rids = ANALYSIS_RIDS

    connection = foundry_stack.get_https_connection()
    base_url = connection.url
    client = connection.get_client()

    pat = foundry_stack.get_secret(PAT_SECRET_NAME)
    client.headers.update({"Authorization": f"Bearer {pat}"})

    all_records = []
    failures = []
    for analysis_rid in enabled_rids:
        try:
            records, params = analyze_contour(client, base_url, analysis_rid)
            all_records.extend(records)
            logger.info(
                "Analyzed %s: %d boards found, %d parameters",
                analysis_rid, len(records), len(params),
            )
        except Exception as e:
            # Fail-fast: collect failures and raise at the end so ALL analyses
            # are tried but the build fails visibly if any broke. (A bare
            # warning here would produce an empty parquet that masquerades as
            # success — PAT rotation / network failures / missing permissions
            # would all silently produce 0 rows.)
            logger.error("Error analyzing %s: %s", analysis_rid, e)
            failures.append((analysis_rid, str(e)))

    if failures:
        msg = "; ".join(f"{rid}: {err}" for rid, err in failures)
        raise RuntimeError(
            f"Reader failed for {len(failures)}/{len(enabled_rids)} analyses. "
            f"Likely cause: PAT secret on the Foundry source has rotated, or "
            f"an analysis RID is inaccessible. Details: {msg}"
        )

    if all_records:
        return pd.DataFrame(all_records)
    else:
        return pd.DataFrame(EMPTY_COLUMNS)


# ============================================================================
# TRANSFORM REGISTRATION
# ----------------------------------------------------------------------------
# The transform is only registered once you have replaced every placeholder in
# the CONFIGURATION block at the top of this file. While the shipped
# placeholders are still in place, the pipeline stays intentionally EMPTY so
# the project builds cleanly out of the box (there is no real output dataset to
# create yet). As soon as you fill in your own Source RID, secret name, output
# path, and analysis RID(s), the transform below activates automatically.
# ============================================================================

_PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"

PLACEHOLDERS_REPLACED = all([
    _PLACEHOLDER_UUID not in FOUNDRY_STACK_SOURCE_RID,
    not PAT_SECRET_NAME.startswith("REPLACE_ME"),
    not OUTPUT_DATASET.startswith("/REPLACE_ME"),
    bool(ANALYSIS_RIDS),
    all(_PLACEHOLDER_UUID not in rid for rid in ANALYSIS_RIDS),
])


if PLACEHOLDERS_REPLACED:

    @lightweight
    @external_systems(
        foundry_stack=Source(FOUNDRY_STACK_SOURCE_RID)
    )
    @transform_pandas(
        Output(OUTPUT_DATASET),
    )
    def compute(foundry_stack: Source) -> pd.DataFrame:
        """Registered Contour translator transform (see _run_translation)."""
        return _run_translation(foundry_stack)

else:  # pragma: no cover
    logger.warning(
        "contour_translator: placeholders not yet replaced — transform is "
        "NOT registered. Edit the CONFIGURATION block at the top of "
        "contour_translator.py to activate it."
    )
