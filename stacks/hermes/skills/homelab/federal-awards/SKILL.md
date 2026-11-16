---
name: federal-awards
description: Answer questions about federal money flowing to a city, county, or organization — grants, contracts, loans, direct payments, pass-through subawards — using the mcp_federal-awards_* tools (USAspending.gov data). Covers which tool answers which question, the resolve → count → search → detail workflow, and the data caveats a finance analyst needs stated. Not for household/device/camera questions, and the cameras/HA/radio logs know nothing about federal spending.
version: 1.0.0
author: homelab
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [federal, grants, contracts, spending, usaspending, awards, subawards, city, government]
    category: homelab
---

# Federal awards (USAspending.gov) — tool usage

Seven read-only `mcp_federal-awards_*` tools over USAspending.gov. Typical
asker: a city-government finance analyst wanting "which federal money came to
our city?" This is adapted from the server repo's own skill — the tool
behavior notes come from live API exploration.

## Before anything: two questions shape every answer

1. **What does "came to the city" mean?** Money awarded to recipients
   *located* in the city (`recipient_city`/`recipient_state`) is different
   from money for work *performed* in the city (`pop_city`/`pop_state`).
   Ask which the analyst means. Location search also catches hospitals,
   nonprofits, and private contractors in town — right for "our local
   economy," wrong for "our city government."
2. **Which entities count as "the city"?** The municipal corporation, its
   child registrations (utility, airport, renewal authority), and sibling
   governments (school district, housing authority, library district) are
   all separate recipients. `mcp_federal-awards_resolve(kind="recipient")`
   enumerates the city's own family; siblings must be resolved by their own
   names or caught with a city-wide location search. Ask whether siblings
   belong in scope.

## The standard workflow

```
resolve(city)        -> canonical city/state (6 Lovelands exist; pass state)
resolve(recipient)   -> UEI(s) + parent's child registrations
count_awards(...)    -> which award groups have data for these filters
search_awards(...)   -> one call per nonzero group (API limit, not a choice)
search_subawards(...)-> pass-through money (DO NOT SKIP; see below)
get_award(generated_id) -> full detail   list_award_transactions -> history
spending_summary     -> totals by year / program (CFDA) / agency / recipient
```

(All of these are `mcp_federal-awards_<name>` tools.)

## Tool notes and gotchas

- **`search_awards`** takes exactly one `award_group` per call
  (`contracts | grants | loans | direct_payments | other | idvs`); the API
  rejects mixed groups. "All federal money" = `count_awards` first, then one
  search per nonzero group. Results sort by amount descending. Page with
  `page` while `has_next` is true (`limit` caps at 100) — for audit-style
  completeness you must page to the end.
- **`search_subawards` is not optional for city totals.** A large share of
  federal money reaches cities as subawards through the state (highway money
  via the state DOT, education money via the state DOE). Prime search alone
  gives a materially wrong answer. In this tool, `recipient_*` filters match
  the SUB-recipient (the final recipient). Subawards exist only for
  `contracts` and `grants`.
- **IDs:** `get_award` and `list_award_transactions` take the `generated_id`
  from search results (strings like `ASST_NON_08CH011828_075` or
  `CONT_AWD_...`) — never the human award number (PIID/FAIN) and never an
  integer id. From subaward rows, drill into the prime award via
  `prime_award_generated_id`.
- **`resolve` first, filter second.** City names collide across states;
  agency filters need the exact toptier name (e.g. "Department of
  Transportation", resolvable from "DOT"); recipient matches return UEI,
  level (R/P/C), and — for parents — child registrations. A UEI-only search
  that ignores children undercounts the municipal total.
- **Fiscal years:** federal FY runs Oct 1–Sep 30, so FY2024 =
  `start_date="2023-10-01"`, `end_date="2024-09-30"`. Dates are YYYY-MM-DD.
- **Emergency funding:** filter `def_codes` — COVID: `L M N O P U V`;
  infrastructure (IIJA): `Z 1`. ARPA is code `V`. `get_award` returns
  per-DEF-code obligations.
- **Programs:** CFDA/assistance-listing numbers (e.g. `93.600` Head Start)
  filter via `cfda_numbers` and aggregate via
  `spending_summary(group="cfda")` — the natural cut for SEFA /
  single-audit prep.
- **`spending_summary`** answers "how much by X" without listing awards:
  `fiscal_year|quarter|month` for trends, `cfda|awarding_agency|recipient`
  for top-N breakdowns (`limit` caps the buckets; `has_more` says the list
  was cut).

## Caveats to state in answers (analysts will reconcile these)

- Amounts are **obligations, not cash received**. They will not tie to the
  city's grant ledger or drawdowns; outlay fields are federal outlays, still
  not payments received.
- **Subaward data is a floor, not a census**: reporting covers subawards
  ≥ $30k and is known-incomplete.
- Search data starts **FY2008** (2007-10-01); earlier dates are clamped
  with a warning.
- Data lags reporting cycles — current-quarter numbers are incomplete.

## Errors

- 400 with a descriptive message = a bad filter (fix the request, don't
  retry). 422 mentioning award_type_codes groups = mixed groups in one call.
- The server retries 5xx once automatically; a surfaced 5xx means the
  USAspending API is having trouble — say so and suggest trying later,
  don't hammer.
