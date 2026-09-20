"""Command line entry point.

Subcommands are deliberately separate rather than a single `run`. Scanning is
free and read-only; filing writes to a repository; dispatching spends a metered
budget. Collapsing those into one command means a re-run of a scan can silently
cost money, so each escalation in consequence is a separate, explicit verb.

    scan        read the repo, print findings and how they would route  (free)
    provision   create playbooks, labels and automations                (free)
    file        create or update the issues                    (writes to GitHub)
    dispatch    start sessions for filed findings                     (SPENDS)
    collect     poll, record, and advance triage into remediation       (free)
    report      render the dashboard to stdout or a file                (free)
    serve       live dashboard on localhost, updating as sessions run    (free)
    arm         arm or disarm the automations — arming makes `file` cost
    cleanup     find and terminate any session still holding resources   (free)
    status      one-line health check                                   (free)

`--dry-run` works on every verb that would otherwise create something.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import pathlib
import sys

from . import automations as auto
from .collector import Collector
from .config import CAMPAIGN_TAG, Config, ConfigError
from .devin import DevinClient, to_record
from .dispatch import Dispatcher
from .github import GitHubClient
from .models import Finding
from .report import build, render_html, render_json, render_text
from .routing import Policy, Route, route, summarise
from .scanner import run_detectors
from .store import Store

log = logging.getLogger("remediation_agent")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    logging.getLogger("remediation_agent.http").setLevel(
        logging.DEBUG if verbose else logging.WARNING)


def _scan(repo_path: pathlib.Path, only: list[str] | None):
    result = run_detectors(repo_path, only)
    log.info("scan: %s", result.summary())
    return result


def _load_findings(repo_path: pathlib.Path) -> dict[str, Finding]:
    result = _scan(repo_path, None)
    result.raise_if_degraded()
    return {f.key: f for f in result.findings}


# ------------------------------------------------------------------ commands

def cmd_scan(args, cfg: Config) -> int:
    result = _scan(pathlib.Path(args.repo_path), args.detector)
    decisions = {f.key: route(f, _policy(args)) for f in result.findings}

    print(f"\n{result.summary()}\n")
    for finding in sorted(result.findings, key=lambda f: f.key):
        print(f"  [{finding.severity.upper():<6}] {finding.key}")
        print(f"           {finding.title}")
        print(f"           {len(finding.evidence)} evidence · "
              f"{len(finding.guardrails)} guardrail(s) · "
              f"{len(finding.open_questions)} open question(s)")
    print()
    print(summarise(decisions))

    if args.json:
        import dataclasses
        import json
        print(json.dumps([dataclasses.asdict(f) for f in result.findings],
                         indent=2, default=str))

    return 1 if result.degraded else 0


def cmd_provision(args, cfg: Config) -> int:
    devin, github = DevinClient(cfg), GitHubClient(cfg)
    dispatcher = Dispatcher(cfg, devin, github, _policy(args))

    provisioned = dispatcher.provision()
    print(f"triage playbook      : {provisioned.triage_playbook_id}")
    print(f"remediation playbook : {provisioned.remediation_playbook_id}")
    print(f"documentation pb     : {provisioned.documentation_playbook_id}")

    if args.skip_automations:
        print("automations          : skipped")
        return 0

    created = auto.ensure(devin, cfg, dry_run=cfg.dry_run,
                          enabled=not args.disabled,
                          triage_playbook=provisioned.triage_playbook_id,
                          remediation_playbook=provisioned.remediation_playbook_id,
                          documentation_playbook=provisioned.documentation_playbook_id)
    state = "DISABLED" if args.disabled else "enabled"
    for name, aid in created.items():
        print(f"automation           : {name} -> {aid} [{state}]")
    if args.disabled:
        print("\nAutomations created but NOT armed. Filing an issue will not "
              "start anything.\nArm them with:  remediation-agent arm")
    return 0


def cmd_arm(args, cfg: Config) -> int:
    """Arm or disarm the automations.

    Separated from `provision` because arming changes what `file` costs: with
    the label automation live, creating an issue starts a session.
    """
    devin = DevinClient(cfg)
    if cfg.dry_run:
        print(f"[dry-run] would {'disarm' if args.off else 'arm'} both automations")
        return 0

    result = auto.set_enabled(devin, enabled=not args.off)
    if not result:
        print("no automations found — run `provision` first")
        return 1
    for name, state in result.items():
        print(f"  {state:<9} {name}")
    if not args.off:
        print("\nLIVE. Applying an agent:* label to an issue now starts a "
              "session and spends credits.")
    return 0


def cmd_file(args, cfg: Config) -> int:
    findings = _load_findings(pathlib.Path(args.repo_path))
    devin, github = DevinClient(cfg), GitHubClient(cfg)
    dispatcher = Dispatcher(cfg, devin, github, _policy(args))

    with Store(cfg.db_path) as store:
        for finding in findings.values():
            decision = route(finding, _policy(args))
            store.upsert_finding(
                finding.key, finding.title, finding.detector, finding.severity.value,
                finding.scanner_hint.value if finding.scanner_hint else None,
                decision.route.value, list(decision.reasons),
            )

        for outcome in dispatcher.file_findings(list(findings.values())):
            print(f"  {outcome.action:<12} {outcome.finding_key}"
                  + (f"  #{outcome.issue_number}" if outcome.issue_number else "")
                  + (f"  {outcome.detail}" if outcome.detail else ""))
            if outcome.issue_number and not cfg.dry_run:
                store.attach_issue(outcome.finding_key, outcome.issue_number,
                                   outcome.detail)
    return 0


def cmd_dispatch(args, cfg: Config) -> int:
    """Start sessions. The only verb that spends money."""
    findings = _load_findings(pathlib.Path(args.repo_path))
    devin, github = DevinClient(cfg), GitHubClient(cfg)
    policy = _policy(args)
    dispatcher = Dispatcher(cfg, devin, github, policy)

    playbooks = dispatcher.provision() if not args.skip_provision else None
    triage_playbook = playbooks.triage_playbook_id if playbooks else None

    with Store(cfg.db_path) as store:
        wanted = [f for f in findings.values()
                  if not args.only or f.key in args.only]
        if not wanted:
            print("nothing selected")
            return 1

        started = 0
        for finding in wanted:
            decision = route(finding, policy)
            if decision.route is Route.HOLD_FOR_HUMAN:
                print(f"  HELD        {finding.key} — {decision.reasons[0]}")
                continue

            stored = store.finding(finding.key)
            issue_number = stored["issue_number"] if stored else None
            if issue_number is None and not cfg.dry_run:
                print(f"  SKIP        {finding.key} — no issue filed yet; "
                      f"run `file` first")
                continue

            if store.latest_session(finding.key, "triage") and not args.force:
                print(f"  SKIP        {finding.key} — already triaged "
                      f"(use --force to run again)")
                continue

            if started >= cfg.max_concurrent_sessions:
                print(f"  DEFERRED    {finding.key} — concurrency cap "
                      f"({cfg.max_concurrent_sessions}) reached")
                continue

            outcome = dispatcher.start_triage(finding, issue_number, triage_playbook)
            started += 1
            print(f"  {outcome.action:<12}{finding.key}")
            if outcome.session_url:
                print(f"              {outcome.session_url}")

        print(f"\n{started} session(s) started"
              + (" (dry run — none actually created)" if cfg.dry_run else ""))
    return 0


def cmd_collect(args, cfg: Config) -> int:
    findings = _load_findings(pathlib.Path(args.repo_path))
    devin, github = DevinClient(cfg), GitHubClient(cfg)
    dispatcher = Dispatcher(cfg, devin, github, _policy(args))

    with Store(cfg.db_path) as store:
        collector = Collector(cfg, devin, store, dispatcher, findings)
        if args.once:
            print(collector.tick().summary())
        else:
            collector.run(until_idle=args.until_idle, max_seconds=args.max_seconds)
    return 0


def cmd_report(args, cfg: Config) -> int:
    with Store(cfg.db_path) as store:
        report = build(store)

    if args.format == "html":
        output = render_html(report)
    elif args.format == "json":
        output = render_json(report)
    else:
        output = render_text(report)

    if args.out:
        pathlib.Path(args.out).write_text(output, encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(output)
    return 0


def cmd_serve(args, cfg: Config) -> int:
    """Live dashboard. Reads are unmetered, so watching costs nothing."""
    from .serve import serve

    collector = None
    if not args.no_collect:
        findings = _load_findings(pathlib.Path(args.repo_path))
        devin, github = DevinClient(cfg), GitHubClient(cfg)
        dispatcher = Dispatcher(cfg, devin, github, _policy(args))
        # The store connection is opened per request inside serve(); the
        # collector gets its own, because SQLite connections are not
        # shareable across threads.
        collector = Collector(cfg, devin, Store(cfg.db_path), dispatcher, findings)

    if args.poll:
        cfg = dataclasses.replace(cfg, poll_interval_seconds=args.poll)
        if collector is not None:
            collector.cfg = cfg
    serve(cfg, host=args.host, port=args.port, collector=collector,
          refresh_seconds=args.refresh)
    return 0


def cmd_cleanup(args, cfg: Config) -> int:
    """Terminate sessions that are done but still open.

    Devin sleeps an idle session after ~30 minutes, and a slept session does not
    consume. This is belt-and-braces: it makes the end deliberate, and it means
    nothing is left holding VM state because a collector was interrupted.

    A session that is still working, or is stalled waiting on a person, is left
    alone — killing either would destroy work or a pending decision.
    """
    devin = DevinClient(cfg)
    everything = not args.campaign_only

    listed = (devin.list_sessions() if everything
              else devin.list_sessions(tags=[CAMPAIGN_TAG]))

    kept, ended, skipped = 0, 0, []
    for payload in listed:
        rec = to_record(payload)

        if rec.status in ("exit", "error"):
            continue                       # already over
        if rec.is_stalled:
            skipped.append((rec, "stalled — a human may still want to reply"))
            continue
        if not rec.is_terminal:
            skipped.append((rec, f"still active ({rec.status_detail or rec.status})"))
            continue

        kept += 1
        if cfg.dry_run:
            print(f"  would terminate  {rec.session_id[:12]}  {(rec.title or '')[:52]}")
            continue
        try:
            devin.terminate_session(rec.session_id)
            ended += 1
            print(f"  terminated       {rec.session_id[:12]}  {(rec.title or '')[:52]}")
        except Exception as exc:                      # noqa: BLE001
            print(f"  FAILED           {rec.session_id[:12]}: {exc}")

    for rec, why in skipped:
        print(f"  left alone       {rec.session_id[:12]}  {why}")

    if not kept and not skipped:
        print("  nothing to do — no open sessions")
    elif cfg.dry_run:
        print(f"\n{kept} session(s) would be terminated (dry run)")
    else:
        print(f"\n{ended} session(s) terminated, {len(skipped)} left alone")
    return 0


def cmd_status(args, cfg: Config) -> int:
    devin = DevinClient(cfg)
    who = devin.whoami()
    print(f"devin    : {who.get('service_user_name')} @ {who.get('org_id')}")
    print(f"repo     : {cfg.repo}")

    missing = auto.verify_trigger_support(devin)
    print(f"triggers : {'all required present' if not missing else 'MISSING ' + str(missing)}")

    live = {a.get("name"): a.get("enabled") for a in devin.list_automations()}
    for name in (auto.TRIAGE_AUTOMATION, auto.REMEDIATION_AUTOMATION,
                 auto.SCHEDULE_AUTOMATION):
        state = "enabled" if live.get(name) else ("disabled" if name in live else "absent")
        print(f"           {name}: {state}")

    open_sessions = [to_record(p) for p in devin.list_sessions()]
    still_open = [r for r in open_sessions
                  if r.status not in ("exit", "error") and not r.is_terminal]
    print(f"sessions : {len(open_sessions)} total, {len(still_open)} still open"
          + ("  → run `cleanup`" if still_open else ""))

    with Store(cfg.db_path) as store:
        counts = store.counts()
    print(f"store    : {counts['findings']} finding(s), "
          f"{counts['issues_filed']} filed, {counts['sessions']} session(s), "
          f"{counts['total_acus']:.2f} ACU")
    return 0


# ------------------------------------------------------------------ plumbing

def _policy(args) -> Policy:
    return Policy(allow_direct_remediation=getattr(args, "allow_direct", False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remediation-agent",
        description="Event-driven remediation of engineering debt via the Devin API",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="resolve and render everything, create nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def repo_arg(sp):
        sp.add_argument("--repo-path", default="/tmp/ss",
                        help="local checkout of the target repository")
        sp.add_argument("--allow-direct", action="store_true",
                        help="permit unambiguous findings to skip triage")

    s = sub.add_parser("scan", help="detect findings (free, read-only)")
    repo_arg(s)
    s.add_argument("--detector", action="append")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("provision", help="create playbooks, labels, automations")
    repo_arg(s)
    s.add_argument("--skip-automations", action="store_true")
    s.add_argument("--disabled", action="store_true",
                   help="create the automations but do not arm them")
    s.set_defaults(func=cmd_provision)

    s = sub.add_parser("arm", help="arm/disarm the automations (arming makes "
                                   "filing an issue cost money)")
    s.add_argument("--off", action="store_true", help="disarm instead")
    s.set_defaults(func=cmd_arm)

    s = sub.add_parser("file", help="create or update issues (writes to GitHub)")
    repo_arg(s)
    s.set_defaults(func=cmd_file)

    s = sub.add_parser("dispatch", help="start triage sessions (SPENDS CREDITS)")
    repo_arg(s)
    s.add_argument("--only", action="append", help="finding key; repeatable")
    s.add_argument("--force", action="store_true", help="re-run an already-triaged finding")
    s.add_argument("--skip-provision", action="store_true")
    s.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("collect", help="poll sessions and advance the pipeline")
    repo_arg(s)
    s.add_argument("--once", action="store_true")
    s.add_argument("--until-idle", action="store_true")
    s.add_argument("--max-seconds", type=int)
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("report", help="render the dashboard")
    s.add_argument("--format", choices=("text", "html", "json"), default="text")
    s.add_argument("--out")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("serve", help="live dashboard on localhost")
    repo_arg(s)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1",
                   help="deliberately localhost: the page has no authentication")
    s.add_argument("--refresh", type=int, default=5,
                   help="browser poll interval in seconds")
    s.add_argument("--no-collect", action="store_true",
                   help="serve a static view without advancing the pipeline")
    s.add_argument("--poll", type=int,
                   help="collector interval in seconds (default 60). Lower it "
                        "for a demo so the board moves while someone is watching")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("cleanup", help="terminate finished sessions still open")
    s.add_argument("--campaign-only", action="store_true",
                   help="only sessions tagged for this campaign")
    s.set_defaults(func=cmd_cleanup)

    s = sub.add_parser("status", help="one-line health check")
    s.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    # Only demand the credentials this command will actually use, so the
    # read-only verbs work with none at all.
    needs: dict[str, tuple[str, ...]] = {
        "scan": (),
        "report": (),
        "file": ("GITHUB_TOKEN",),
    }
    # `serve --no-collect` only reads the store, so it needs nothing either.
    # That is what lets a reviewer open a recorded run without an account.
    if args.command == "serve" and getattr(args, "no_collect", False):
        needs["serve"] = ()
    try:
        cfg = Config.from_env(
            dry_run=args.dry_run,
            require=needs.get(args.command,
                              ("DEVIN_API_KEY", "DEVIN_ORG_ID", "GITHUB_TOKEN")),
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if cfg.dry_run:
        log.info("DRY RUN — nothing will be created")

    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:                          # noqa: BLE001
        log.exception("%s failed", args.command)
        print(f"\n{args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
