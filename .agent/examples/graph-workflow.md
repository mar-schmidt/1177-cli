# 1177 Graph Workflow Examples

## Minimal sequence

```bash
1177 auth login
1177 auth status
1177 journal results graph analyses
1177 journal results graph data --analysis-id NPU02902
```

## Multi-analysis with date bounds

```bash
1177 journal results graph data \
  --analysis-id NPU02902 \
  --analysis-id NPU04111 \
  --analysis-id NPU19676 \
  --date-from 2026-01-01 \
  --date-to 2026-06-30
```

## JSON extraction pattern

Pick first analysis id from analyses output:

```bash
analysis_id="$(
  1177 journal results graph analyses | \
  python -c 'import json,sys;print(json.load(sys.stdin)["analyses"][0]
["analysis_id"])'
)"
1177 journal results graph data --analysis-id "$analysis_id"
```

## Verify response shape

```bash
1177 journal results graph data --analysis-id NPU02902 | \
python -c 'import json,sys;p=json.load(sys.stdin);print(sorted(p.keys()))'
```

Expected keys include:

- `ok`
- `analysis_ids`
- `date_from`
- `date_to`
- `point_count`
- `series`
