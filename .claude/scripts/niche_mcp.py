#!/usr/bin/env python3
"""
TubeFlow Niche MCP server.

Exposes the niche finder as standard MCP tools so ANY MCP-capable agent or app
(Claude, or an external/open-model agent like Hermes) can drive it:

  find_niches         - discover + score outlier niches (returns a run dir + top niches)
  list_top_niches     - re-rank a previous run by opportunity / automation / rpm / fit
  get_niche_workflow  - a ready-to-make video plan for one niche (LLM-authored if configured)
  build_dashboard     - render the clickable dashboard.html for a run

The `mcp` package is an OPTIONAL dependency - the rest of TubeFlow works without
it. Install with: pip install mcp

Run:  python .claude/scripts/niche_mcp.py
Register it with your agent/host like any stdio MCP server (command = this file).
See docs/AGENT_API.md.
"""

import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import niche_api as api  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write("The 'mcp' package is required to run the MCP server: pip install mcp\n")
    sys.exit(1)

mcp = FastMCP("tubeflow-niche")


@mcp.tool()
def find_niches(seeds: Optional[list] = None, sub_min: Optional[int] = None,
                sub_max: Optional[int] = None, days: Optional[int] = None,
                region: Optional[str] = None) -> dict:
    """
    Discover low-subscriber / high-view outlier niches (default 500-15k subs, last
    30 days) and score them. Needs YOUTUBE_API_KEY in the environment. Returns
    {ok, out_dir, n_videos, quota, top_niches}. Pass out_dir to the other tools.
    """
    res = api.discover(seeds=seeds, sub_min=sub_min, sub_max=sub_max, days=days,
                       region=region, verbose=False)
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"]}
    return {"ok": True, "out_dir": res["out_dir"], "n_videos": res["n_videos"],
            "quota": res["quota"], "top_niches": api.top_niches(res["niches"], n=10)}


@mcp.tool()
def list_top_niches(out_dir: str, by: str = "opportunity_score", n: int = 10) -> list:
    """
    Re-rank the niches from a previous find_niches run. `by` is one of
    opportunity_score, automation_score, rpm_usd, fit_score.
    """
    return api.top_niches(api.load_data(out_dir), by=by, n=n)


@mcp.tool()
def get_niche_workflow(out_dir: str, niche: str) -> dict:
    """
    A ready-to-make video plan for one niche (title, hook, outline, thumbnail,
    description, backups) plus why-it-works and a format fingerprint. If an LLM
    endpoint is configured (LLM_BASE_URL/LLM_MODEL) it is authored server-side;
    otherwise the niche data + real examples are returned for the caller's own
    model to author from.
    """
    data = api.load_data(out_dir)
    match = [s for s in data.get("niches", []) if s["niche"] == niche]
    if not match:
        return {"error": f"niche '{niche}' not found in {out_dir}"}
    examples = api._examples_for(data, niche)
    if api.llm_configured():
        return api.author_workflow(match[0], examples, llm=None)
    return {"llm": "not-configured", "niche": match[0], "examples": examples,
            "note": "Set LLM_BASE_URL/LLM_MODEL to author server-side, or author from this data."}


@mcp.tool()
def build_dashboard(out_dir: str) -> dict:
    """Render the clickable dashboard.html for a run directory. Returns its path."""
    return {"dashboard_path": api.build_dashboard(out_dir)}


if __name__ == "__main__":
    mcp.run()
