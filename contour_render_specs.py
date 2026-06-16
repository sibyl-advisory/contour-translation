"""
Contour Render Specs

Translates raw Contour ``boardState`` (and ``boardViewState``) JSON into a
normalized, portable, OSDK-friendly "render spec" — a stable JSON shape that
downstream consumers (an OSDK React app, a code-generator, a documentation
exporter) can rely on without parsing Contour's internal Latitude/Jackson
shapes.

This module is STACK-AGNOSTIC: it contains no RIDs, paths, or secrets and can
be copied verbatim onto any Foundry stack.

Public API
----------
build_render_spec(board_type, board_state, board_view_state, title) -> dict
    Always returns a dict with at minimum:
        {
          "specVersion": "1",
          "kind": <stable kind enum, see RENDER_KINDS>,
          "title": <board title or None>,
          ...kind-specific fields...,
          "isRenderable": bool,
        }
    Never raises — translation errors surface as kind="error" with a message.

Design notes
------------
- All spec output uses camelCase to match OSDK / Foundry HTTP API conventions.
- Column references are normalized via `_normalize_column` to a flat
  {name, fieldType, alias?} shape, regardless of whether the source was a
  BasicColumnInfoV2, AliasedColumnV1, TimeBucketColumnInfoV1, or
  ExpressionColumnInfoV2 wrapper.
- "isRenderable" is True only when the spec contains enough information
  for a downstream renderer to produce output without inspecting the raw
  boardState. For unsupported / partial kinds it is False so callers can
  filter them out cleanly.
- The module is intentionally side-effect-free and deterministic: same
  inputs -> same JSON.
"""

from __future__ import annotations

from typing import Any, Optional


SPEC_VERSION = "1"

# Stable enum of render kinds. Update this list when adding new builders.
RENDER_KINDS = (
    "input_dataset",     # starting board pointing at a dataset
    "input_ref",         # starting board pointing at another ref
    "markdown",          # custom markdown card
    "histogram",         # histogram board
    "timeseries",        # timeseries board
    "chart",             # custom "chart" board (pie/bar/line/etc., with layers)
    "table",             # tabular display
    "details",           # details board
    "filter",            # filter transform
    "expression",        # computed columns
    "aggregate",         # group-by / aggregate
    "pivot_table",       # pivot-table board
    "column_editor",     # custom bulk-column-editor
    "join",              # custom join-columns
    "join_rows",         # custom join-rows (union / subtract)
    "sort",              # custom sort-columns
    "calculation",       # custom calculation
    "unsupported",       # known board type, no translator yet
    "error",             # translation raised an exception
)


# ---------------------------------------------------------------------------
# Column reference normalization
# ---------------------------------------------------------------------------

def _normalize_column(col: Any) -> Optional[dict]:
    """
    Normalize any Contour column wrapper into:
        {"name": str, "fieldType": Optional[str], "alias": Optional[str],
         "expression": Optional[str], "timeBucket": Optional[str],
         "timezone": Optional[str]}

    Returns None when no usable column information is present.

    Handles BasicColumnInfoV2, AliasedColumnV1, TimeBucketColumnInfoV1, and
    ExpressionColumnInfoV2 wrappers.
    """
    if not isinstance(col, dict):
        return None

    cls = col.get("@class", "") or ""

    # AliasedColumnV1 wraps an inner BasicColumn under "column"; the alias
    # is on the wrapper's "identifier".
    if "AliasedColumn" in cls:
        inner = _normalize_column(col.get("column", {}))
        if inner is None:
            return None
        alias = col.get("identifier")
        if alias and alias != inner.get("name"):
            inner["alias"] = alias
        return inner

    # TimeBucket wraps a column + bucket + timezone.
    if "TimeBucket" in cls or col.get("timeBucket") is not None:
        inner = _normalize_column(col.get("column", {})) or {}
        inner["timeBucket"] = col.get("timeBucket")
        inner["timezone"] = col.get("timeZone") or col.get("timezone")
        if not inner.get("name"):
            return None
        return inner

    # Expression columns: there's no underlying "name" in the data sense,
    # but we surface the columnName / expression.
    if "Expression" in cls:
        return {
            "name": col.get("columnName") or col.get("identifier"),
            "fieldType": None,
            "expression": col.get("expression") if isinstance(col.get("expression"), str)
                          else "(complex expression)",
        }

    # BasicColumnInfoV2 / fallback.
    name = col.get("name") or col.get("identifier")
    if not name:
        return None
    field_type = (col.get("fieldType") or {}).get("foundryFieldType")
    return {"name": name, "fieldType": field_type}


# ---------------------------------------------------------------------------
# Filter normalization (Latitude api filter classes)
# ---------------------------------------------------------------------------

# Keys we copy through verbatim from Latitude filter nodes when present.
_FILTER_PASSTHROUGH_KEYS = (
    "value", "values", "mode", "comparison", "min", "max", "regex", "negated",
)

def _normalize_filter_node(f: Any) -> Any:
    """
    Recursively normalize a Latitude filter node into a portable dict.

    Latitude classes look like ``latitude.api.filters.OrFilter`` /
    ``ColumnEqualsConstantFilter`` etc. We strip the package prefix, keep the
    short class name as ``type``, normalize any ``column`` reference, and
    recurse into ``subFilters``.
    """
    if not isinstance(f, dict):
        return f

    cls = f.get("@class", "") or ""
    short = cls.rsplit(".", 1)[-1] if cls else "Unknown"

    out: dict = {"type": short}

    column = _normalize_column(f.get("column"))
    if column is not None:
        out["column"] = column

    if isinstance(f.get("subFilters"), list):
        out["subFilters"] = [_normalize_filter_node(sub) for sub in f["subFilters"]]

    for k in _FILTER_PASSTHROUGH_KEYS:
        if f.get(k) is not None:
            out[k] = f[k]

    return out


# ---------------------------------------------------------------------------
# Per-kind spec builders
# ---------------------------------------------------------------------------
# Each builder returns the kind-specific portion of the spec (kind + payload).
# Common fields (specVersion, title, isRenderable) are added by build_render_spec.

def _spec_starting(state: dict) -> dict:
    """Starting board → input_dataset or input_ref."""
    desc = state.get("startingSetDescription") or {}
    desc_type = desc.get("type")

    if desc_type in ("initialwithtransaction", "initial"):
        identifier = desc.get("identifier", "") or ""
        # Foundry encodes datasets as "ri.foundry.main.dataset.<uuid>:<branch>"
        if ":" in identifier and identifier.startswith("ri.foundry.main.dataset."):
            ds_rid, branch = identifier.rsplit(":", 1)
        else:
            ds_rid, branch = (identifier or None), None
        return {
            "kind": "input_dataset",
            "datasetRid": ds_rid or None,
            "branch": branch,
            "transactionRid": desc.get("transaction") or desc.get("transactionRid"),
            "asOfDate": state.get("asOfDate"),
        }

    if desc_type == "refIdentified":
        ref_rid = (desc.get("refRid") or {}).get("rid")
        return {
            "kind": "input_ref",
            "refRid": ref_rid,
        }

    return {
        "kind": "unsupported",
        "boardType": "starting",
        "reason": f"unknown startingSetDescription.type={desc_type!r}",
    }


def _spec_histogram(state: dict) -> dict:
    """Histogram board."""
    sel_bars = state.get("selectionBars") or []
    selection_lower = state.get("selectionLowerBound")
    selection_upper = state.get("selectionUpperBound")
    has_selection = bool(sel_bars) or selection_lower is not None or selection_upper is not None

    return {
        "kind": "histogram",
        "groupBy": _normalize_column(state.get("groupBy")),
        "aggregate": {
            "function": state.get("aggregateType", "COUNT"),
            "column": _normalize_column(state.get("aggregate")),
        },
        "displayType": state.get("displayType"),
        "childSetMode": state.get("childSetMode"),
        "selection": {
            "mode": state.get("selectionMode"),
            "values": list(sel_bars),
            "lowerBound": selection_lower,
            "upperBound": selection_upper,
            "inverted": bool(state.get("inverted")),
        } if has_selection else None,
    }


def _spec_timeseries(state: dict) -> dict:
    """Timeseries board."""
    selected = state.get("selected") or {}
    has_window = selected.get("start") is not None or selected.get("end") is not None
    return {
        "kind": "timeseries",
        "timeColumn": _normalize_column(state.get("timeColumn")),
        "timeBucket": state.get("timeBucket"),
        "timezone": state.get("timezone") or state.get("timeZone"),
        "aggregate": {
            "function": state.get("valueColumnAggregate", "COUNT"),
            "column": _normalize_column(state.get("valueColumn")),
        },
        "seriesColumn": _normalize_column(state.get("seriesColumn")),
        "includeOverall": bool(state.get("includeOverall")),
        "selection": {
            "start": selected.get("start"),
            "end": selected.get("end"),
            "inverted": bool(state.get("inverted")),
        } if has_window else None,
    }


def _spec_table(state: dict) -> dict:
    """Tabular display board."""
    pcols = state.get("prioritizedColumnsForComputation") or []
    return {
        "kind": "table",
        "prioritizedColumns": [c for c in (_normalize_column(c) for c in pcols) if c],
        "limitNumberOfComputedColumns": bool(state.get("limitNumberOfComputedColumns")),
        "isDisabled": bool(state.get("isDisabled")),
    }


def _spec_details(state: dict) -> dict:
    """Details board."""
    cols = state.get("columns") or state.get("displayedColumns") or []
    return {
        "kind": "details",
        "columns": [c for c in (_normalize_column(c) for c in cols) if c],
    }


def _spec_filter(state: dict) -> dict:
    """Filter transform board (uses filterTypes, not Latitude class filters)."""
    fg = state.get("filterGroup") or {}
    raw = fg.get("filterTypes") or []
    out_filters = []
    for ft in raw:
        ftype = ft.get("type", "unknown")
        norm: dict = {"type": ftype}
        col = _normalize_column(ft.get("column"))
        if col:
            norm["column"] = col
        cols = ft.get("columns")
        if isinstance(cols, list):
            normalized = [_normalize_column(c) for c in cols]
            norm["columns"] = [c for c in normalized if c]
        for k in ("contains", "comparison", "min", "max", "regex", "value", "values"):
            if k in ft and ft[k] is not None:
                norm[k] = ft[k]
        # alpha-filter "terms" (concrete values) become a flat list of values
        if "terms" in ft and isinstance(ft["terms"], list):
            norm["values"] = [
                t.get("value") for t in ft["terms"]
                if isinstance(t, dict) and t.get("type") == "concrete"
            ]
        out_filters.append(norm)

    return {
        "kind": "filter",
        "operation": fg.get("operation", "AND"),
        "filters": out_filters,
    }


def _spec_expression(state: dict) -> dict:
    """Expression / computed columns board."""
    exprs = state.get("expressions") or []
    return {
        "kind": "expression",
        "computedColumns": [
            {
                "columnName": e.get("columnName"),
                "expression": e.get("expression"),
            }
            for e in exprs
            if isinstance(e, dict)
        ],
    }


def _spec_aggregate(state: dict) -> dict:
    """Aggregate / group-by board."""
    group_cols = state.get("groupByColumns") or state.get("groupColumns") or []
    aggs = state.get("aggregations") or []
    return {
        "kind": "aggregate",
        "groupBy": [str(c) for c in group_cols],
        "aggregations": [
            {
                "function": a.get("function") or a.get("type"),
                "column": a.get("column") or a.get("columnName"),
                "alias": a.get("alias") or a.get("outputName"),
            }
            for a in aggs
            if isinstance(a, dict)
        ],
    }


def _spec_pivot_table(state: dict) -> dict:
    """Pivot-table board (rows × cols × aggs)."""
    rows = [
        _normalize_column((ra or {}).get("column"))
        for ra in (state.get("rowAggregates") or [])
    ]
    cols = [
        _normalize_column((ca or {}).get("column"))
        for ca in (state.get("columnAggregates") or [])
    ]

    aggs_out = []
    for agg in state.get("aggregates") or []:
        if not isinstance(agg, dict):
            continue
        agg_class = agg.get("@type") or ""
        if "Expression" in agg_class:
            aggs_out.append({
                "type": "expression",
                "columnName": agg.get("columnName"),
                "expression": agg.get("expression"),
            })
        else:
            aggs_out.append({
                "type": "function",
                "function": agg.get("type"),
                "column": _normalize_column(agg.get("column")),
            })

    return {
        "kind": "pivot_table",
        "rowDimensions": [c for c in rows if c],
        "columnDimensions": [c for c in cols if c],
        "aggregates": aggs_out,
    }


# ----- custom (customBoardId-driven) builders --------------------------------

def _spec_custom_chart(internal: dict, child_set: dict) -> dict:
    """customBoardId='chart' → chart with layers + (optional) slice filters."""
    layers_out = []
    for layer in internal.get("layers", []):
        if not isinstance(layer, dict):
            continue
        gb_wrapper = (layer.get("groupByColumn") or {}).get("column")
        gb = _normalize_column(gb_wrapper)
        value_selections = []
        for sel in layer.get("selections") or []:
            for cs in (sel or {}).get("columnSelections") or []:
                value_selections.append({
                    "column": _normalize_column(cs.get("column")),
                    "selectionType": cs.get("selectionType"),
                    "value": cs.get("value"),
                })
        layers_out.append({
            "chartType": layer.get("chartType"),
            "displayName": layer.get("displayName"),
            "layerId": layer.get("layerId"),
            "groupBy": gb,
            "valueSelections": value_selections,
            "sortDirection": layer.get("sortDirection"),
            "sortColumnIdentifier": layer.get("sortColumnIdentifier"),
        })

    filters_out = None
    if isinstance(child_set, dict) and child_set.get("type") == "filter":
        filters_out = [
            _normalize_filter_node(f) for f in (child_set.get("filters") or [])
        ]

    return {
        "kind": "chart",
        "layers": layers_out,
        "filters": filters_out,
    }


def _spec_custom_markdown(view_state: Optional[dict]) -> dict:
    """
    customBoardId='markdown' — body lives in boardViewState, not boardState.

    Observed shape (CustomBoardViewStateV1):
        {"@type":"CustomBoardViewStateV1",
         "boardTitle": "...",
         "internalState": {"sourceText": "<markdown body>", ...}}

    We probe both the nested internalState and a handful of top-level keys
    for robustness against minor schema variations across Contour versions.
    """
    content = None
    if isinstance(view_state, dict):
        # Primary location: boardViewState.internalState.sourceText
        internal = view_state.get("internalState")
        if isinstance(internal, dict):
            for k in ("sourceText", "markdown", "content", "body", "text"):
                v = internal.get(k)
                if isinstance(v, str) and v.strip():
                    content = v
                    break
        # Fallback: top-level keys (defensive — never observed in practice).
        if content is None:
            for k in ("markdown", "content", "markdownContent", "body", "text", "sourceText"):
                v = view_state.get(k)
                if isinstance(v, str) and v.strip():
                    content = v
                    break
    return {
        "kind": "markdown",
        "content": content,
        # Surface a small excerpt for previews; downstream can recompute.
        "preview": (content.strip().splitlines()[0][:120] if isinstance(content, str) else None),
        # Markdown is renderable only when we actually recovered the body.
        # Without content, downstream has nothing to show; signal that
        # explicitly so consumers can filter or surface a fallback.
        "isRenderable": content is not None,
    }


def _spec_custom_join(internal: dict) -> dict:
    """customBoardId='join-columns'."""
    join_type_map = {
        "INTERSECTION": "INNER",
        "LEFT_OUTER": "LEFT",
        "RIGHT_OUTER": "RIGHT",
        "FULL_OUTER": "FULL_OUTER",
        "ANTI": "ANTI",
        "CROSS": "CROSS",
    }
    combine_type = internal.get("combineType", "INTERSECTION")
    conditions = []
    for mc in internal.get("matchConditions") or []:
        conditions.append({
            "sourceColumn": _normalize_column((mc or {}).get("sourceColumn")),
            "joinedColumn": _normalize_column((mc or {}).get("joinedColumn")),
        })
    incoming = (internal.get("incomingSetWithDescription") or {}).get("latitudeSet") or {}
    return {
        "kind": "join",
        "joinType": join_type_map.get(combine_type, combine_type),
        "conditions": conditions,
        "joinedSetIdentifier": incoming.get("identifier"),
    }


def _spec_custom_join_rows(internal: dict) -> dict:
    """customBoardId='join-rows' (union / intersect / subtract)."""
    combine_map = {"UNION": "UNION_ALL", "INTERSECTION": "INTERSECT", "SUBTRACT": "SUBTRACT"}
    combine_type = internal.get("combineType", "UNION")
    incoming = (internal.get("incomingSetWithDescription") or {}).get("latitudeSet") or {}
    return {
        "kind": "join_rows",
        "operation": combine_map.get(combine_type, combine_type),
        "joinedSetIdentifier": incoming.get("identifier"),
    }


def _spec_custom_calculation(internal: dict) -> dict:
    """customBoardId='calculation'."""
    calcs = []
    for calc_id, config in (internal.get("calculationResultConfigMap") or {}).items():
        if not isinstance(config, dict):
            continue
        calcs.append({
            "calculationId": calc_id,
            "function": config.get("selectedAggregateType"),
            "column": _normalize_column(config.get("selectedColumn")),
        })
    return {
        "kind": "calculation",
        "calculations": calcs,
    }


def _spec_custom_bulk_column_editor(internal: dict) -> dict:
    """customBoardId='bulk-column-editor'."""
    renamed = internal.get("renamedColumns") or {}
    if isinstance(renamed, dict):
        rename_pairs = [{"from": k, "to": v} for k, v in renamed.items()]
    elif isinstance(renamed, list):
        rename_pairs = [
            {"from": (r or {}).get("from") or (r or {}).get("oldName"),
             "to":   (r or {}).get("to")   or (r or {}).get("newName")}
            for r in renamed
        ]
    else:
        rename_pairs = []

    def _col_names(seq):
        out = []
        for c in seq or []:
            if isinstance(c, str):
                out.append(c)
            else:
                norm = _normalize_column(c)
                if norm:
                    out.append(norm["name"])
        return out

    return {
        "kind": "column_editor",
        "rename": rename_pairs,
        "remove": _col_names(internal.get("removedColumns")),
        "keep": _col_names(internal.get("keptColumns")),
        "deduplicateRows": bool(internal.get("removeDuplicateRows")),
    }


def _spec_custom_sort(internal: dict) -> dict:
    """customBoardId='sort-columns'."""
    sorts = []
    for s in internal.get("columnsToSort") or []:
        if not isinstance(s, dict):
            continue
        col_ref = s.get("column") if isinstance(s.get("column"), dict) else None
        col_norm = _normalize_column(col_ref) if col_ref else (
            {"name": s.get("column"), "fieldType": None}
            if isinstance(s.get("column"), str) else None
        )
        sorts.append({
            "column": col_norm,
            "direction": s.get("direction") or s.get("order") or "ASC",
        })
    return {
        "kind": "sort",
        "columns": sorts,
        "applyLimit": bool(internal.get("applyLimit")),
    }


# ---------------------------------------------------------------------------
# Dispatch table for top-level board types (excluding "custom" which is
# routed through customBoardId below).
# ---------------------------------------------------------------------------

_BOARD_BUILDERS = {
    "starting": _spec_starting,
    "histogram": _spec_histogram,
    "timeseries": _spec_timeseries,
    "table": _spec_table,
    "details": _spec_details,
    "filter": _spec_filter,
    "expression": _spec_expression,
    "aggregate": _spec_aggregate,
    "group-by": _spec_aggregate,
    "pivot-table": _spec_pivot_table,
}

# Dispatch for custom boards. Each builder takes (internalState, childSetDescription, viewState).
# Most custom builders only use internalState; chart and markdown need extra args.

def _route_custom(state: dict, view_state: Optional[dict]) -> dict:
    cb_id = state.get("customBoardId") or ""
    internal = state.get("internalState") or {}
    csd = state.get("childSetDescription") or {}

    if cb_id == "markdown":
        return _spec_custom_markdown(view_state)
    if cb_id == "chart":
        return _spec_custom_chart(internal, csd)
    if cb_id == "join-columns":
        return _spec_custom_join(internal)
    if cb_id == "join-rows":
        return _spec_custom_join_rows(internal)
    if cb_id == "calculation":
        return _spec_custom_calculation(internal)
    if cb_id == "bulk-column-editor":
        return _spec_custom_bulk_column_editor(internal)
    if cb_id == "sort-columns":
        return _spec_custom_sort(internal)
    return {
        "kind": "unsupported",
        "boardType": "custom",
        "customBoardId": cb_id or None,
        "reason": "no translator for this customBoardId",
    }


# Kinds that we consider "renderable" — i.e. a downstream consumer has enough
# information to actually render or query without dipping into raw boardState.
_RENDERABLE_KINDS = {
    "input_dataset",
    "input_ref",
    "histogram",
    "timeseries",
    "chart",
    "table",
    "details",
    "pivot_table",
    "aggregate",
    "filter",
    "expression",
    "column_editor",
    "join",
    "join_rows",
    "sort",
    "calculation",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_render_spec(
    board_type: str,
    board_state: Optional[dict],
    board_view_state: Optional[dict],
    title: Optional[str],
) -> dict:
    """
    Build a normalized render spec for a single Contour board.

    Always returns a dict with at minimum: specVersion, kind, title,
    isRenderable. Translation errors are caught and returned as
    kind="error" so the caller can keep processing other boards.
    """
    state = board_state or {}
    try:
        if board_type == "custom":
            payload = _route_custom(state, board_view_state)
        else:
            builder = _BOARD_BUILDERS.get(board_type)
            if builder is None:
                payload = {
                    "kind": "unsupported",
                    "boardType": board_type,
                    "reason": "no translator for this boardType",
                }
            else:
                payload = builder(state)
    except Exception as e:  # noqa: BLE001 — translators must never crash builds
        payload = {
            "kind": "error",
            "boardType": board_type,
            "error": f"{type(e).__name__}: {e}",
        }

    kind = payload.get("kind", "unsupported")
    spec = {
        "specVersion": SPEC_VERSION,
        "kind": kind,
        "title": title,
        "isRenderable": kind in _RENDERABLE_KINDS,
    }
    # Merge payload (kind already set, but harmless to overwrite with same value)
    spec.update(payload)
    spec["specVersion"] = SPEC_VERSION  # ensure not overwritten by payload
    return spec
