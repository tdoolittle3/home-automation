---
name: daily-stock-report
description: "Daily portfolio research digest from the portfolio-advisor MCP tools. Writes an HTML dashboard to /opt/data/reports (served) and pushes an ntfy notification with the link. Research aid, NOT financial advice. Bounded tool use; scheduled-run safe."
version: 1.2.0
author: homelab
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Finance, Portfolio, Report, Dashboard, ntfy]
---

<!-- Deployed to /opt/stacks/hermes/data/skills/finance/daily-stock-report/SKILL.md
     (data/ gitignored). Scheduled-run constraints learned the hard way:
       - execute_code is BLOCKED in cron -> use write_file for the report.
       - write_file is confined to HERMES_WRITE_SAFE_ROOT=/opt/data -> write to
         /opt/data/reports/ (the gateway mounts host ./reports there; nginx on
         :9123 serves it). Do NOT write to /reports (that's the sandbox path).
       - ntfy POST via the `terminal` tool (curl in sandbox; $NTFY_URL forwarded
         via config.yaml terminal.docker_forward_env). Needs HERMES_ACCEPT_HOOKS=1.
     Scheduled by `hermes cron` (0 7 * * *, America/Chicago), --deliver telegram. -->

# Daily Stock Report

Build a **research digest** of the owner's portfolio with the `portfolio-advisor` MCP tools, write an HTML dashboard into the served reports dir, and push an ntfy notification linking to it.

> **Research aid, NOT financial advice.** No trading tools exist; never imply otherwise. Use ONLY tool-returned values, cite the field, say "missing" when absent, NEVER invent numbers, ALWAYS compare ideas to just holding the benchmark. Past performance != future results.

## CRITICAL rules (scheduled runs — obey exactly)
- Call ONE tool at a time. Never batch tool calls.
- **To write the report use the `write_file` tool** to a path under **/opt/data/reports/** (the write-safe root, served by nginx). **Do NOT use execute_code / code execution — it is BLOCKED in scheduled runs.**
- **For the ntfy POST use the `terminal` tool** (curl runs in the sandbox where `$NTFY_URL` is set). Do NOT use execute_code.
- Prefer AGGREGATE tools; do NOT loop a per-ticker tool over every holding. Cap per-ticker deep-dives at 3. Context window is ~64k — stay well under it.
- Do NOT edit this skill or any file except the report HTML.

## Parameters
- LONG_TERM_FLAG_DAYS = 30 ; MAX_LOT_CHECKS = 3 ; MAX_CANDIDATES = 3
- REPORT path = /opt/data/reports/stock-report-<YYYY-MM-DD>.html  (served at https://ladybird.tail0e6756.ts.net:9123/stock-report-<YYYY-MM-DD>.html)

## Procedure
1. **Freshness.** `get_connection_status`. If auth stale/failed, lead with a "data may be stale" banner.
2. **Core digest.** `get_latest_report` (bundles metrics, flags, allocation, news, week summary) then `get_positions`. Use THESE for snapshot, allocation, AND news — do NOT call get_news per ticker.
3. **Tax.** `get_tax_summary`; then `get_lots` for ONLY the MAX_LOT_CHECKS largest holdings; flag lots within LONG_TERM_FLAG_DAYS of long-term. Note if rates are placeholders/missing.
4. **Candidates.** `get_strategy` (targets + benchmark); compare to current allocation; take top MAX_CANDIDATES UNDERWEIGHT names. For THOSE only: `research_ticker` then `evaluate_move`, each vs the benchmark with "what would change this view."
5. **Track record.** `get_track_record`.
6. **Log.** `log_recommendation` action "watch" for each candidate.
7. **Write the dashboard.** Use `write_file` to /opt/data/reports/stock-report-<YYYY-MM-DD>.html with a self-contained, mobile-readable, dark-theme HTML page: freshness banner, snapshot+allocation, tax flags, news, candidates (benchmark + what-changes-view), track record; footer "Research aid, not financial advice." Then `write_file` the same content to /opt/data/reports/latest.html. Every number must trace to a tool field.
8. **Notify.** Use the `terminal` tool:
   ```bash
   D=$(date +%F)
   curl -s -H "Title: Daily Stock Report - $D" \
        -H "Click: https://ladybird.tail0e6756.ts.net:9123/stock-report-$D.html" \
        -H "Tags: chart_with_upwards_trend" \
        -d "<2-4 line summary>" "$NTFY_URL"
   ```
9. **Final message.** Concise summary (also sent to Telegram): headline numbers, any tax flag, candidate tickers + benchmark note, dashboard link.

## Guardrails
- Research only; never call update_strategy or change config (read-only except log_recommendation "watch").
- Only tool values; cite fields; "missing" when absent; never invent. Compare to the benchmark; lead with staleness caveat if data is old.
