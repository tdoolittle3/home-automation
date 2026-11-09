---
name: camera-archive
description: Answer questions about what or who the security cameras saw — people, vehicles, animals, by time or description — using the mcp_vision_* tools, which hold the full detection archive. Home Assistant's frigate sensors are the wrong source for these questions, and so is this archive for aircraft (planes come from the radio-logs skill, not the cameras).
version: 1.0.0
author: homelab
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [cameras, security, frigate, vision, driveway, backyard, homelab]
    category: homelab
---

# Camera archive (vision search)

The house cameras (driveway, backyard) feed a vision pipeline on snibbly that
keeps every detection: timestamps, object labels, image crops, visual
embeddings, and re-identified recurring individuals. The `mcp_vision_*` tools
query that archive directly.

## Routing rule — read this first

For ANY question about what or who the cameras saw, use the `mcp_vision_*`
tools, **not Home Assistant**. HA's frigate entities only expose coarse
live occupancy ("1 person in zone right now") — they cannot answer who, when,
how many events, or anything historical. The archive can, with footage links.

## Which tool

| Question shape | Tool and call |
|---|---|
| "Who was by the driveway today?" | `mcp_vision_recent_events` with label='person', camera='driveway' |
| "Any motion last night?" | `mcp_vision_recent_events` with night_only=true |
| "Was there a white van this week?" | `mcp_vision_search` with query='white delivery van', since='7d' |
| "Person walking a dog?" | `mcp_vision_frames_with` with labels=['person','dog'] |
| "When did the mail truck last come?" | `mcp_vision_entities`, find it by name, report last_seen |
| "Is that the same person as yesterday?" | `mcp_vision_entities` with label='person' — sighting counts + first/last seen |

## Answering well

- Every result carries a `url` into the vision-web UI — include the links so
  the owner can see the footage.
- Events have `source`: 'frigate' (live detection) or 'sweep' (found later in
  the recordings — things Frigate missed). Both are real; mention sweep finds.
- `search` scores are cosine similarity: ~0.22 is a loose match, ~0.30 strong.
  No hits? Lower min_score, widen the window, or fall back to recent_events.
- Times in results are already house-local.
