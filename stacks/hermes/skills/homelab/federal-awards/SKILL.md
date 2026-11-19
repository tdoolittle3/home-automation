---
name: federal-awards
description: Answer questions about federal money flowing to a US city — grants, contracts, loans, direct payments — using the mcp_federal-awards_* tools (USAspending.gov data). Covers which tool answers which question, the resolve-city-first workflow, and the data caveats a finance analyst needs stated. Not for household/device/camera questions, and the cameras/HA/radio logs know nothing about federal spending.
version: 2.0.0
author: homelab
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [federal, grants, contracts, loans, spending, usaspending, awards, city, government]
    category: homelab
---

# Federal awards (USAspending.gov) — tool usage

Four read-only `mcp_federal-awards_*` tools over USAspending.gov. Typical
asker: a city-government finance analyst wanting "which federal money came to
our city?" Data is **obligations** (money committed), not outlays or cash
received. This is adapted from the server repo's own skill; each rule prevents
a specific confidently-wrong answer.

## Quick tool reference

| Tool (`mcp_federal-awards_<name>`) | Use for | Key params |
|---|---|---|
| `resolve_city` | Disambiguate a city name — always first when the state is unknown; pass `state_code` when the user named it and proceed without asking | `query`, `state_code?` |
| `get_award_totals` | "How much?" — per-FY totals + grand total; `all` adds a per-group breakdown | `city`, `state_code`, `fiscal_years[]`, `award_group` (default `all`), `location_scope` |
| `top_agencies` | "Who awarded the most?" — ranked agencies | same + `limit` (default 10) |
| `list_top_awards` | "Largest individual awards" — each with a usaspending.gov URL | same, but `award_group` is required and single-group; `limit` default 5 |

## Rule 1 — Resolve the city first; never guess a state, never re-ask a stated one

- City named but NO state → `resolve_city(query)` before any other tool. More
  than one candidate → ask the user which one (there are 43 Springfields;
  picking one silently is a wrong answer with no warning sign).
- State already named ("Loveland, Colorado") → `resolve_city(query,
  state_code)` and go straight to the data tools in the same turn. Asking a
  user to confirm a state they just told you is a failure.
- Zero candidates for a bare city name → ask about the spelling. But the list
  only contains cities appearing in spending records — if the user clearly
  named city AND state and it's absent, query `get_award_totals` anyway and
  expect "no recorded spending".

## Rule 2 — Name the location scope in every answer

- Default `location_scope` is `recipient_location`. Every answer must say in
  plain words which was used: "awards to recipients located in X" vs "awards
  for work performed in X" (`place_of_performance`). They differ by up to 7x
  in state capitals.
- If the city is a state capital or the top recipients are clearly state
  agencies, add: state-government recipients headquartered there may dominate
  the number; offer to re-run with `location_scope: "place_of_performance"`.

## Rule 3 — Fiscal years, never calendar years silently

- FY N = Oct 1 of N-1 through Sep 30 of N. Pass integers in `fiscal_years`.
- "Last three years" = the last three COMPLETE fiscal years; if the current
  (in-progress) FY is included, say it is partial.
- A bare year ("2024") = FY2024, and say so ("federal fiscal year 2024,
  Oct 2023–Sep 2024"). Supported range: FY2008 through the current FY.

## Rule 4 — Award groups: "grants" is not "all federal money"

- Explicit "grants"/"contracts"/"loans" → that `award_group`. Ambiguous
  "money"/"funding" → `award_group: "all"` on `get_award_totals` and present
  the per-group breakdown.
- `list_top_awards` requires ONE group (API limit). "Largest awards overall"
  = query at least `contracts` and `grants` separately, merge yourself, and
  say you merged per-group queries.
- Never sum individual award rows into a total; totals come only from
  `get_award_totals`.

## Rule 5 — Loans are not grants

Loan amounts are **loan face value**, never money granted or received.
Mention `subsidy_cost` as the estimated budgetary cost to the government —
a $3.8M loan guarantee is not $3.8M given to the city.

## Rule 6 — Provenance in every numeric answer

- The `data_as_of` date and the `audit_id` from the tool result; for
  individual awards, their usaspending.gov `url`.
- Exact dollars with thousands separators ("$31,811,543,089"); a rounded
  figure may follow, never replace.

## Rule 7 — Honesty

- A tool error → relay its plain-language message (and audit_id). Never
  invent, estimate, or reuse a stale number in place of a failed call.
- `fiscal_years_with_no_recorded_spending` non-empty → say which years.
- Negative amounts are de-obligations (committed money taken back) — report
  them as such, don't hide or clamp.
- Obligations ≠ money spent; say so if the user asks about money "spent".
- Data lags reporting cycles — current-quarter numbers are incomplete.
