#!/usr/bin/env python3
"""
TubeFlow Niche Dashboard.

Turns the niche finder's output into a single self-contained, clickable
dashboard.html: a card per niche (thumbnail, virality + AI-autonomy scores, why
it works, recommendation) that opens a full workflow on click - a ready-to-make
video plus backup ideas.

Reads:
  niche-data.json           (required) - from niche_finder.py
  niche-workflows.json      (optional) - per-niche workflow content authored by
                             the yt-niche-finder agent: {"<niche>": {channel_concept,
                             ready_video: {title, hook, outline[], thumbnail_brief,
                             description}, backups[], why, recommendation, fingerprint}}

Writes:
  dashboard.html            - one portable file (data inlined, thumbnails remote)

Usage:
  python .claude/scripts/niche_dashboard.py <research-dir>
  python .claude/scripts/niche_dashboard.py 03-YouTube/niche-research/2026-06-29
"""

import json
import sys
from pathlib import Path


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def derive_why(niche: dict, examples: list) -> str:
    """Build a data-grounded 'why it works' line if the agent didn't supply one."""
    parts = []
    if niche.get("breakout_channels"):
        parts.append(f"{niche['breakout_channels']} small channels broke out here recently")
    if examples:
        top = examples[0]
        parts.append(f"top example hit {top['outlier_ratio']}x its subscriber count "
                     f"({top['views']:,} views on {top['subscribers']:,} subs)")
    if niche.get("gap_score", 0) >= 50:
        parts.append("high demand but few small-creator wins (an opening)")
    return "; ".join(parts) + "." if parts else "Outlier activity detected in this niche."


def derive_reco(niche: dict) -> str:
    """Heuristic recommendation if the agent didn't supply one."""
    auto = niche.get("automatability", 0)
    rpm = niche.get("rpm_usd")
    bits = []
    if auto >= 90:
        bits.append("nearly fully automatable (faceless)")
    elif auto >= 70:
        bits.append("mostly automatable with light human input")
    else:
        bits.append("needs meaningful human production")
    if rpm is not None:
        if rpm >= 10:
            bits.append(f"strong monetization (~${rpm} RPM)")
        elif rpm <= 3:
            bits.append(f"low RPM (~${rpm}) - go for volume or use it to grow, not earn")
        else:
            bits.append(f"moderate monetization (~${rpm} RPM)")
    return "Good pick: " + ", ".join(bits) + "."


def effort_badge(auto: int) -> str:
    if auto >= 90:
        return "Very easy - days to first video"
    if auto >= 70:
        return "Easy - light editing/recording"
    if auto >= 50:
        return "Moderate - on-camera or real footage"
    return "Hard - significant production"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TubeFlow Niche Dashboard</title>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --card2:#222633; --txt:#e8eaf0; --mut:#9aa0b4;
          --accent:#ff5252; --good:#42d77d; --warn:#ffb74d; --blue:#5b8cff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:24px 28px 8px; }
  h1 { margin:0 0 4px; font-size:24px; }
  .sub { color:var(--mut); font-size:13px; }
  .controls { display:flex; flex-wrap:wrap; gap:8px; padding:14px 28px; align-items:center; }
  .controls label { color:var(--mut); font-size:13px; margin-right:4px; }
  button.sort { background:var(--card2); color:var(--txt); border:1px solid #313749;
                border-radius:20px; padding:6px 14px; cursor:pointer; font-size:13px; }
  button.sort.active { background:var(--blue); border-color:var(--blue); color:#fff; }
  #search { background:var(--card2); border:1px solid #313749; color:var(--txt);
            border-radius:20px; padding:6px 14px; font-size:13px; margin-left:auto; min-width:200px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr));
          gap:18px; padding:14px 28px 40px; }
  .card { background:var(--card); border:1px solid #262a37; border-radius:14px; overflow:hidden;
          cursor:pointer; transition:transform .12s, border-color .12s; display:flex; flex-direction:column; }
  .card:hover { transform:translateY(-3px); border-color:var(--blue); }
  .thumb { width:100%; aspect-ratio:16/9; object-fit:cover; background:#000; display:block; }
  .card .body { padding:14px 16px 16px; display:flex; flex-direction:column; gap:8px; }
  .niche-name { font-weight:700; font-size:17px; }
  .scores { display:flex; gap:10px; }
  .score { flex:1; background:var(--card2); border-radius:10px; padding:8px 10px; text-align:center; }
  .score .n { font-size:22px; font-weight:800; line-height:1; }
  .score .l { font-size:10px; color:var(--mut); text-transform:uppercase; letter-spacing:.5px; margin-top:3px; }
  .meta { display:flex; flex-wrap:wrap; gap:6px; }
  .pill { font-size:11px; padding:3px 8px; border-radius:20px; background:#2a2f3e; color:var(--mut); }
  .pill.good { background:rgba(66,215,125,.15); color:var(--good); }
  .pill.warn { background:rgba(255,183,77,.15); color:var(--warn); }
  .why { color:var(--mut); font-size:13px; }
  .reco { font-size:13px; color:var(--txt); }
  /* modal */
  .overlay { position:fixed; inset:0; background:rgba(0,0,0,.7); display:none;
             align-items:flex-start; justify-content:center; padding:40px 16px; overflow:auto; z-index:10; }
  .overlay.open { display:flex; }
  .modal { background:var(--card); border:1px solid #2c3142; border-radius:16px; max-width:780px;
           width:100%; padding:28px 30px 34px; }
  .modal h2 { margin:0 0 4px; font-size:23px; }
  .modal .close { float:right; font-size:24px; color:var(--mut); cursor:pointer; line-height:1; }
  .modal h3 { margin:22px 0 8px; font-size:15px; color:var(--blue); text-transform:uppercase;
              letter-spacing:.6px; }
  .modal p, .modal li { font-size:14px; }
  .vid { background:var(--card2); border-radius:12px; padding:16px 18px; }
  .vid .title { font-weight:700; font-size:17px; margin-bottom:6px; }
  .cmd { background:#0b0d13; border:1px solid #2c3142; border-radius:8px; padding:10px 12px;
         font-family:ui-monospace,Menlo,monospace; font-size:13px; color:var(--good);
         display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .cmd button { background:var(--blue); color:#fff; border:0; border-radius:6px; padding:5px 10px;
                cursor:pointer; font-size:12px; }
  .ex { display:flex; gap:12px; margin:10px 0; align-items:flex-start; }
  .ex img { width:120px; aspect-ratio:16/9; object-fit:cover; border-radius:6px; }
  .ex .t { font-size:13px; }
  .ex .s { color:var(--mut); font-size:12px; }
  a { color:var(--blue); }
</style>
</head>
<body>
<header>
  <h1>TubeFlow Niche Dashboard</h1>
  <div class="sub">__SUB__</div>
</header>
<div class="controls">
  <label>Sort by</label>
  <button class="sort active" data-sort="opportunity_score">Viral Opportunity</button>
  <button class="sort" data-sort="automation_score">Best for Automation</button>
  <button class="sort" data-sort="rpm_usd">Highest RPM</button>
  <button class="sort" data-sort="fit_score">Best Fit</button>
  <input id="search" placeholder="Filter niches...">
</div>
<div class="grid" id="grid"></div>
<div class="overlay" id="overlay"><div class="modal" id="modal"></div></div>
<script>
const DATA = __DATA__;
let sortKey = "opportunity_score";

function pill(txt, cls){ return `<span class="pill ${cls||''}">${txt}</span>`; }
function esc(s){ return (s||"").replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function cardHTML(n){
  const ex = (n.examples&&n.examples[0]) || {};
  const thumb = ex.thumbnail || "";
  const rpm = n.rpm_usd!=null ? `$${n.rpm_usd} RPM` : "RPM n/a";
  const rpmCls = n.rpm_usd!=null ? (n.rpm_usd>=10?"good":(n.rpm_usd<=3?"warn":"")) : "warn";
  return `<div class="card" data-niche="${esc(n.niche)}">
    ${thumb?`<img class="thumb" loading="lazy" src="${esc(thumb)}" alt="">`:`<div class="thumb"></div>`}
    <div class="body">
      <div class="niche-name">${esc(n.niche)}</div>
      <div class="scores">
        <div class="score"><div class="n">${Math.round(n.opportunity_score)}</div><div class="l">Virality</div></div>
        <div class="score"><div class="n">${n.automatability}%</div><div class="l">AI Autonomy</div></div>
      </div>
      <div class="meta">${pill(rpm,rpmCls)}${pill(n.effort)}${n.gap_score>=50?pill("Open gap","good"):""}</div>
      <div class="why">${esc(n.why)}</div>
      <div class="reco">${esc(n.recommendation)}</div>
    </div></div>`;
}

function render(){
  const q = (document.getElementById('search').value||"").toLowerCase();
  const list = DATA.filter(n=>n.niche.toLowerCase().includes(q))
    .sort((a,b)=>(b[sortKey]||0)-(a[sortKey]||0));
  document.getElementById('grid').innerHTML = list.map(cardHTML).join("");
  document.querySelectorAll('.card').forEach(c=>c.onclick=()=>openModal(c.dataset.niche));
}

function openModal(niche){
  const n = DATA.find(x=>x.niche===niche); if(!n) return;
  const rv = n.ready_video||{};
  const exs = (n.examples||[]).slice(0,3).map(e=>`<div class="ex">
      ${e.thumbnail?`<img src="${esc(e.thumbnail)}">`:""}
      <div><div class="t">${esc(e.title)}</div>
      <div class="s">${esc(e.channel_title)} · ${e.subscribers.toLocaleString()} subs · ${e.views.toLocaleString()} views · ${e.outlier_ratio}x</div>
      <div class="s"><a href="${e.url}" target="_blank">watch</a></div></div></div>`).join("");
  const outline = (rv.outline||[]).map(o=>`<li>${esc(o)}</li>`).join("");
  const backups = (n.backups||[]).map(b=>`<li>${esc(b)}</li>`).join("");
  const cmd = rv.title ? `/youtube full "${rv.title.replace(/"/g,'')}"` : `/youtube full "${niche} video 1"`;
  document.getElementById('modal').innerHTML = `
    <span class="close" onclick="closeModal()">&times;</span>
    <h2>${esc(n.niche)}</h2>
    <div class="meta">${pill("Virality "+Math.round(n.opportunity_score))}${pill("AI "+n.automatability+"%")}${pill(n.rpm_usd!=null?"$"+n.rpm_usd+" RPM":"RPM n/a")}${pill(n.effort)}</div>
    <h3>Why this works</h3><p>${esc(n.why_long||n.why)}</p>
    <h3>Recommendation</h3><p>${esc(n.recommendation)}</p>
    ${n.fingerprint?`<h3>Winning format</h3><p>${esc(n.fingerprint)}</p>`:""}
    ${n.channel_concept?`<h3>Channel concept</h3><p>${esc(n.channel_concept)}</p>`:""}
    <h3>Your first video (ready to make)</h3>
    <div class="vid">
      <div class="title">${esc(rv.title||"(run /youtube niche to generate)")}</div>
      ${rv.hook?`<p><b>Hook:</b> ${esc(rv.hook)}</p>`:""}
      ${outline?`<p><b>Outline:</b></p><ul>${outline}</ul>`:""}
      ${rv.thumbnail_brief?`<p><b>Thumbnail:</b> ${esc(rv.thumbnail_brief)}</p>`:""}
      ${rv.description?`<p><b>Description:</b> ${esc(rv.description)}</p>`:""}
    </div>
    <div class="cmd" style="margin-top:12px"><span id="cmd">${esc(cmd)}</span>
      <button onclick="navigator.clipboard.writeText(document.getElementById('cmd').innerText)">Copy command</button></div>
    ${backups?`<h3>Backup video ideas</h3><ul>${backups}</ul>`:""}
    ${exs?`<h3>Proof: real outliers in this niche</h3>${exs}`:""}`;
  document.getElementById('overlay').classList.add('open');
}
function closeModal(){ document.getElementById('overlay').classList.remove('open'); }
document.getElementById('overlay').onclick=e=>{ if(e.target.id==='overlay') closeModal(); };
document.querySelectorAll('button.sort').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('button.sort').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); sortKey=b.dataset.sort; render();
});
document.getElementById('search').oninput=render;
render();
</script>
</body>
</html>
"""


def build(research_dir: Path) -> Path:
    data = load_json(research_dir / "niche-data.json", None)
    if not data:
        print(f"Error: no niche-data.json in {research_dir}. Run niche_finder.py first.")
        sys.exit(1)
    workflows = load_json(research_dir / "niche-workflows.json", {})

    videos = data.get("videos", [])
    by_niche = {}
    for v in videos:
        by_niche.setdefault(v["niche"], []).append(v)

    cards = []
    for n in data.get("niches", []):
        name = n["niche"]
        examples = sorted(by_niche.get(name, []),
                          key=lambda r: r.get("virality_score", 0), reverse=True)[:3]
        wf = workflows.get(name, {})
        card = dict(n)
        card["effort"] = effort_badge(n.get("automatability", 0))
        card["examples"] = [{
            "title": e["title"], "channel_title": e["channel_title"],
            "subscribers": e["subscribers"], "views": e["views"],
            "outlier_ratio": e["outlier_ratio"], "url": e["url"],
            "thumbnail": e.get("thumbnail_local") or e.get("thumbnail", ""),
        } for e in examples]
        card["why"] = wf.get("why") or derive_why(n, card["examples"])
        card["why_long"] = wf.get("why_long") or card["why"]
        card["recommendation"] = wf.get("recommendation") or derive_reco(n)
        card["channel_concept"] = wf.get("channel_concept", "")
        card["fingerprint"] = wf.get("fingerprint", "")
        card["ready_video"] = wf.get("ready_video", {})
        card["backups"] = wf.get("backups", [])
        cards.append(card)

    params = data.get("params", {})
    sub = (f"{len(cards)} niches | subs {params.get('sub_min')}-{params.get('sub_max')} | "
           f"last {params.get('days')} days | generated {data.get('generated_at','')[:16]}")
    html = (HTML_TEMPLATE
            .replace("__SUB__", sub)
            .replace("__DATA__", json.dumps(cards, ensure_ascii=False)))
    out = research_dir / "dashboard.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def main():
    if len(sys.argv) < 2:
        print("Usage: python niche_dashboard.py <research-dir>")
        sys.exit(1)
    out = build(Path(sys.argv[1]))
    print(f"Dashboard written: {out}")
    print("Open it in a browser (double-click). Click any card for the full workflow.")


if __name__ == "__main__":
    main()
