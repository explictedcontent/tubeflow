# Agent API

The niche finder is built to be driven **by an agent in one trigger**, and to be
callable by **any agent** - including non-Claude / open models like Hermes - via a
clean importable layer and an optional MCP server. One source of truth
(`niche_finder.run_discovery`) backs the CLI, the Claude agent, and MCP.

---

## 1. One command (autonomous)

```bash
export YOUTUBE_API_KEY=...
python .claude/scripts/niche_pipeline.py            # sweep + dashboard
python .claude/scripts/niche_pipeline.py --seeds "homelab, ai tools"
python .claude/scripts/niche_pipeline.py --llm      # also author workflows headlessly
python .claude/scripts/niche_pipeline.py --json     # machine-readable summary to stdout
```

`--json` prints a single JSON object an agent can parse:

```json
{
  "ok": true,
  "out_dir": ".../niche-research/2026-06-30",
  "dashboard_path": ".../dashboard.html",
  "workflows_status": "authored | deferred-to-caller | llm-not-configured",
  "quota_spent": 3262,
  "n_videos": 275,
  "top_niches": [{"niche": "...", "opportunity": 73.0, "automatability": 99, "rpm_usd": 1}]
}
```

Exit code `0` on success, `2` on no-results/failure.

**Workflow authoring is pluggable:**
- default: workflows are left to the caller (the Claude agent writes
  `niche-workflows.json`, then re-runs the dashboard step).
- `--llm`: authored headlessly via an OpenAI-compatible endpoint (below) - fully
  autonomous, no agent in the loop.

---

## 2. Model-agnostic LLM ("Hermes-friendly")

Workflow authoring calls any **OpenAI-compatible** `/chat/completions` endpoint, so
it works with Anthropic, OpenAI, or a local open model. Configure via env:

| Var | Example |
|-----|---------|
| `LLM_BASE_URL` | `https://api.openai.com/v1` · `http://localhost:11434/v1` (Ollama) · vLLM URL |
| `LLM_API_KEY` | provider key (optional for local models) |
| `LLM_MODEL` | `gpt-4o-mini` · `claude-...` · `hermes-3-llama-3.1-8b` |

---

## 3. Importable API (`niche_api.py`) - the contract

```python
import niche_api as api

res   = api.discover(seeds=["homelab"], sub_min=500, sub_max=15000, days=30)  # -> run dict
data  = api.load_data(res["out_dir"])                                         # niche-data.json
top   = api.top_niches(data, by="opportunity_score", n=5)                     # list of niches
wf    = api.author_workflow(top[0], api._examples_for(data, top[0]["niche"])) # needs LLM env
path  = api.author_all_workflows(res["out_dir"], top=8)                       # writes workflows json
html  = api.build_dashboard(res["out_dir"])                                   # dashboard.html
```

**`discover(...)` returns:** `{ok, out_dir, data_path, report_path, niches, n_videos, quota}`.

**A niche summary** (`niches[]`): `niche, format, opportunity_score, automatability,
automation_score, rpm_usd, demand_score, gap_score, fit_score, breakout_channels,
avg_outlier_ratio, median_virality, saturation, n_videos_in_band`.

**A workflow** (`author_workflow` / `niche-workflows.json` value): `why_long,
recommendation, channel_concept, fingerprint, ready_video{title, hook, outline[],
thumbnail_brief, description}, backups[]`.

---

## 4. MCP server (`niche_mcp.py`) - for any agent

Optional dependency: `pip install mcp`. Run `python .claude/scripts/niche_mcp.py`
and register it with your host as a stdio MCP server. Tools:

| Tool | Purpose |
|------|---------|
| `find_niches(seeds, sub_min, sub_max, days, region)` | discover + score → `{out_dir, top_niches, ...}` |
| `list_top_niches(out_dir, by, n)` | re-rank a run by opportunity / automation / rpm / fit |
| `get_niche_workflow(out_dir, niche)` | ready-to-make video plan (LLM-authored if configured, else returns data for the caller's model) |
| `build_dashboard(out_dir)` | render `dashboard.html` |

Pure tools (`find_niches`, `build_dashboard`) need no LLM. `get_niche_workflow`
uses the configured endpoint, or returns the niche data + real examples so the
**calling** agent's own model can author the plan - keeping it model-agnostic.

---

## Production is out of scope here

The agent stops at the **ready video plan** (script / title / thumbnail brief /
description). Actual video-file generation (TTS + visuals + assembly) is a separate
production layer that can consume this output - e.g. a node-based flow (n8n/Make),
custom code, or manual assembly. The `ready_video` + `fingerprint` fields are the
clean hand-off point.
