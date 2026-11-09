---
name: frigate-incidents
description: Diagnose why Frigate (the camera NVR) went down or was auto-restarted, by reading the incident bundles the frigate-guard saved before restarting it. Read-only; restarts are done by the guard, not by this agent.
version: 1.0.0
author: homelab
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [frigate, cameras, incident, logs, diagnosis, homelab]
    category: homelab
    requires_toolsets: [file]
---

# Frigate incident diagnosis

When Frigate's API hangs (container "running" but unhealthy), `frigate-guard`
on ladybird restarts it after 3 failed 2-minute runs and first saves evidence.
You diagnose from that evidence. You do NOT restart anything: if Frigate is
still down, say so and tell the user to check the guard / restart manually.

## Where

`/opt/data/workspace/frigate-incidents/<YYYYMMDDTHHMMSS>/` (read-only; newest
directory name = latest incident; empty/missing = no auto-restart has happened).

| File | Contents |
|---|---|
| `container-state.json` | Docker State: exit code, OOMKilled, health streak, start/finish times |
| `frigate.log`, `nginx.log`, `go2rtc.log` | Last 300 lines of each service (credentials redacted) |
| `docker-stats.txt` | CPU / memory / PIDs at the time |
| `host.txt` | uptime, memory, disk on the host |

## How to diagnose

1. List the directory, open the newest bundle (or the one the user names).
2. `container-state.json`: `OOMKilled: true` → memory. `Health.FailingStreak` and
   the health log say how long it was wedged.
3. `go2rtc.log`: `no route to host` / `i/o timeout` to 10.10.10.x = the camera
   or its network dropped (cameras: driveway .201, backyard .202).
4. `nginx.log`: `auth request unexpected status: 504` / `upstream timed out` on
   `/api/*` while `/` returns 200 = the Frigate Python API (port 5001) is hung.
5. `frigate.log`: last timestamp vs. the incident time — if it stops long before,
   the main process wedged (note when). ffmpeg crash loops = camera/stream side.
6. `host.txt`: low `free`/full `df` points at host pressure.

## Answer format

Lead with the most likely cause and confidence, then 2-4 bullets of evidence
with timestamps, then what would confirm or change the view. Label anything not
in the logs as a guess. Never print camera credentials even if you see one.
