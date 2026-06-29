# Niche Finder

Find niches a new small channel can actually break into, rate them by viral
likelihood and by how much AI can produce, and get a per-niche playbook with real
thumbnails and titles. This is TubeFlow's answer to tools like ViewStats and
TubeMagic, built on public YouTube data you control.

---

## What it does

```
/youtube niche                      # sweep the built-in candidate niches
/youtube niche "homelab, ai tools"  # add your own seed niches
```

For each niche it produces:

1. **A virality rating (0-100)** per outlier video, built around *low subs, high views*.
2. **A niche opportunity score** - is this a niche where small channels *repeatedly* break out?
3. **An AI-automatability rating** - how much of the content AI can produce (100% down).
4. **Two rankings** - "Viral Opportunity" and "Best for Automation".
5. **A playbook** - what channel to start, what videos to make, and the workflow to do it.
6. **Visual evidence** - the actual thumbnails + titles of the breakout videos.

---

## How the virality score works

A video is "viral relative to its size" when it pulls views far beyond what its
subscriber count predicts. The score blends three public signals:

| Signal | Formula | Why |
|--------|---------|-----|
| **Outlier ratio** (50%) | `views / subscribers` | The core "small channel, big views" breakout signal |
| **View velocity** (30%) | `views / days since upload` | Is it accelerating *now*, not just old and large |
| **Engagement** (20%) | `(likes + comments) / views` | Audience is reacting, not just impressions |

The heavy-tailed signals (outlier, velocity) are log-scaled and normalized across
the batch so one mega-hit doesn't flatten everything else. The score is **relative
to the batch**; the report always also shows the raw **outlier multiplier** (e.g.
`12.4x`) as the absolute, interpretable number.

Weights are configurable in `config.yaml` under `niche.weights`.

## How the niche score works

A single fluke isn't a niche you can enter. The **opportunity score** rewards
niches where the breakout is *repeatable*:

| Factor | Weight | Meaning |
|--------|--------|---------|
| **Breakout frequency** | 30% | How many *distinct* small channels broke out (not one fluke) |
| **Avg outlier strength** | 25% | How hard the breakouts over-performed |
| **Median virality** | 25% | The typical (not best-case) result |
| **Openness** | 20% | Low saturation by mega-channels = room for a newcomer |

Configurable under `niche.niche_weights`.

## How automatability works

Each niche gets a score for **how much of the work AI can do end-to-end**, 100% down:

| Format | Automatability |
|--------|----------------|
| Text + TTS voice + stock/AI visuals (faceless) | 90-100% |
| Screen recording + voiceover | 70-85% |
| Compilation / curation | 75-90% |
| Generic original footage (b-roll, slideshow) | 55-70% |
| On-camera presenter / physical reviews | 30-55% |
| IRL stunts, location shoots | 0-30% |

The `niche_finder.py` script seeds this from the candidate list; the
`yt-niche-finder` agent re-rates it against the actual top videos and spells out
**what AI does vs. what the human does**. The "Best for Automation" ranking is
`opportunity x automatability`, surfacing niches that are both hot *and* faceless.

---

## Using it with the algorithm

The reason the outlier ratio matters: when a video pulls views far beyond a
channel's subscriber base, that traffic is coming from **Browse and Suggested**,
not subscribers. That only happens when YouTube's algorithm *chooses* to push it.
So a high outlier ratio in the last 30 days is direct evidence the algorithm is
distributing that format to cold audiences - exactly what a new channel needs.

What the algorithm actually rewards downstream is **click-through rate** (title +
thumbnail) and **average view duration** (retention). We can't see those - YouTube
keeps them private to the channel owner, and *no* external tool (paid included) has
them. The score is a **public-signal proxy**. That's why the playbook focuses you
on the **title and thumbnail pattern** of proven outliers: copying the pattern that
already won CTR in that niche is the closest you get to optimizing the levers you
can't measure.

**The workflow:**
```
/youtube niche            ->  pick a niche from the playbook
/youtube-research "Topic" ->  deepen it (competitors, SEO, community)
/youtube full "Title"     ->  produce the draft, matching the winning pattern
[production]              ->  film/assemble (see automatability % for how much is AI)
```

---

## Honest limitations

- **Seed-driven, not a blind crawl.** The YouTube API has no "search channels by
  subscriber range" endpoint. Discovery works from candidate/seed keywords, so it
  compares niches you point it at - it does not scan all of YouTube. (A blind crawl
  would require scraping, which violates YouTube's ToS and is deliberately avoided.)
- **Quota-limited.** `search.list` costs 100 units; the default free quota is 10,000
  units/day (~90 searches). The script prints an estimate and caches every response
  so you can re-score with `--rescore` for zero extra quota.
- **Relative scores.** Scores rank within a run; the raw outlier multiplier is the
  absolute number to trust across runs.
- **No private analytics.** CTR and retention are invisible to every external tool.
- **Rounded subs.** YouTube rounds public subscriber counts, so band edges are fuzzy.

---

## Setup

1. Create a key: [Google Cloud Console](https://console.cloud.google.com/) ->
   "APIs & Services" -> enable **YouTube Data API v3** -> create an **API key**.
2. Export it: `export YOUTUBE_API_KEY="your-key"`
3. Install deps: `pip install requests pyyaml`
4. Run: `/youtube niche` (or `python .claude/scripts/niche_finder.py`)

Tune `sub_min`, `sub_max`, `days`, `region`, weights, and the candidate niche list
in `config.yaml` under `niche:` (see `config.example.yaml`).

---

## Roadmap: automated production

The niche finder is step one. Once it surfaces a niche you trust, the planned Phase 2
ties the automatability score into production:

- **Faceless / fully automated:** script -> TTS voiceover -> stock/AI visuals ->
  assembled with **ffmpeg**, **Remotion**, or **OpenCut**.
- **Assisted / templated:** AI-generated assets + **Canva** templates for
  thumbnails/overlays; you record or finalize.

Build the finder's signal first, then automate against proven niches - not guesses.
