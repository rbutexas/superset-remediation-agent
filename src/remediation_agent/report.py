"""Reporting.

The question this has to answer is "if I were an engineering leader, how would I
know this is working?" — and the honest answer is not a count of fixes.

A pipeline measured on fixes will produce fixes, including for findings that
should have been left alone. So the headline metric here is the **outcome mix**:
how many findings reached a verdict, and what the verdicts were. A dismissal with
evidence resolves a finding just as a pull request does, and it is the cheaper
and scarcer outcome.

Four things are surfaced that a naive dashboard would omit:

  * **Unresolved findings** — filed but with no verdict. The queue's true backlog.
  * **Stalled sessions** — billing, blocked on a person, nobody told.
  * **Heuristic agreement** — how often the cheap rule matched the agent. Where
    they disagree is where the judgment actually happened.
  * **Unverifiable claims** — where a remediation admitted it could not prove
    something. Hiding these would make the dashboard less trustworthy, not more.
"""

from __future__ import annotations

import dataclasses
import html
import json
import sqlite3
import time
from collections import Counter
from typing import Any

from .models import RemediationOutcome, Stage, TriageDecision
from .store import Store

BAR_WIDTH = 28


@dataclasses.dataclass(slots=True)
class FindingRow:
    key: str
    title: str
    detector: str
    severity: str
    issue_number: int | None
    issue_url: str | None
    scanner_hint: str | None
    triage: str | None
    outcome: str | None
    acus: float
    prs: list[str]
    first_seen: int
    verdict_at: int | None
    stalled: bool
    unverifiable: str | None

    @property
    def resolved(self) -> bool:
        """A finding is resolved when it has an answer — not only when it has a fix."""
        if self.outcome:
            return RemediationOutcome(self.outcome).is_success
        if self.triage:
            return TriageDecision(self.triage).resolves_finding
        return False

    @property
    def time_to_verdict(self) -> int | None:
        return (self.verdict_at - self.first_seen) if self.verdict_at else None

    @property
    def status(self) -> str:
        if self.stalled:
            return "STALLED"
        if self.outcome:
            return self.outcome
        if self.triage:
            return self.triage if not TriageDecision(self.triage).dispatches_work \
                else "remediating"
        if self.issue_number:
            return "awaiting triage"
        return "detected"


@dataclasses.dataclass(slots=True)
class Report:
    generated_at: int
    findings: list[FindingRow]
    total_acus: float
    sessions: int
    sessions_complete: int
    stalled_sessions: list[dict[str, Any]]
    acus_are_reliable: bool

    # ------------------------------------------------------------ derived

    @property
    def resolved(self) -> list[FindingRow]:
        return [f for f in self.findings if f.resolved]

    @property
    def unresolved(self) -> list[FindingRow]:
        return [f for f in self.findings if not f.resolved]

    @property
    def outcome_mix(self) -> Counter:
        return Counter(f.status for f in self.findings)

    @property
    def prs(self) -> list[str]:
        return [pr for f in self.findings for pr in f.prs]

    @property
    def heuristic_agreement(self) -> tuple[int, int]:
        """(agreed, compared). Disagreement is where the judgment happened."""
        compared = [f for f in self.findings if f.scanner_hint and f.triage]
        agreed = [f for f in compared if f.scanner_hint == f.triage]
        return len(agreed), len(compared)

    @property
    def cost_per_resolution(self) -> float | None:
        if not self.resolved or not self.acus_are_reliable:
            return None
        return self.total_acus / len(self.resolved)

    @property
    def admitted_gaps(self) -> list[FindingRow]:
        return [f for f in self.findings if f.unverifiable]


# ---------------------------------------------------------------- building

def build(store: Store) -> Report:
    rows: list[FindingRow] = []
    stalled_sessions: list[dict[str, Any]] = []

    for finding in store.findings():
        sessions = store.sessions_for(finding["key"])
        triage = next((s for s in sessions if s["stage"] == Stage.TRIAGE.value), None)
        remediation = next(
            (s for s in sessions if s["stage"] == Stage.REMEDIATION.value), None)

        acus = sum(float(s["acus"] or 0) for s in sessions)
        prs = [pr for s in sessions for pr in Store.prs(s)]

        verdict_at = None
        for candidate in (remediation, triage):
            if candidate and candidate["completed_at"]:
                verdict_at = candidate["completed_at"]
                break

        unverifiable = None
        if remediation:
            verification = Store.output(remediation).get("verification") or {}
            unverifiable = verification.get("unverifiable") or None

        rows.append(FindingRow(
            key=finding["key"],
            title=finding["title"],
            detector=finding["detector"],
            severity=finding["severity"],
            issue_number=finding["issue_number"],
            issue_url=finding["issue_url"],
            scanner_hint=finding["scanner_hint"],
            triage=triage["triage_decision"] if triage else None,
            outcome=remediation["remediation_outcome"] if remediation else None,
            acus=acus,
            prs=prs,
            first_seen=finding["first_seen"],
            verdict_at=verdict_at,
            stalled=any(_is_stalled(s) for s in sessions),
            unverifiable=unverifiable,
        ))

    for session in store.sessions():
        if _is_stalled(session):
            stalled_sessions.append({
                "session_id": session["session_id"],
                "url": session["url"],
                "finding": session["finding_key"],
                "waiting_since": session["last_seen"],
            })

    counts = store.counts()
    total = float(counts["total_acus"])

    return Report(
        generated_at=int(time.time()),
        findings=rows,
        total_acus=total,
        sessions=counts["sessions"],
        sessions_complete=counts["sessions_complete"],
        stalled_sessions=stalled_sessions,
        # Devin's consumption endpoints aggregate on a delay; a zero here means
        # "not reported yet", not "free". Saying so is better than printing a
        # confident nought.
        acus_are_reliable=total > 0,
    )


def _is_stalled(session: sqlite3.Row) -> bool:
    return (session["status_detail"] in ("waiting_for_user", "waiting_for_approval")
            and not session["structured_output"])


# ---------------------------------------------------------------- rendering

def _bar(count: int, total: int) -> str:
    if not total:
        return ""
    filled = round(BAR_WIDTH * count / total)
    return "█" * filled + "·" * (BAR_WIDTH - filled)


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def render_text(report: Report) -> str:
    rule = "─" * 78
    out: list[str] = [
        rule,
        " REMEDIATION PIPELINE",
        f" {time.strftime('%Y-%m-%d %H:%M', time.localtime(report.generated_at))}",
        rule,
        "",
    ]

    total = len(report.findings)
    out += [
        f"  {total} finding(s) detected · {len(report.resolved)} resolved · "
        f"{len(report.unresolved)} outstanding",
        "",
    ]

    # --- outcome mix: the headline, deliberately not a fix count ---
    out += ["  OUTCOME MIX", ""]
    for status, count in report.outcome_mix.most_common():
        out.append(f"    {status:<26} {count:>3}  {_bar(count, total)}")
    out.append("")

    # --- cost ---
    out += ["  COST", ""]
    if report.acus_are_reliable:
        out.append(f"    total                      {report.total_acus:>6.2f} ACU")
        per = report.cost_per_resolution
        if per is not None:
            out.append(f"    per resolved finding       {per:>6.2f} ACU")
    else:
        out.append("    not yet reported by the API — Devin aggregates consumption")
        out.append("    on a delay, so 0.00 here means 'unknown', not 'free'.")
    out.append("")

    # --- per finding ---
    out += ["  FINDINGS", ""]
    for f in sorted(report.findings, key=lambda r: r.key):
        issue = f"#{f.issue_number}" if f.issue_number else "not filed"
        out.append(f"    {f.key}")
        out.append(f"      {f.title[:70]}")
        out.append(f"      issue {issue:<10} status {f.status:<22} "
                   f"time-to-verdict {_duration(f.time_to_verdict)}")
        for pr in f.prs:
            out.append(f"      PR  {pr}")
        if f.unverifiable:
            out.append(f"      ADMITTED GAP: {f.unverifiable[:120]}")
        out.append("")

    # --- the things a naive dashboard omits ---
    if report.stalled_sessions:
        out += ["  ⚠ STALLED — blocked on a person, and nobody has been told", ""]
        for s in report.stalled_sessions:
            out.append(f"    {s['finding'] or s['session_id'][:12]}  "
                       f"waiting {_duration(report.generated_at - s['waiting_since'])}")
            out.append(f"      {s['url']}")
        out.append("")

    agreed, compared = report.heuristic_agreement
    if compared:
        out += [
            "  HEURISTIC vs AGENT",
            "",
            f"    the cheap rule matched the agent on {agreed}/{compared}",
            "    where they differ is where the judgement actually happened",
            "",
        ]

    if report.admitted_gaps:
        out += ["  ADMITTED GAPS — work the agent declined to claim as verified", ""]
        for f in report.admitted_gaps:
            out.append(f"    {f.key}: {f.unverifiable[:100]}")
        out.append("")

    out.append(rule)
    return "\n".join(out)


def render_html(report: Report) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value))

    rows = "\n".join(
        f"<tr><td><code>{esc(f.key)}</code></td>"
        f"<td>{esc(f.title[:80])}</td>"
        f"<td>{'<a href=' + chr(34) + esc(f.issue_url) + chr(34) + '>#' + str(f.issue_number) + '</a>' if f.issue_url else '—'}</td>"
        f"<td class='s-{esc(f.status).replace(' ', '-')}'>{esc(f.status)}</td>"
        f"<td>{_duration(f.time_to_verdict)}</td>"
        f"<td>{''.join('<a href=' + chr(34) + esc(p) + chr(34) + '>PR</a> ' for p in f.prs) or '—'}</td>"
        f"</tr>"
        for f in sorted(report.findings, key=lambda r: r.key)
    )

    mix = "\n".join(
        f"<li><b>{esc(k)}</b> — {v}</li>"
        for k, v in report.outcome_mix.most_common()
    )

    agreed, compared = report.heuristic_agreement
    cost = (f"{report.total_acus:.2f} ACU" if report.acus_are_reliable
            else "not yet reported (Devin aggregates on a delay)")

    stalled = "".join(
        f"<li><a href='{esc(s['url'])}'>{esc(s['finding'] or s['session_id'])}</a></li>"
        for s in report.stalled_sessions
    ) or "<li>none</li>"

    return f"""<!doctype html>
<meta charset="utf-8"><title>Remediation pipeline</title>
<style>
 body{{font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
       max-width:1100px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
 h1{{font-size:1.3rem}} h2{{font-size:1rem;margin-top:2rem;color:#555}}
 table{{border-collapse:collapse;width:100%}}
 td,th{{border-bottom:1px solid #e5e5e5;padding:.45rem .6rem;text-align:left;
        vertical-align:top}}
 th{{font-weight:600;color:#666;font-size:.8rem;text-transform:uppercase}}
 .big{{font-size:2rem;font-weight:600}}
 .k{{display:inline-block;margin-right:2.5rem}}
 .note{{color:#666;font-size:.85rem}}
 [class^=s-]{{font-weight:600}}
 .s-fixed,.s-decline_not_actionable,.s-blocked_upstream{{color:#0a7d33}}
 .s-STALLED,.s-failed_verification{{color:#b00020}}
</style>
<h1>Remediation pipeline</h1>
<p class="note">generated {time.strftime('%Y-%m-%d %H:%M',
                                         time.localtime(report.generated_at))}</p>

<p>
 <span class="k"><span class="big">{len(report.findings)}</span><br>findings</span>
 <span class="k"><span class="big">{len(report.resolved)}</span><br>resolved</span>
 <span class="k"><span class="big">{len(report.prs)}</span><br>pull requests</span>
 <span class="k"><span class="big">{len(report.stalled_sessions)}</span><br>stalled</span>
</p>

<h2>Outcome mix</h2>
<ul>{mix}</ul>
<p class="note">A dismissal with evidence resolves a finding just as a pull
request does. Counting only fixes would reward changing code that should have
been left alone.</p>

<h2>Cost</h2><p>{esc(cost)}</p>

<h2>Findings</h2>
<table><tr><th>key</th><th>title</th><th>issue</th><th>status</th>
<th>time to verdict</th><th>output</th></tr>
{rows}
</table>

<h2>Stalled — blocked on a person</h2><ul>{stalled}</ul>

<h2>Heuristic vs agent</h2>
<p>matched on {agreed}/{compared}. Where they differ is where the judgement
actually happened.</p>
"""


def render_json(report: Report) -> str:
    payload = dataclasses.asdict(report)
    payload["outcome_mix"] = dict(report.outcome_mix)
    payload["resolved_count"] = len(report.resolved)
    payload["pull_requests"] = report.prs
    agreed, compared = report.heuristic_agreement
    payload["heuristic_agreement"] = {"agreed": agreed, "compared": compared}
    return json.dumps(payload, indent=2)
