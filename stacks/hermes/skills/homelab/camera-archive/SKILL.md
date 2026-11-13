---
name: camera-archive
description: Answer questions about what or who the security cameras saw — people, vehicles (garbage truck, delivery van, mail truck, school bus), animals, by day or description — using the mcp_vision_* tools, which hold the full detection archive. Home Assistant's frigate sensors are the wrong source for these questions, and so is this archive for aircraft (planes come from the radio-logs skill, not the cameras).
version: 1.1.0
author: homelab
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [cameras, security, frigate, vision, driveway, backyard, garbage truck, homelab]
    category: homelab
---

# Camera archive (vision search)

## Two systems, one archive: who does what

- **Frigate** (on ladybird) watches the camera streams live. It decides in real
  time that "something is moving" and fires an event. It drives the live
  alerts and Home Assistant's frigate sensors. It's fast, but it only ever
  looks at the low-res detect stream and it misses things.
- **The vision worker** (on snibbly) is the **primary source for questions about
  what happened**. It pulls the full-resolution footage for every Frigate
  event and also runs its own **sweep** over the continuous recordings, which
  catches what Frigate missed. It re-detects, recognizes kinds of vehicles
  (garbage truck, delivery van, mail truck, school bus), clusters recurring
  individuals, and keeps it all. The `mcp_vision_*` tools query it.

So: answer from `mcp_vision_*`. Frigate's events are *inputs* to it — results
with `source: frigate` started as a Frigate event, `source: sweep` are finds
Frigate never flagged. Both are real. Never use HA's frigate sensors for
"what/who/when" questions; they only say what's in frame right now.

## Dates: pass words, not dates

For a named day, pass the words as `since` and leave `until` empty — the
server resolves them in house time:
`since='yesterday'`, `'wednesday morning'`, `'last night'`, `'today afternoon'`,
`'tue night'`. **Do not work out the date yourself**; the default window
(last 24h) is not "yesterday". Morning = 05–12, afternoon = 12–17, evening =
17–21, night = 21–07. Every result echoes the resolved `window` — say it in
your answer.

## Which tool

| Question shape | Tool and call |
|---|---|
| "Did the garbage truck come Wednesday morning?" | `mcp_vision_recent_events` tag='garbage truck', since='wednesday morning' |
| "When did the mail truck last come?" | `mcp_vision_tags` (last_seen per kind), then `recent_events` tag='mail truck' for detail |
| "Who was by the driveway today?" | `mcp_vision_recent_events` label='person', camera='driveway', since='today' |
| "Any motion last night?" | `mcp_vision_recent_events` since='last night' |
| "Was there a red pickup this week?" (not a tag) | `mcp_vision_search` query='red pickup truck', since='7d' |
| "Person walking a dog?" | `mcp_vision_frames_with` labels=['person','dog'] |
| "Is that the same person as yesterday?" | `mcp_vision_entities` label='person' |

Order of preference for "did X come/happen": a **tag** if X is one (check
`mcp_vision_tags`) → a **label** (person, car, truck, dog, cat…) →
`search` with a description. A tag hit is precise; `search` is a fuzzy
similarity ranking and will always return *something* — only trust scores
around 0.28+ and say it's a visual-similarity match.

## Answering well

- Lead with yes/no and the time: "Yes — a garbage truck passed at 09:53
  Wednesday (driveway camera)", then the footage link (`url`).
- No hit is an answer too, but state the window you searched and that the
  archive only knows what the two cameras (driveway, backyard) could see.
- Group repeats: many events of the same parked car or person minutes apart
  are one thing, not a list of twenty.
- Times in results are already house-local.
