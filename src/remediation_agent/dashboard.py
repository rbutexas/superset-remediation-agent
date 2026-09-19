"""HTML dashboard.

Self-contained: inline CSS and SVG, no scripts, no fonts, no CDN. Consistent
with the package having no runtime dependencies, and it means the file can be
opened from disk, attached to an email, or screen-shared without a server.

Two colour decisions worth stating, because they encode the argument the system
makes rather than just decorating it:

**Resolution outcomes use categorical hues, not a good/bad ramp.** `fixed` and
`decline_not_actionable` are different *kinds* of answer, not different *grades*
of one. Painting the fix green and the dismissal amber would say the dismissal
was second best, which is precisely the claim being argued against. They get
equal visual standing.

**Status colours are reserved for genuine problems** — a stalled session — and
always ship with an icon and a label, never colour alone.

Palette is the validated default. The four categorical slots clear every
adjacent gate in both modes (worst CVD ΔE 9.1 light / 8.4 dark; worst
normal-vision ΔE 22.9 / 19.8). Two light-mode slots sit below 3:1 on the light
surface, so the relief rule applies: every segment carries a direct label and a
full table view exists below.
"""

from __future__ import annotations

import html
import time
from typing import Any

from .report import Report, _duration

# Outcome -> (css role, human label). Order is fixed: categorical hues are
# assigned in slot order and never cycled.
OUTCOME_STYLE: dict[str, tuple[str, str]] = {
    "fixed":                  ("series-1", "Fixed"),
    "decline_not_actionable": ("series-2", "Dismissed with evidence"),
    "blocked_upstream":       ("series-3", "Blocked upstream"),
    "mitigated":              ("series-4", "Mitigated"),
    "STALLED":                ("critical", "Stalled"),
    "ABANDONED":              ("critical", "Ended without answering"),
    "failed_verification":    ("critical", "Failed verification"),
    "escalate_to_human":      ("warning",  "Escalated"),
    "remediating":            ("muted",    "Remediating"),
    "awaiting triage":        ("muted",    "Awaiting triage"),
    "detected":               ("muted",    "Detected"),
}

RESOLVED_ROLES = {"series-1", "series-2", "series-3", "series-4"}


def _style(status: str) -> tuple[str, str]:
    return OUTCOME_STYLE.get(status, ("muted", status))


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


# ------------------------------------------------------------------ marks

def _stacked_bar(report: Report) -> str:
    """One horizontal stacked bar: the composition of all answers given.

    A 2px surface gap separates segments, and the outer ends are rounded 4px and
    anchored to the track — the shape reads as one whole divided up, which is
    what the data is.
    """
    mix = report.outcome_mix.most_common()
    total = sum(count for _, count in mix) or 1
    width, height, gap, radius = 960, 34, 2, 4

    segments, legend, x = [], [], 0.0
    for index, (status, count) in enumerate(mix):
        role, label = _style(status)
        raw = width * count / total
        seg_w = max(raw - (gap if index < len(mix) - 1 else 0), 2)

        first, last = index == 0, index == len(mix) - 1
        # Round only the outer ends of the whole track.
        path = _rounded_rect(x, 0, seg_w, height, radius,
                             left=first, right=last)
        segments.append(
            f'<path d="{path}" fill="var(--{role})" class="seg">'
            f'<title>{esc(label)}: {count} of {total}</title></path>'
        )

        # Direct label inside the segment when it fits; this is the relief for
        # the sub-3:1 light-mode slots, so it is not optional.
        if seg_w > 46:
            segments.append(
                f'<text x="{x + seg_w / 2:.1f}" y="{height / 2 + 5:.0f}" '
                f'class="seg-label" text-anchor="middle">{count}</text>'
            )

        legend.append(
            f'<li><span class="swatch" style="background:var(--{role})"></span>'
            f'{esc(label)} <b>{count}</b></li>'
        )
        x += raw

    return (
        f'<svg viewBox="0 0 {width} {height}" class="stack" role="img" '
        f'aria-label="Outcome mix across {total} findings">'
        + "".join(segments) + "</svg>"
        + f'<ul class="legend">{"".join(legend)}</ul>'
    )


def _rounded_rect(x: float, y: float, w: float, h: float, r: float,
                  *, left: bool, right: bool) -> str:
    r = min(r, w / 2, h / 2)
    rl, rr = (r if left else 0), (r if right else 0)
    return (
        f"M{x + rl:.1f},{y} H{x + w - rr:.1f} "
        + (f"A{rr},{rr} 0 0 1 {x + w:.1f},{y + rr:.1f} " if rr else "")
        + f"V{y + h - rr:.1f} "
        + (f"A{rr},{rr} 0 0 1 {x + w - rr:.1f},{y + h:.1f} " if rr else f"H{x + w:.1f} ")
        + f"H{x + rl:.1f} "
        + (f"A{rl},{rl} 0 0 1 {x:.1f},{y + h - rl:.1f} " if rl else "")
        + f"V{y + rl:.1f} "
        + (f"A{rl},{rl} 0 0 1 {x + rl:.1f},{y:.1f} " if rl else "")
        + "Z"
    )


def _verdict_bars(report: Report) -> str:
    """Time from detection to a verdict, one bar per finding.

    Answers "is this keeping up?" — the throughput question — without a time
    series, which four data points would not support.
    """
    rows = [f for f in report.findings if f.time_to_verdict is not None]
    if not rows:
        return '<p class="empty">No verdicts recorded yet.</p>'

    longest = max(f.time_to_verdict for f in rows) or 1
    width, bar_h, step = 720, 16, 30
    out = []

    for i, f in enumerate(sorted(rows, key=lambda r: r.time_to_verdict or 0,
                                 reverse=True)):
        role, label = _style(f.status)
        y = i * step
        bar_w = max(width * (f.time_to_verdict or 0) / longest, 3)
        out.append(
            f'<path d="{_rounded_rect(0, y, bar_w, bar_h, 4, left=False, right=True)}" '
            f'fill="var(--{role})" class="seg">'
            f'<title>{esc(f.key)} — {_duration(f.time_to_verdict)} to {esc(label)}</title>'
            f'</path>'
            f'<text x="{bar_w + 8:.0f}" y="{y + bar_h - 3}" class="bar-label">'
            f'{_duration(f.time_to_verdict)}</text>'
        )

    labels = "".join(
        f'<li title="{esc(f.key)}">{esc(f.key.split(":")[-1])}</li>'
        for f in sorted(rows, key=lambda r: r.time_to_verdict or 0, reverse=True)
    )
    return (
        '<div class="bars">'
        f'<ul class="bar-names">{labels}</ul>'
        f'<svg viewBox="0 0 {width + 70} {len(rows) * step}" role="img" '
        f'aria-label="Time from detection to verdict, per finding">'
        + "".join(out) + "</svg></div>"
    )


# ------------------------------------------------------------------ sections

def _tiles(report: Report) -> str:
    stalled = len(report.stalled_sessions)
    needs_you = stalled + len(report.abandoned_sessions)
    tiles = [
        ("Found", len(report.findings), "problems detected", ""),
        ("Answered", len(report.resolved), "reached a decision", "good"),
        ("Pull requests", len(report.prs), "raised for review", ""),
        ("Needs you", needs_you, "blocked or unanswered",
         "bad" if needs_you else ""),
    ]
    return "".join(
        f'<div class="tile {tone}"><div class="tile-n">{value}</div>'
        f'<div class="tile-k">{esc(name)}</div>'
        f'<div class="tile-s">{esc(sub)}</div></div>'
        for name, value, sub, tone in tiles
    )


def _attention_panel(report: Report) -> str:
    """What needs a person. Counted in findings, because that is the unit of
    work; the session link is only how you go and look."""
    blocks = []

    if report.stalled_sessions:
        items = "".join(
            f'<li><b>{esc(s["finding"] or s["session_id"][:12])}</b> — the agent '
            f'asked a question {_duration(report.generated_at - s["waiting_since"])} '
            f'ago and is waiting. <a href="{esc(s["url"])}">Answer it</a></li>'
            for s in report.stalled_sessions
        )
        blocks.append(
            '<div class="callout bad"><span class="ico">!</span><div>'
            f'<b>{len(report.stalled_sessions)} item(s) waiting on a person.</b> '
            'Work has stopped on these, and nobody has been told.'
            f'<ul>{items}</ul></div></div>'
        )

    if report.abandoned_sessions:
        items = "".join(
            f'<li><b>{esc(s["finding"] or s["session_id"][:12])}</b> — stopped '
            f'({esc(s["detail"])}) without reaching an answer. '
            f'<a href="{esc(s["url"])}">See why</a></li>'
            for s in report.abandoned_sessions
        )
        blocks.append(
            '<div class="callout bad"><span class="ico">!</span><div>'
            f'<b>{len(report.abandoned_sessions)} item(s) ended without an answer.</b> '
            'Out of budget, errored, or left waiting too long. Without this they '
            'would be counted as finished.'
            f'<ul>{items}</ul></div></div>'
        )

    if not blocks:
        return ('<div class="callout ok"><span class="ico">✓</span>'
                '<div><b>Nothing needs you.</b> No work item is blocked or has '
                'stopped without an answer.</div></div>')
    return "".join(blocks)


STAGE_WORDS = {
    "triage": "Being assessed",
    "remediation": "Being fixed",
}

# Short forms for the table's Agent column — the transcript link, so the
# dashboard is one stop: the issue, the live status, and what the agent did.
STAGE_SHORT = {
    "triage": "triage",
    "remediation": "fix",
    "revalidation": "re-check",
}


def _active_panel(report: Report) -> str:
    """What is being worked on right now — stated as work, not as sessions.

    A session is the mechanism; the finding is the unit anyone actually cares
    about. So this reads "Being fixed · 4m", not "remediation session running".
    The link through to the session is there for whoever wants to watch the
    agent, but it is not the subject of the sentence.
    """
    if not report.active_sessions:
        return '<p class="empty">Nothing in progress right now.</p>'

    # Group by finding: one work item may have had several attempts, and a
    # reader should see the item once.
    by_finding: dict[str, dict] = {}
    for s_ in report.active_sessions:
        key = s_["finding"] or s_["session_id"]
        prior = by_finding.get(key)
        if prior is None or s_["started"] < prior["started"]:
            by_finding[key] = s_

    rows = []
    for key, s_ in sorted(by_finding.items(), key=lambda kv: kv[1]["started"]):
        elapsed = _duration(report.generated_at - s_["started"])
        doing = STAGE_WORDS.get(s_["stage"] or "", "In progress")
        working = s_["detail"] == "working"
        # The issue comes first: it is the work. The transcript is how you go
        # and look at what the agent is doing about it.
        issue = (
            f'<a href="{esc(s_["issue_url"])}">#{s_["issue_number"]}</a> · '
            if s_.get("issue_url") and s_.get("issue_number") else
            (f'#{s_["issue_number"]} · ' if s_.get("issue_number") else "")
        )
        rows.append(
            f'<li class="live-row">'
            f'<span class="pulse{"" if working else " idle"}"></span>'
            f'<div class="live-main">'
            f'<b>{esc(key)}</b>'
            f'<div class="sub">{issue}{esc(doing)}'
            f' · <a href="{esc(s_["url"])}">watch the agent</a></div></div>'
            f'<div class="live-meta">{elapsed}</div></li>'
        )
    return f'<ul class="live">{"".join(rows)}</ul>'


def _table(report: Report) -> str:
    rows = []
    for f in sorted(report.findings, key=lambda r: r.key):
        role, label = _style(f.status)
        issue = (f'<a href="{esc(f.issue_url)}">#{f.issue_number}</a>'
                 if f.issue_url else "—")
        prs = " ".join(
            f'<a href="{esc(p)}">#{esc(p.rsplit("/", 1)[-1])}</a>' for p in f.prs
        ) or "—"
        agent = " ".join(
            f'<a href="{esc(url)}" title="{esc(status)}">'
            f'{esc(STAGE_SHORT.get(stage, stage))}</a>'
            for stage, url, status in f.sessions
        ) or "—"
        gap = (f'<div class="gap"><b>Admitted gap:</b> {esc(f.unverifiable)}</div>'
               if f.unverifiable else "")
        rows.append(
            f"<tr><td><code>{esc(f.key)}</code>"
            f'<div class="sub">{esc(f.title)}</div>{gap}</td>'
            f"<td>{issue}</td>"
            f'<td><span class="badge b-{role}">{esc(label)}</span></td>'
            f'<td class="num">{_duration(f.time_to_verdict)}</td>'
            f"<td>{agent}</td>"
            f"<td>{prs}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Finding</th><th>Issue</th><th>Outcome</th>"
        "<th>Time to verdict</th><th>Agent</th><th>PR</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _cost(report: Report) -> str:
    if not report.acus_are_reliable:
        return (
            '<p class="big-muted">not yet reported</p>'
            '<p class="note">Devin aggregates consumption on a delay, so a zero '
            'here means <em>unknown</em>, not <em>free</em>. Session size '
            'classification is the interim proxy.</p>'
        )
    per = report.cost_per_resolution
    return (
        f'<p class="big">{report.total_acus:.1f} <span>ACU</span></p>'
        + (f'<p class="note">{per:.1f} ACU per resolved finding — '
           f'counting dismissals as resolutions, because they are.</p>'
           if per else "")
    )


# ------------------------------------------------------------------ document

def render(report: Report, *, live: bool = False,
           refresh_seconds: int = 5) -> str:
    """`live=True` adds a polling shim and a freshness stamp.

    The shim fetches `/partial` and swaps the body content, rather than
    reloading. A full reload flashes and loses scroll position, which is exactly
    wrong for something being screen-shared while sessions progress.
    """
    agreed, compared = report.heuristic_agreement
    when = time.strftime("%d %b %Y, %H:%M", time.localtime(report.generated_at))

    agreement = (
        f'<p class="big">{agreed}<span>/{compared}</span></p>'
        f'<p class="note">The cheap heuristic matched the agent on {agreed} of '
        f'{compared}. Where they differ is where the judgement actually happened.</p>'
    ) if compared else '<p class="big-muted">no comparisons yet</p>'

    live_badge = (
        f'<span class="livedot"><i></i>live · refreshing every {refresh_seconds}s</span>'
        if live else ""
    )
    in_flight = len(report.active_sessions)
    in_flight_count = f" — {in_flight}" if in_flight else ""

    # Swap the content of .wrap rather than reloading: a full reload flashes and
    # loses scroll position, which is precisely wrong for something being
    # screen-shared while sessions progress.
    shim = f"""<script>
const REFRESH={refresh_seconds}000;
async function poll(){{
  try{{
    const r=await fetch('/partial',{{cache:'no-store'}});
    if(r.ok){{
      const y=window.scrollY;
      document.querySelector('.wrap').innerHTML=await r.text();
      window.scrollTo(0,y);
    }}
  }}catch(e){{/* server restarting; try again next tick */}}
  setTimeout(poll,REFRESH);
}}
setTimeout(poll,REFRESH);
</script>""" if live else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Remediation pipeline</title>
<style>
:root {{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --border:rgba(11,11,11,.10);
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a; --series-4:#eda100;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --border:rgba(255,255,255,.10);
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7;
  --grid:#2c2c2a; --border:rgba(255,255,255,.10);
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
}}

*{{box-sizing:border-box}}
body{{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:40px 24px 72px}}
.wrap{{max-width:1080px;margin:0 auto}}

header{{margin-bottom:28px}}
h1{{font-size:1.45rem;margin:0 0 4px;letter-spacing:-.01em}}
.meta{{color:var(--muted);font-size:.85rem}}
.meta code{{color:var(--ink-2)}}

section{{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:22px 24px;margin-bottom:16px}}
h2{{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;
  color:var(--muted);margin:0 0 16px;font-weight:600}}
.lede{{color:var(--ink-2);font-size:.88rem;margin:14px 0 0;max-width:62ch}}
.note{{color:var(--muted);font-size:.82rem;margin:10px 0 0;max-width:62ch}}
.empty{{color:var(--muted);font-size:.88rem;margin:0}}

.tiles{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:16px}}
.tile{{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:18px 20px}}
.tile-n{{font-size:2.6rem;line-height:1;font-weight:600;letter-spacing:-.03em}}
.tile-k{{font-size:.92rem;font-weight:600;margin-top:8px}}
.tile-s{{font-size:.78rem;color:var(--muted);margin-top:2px}}
.tile.good .tile-n{{color:var(--good)}}
.tile.bad .tile-n{{color:var(--critical)}}

.stack{{width:100%;height:34px;display:block}}
.seg{{transition:opacity .12s}}
.seg:hover{{opacity:.82}}
.seg-label{{fill:#fff;font-size:13px;font-weight:600;
  paint-order:stroke;stroke:rgba(0,0,0,.28);stroke-width:2.5px}}
.legend{{list-style:none;display:flex;flex-wrap:wrap;gap:8px 22px;
  padding:0;margin:16px 0 0;font-size:.86rem;color:var(--ink-2)}}
.legend b{{color:var(--ink);font-variant-numeric:tabular-nums}}
.swatch{{display:inline-block;width:11px;height:11px;border-radius:3px;
  margin-right:7px;vertical-align:-1px}}

.bars{{display:flex;gap:18px;align-items:flex-start}}
.bar-names{{list-style:none;margin:0;padding:0;font-size:.82rem;
  color:var(--ink-2);min-width:190px}}
.bar-names li{{height:30px;line-height:16px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}}
.bars svg{{flex:1}}
.bar-label{{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}}

.callout{{display:flex;gap:14px;align-items:flex-start;border-radius:10px;
  padding:16px 18px;font-size:.9rem;border:1px solid var(--border)}}
.callout.bad{{background:color-mix(in srgb,var(--critical) 9%,var(--surface));
  border-color:color-mix(in srgb,var(--critical) 34%,transparent)}}
.callout.ok{{background:var(--surface)}}
.callout ul{{margin:10px 0 0;padding-left:18px}}
.ico{{flex:none;width:22px;height:22px;border-radius:50%;display:grid;
  place-items:center;font-weight:700;font-size:.78rem;color:#fff}}
.callout.bad .ico{{background:var(--critical)}}
.callout.ok .ico{{background:var(--good)}}

table{{width:100%;border-collapse:collapse;font-size:.88rem}}
th{{text-align:left;font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;
  color:var(--muted);padding:0 12px 10px 0;border-bottom:1px solid var(--grid)}}
td{{padding:14px 12px 14px 0;border-bottom:1px solid var(--grid);
  vertical-align:top}}
td.num{{font-variant-numeric:tabular-nums;color:var(--ink-2);white-space:nowrap}}
code{{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink)}}
.sub{{color:var(--ink-2);font-size:.83rem;margin-top:4px;max-width:58ch}}
.gap{{margin-top:8px;font-size:.8rem;color:var(--ink-2);
  border-left:2px solid var(--warning);padding-left:10px}}
a{{color:var(--series-1)}}

.badge{{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;
  font-size:.79rem;font-weight:600;color:var(--ink-2)}}
.badge::before{{content:"";width:9px;height:9px;border-radius:2px;flex:none}}
.b-series-1::before{{background:var(--series-1)}}
.b-series-2::before{{background:var(--series-2)}}
.b-series-3::before{{background:var(--series-3)}}
.b-series-4::before{{background:var(--series-4)}}
.b-critical::before{{background:var(--critical)}}
.b-warning::before{{background:var(--warning)}}
.b-muted::before{{background:var(--muted)}}
.b-critical{{color:var(--critical)}}

.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}

.live{{list-style:none;margin:0;padding:0}}
.live-row{{display:flex;align-items:center;gap:14px;padding:12px 0;
  border-bottom:1px solid var(--grid)}}
.live-row:last-child{{border-bottom:0}}
.live-main{{flex:1;min-width:0}}
.live-main a{{font-weight:600;text-decoration:none}}
.live-meta{{font-variant-numeric:tabular-nums;color:var(--muted);
  font-size:.84rem;white-space:nowrap}}
.pulse{{flex:none;width:9px;height:9px;border-radius:50%;
  background:var(--series-1);box-shadow:0 0 0 0 var(--series-1);
  animation:pulse 1.8s infinite}}
.pulse.idle{{background:var(--muted);animation:none}}
@keyframes pulse{{
  0%{{box-shadow:0 0 0 0 color-mix(in srgb,var(--series-1) 55%,transparent)}}
  70%{{box-shadow:0 0 0 7px transparent}}
  100%{{box-shadow:0 0 0 0 transparent}}
}}
@media (prefers-reduced-motion:reduce){{.pulse{{animation:none}}}}

.livedot{{display:inline-flex;align-items:center;gap:7px;font-size:.78rem;
  color:var(--ink-2);border:1px solid var(--border);border-radius:999px;
  padding:4px 11px;background:var(--surface)}}
.livedot i{{width:7px;height:7px;border-radius:50%;background:var(--good);
  animation:pulse 1.8s infinite;font-style:normal}}
.hdr{{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}}
.big{{font-size:2.4rem;font-weight:600;margin:0;letter-spacing:-.02em}}
.big span{{font-size:1.1rem;color:var(--muted);font-weight:400}}
.big-muted{{font-size:1.15rem;color:var(--muted);margin:0}}

@media (max-width:820px){{
  .tiles,.two{{grid-template-columns:1fr 1fr}}
  .bars{{flex-direction:column}} .bar-names{{display:none}}
}}
</style></head>
<body>
<div class="wrap">

<header class="hdr">
  <div>
    <h1>Remediation pipeline</h1>
    <p class="meta">Apache Superset · <code>rbutexas/superset</code> · {esc(when)}</p>
  </div>
  {live_badge}
</header>

<div class="tiles">{_tiles(report)}</div>

<section>
  <h2>In progress{in_flight_count}</h2>
  {_active_panel(report)}
</section>

<section>
  <h2>What happened to the work</h2>
  {_stacked_bar(report)}
  <p class="lede">Every finding that reached an answer, by the kind of answer.
  A dismissal backed by evidence resolves a finding exactly as a pull request
  does — so it is shown at equal weight, not as a lesser shade of green. A
  pipeline scored on fixes will produce fixes, including for findings that
  should have been left alone.</p>
</section>

<section>{_attention_panel(report)}</section>

<div class="two">
  <section><h2>Cost</h2>{_cost(report)}</section>
  <section><h2>Was the agent needed?</h2>{agreement}</section>
</div>

<section>
  <h2>How long each item took to get an answer</h2>
  {_verdict_bars(report)}
</section>

<section>
  <h2>Every item</h2>
  {_table(report)}
  <p class="note">“Admitted gap” is where the agent reported something it could
  not verify in its environment. Surfacing those makes the rest of the row
  trustworthy; hiding them would not.</p>
</section>

</div>{shim}</body></html>
"""


def render_partial(report: Report, *, refresh_seconds: int = 5) -> str:
    """Just the contents of `.wrap`, for the polling shim to swap in.

    Rendered by the same code path as the full page, so the live view and a
    saved file can never drift apart.
    """
    full = render(report, live=True, refresh_seconds=refresh_seconds)
    start = full.index('<div class="wrap">') + len('<div class="wrap">')
    end = full.rindex("</div><script")
    return full[start:end]
