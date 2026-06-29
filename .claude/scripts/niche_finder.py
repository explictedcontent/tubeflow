#!/usr/bin/env python3
"""
TubeFlow Niche Finder.

Discover low-subscriber / high-view "outlier" videos via the YouTube Data API
v3, score each video's virality likelihood (0-100), cluster the results into
niches, and rank those niches by opportunity. Two rankings are produced:

  1. Viral Opportunity  - pure niche score (automatability shown as a label)
  2. Best for Automation - niche score weighted by how much AI can produce

The output (niche-data.json + niche-report.md, plus downloaded thumbnails) is
consumed by the yt-niche-finder agent, which adds the human-facing playbook.

Usage:
  export YOUTUBE_API_KEY="your-key"
  python .claude/scripts/niche_finder.py                       # sweep built-in niches
  python .claude/scripts/niche_finder.py --seeds "homelab,self hosting"
  python .claude/scripts/niche_finder.py --sub-min 500 --sub-max 15000 --days 30
  python .claude/scripts/niche_finder.py --rescore             # re-score from cache (0 quota)

Prerequisites:
  pip install requests pyyaml
  A YouTube Data API v3 key: https://console.cloud.google.com/ ->
  "APIs & Services" -> enable "YouTube Data API v3" -> create an API key.

Quota notes:
  search.list costs 100 units/call; videos.list and channels.list cost 1 unit.
  The default free quota is 10,000 units/day (~90 searches). The script prints
  an estimate before spending quota and caches every raw response so you can
  re-score with --rescore for zero additional quota.
"""

import argparse
import json
import math
import os
import sys
import time
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Optional

try:
    import requests
except ImportError:
    print("Error: 'requests' is not installed. Install with: pip install requests")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("Error: 'pyyaml' is not installed. Install with: pip install pyyaml")
    sys.exit(1)

# Local SQLite store (stdlib sqlite3) for trajectory + backtesting. Optional:
# if the module is missing the finder still runs, just without accumulation.
try:
    import niche_db
except ImportError:
    niche_db = None


API_BASE = "https://www.googleapis.com/youtube/v3"


# === CONFIGURATION LOADING (mirrors fetch_descriptions.py) ===

def find_config_path() -> Optional[Path]:
    """Find config.yaml by searching up from the script location."""
    script_dir = Path(__file__).parent

    claude_config = script_dir.parent / "config.yaml"
    if claude_config.exists():
        return claude_config

    repo_root = script_dir.parent.parent
    root_config = repo_root / "config.yaml"
    if root_config.exists():
        return root_config

    example_config = repo_root / "config.example.yaml"
    if example_config.exists():
        print("Warning: Using config.example.yaml. Copy it to config.yaml and customize.")
        return example_config

    return None


def load_config() -> dict:
    """Load configuration from config.yaml (empty dict if none found)."""
    config_path = find_config_path()
    if config_path is None:
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# === BUILT-IN CANDIDATE NICHES ===
# Each niche carries 1-2 search keywords plus an "automatability" hint (0-100)
# and a "format" label. The hint seeds the Best-for-Automation ranking; the
# yt-niche-finder agent refines it with real judgement in the final report.

# "rpm" is a rough estimated USD revenue-per-1000-views band so virality is not
# mistaken for income (finance/tech earn many times what compilations do).
DEFAULT_CANDIDATE_NICHES = [
    {"name": "Sleep / Meditation / Ambient", "keywords": ["sleep music", "rain sounds"], "format": "ambient audio + visuals", "automatability": 99, "rpm": 1},
    {"name": "Motivation / Stoicism", "keywords": ["stoicism motivation", "self discipline"], "format": "voiceover + stock", "automatability": 98, "rpm": 3},
    {"name": "Scary Stories / Horror Narration", "keywords": ["scary stories", "horror narration"], "format": "faceless narration", "automatability": 97, "rpm": 3},
    {"name": "Top 10 / Listicles / Facts", "keywords": ["top 10 facts", "amazing facts"], "format": "faceless listicle", "automatability": 96, "rpm": 4},
    {"name": "AI Tools & News", "keywords": ["ai tools", "ai news"], "format": "screen-rec + voice", "automatability": 95, "rpm": 12},
    {"name": "Space / Science Explainers", "keywords": ["space facts", "science explained"], "format": "faceless explainer", "automatability": 94, "rpm": 5},
    {"name": "True Crime", "keywords": ["true crime story", "unsolved cases"], "format": "faceless narration", "automatability": 93, "rpm": 5},
    {"name": "History Explainers", "keywords": ["history explained", "historical events"], "format": "faceless explainer", "automatability": 92, "rpm": 5},
    {"name": "Personal Finance / Side Hustles", "keywords": ["side hustle ideas", "passive income"], "format": "faceless voiceover", "automatability": 90, "rpm": 18},
    {"name": "Crypto / Investing", "keywords": ["crypto news", "investing for beginners"], "format": "faceless voiceover", "automatability": 88, "rpm": 15},
    {"name": "Gaming Highlights / Compilations", "keywords": ["gaming funny moments", "gaming highlights"], "format": "compilation", "automatability": 85, "rpm": 2},
    {"name": "Productivity / Study", "keywords": ["productivity tips", "study with me"], "format": "mixed", "automatability": 70, "rpm": 8},
    {"name": "Self-Hosting / Homelab", "keywords": ["homelab", "self hosting"], "format": "screen recording", "automatability": 70, "rpm": 10},
    {"name": "DIY / Life Hacks", "keywords": ["life hacks", "diy projects"], "format": "compilation / on-camera", "automatability": 65, "rpm": 4},
    {"name": "Health / Fitness Tips", "keywords": ["fitness tips", "home workout"], "format": "mixed", "automatability": 60, "rpm": 8},
    {"name": "Tech Reviews / Gadgets", "keywords": ["tech review", "best gadgets"], "format": "on-camera", "automatability": 55, "rpm": 12},
]


def get_candidate_niches(config: dict, seeds: Optional[list]) -> list:
    """Resolve the niche list: config override or built-in, plus optional seeds."""
    niche_cfg = config.get("niche", {}) or {}
    candidates = niche_cfg.get("candidates") or DEFAULT_CANDIDATE_NICHES

    niches = [dict(n) for n in candidates]

    if seeds:
        for seed in seeds:
            seed = seed.strip()
            if seed:
                niches.append({
                    "name": f"(seed) {seed}",
                    "keywords": [seed],
                    "format": "unknown",
                    "automatability": 75,  # neutral default until the agent judges it
                })
    return niches


# === API CACHE ===

class ApiClient:
    """Thin YouTube Data API client with on-disk caching and quota tracking."""

    def __init__(self, api_key: str, cache_dir: Path, rescore: bool = False,
                 force_refresh: bool = False):
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.rescore = rescore           # cache-only mode (no network, 0 quota)
        self.force_refresh = force_refresh  # always hit network (for fresh snapshots)
        self.quota_spent = 0

    def _cache_path(self, endpoint: str, params: dict) -> Path:
        keyable = {k: v for k, v in params.items() if k != "key"}
        raw = endpoint + "?" + json.dumps(keyable, sort_keys=True)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{endpoint}_{digest}.json"

    def get(self, endpoint: str, params: dict, quota_cost: int) -> Optional[dict]:
        cache_path = self._cache_path(endpoint, params)

        if cache_path.exists() and not self.force_refresh:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)

        if self.rescore:
            # Cache-only mode: a miss means we simply skip (no quota spend).
            return None

        params = dict(params)
        params["key"] = self.api_key

        for attempt in range(4):
            try:
                resp = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
            except requests.RequestException as e:
                wait = 2 ** attempt
                print(f"  Network error ({e}); retrying in {wait}s...")
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                self.quota_spent += quota_cost
                data = resp.json()
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                return data

            if resp.status_code == 403:
                # Quota exceeded or key/permission problem - fatal, surface it.
                print(f"  API 403: {resp.text[:300]}")
                print("  This usually means the daily quota is exhausted or the API key "
                      "lacks YouTube Data API v3 access.")
                return None

            if resp.status_code in (429, 500, 503):
                wait = 2 ** attempt
                print(f"  API {resp.status_code}; retrying in {wait}s...")
                time.sleep(wait)
                continue

            print(f"  API {resp.status_code}: {resp.text[:300]}")
            return None

        return None


# === DISCOVERY ===

def search_videos(client: ApiClient, keyword: str, published_after: str,
                  region: str, max_results: int) -> list:
    """Return up to max_results recent video IDs for a keyword (order=viewCount)."""
    video_ids = []
    page_token = None

    while len(video_ids) < max_results:
        params = {
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "order": "viewCount",
            "publishedAfter": published_after,
            "regionCode": region,
            "relevanceLanguage": "en",
            "maxResults": min(50, max_results - len(video_ids)),
        }
        if page_token:
            params["pageToken"] = page_token

        data = client.get("search", params, quota_cost=100)
        if not data:
            break

        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid:
                video_ids.append(vid)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return video_ids


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_videos(client: ApiClient, video_ids: list) -> dict:
    """Map video_id -> video detail (statistics + snippet + contentDetails)."""
    out = {}
    for batch in _batched(video_ids, 50):
        params = {"part": "statistics,snippet,contentDetails", "id": ",".join(batch)}
        data = client.get("videos", params, quota_cost=1)
        if not data:
            continue
        for item in data.get("items", []):
            out[item["id"]] = item
    return out


def fetch_channels(client: ApiClient, channel_ids: list) -> dict:
    """Map channel_id -> channel detail (statistics + snippet)."""
    out = {}
    for batch in _batched(list(channel_ids), 50):
        params = {"part": "statistics,snippet", "id": ",".join(batch)}
        data = client.get("channels", params, quota_cost=1)
        if not data:
            continue
        for item in data.get("items", []):
            out[item["id"]] = item
    return out


# === SCORING ===

def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _days_since(published: str) -> float:
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 1.0
    delta = datetime.now(timezone.utc) - dt
    return max(delta.total_seconds() / 86400.0, 0.5)


def _norm(values: list) -> list:
    """Min-max normalize a list to 0..1 (0.5 when all values are equal)."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def build_video_records(videos: dict, channels: dict, niche_name: str,
                        sub_min: int, sub_max: int) -> tuple:
    """
    Turn raw API data into scored records for one niche.

    Returns (in_band_records, total_found, big_channel_count, total_views_all)
    where big_channel_count counts videos whose channel exceeds sub_max (a
    saturation signal) and total_views_all sums views across every found video
    regardless of band (a demand signal - how much attention the niche pulls).
    """
    records = []
    total_found = 0
    big_channel_count = 0
    total_views_all = 0

    for vid, video in videos.items():
        stats = video.get("statistics", {})
        snippet = video.get("snippet", {})
        channel_id = snippet.get("channelId")
        channel = channels.get(channel_id, {})
        ch_stats = channel.get("statistics", {})

        if ch_stats.get("hiddenSubscriberCount"):
            continue  # cannot place it in a sub band

        subs = _safe_int(ch_stats.get("subscriberCount"))
        views = _safe_int(stats.get("viewCount"))
        likes = _safe_int(stats.get("likeCount"))
        comments = _safe_int(stats.get("commentCount"))

        total_found += 1
        total_views_all += views
        if subs > sub_max:
            big_channel_count += 1
        if subs < sub_min or subs > sub_max:
            continue

        days = _days_since(snippet.get("publishedAt", ""))
        outlier_ratio = views / max(subs, 1)
        velocity = views / days
        engagement = (likes + comments) / max(views, 1)

        thumbs = snippet.get("thumbnails", {})
        thumb_url = (thumbs.get("high") or thumbs.get("medium")
                     or thumbs.get("default") or {}).get("url", "")

        records.append({
            "niche": niche_name,
            "video_id": vid,
            "title": snippet.get("title", ""),
            "channel_id": channel_id,
            "channel_title": snippet.get("channelTitle", ""),
            "channel_thumb": (channel.get("snippet", {}).get("thumbnails", {})
                              .get("default", {}).get("url", "")),
            "channel_video_count": _safe_int(ch_stats.get("videoCount")),
            "channel_total_views": _safe_int(ch_stats.get("viewCount")),
            "channel_country": channel.get("snippet", {}).get("country", ""),
            "channel_created": channel.get("snippet", {}).get("publishedAt", ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "channel_url": f"https://www.youtube.com/channel/{channel_id}",
            "thumbnail": thumb_url,
            "subscribers": subs,
            "views": views,
            "likes": likes,
            "comments": comments,
            "days_since_publish": round(days, 1),
            "published_at": snippet.get("publishedAt", ""),
            "outlier_ratio": round(outlier_ratio, 2),
            "view_velocity": round(velocity, 1),
            "engagement_rate": round(engagement, 4),
        })

    return records, total_found, big_channel_count, total_views_all


def score_videos(records: list, weights: dict) -> None:
    """Assign each record a virality score (0-100), normalized across the batch."""
    if not records:
        return

    log_outlier = [math.log1p(r["outlier_ratio"]) for r in records]
    log_velocity = [math.log1p(r["view_velocity"]) for r in records]
    engagement = [r["engagement_rate"] for r in records]

    n_outlier = _norm(log_outlier)
    n_velocity = _norm(log_velocity)
    n_engagement = _norm(engagement)

    w_o = weights.get("outlier", 0.5)
    w_v = weights.get("velocity", 0.3)
    w_e = weights.get("engagement", 0.2)

    for i, r in enumerate(records):
        score = (w_o * n_outlier[i] + w_v * n_velocity[i] + w_e * n_engagement[i]) * 100
        r["virality_score"] = round(score, 1)


def score_niches(niche_records: dict, niche_meta: dict, found_counts: dict,
                 big_counts: dict, view_totals: dict, niche_weights: dict,
                 fit: Optional[dict] = None) -> list:
    """
    Aggregate per-video scores into per-niche opportunity, automation, demand/
    supply, monetization (RPM), and fit signals. Returns a list of summaries.
    """
    summaries = []

    raw = {}
    for name, records in niche_records.items():
        if not records:
            continue
        distinct_channels = len({r["channel_id"] for r in records})
        avg_outlier = sum(r["outlier_ratio"] for r in records) / len(records)
        median_virality = median([r["virality_score"] for r in records])
        total_found = max(found_counts.get(name, len(records)), 1)
        saturation = big_counts.get(name, 0) / total_found  # 0..1, lower is better
        raw[name] = {
            "breakout_freq": distinct_channels,
            "avg_outlier": avg_outlier,
            "median_virality": median_virality,
            "saturation": saturation,
            "n_videos": len(records),
            "demand_views": view_totals.get(name, 0),
        }

    if not raw:
        return summaries

    names = list(raw.keys())
    n_breakout = _norm([raw[n]["breakout_freq"] for n in names])
    n_outlier = _norm([math.log1p(raw[n]["avg_outlier"]) for n in names])
    n_virality = _norm([raw[n]["median_virality"] for n in names])
    n_open = _norm([1.0 - raw[n]["saturation"] for n in names])  # openness = low saturation
    n_demand = _norm([math.log1p(raw[n]["demand_views"]) for n in names])
    n_supply = _norm([float(raw[n]["n_videos"]) for n in names])  # in-band small-creator supply

    w_b = niche_weights.get("breakout", 0.30)
    w_o = niche_weights.get("outlier", 0.25)
    w_v = niche_weights.get("virality", 0.25)
    w_s = niche_weights.get("openness", 0.20)

    for i, name in enumerate(names):
        opportunity = (w_b * n_breakout[i] + w_o * n_outlier[i]
                       + w_v * n_virality[i] + w_s * n_open[i]) * 100
        meta = niche_meta.get(name, {})
        automatability = meta.get("automatability", 75)
        rpm = meta.get("rpm")
        auto_score = opportunity * (automatability / 100.0)

        # Demand vs supply: high attention but few small-creator wins = a gap.
        demand = n_demand[i]
        gap_score = round(demand * (1.0 - n_supply[i]) * 100, 1)

        fit_score = _fit_score(automatability, fit) if fit else None

        summaries.append({
            "niche": name,
            "format": meta.get("format", "unknown"),
            "opportunity_score": round(opportunity, 1),
            "automatability": automatability,
            "automation_score": round(auto_score, 1),
            "rpm_usd": rpm,
            "demand_score": round(demand * 100, 1),
            "gap_score": gap_score,
            "fit_score": fit_score,
            "breakout_channels": raw[name]["breakout_freq"],
            "avg_outlier_ratio": round(raw[name]["avg_outlier"], 2),
            "median_virality": round(raw[name]["median_virality"], 1),
            "saturation": round(raw[name]["saturation"], 2),
            "n_videos_in_band": raw[name]["n_videos"],
        })

    summaries.sort(key=lambda s: s["opportunity_score"], reverse=True)
    return summaries


def _fit_score(automatability: int, fit: dict) -> float:
    """
    Score how well a niche fits the user's constraints (0-100). Penalizes
    low-automatability niches when the user won't go on camera or has few hours.
    """
    score = 100.0
    if fit.get("on_camera") is False and automatability < 60:
        score -= (60 - automatability)            # camera-heavy niche, no camera
    hours = fit.get("hours_per_week")
    if isinstance(hours, (int, float)) and hours < 5 and automatability < 80:
        score -= (80 - automatability) * 0.5       # little time, needs lots of manual work
    return round(max(score, 0.0), 1)


# === THUMBNAILS ===

def download_thumbnails(records: list, out_dir: Path, per_niche: int) -> None:
    """Download top-N video thumbnails per niche into out_dir/thumbs/."""
    thumbs_dir = out_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    by_niche = {}
    for r in records:
        by_niche.setdefault(r["niche"], []).append(r)

    for niche, recs in by_niche.items():
        top = sorted(recs, key=lambda r: r.get("virality_score", 0), reverse=True)[:per_niche]
        for r in top:
            url = r.get("thumbnail")
            if not url:
                continue
            local = thumbs_dir / f"{r['video_id']}.jpg"
            r["thumbnail_local"] = str(Path("thumbs") / f"{r['video_id']}.jpg")
            if local.exists():
                continue
            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code == 200:
                    with open(local, "wb") as f:
                        f.write(resp.content)
            except requests.RequestException:
                continue


# === OUTPUT ===

def write_report(out_dir: Path, niche_summaries: list, all_records: list,
                 params: dict, top_examples: int) -> Path:
    """Write a scannable markdown report with both rankings and visual examples."""
    lines = []
    lines.append("# Niche Finder Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Filters: subs {params['sub_min']}-{params['sub_max']} | "
                 f"last {params['days']} days | region {params['region']}")
    lines.append(f"Videos in band: {len(all_records)} across {len(niche_summaries)} niches")
    lines.append("")
    lines.append("> Scores are relative to this batch. `outlier x` is the absolute, "
                 "interpretable signal: views divided by subscribers.")
    lines.append("")

    lines.append("## Ranking 1 - Viral Opportunity")
    lines.append("")
    lines.append("| # | Niche | Opportunity | Auto % | ~RPM | Demand | Gap | Avg outlier | Saturation |")
    lines.append("|---|-------|-------------|--------|------|--------|-----|-------------|------------|")
    for i, s in enumerate(niche_summaries, 1):
        rpm = f"${s['rpm_usd']}" if s.get("rpm_usd") is not None else "-"
        lines.append(f"| {i} | {s['niche']} | {s['opportunity_score']} | {s['automatability']}% | "
                     f"{rpm} | {s.get('demand_score', '-')} | {s.get('gap_score', '-')} | "
                     f"{s['avg_outlier_ratio']}x | {s['saturation']} |")
    lines.append("")
    lines.append("*Demand = attention in the niche; Gap = high demand but few small-creator wins "
                 "(opportunity); ~RPM = rough $/1000 views, so virality is not mistaken for income.*")
    lines.append("")
    if any(s.get("fit_score") is not None for s in niche_summaries):
        fit_sorted = sorted(niche_summaries, key=lambda s: s.get("fit_score") or 0, reverse=True)
        lines.append("**Best fit for your constraints:** "
                     + ", ".join(f"{s['niche']} ({s['fit_score']})" for s in fit_sorted[:3]))
        lines.append("")

    auto_sorted = sorted(niche_summaries, key=lambda s: s["automation_score"], reverse=True)
    lines.append("## Ranking 2 - Best for Automation")
    lines.append("")
    lines.append("Opportunity weighted by how much AI can produce end-to-end (faceless-friendly).")
    lines.append("")
    lines.append("| # | Niche | Auto score | Auto % | Format | Opportunity |")
    lines.append("|---|-------|-----------|--------|--------|-------------|")
    for i, s in enumerate(auto_sorted, 1):
        lines.append(f"| {i} | {s['niche']} | {s['automation_score']} | {s['automatability']}% | "
                     f"{s['format']} | {s['opportunity_score']} |")
    lines.append("")

    # Per-niche visual evidence (top examples by virality score).
    lines.append("## Visual Evidence (top outliers per niche)")
    lines.append("")
    by_niche = {}
    for r in all_records:
        by_niche.setdefault(r["niche"], []).append(r)

    for s in niche_summaries:
        niche = s["niche"]
        recs = sorted(by_niche.get(niche, []),
                      key=lambda r: r.get("virality_score", 0), reverse=True)[:top_examples]
        if not recs:
            continue
        lines.append(f"### {niche}  (opportunity {s['opportunity_score']}, "
                     f"automatability {s['automatability']}%)")
        lines.append("")
        for r in recs:
            thumb = r.get("thumbnail_local") or r.get("thumbnail")
            if thumb:
                lines.append(f"![thumb]({thumb})")
            lines.append("")
            lines.append(f"**{r['title']}**  ")
            lines.append(f"{r['channel_title']} | {r['subscribers']:,} subs | "
                         f"{r['views']:,} views | **{r['outlier_ratio']}x outlier** | "
                         f"score {r.get('virality_score', 0)} | {r['days_since_publish']}d old  ")
            traj = r.get("trajectory") or {}
            if traj.get("has_history"):
                lines.append(f"_trajectory: {traj['sub_growth_per_week']:+} subs/week over "
                             f"{traj['span_days']}d ({traj['n_snapshots']} snapshots)_  ")
            lines.append(f"[video]({r['url']}) | [channel]({r['channel_url']})")
            lines.append("")

    report_path = out_dir / "niche-report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return report_path


def write_data(out_dir: Path, niche_summaries: list, all_records: list, params: dict) -> Path:
    data = {
        "generated_at": datetime.now().isoformat(),
        "params": params,
        "niches": niche_summaries,
        "videos": all_records,
    }
    data_path = out_dir / "niche-data.json"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data_path


# === REPOLL & BACKTEST (Phase 1.5: trajectory + calibration) ===

def run_repoll(client: "ApiClient", conn) -> None:
    """Re-fetch stats for every channel/video in the DB and append snapshots."""
    ts = niche_db.now_iso()
    channel_ids = niche_db.known_channel_ids(conn)
    video_ids = niche_db.known_video_ids(conn)
    print(f"Re-polling {len(channel_ids)} channels and {len(video_ids)} videos...")

    channels = fetch_channels(client, channel_ids)
    n_ch = 0
    for cid, ch in channels.items():
        s = ch.get("statistics", {})
        sn = ch.get("snippet", {})
        niche_db.record_channel(conn, cid, sn.get("title", ""), sn.get("country", ""),
                                sn.get("publishedAt", ""), _safe_int(s.get("subscriberCount")),
                                _safe_int(s.get("videoCount")), _safe_int(s.get("viewCount")), ts)
        n_ch += 1

    videos = fetch_videos(client, video_ids)
    n_vid = 0
    for vid, v in videos.items():
        s = v.get("statistics", {})
        sn = v.get("snippet", {})
        niche_db.record_video(conn, vid, sn.get("channelId", ""), sn.get("title", ""),
                              None, sn.get("publishedAt", ""),
                              (sn.get("thumbnails", {}).get("high", {}) or {}).get("url", ""),
                              _safe_int(s.get("viewCount")), _safe_int(s.get("likeCount")),
                              _safe_int(s.get("commentCount")), ts)
        n_vid += 1

    conn.commit()
    print(f"Appended snapshots for {n_ch} channels and {n_vid} videos. Quota spent: "
          f"{client.quota_spent} units. Run this every few days to build trajectory history.")


def run_backtest(client: "ApiClient", conn, min_age_days: int) -> None:
    """Check predictions against current reality and print a calibration report."""
    preds = niche_db.due_predictions(conn, min_age_days)
    if not preds:
        print(f"No predictions are at least {min_age_days} days old yet. Backtesting needs "
              f"history - keep running the finder and check back later.")
        return

    print(f"Backtesting {len(preds)} predictions (>= {min_age_days} days old)...")
    video_ids = [p["video_id"] for p in preds]
    current = fetch_videos(client, video_ids)

    rows = []
    for p in preds:
        v = current.get(p["video_id"])
        if not v:
            continue  # video deleted/private
        now_views = _safe_int(v.get("statistics", {}).get("viewCount"))
        subs0 = max(p["subs_at_prediction"] or 1, 1)
        actual_outlier = now_views / subs0
        rows.append({
            "score": p["virality_score"] or 0,
            "actual_outlier": actual_outlier,
            "hit": actual_outlier >= 10,  # >=10x then-subscriber count = real breakout
        })

    if not rows:
        print("None of the predicted videos are still available to evaluate.")
        return

    # Hit rate by predicted-score band.
    bands = [(0, 25), (25, 50), (50, 75), (75, 101)]
    print("\nPredicted score band -> actual breakout rate (>=10x then-subs):")
    for lo, hi in bands:
        b = [r for r in rows if lo <= r["score"] < hi]
        if not b:
            continue
        rate = sum(1 for r in b if r["hit"]) / len(b) * 100
        avg = sum(r["actual_outlier"] for r in b) / len(b)
        print(f"  {lo:>3}-{hi-1:<3}: {rate:5.1f}% breakout  (n={len(b)}, avg {avg:.1f}x outlier)")

    # Rank correlation sanity check (does higher score -> higher actual outlier?).
    ordered = sorted(rows, key=lambda r: r["score"])
    n = len(ordered)
    top_half = ordered[n // 2:]
    bot_half = ordered[:n // 2]
    if top_half and bot_half:
        ta = sum(r["actual_outlier"] for r in top_half) / len(top_half)
        ba = sum(r["actual_outlier"] for r in bot_half) / len(bot_half)
        verdict = "predictive" if ta > ba else "NOT predictive - tune weights"
        print(f"\nTop-half avg {ta:.1f}x vs bottom-half avg {ba:.1f}x outlier -> score looks {verdict}.")


# === MAIN ===

def main():
    config = load_config()
    niche_cfg = config.get("niche", {}) or {}

    parser = argparse.ArgumentParser(description="TubeFlow Niche Finder")
    parser.add_argument("--seeds", type=str, default="",
                        help="Comma-separated extra niches/keywords to add to the sweep")
    parser.add_argument("--sub-min", type=int, default=niche_cfg.get("sub_min", 500))
    parser.add_argument("--sub-max", type=int, default=niche_cfg.get("sub_max", 15000))
    parser.add_argument("--days", type=int, default=niche_cfg.get("days", 30))
    parser.add_argument("--region", type=str, default=niche_cfg.get("region", "US"))
    parser.add_argument("--max-per-keyword", type=int,
                        default=niche_cfg.get("max_per_keyword", 50))
    parser.add_argument("--api-key", type=str, default="",
                        help="YouTube Data API key (else read from env)")
    parser.add_argument("--output", type=str, default="",
                        help="Output directory (default: <youtube_root>/niche-research/<date>)")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-score from cached responses only (no network, 0 quota)")
    parser.add_argument("--repoll", action="store_true",
                        help="Re-fetch stats for channels/videos already in the DB and append "
                             "snapshots (builds trajectory history). Cheap: 1 quota unit/50 items")
    parser.add_argument("--backtest", action="store_true",
                        help="Evaluate past predictions (>= --backtest-min-age days old) against "
                             "current reality and print a calibration report")
    parser.add_argument("--backtest-min-age", type=int, default=30,
                        help="Minimum age in days for a prediction to be backtested (default 30)")
    parser.add_argument("--no-db", action="store_true",
                        help="Skip writing to the local accumulation DB")
    parser.add_argument("--no-thumbnails", action="store_true",
                        help="Skip downloading thumbnail images")
    parser.add_argument("--top-examples", type=int, default=3,
                        help="Top videos shown per niche in the report")
    args = parser.parse_args()

    # Resolve API key.
    api_key_env = niche_cfg.get("api_key_env", "YOUTUBE_API_KEY")
    api_key = args.api_key or os.environ.get(api_key_env, "")
    if not api_key and not args.rescore:
        print(f"Error: No API key. Set ${api_key_env} or pass --api-key.")
        print("Create one at https://console.cloud.google.com/ (enable YouTube Data API v3).")
        sys.exit(1)

    # Phase 1.5 modes that operate on the accumulation DB instead of discovering.
    if args.repoll or args.backtest:
        if niche_db is None:
            print("Error: niche_db module not found next to this script; "
                  "--repoll/--backtest require it.")
            sys.exit(1)
        cache_dir = Path(__file__).parent / ".niche_cache"
        client = ApiClient(api_key or "none", cache_dir, rescore=False, force_refresh=True)
        conn = niche_db.connect()
        if args.repoll:
            run_repoll(client, conn)
        if args.backtest:
            run_backtest(client, conn, args.backtest_min_age)
        conn.close()
        return

    seeds = [s for s in args.seeds.split(",") if s.strip()] if args.seeds else []
    niches = get_candidate_niches(config, seeds)

    # Quota estimate.
    total_searches = sum(len(n.get("keywords", [])) for n in niches)
    est_quota = total_searches * 100
    print(f"Niches to sweep: {len(niches)} ({total_searches} keyword searches)")
    if args.rescore:
        print("Mode: --rescore (cache only, 0 quota)")
    else:
        print(f"Estimated quota: ~{est_quota} units (search) + a few units for lookups")
        print(f"  Default daily quota is 10,000 units. Filters: subs "
              f"{args.sub_min}-{args.sub_max}, last {args.days} days, region {args.region}")
    print("")

    published_after = (datetime.now(timezone.utc) - timedelta(days=args.days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")

    cache_dir = Path(__file__).parent / ".niche_cache"
    client = ApiClient(api_key or "none", cache_dir, rescore=args.rescore)

    weights = niche_cfg.get("weights", {"outlier": 0.5, "velocity": 0.3, "engagement": 0.2})
    niche_weights = niche_cfg.get("niche_weights",
                                  {"breakout": 0.30, "outlier": 0.25,
                                   "virality": 0.25, "openness": 0.20})

    niche_records = {}
    niche_meta = {}
    found_counts = {}
    big_counts = {}
    view_totals = {}

    for niche in niches:
        name = niche["name"]
        niche_meta[name] = niche
        print(f"[{name}] searching {niche.get('keywords', [])}...")

        video_ids = []
        for kw in niche.get("keywords", []):
            video_ids.extend(search_videos(client, kw, published_after,
                                           args.region, args.max_per_keyword))
        video_ids = list(dict.fromkeys(video_ids))  # dedupe, keep order
        if not video_ids:
            print("  no videos found")
            niche_records[name] = []
            continue

        videos = fetch_videos(client, video_ids)
        channel_ids = {v.get("snippet", {}).get("channelId")
                       for v in videos.values() if v.get("snippet", {}).get("channelId")}
        channels = fetch_channels(client, channel_ids)

        records, total_found, big, views_all = build_video_records(
            videos, channels, name, args.sub_min, args.sub_max)
        niche_records[name] = records
        found_counts[name] = total_found
        big_counts[name] = big
        view_totals[name] = views_all
        print(f"  {len(records)} in band (of {total_found} found, {big} big channels)")

    # Score every in-band video globally (so scores compare across niches).
    all_records = [r for recs in niche_records.values() for r in recs]
    score_videos(all_records, weights)

    if not all_records:
        print("\nNo videos matched the subscriber band. Try widening --sub-max or "
              "increasing --days, or check your API key/quota.")
        sys.exit(0)

    # Accumulate into the DB (channels, videos, snapshots, predictions) and
    # enrich each record with the channel's growth trajectory from history.
    if niche_db is not None and not args.no_db:
        conn = niche_db.connect()
        ts = niche_db.now_iso()
        seen_channels = {}
        for r in all_records:
            cid = r["channel_id"]
            if cid and cid not in seen_channels:
                niche_db.record_channel(conn, cid, r["channel_title"],
                                        r["channel_country"], r["channel_created"],
                                        r["subscribers"], r["channel_video_count"],
                                        r["channel_total_views"], ts)
                seen_channels[cid] = True
            niche_db.record_video(conn, r["video_id"], cid, r["title"], r["niche"],
                                  r["published_at"], r["thumbnail"],
                                  r["views"], r["likes"], r["comments"], ts)
            niche_db.log_prediction(conn, r["video_id"], r["niche"],
                                    r.get("virality_score", 0), r["subscribers"],
                                    r["views"], r["outlier_ratio"], ts)
        conn.commit()
        for r in all_records:
            traj = niche_db.channel_trajectory(conn, r["channel_id"])
            r["trajectory"] = traj
        db_stats = niche_db.stats(conn)
        conn.close()
        print(f"\nDB now holds {db_stats['channels']} channels, {db_stats['videos']} videos, "
              f"{db_stats['channel_snapshots']} channel snapshots "
              f"(re-run with --repoll over days to build trajectory history).")

    fit = niche_cfg.get("fit") or None
    niche_summaries = score_niches(niche_records, niche_meta, found_counts,
                                   big_counts, view_totals, niche_weights, fit)

    # Output location.
    if args.output:
        out_dir = Path(os.path.expanduser(args.output))
    else:
        youtube_root = (config.get("structure", {}) or {}).get("youtube_root", "03-YouTube")
        date_str = datetime.now().strftime("%Y-%m-%d")
        out_dir = Path.cwd() / youtube_root / "niche-research" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_thumbnails and not args.rescore:
        per_niche = max(args.top_examples, 3)
        download_thumbnails(all_records, out_dir, per_niche)

    data_path = write_data(out_dir, niche_summaries, all_records,
                           {"sub_min": args.sub_min, "sub_max": args.sub_max,
                            "days": args.days, "region": args.region})
    report_path = write_report(out_dir, niche_summaries, all_records,
                               {"sub_min": args.sub_min, "sub_max": args.sub_max,
                                "days": args.days, "region": args.region},
                               args.top_examples)

    print(f"\nDone. Quota spent this run: {client.quota_spent} units")
    print(f"  Data:   {data_path}")
    print(f"  Report: {report_path}")
    print("\nTop niches by viral opportunity:")
    for i, s in enumerate(niche_summaries[:5], 1):
        print(f"  {i}. {s['niche']} - opportunity {s['opportunity_score']}, "
              f"automatability {s['automatability']}%")
    print("\nNext: review the report, or run /youtube niche to get the full playbook.")


if __name__ == "__main__":
    main()
