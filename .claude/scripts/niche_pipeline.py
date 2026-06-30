#!/usr/bin/env python3
"""
TubeFlow Niche Pipeline - one headless command for an agent to drive.

Runs the whole flow in one shot:  discover -> (author workflows) -> dashboard.

Designed to be triggered by an agent (or a one-line command). No interactive
prompts. With --json it prints a machine-readable summary to stdout for the
caller to parse.

Usage:
  export YOUTUBE_API_KEY=...
  python .claude/scripts/niche_pipeline.py                 # sweep + dashboard
  python .claude/scripts/niche_pipeline.py --seeds "homelab, ai tools"
  python .claude/scripts/niche_pipeline.py --llm           # also author workflows via LLM
  python .claude/scripts/niche_pipeline.py --json          # machine-readable output

Workflow authoring:
  - default: workflows are left to the caller (the Claude agent fills
    niche-workflows.json in-loop, then re-runs the dashboard step).
  - --llm: authors them headlessly via an OpenAI-compatible endpoint
    (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL). Fully autonomous, no agent in loop.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import niche_api as api  # noqa: E402


def run(seeds=None, top=8, llm=False, by="opportunity_score", sub_min=None, sub_max=None,
        days=None, region=None, max_per_keyword=None, output_dir="", no_thumbnails=False,
        verbose=True):
    """Run the full pipeline. Returns a structured summary dict."""
    result = api.discover(seeds=seeds, sub_min=sub_min, sub_max=sub_max, days=days,
                          region=region, max_per_keyword=max_per_keyword,
                          output_dir=output_dir, no_thumbnails=no_thumbnails,
                          verbose=verbose)
    if not result["ok"]:
        return {"ok": False, "reason": result["reason"]}

    out_dir = result["out_dir"]
    workflows_path = None
    workflows_status = "deferred-to-caller"
    if llm:
        if not api.llm_configured():
            workflows_status = "llm-not-configured"
        else:
            workflows_path = api.author_all_workflows(out_dir, top=top, by=by)
            workflows_status = "authored"

    dashboard = api.build_dashboard(out_dir)

    return {
        "ok": True,
        "out_dir": out_dir,
        "data_path": result["data_path"],
        "report_path": result["report_path"],
        "dashboard_path": dashboard,
        "workflows_path": workflows_path,
        "workflows_status": workflows_status,
        "quota_spent": result["quota"],
        "n_videos": result["n_videos"],
        "top_niches": [
            {"niche": s["niche"], "opportunity": s["opportunity_score"],
             "automatability": s["automatability"], "rpm_usd": s.get("rpm_usd"),
             "automation_score": s.get("automation_score")}
            for s in api.top_niches(result["niches"], by=by, n=10)
        ],
    }


def main():
    p = argparse.ArgumentParser(description="TubeFlow Niche Pipeline (one-command)")
    p.add_argument("--seeds", type=str, default="", help="Comma-separated extra niches")
    p.add_argument("--top", type=int, default=8, help="How many top niches to author workflows for")
    p.add_argument("--by", type=str, default="opportunity_score",
                   help="Rank key: opportunity_score | automation_score | rpm_usd | fit_score")
    p.add_argument("--llm", action="store_true", help="Author workflows via OpenAI-compatible LLM")
    p.add_argument("--sub-min", type=int, default=None)
    p.add_argument("--sub-max", type=int, default=None)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--region", type=str, default=None)
    p.add_argument("--max-per-keyword", type=int, default=None)
    p.add_argument("--output", type=str, default="")
    p.add_argument("--no-thumbnails", action="store_true")
    p.add_argument("--json", action="store_true", help="Print a machine-readable JSON summary")
    args = p.parse_args()

    seeds = [s for s in args.seeds.split(",") if s.strip()] if args.seeds else []
    summary = run(seeds=seeds, top=args.top, llm=args.llm, by=args.by,
                  sub_min=args.sub_min, sub_max=args.sub_max, days=args.days,
                  region=args.region, max_per_keyword=args.max_per_keyword,
                  output_dir=args.output, no_thumbnails=args.no_thumbnails,
                  verbose=not args.json)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
        sys.exit(0 if summary.get("ok") else 2)

    if not summary.get("ok"):
        print(f"\nNo results: {summary.get('reason')}. Widen --sub-max / --days or check API key/quota.")
        sys.exit(2)

    print(f"\nPipeline complete. Quota spent: {summary['quota_spent']} units")
    print(f"  Dashboard: {summary['dashboard_path']}")
    print(f"  Workflows: {summary['workflows_status']}"
          + (f" ({summary['workflows_path']})" if summary['workflows_path'] else ""))
    print("\nTop niches:")
    for i, s in enumerate(summary["top_niches"][:5], 1):
        print(f"  {i}. {s['niche']} - opportunity {s['opportunity']}, AI {s['automatability']}%")


if __name__ == "__main__":
    main()
