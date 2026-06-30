#!/usr/bin/env python3
"""
TubeFlow Niche API - the "agent contract".

Pure, importable functions that back the CLI, the Claude agent, and the MCP
server, so there is one source of truth. No interactive prompts, no argparse.

Model-agnostic ("Hermes-friendly"): workflow authoring calls any
OpenAI-compatible /chat/completions endpoint (Anthropic, OpenAI, or a local
open model via Ollama/vLLM), configured by env:
  LLM_BASE_URL  e.g. https://api.openai.com/v1  or  http://localhost:11434/v1
  LLM_API_KEY   (optional for local models)
  LLM_MODEL     e.g. gpt-4o-mini, claude-..., hermes-3-llama-3.1-8b

See docs/AGENT_API.md for the full contract and JSON schemas.
"""

import json
import os
import re
import sys
from pathlib import Path

# Make sibling scripts importable regardless of the caller's cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import niche_finder as nf          # noqa: E402
import niche_dashboard as nd       # noqa: E402

try:
    import requests
except ImportError:
    requests = None

WF_KEYS = ["why_long", "recommendation", "channel_concept",
           "fingerprint", "ready_video", "backups"]


def _config():
    cfg = nf.load_config()
    return cfg, (cfg.get("niche", {}) or {})


# === DISCOVERY ===

def discover(seeds=None, sub_min=None, sub_max=None, days=None, region=None,
             max_per_keyword=None, api_key=None, output_dir="", rescore=False,
             no_thumbnails=False, verbose=False):
    """
    Run discovery + scoring. Returns the run_discovery result dict
    {ok, out_dir, data_path, report_path, niches, n_videos, quota}.
    Raises RuntimeError if no API key and not rescoring.
    """
    cfg, ncfg = _config()
    api_key = api_key or os.environ.get(ncfg.get("api_key_env", "YOUTUBE_API_KEY"), "")
    if not api_key and not rescore:
        raise RuntimeError("No YouTube API key. Set YOUTUBE_API_KEY or pass api_key=.")

    def pick(v, key, default):
        return v if v is not None else ncfg.get(key, default)

    return nf.run_discovery(
        cfg, ncfg, seeds=seeds or [],
        sub_min=pick(sub_min, "sub_min", 500),
        sub_max=pick(sub_max, "sub_max", 15000),
        days=pick(days, "days", 30),
        region=region or ncfg.get("region", "US"),
        max_per_keyword=pick(max_per_keyword, "max_per_keyword", 50),
        api_key=api_key, output_dir=output_dir, rescore=rescore,
        no_thumbnails=no_thumbnails, verbose=verbose)


def load_data(out_dir):
    with open(Path(out_dir) / "niche-data.json", encoding="utf-8") as f:
        return json.load(f)


def top_niches(data, by="opportunity_score", n=5):
    """Top N niche summaries sorted by a key (opportunity_score / automation_score / rpm_usd / fit_score)."""
    niches = data["niches"] if isinstance(data, dict) else data
    return sorted(niches, key=lambda s: s.get(by) or 0, reverse=True)[:n]


def _examples_for(data, niche_name, k=3):
    vids = [v for v in data.get("videos", []) if v.get("niche") == niche_name]
    vids.sort(key=lambda r: r.get("virality_score", 0), reverse=True)
    return vids[:k]


# === MODEL-AGNOSTIC WORKFLOW AUTHORING ===

def _llm_cfg(override=None):
    o = override or {}
    return {
        "base_url": o.get("base_url") or os.environ.get("LLM_BASE_URL", ""),
        "api_key": o.get("api_key") or os.environ.get("LLM_API_KEY", ""),
        "model": o.get("model") or os.environ.get("LLM_MODEL", ""),
    }


def llm_configured(override=None):
    c = _llm_cfg(override)
    return bool(c["base_url"] and c["model"])


def _build_wf_prompt(niche, examples):
    ex = "\n".join(
        f'- "{e["title"]}" - {e["channel_title"]}, {e["subscribers"]:,} subs, '
        f'{e["views"]:,} views = {e["outlier_ratio"]}x'
        for e in examples)
    return (
        "You are a YouTube niche strategist. Using ONLY the real data below, write a "
        "ready-to-execute workflow for a brand-new small channel entering this niche. "
        "Be concrete and honest.\n\n"
        f"NICHE: {niche['niche']}\n"
        f"Virality/opportunity: {niche['opportunity_score']}/100 | "
        f"AI-automatability: {niche['automatability']}% | est. RPM: ${niche.get('rpm_usd')}/1000 views\n"
        f"Demand: {niche.get('demand_score')} | Gap: {niche.get('gap_score')} | "
        f"Breakout channels: {niche.get('breakout_channels')} | "
        f"Avg outlier: {niche.get('avg_outlier_ratio')}x | Saturation: {niche.get('saturation')}\n\n"
        f"REAL OUTLIER EXAMPLES (last 30 days, 500-15k sub channels):\n{ex}\n\n"
        "Return ONLY a JSON object with these keys:\n"
        "- why_long: paragraph citing the specific numbers/channels above; honest about "
        "saturation and whether the wins are Shorts.\n"
        "- recommendation: honest verdict on automatability + RPM/income reality + the catch.\n"
        "- channel_concept: one line.\n"
        "- fingerprint: the recurring TITLE formula and likely THUMBNAIL formula from the examples.\n"
        "- ready_video: object with title, hook, outline (array of 5-7 strings), thumbnail_brief, description.\n"
        "- backups: array of 5 follow-up title ideas.\n"
        "No prose outside the JSON. No em dashes.")


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s:e + 1])
        raise


def author_workflow(niche, examples, llm=None):
    """Author one niche's workflow via an OpenAI-compatible endpoint. Returns a dict."""
    if requests is None:
        raise RuntimeError("requests not installed (pip install requests).")
    c = _llm_cfg(llm)
    if not c["base_url"] or not c["model"]:
        raise RuntimeError("LLM not configured. Set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL.")

    url = c["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if c["api_key"]:
        headers["Authorization"] = f"Bearer {c['api_key']}"
    payload = {
        "model": c["model"],
        "messages": [{"role": "user", "content": _build_wf_prompt(niche, examples)}],
        "temperature": 0.7,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    wf = _extract_json(content)
    for k in WF_KEYS:
        wf.setdefault(k, [] if k == "backups" else "")
    return wf


def author_all_workflows(out_dir, top=8, llm=None, by="opportunity_score"):
    """Author workflows for the top N niches and write niche-workflows.json. Returns its path."""
    data = load_data(out_dir)
    workflows = {}
    for niche in top_niches(data, by=by, n=top):
        examples = _examples_for(data, niche["niche"])
        try:
            workflows[niche["niche"]] = author_workflow(niche, examples, llm=llm)
        except Exception as e:  # one failure should not kill the batch
            workflows[niche["niche"]] = {"error": str(e)}
    out = Path(out_dir) / "niche-workflows.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(workflows, f, indent=2, ensure_ascii=False)
    return str(out)


# === DASHBOARD ===

def build_dashboard(out_dir):
    """Build dashboard.html from niche-data.json (+ niche-workflows.json if present)."""
    return str(nd.build(Path(out_dir)))
