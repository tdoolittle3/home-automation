# `ladybird` — the house server

One mini PC in the rack runs the security cameras, the movie library, the photo library, the home
automation, and the DNS for every device in the house.

**This repository is its blueprint** — every configuration file needed to rebuild the machine from a
blank Debian install, minus the passwords. Nothing here is running code; it is the plan for what
runs on the server.

![Ladybird's hardware, Debian host services, and container workloads in three layers.](docs/diagrams/architecture.svg)

---

## What it does

| | |
|---|---|
| 📹 **Watches the house** | Two PoE cameras record continuously and flag people and cars, using the built-in GPU to do it. The cameras sit on their own cable network with no route to the internet. |
| 🏠 **Runs the automation** | Home Assistant, with the cameras, lights and the server's own health all wired into it. |
| 🍿 **Serves the media** | Jellyfin for films and TV, with a live-TV guide rebuilt nightly. |
| 🖼️ **Keeps the photos** | Immich — a self-hosted photo library that replaces the cloud one. |
| 🛡️ **Filters the ads** | AdGuard Home answers DNS for the whole house, so ads are gone on every device without installing anything on them. |
| ✈️ **Tracks the planes** | An RTL-SDR radio decodes ADS-B and draws a live aircraft map, because why not. |
| 👀 **Watches itself** | Uptime Kuma, a UPS that shuts the box down cleanly, and small guards that shout if the disk fills or the power fails. |

## Where to click

Everything is on the home network only. Nothing is port-forwarded; from outside the house you come
in over Tailscale.

| | Address | Notes |
|---|---|---|
| **Dashboard** | `http://ladybird/` | the everyday view — custom UI over the Home Assistant API |
| **Cameras** (Frigate) | `https://192.168.0.13:8971` | login required |
| **Home Assistant** | `http://192.168.0.13:8123` | http, not https — by design |
| **Photos** (Immich) | `http://192.168.0.13:2283` | first account created becomes the admin |
| **Media** (Jellyfin) | `http://192.168.0.13:8096` | |
| **Ad filtering** (AdGuard) | `http://192.168.0.13:8053` | also answers DNS on port 53 for the LAN |
| **Monitoring** (Uptime Kuma) | `http://192.168.0.13:3001` | [setup notes](docs/uptime-kuma.md) |
| **Automations** (n8n) | `http://192.168.0.13:5678` | |
| **Aircraft map** (tar1090) | `http://192.168.0.13:8080` | [setup notes](docs/sdr.md) |
| **File share** (Samba) | `//192.168.0.13/files` | serves `/srv/storage/files` |
| Frigate API | `http://192.168.0.13:5000` | **no login** — LAN only, never port-forward |
| MQTT (Mosquitto) | `192.168.0.13:1883` | anonymous, LAN only |
| TV guide (XMLTV) | `http://192.168.0.13:3000/guide.xml` | feeds Jellyfin's live TV |

## Read this before changing anything

- **DNS for the whole house runs here.** When this box is down, nothing in the house resolves — and
  it does not look like "the server is down", it looks like the internet is broken, on every device
  at once. The old Pi-hole at `192.168.0.14` is the rollback and stays powered.
  Read [docs/dns.md](docs/dns.md) before touching that stack.
- **The dashboard and the Frigate API have no login.** Anyone who can reach them can use them. Keep
  them on the LAN.
- **Two configs rewrite themselves** — Frigate's and AdGuard's. What is in this repo is the seed,
  not a live copy. Editing them carelessly has a specific way of going wrong; see
  [docs/gotchas.md](docs/gotchas.md).
- **This machine has panicked before.** A frozen box with the lights on is a kernel panic, not a
  network fault. The crash record is in `/var/lib/systemd/pstore/` after the next boot.

## The shape of it

Two physical networks, and the server is the only thing on both. The cameras live on their own
segment with **no gateway**, so they cannot reach the internet whatever their firmware would prefer;
the server serves them time and pulls their video. Everything else sits on the home LAN, reachable
from outside only through Tailscale.

![Home LAN and Tailscale access alongside the dedicated PoE camera network.](docs/diagrams/network.svg)

Five drawings cover the system from different angles — all of them are plain SVG you can open,
zoom and edit:

| Drawing | What it explains |
|---|---|
| [Inside ladybird](docs/diagrams/architecture.svg) | machine → Debian → containers, GPU access and storage |
| [Network topology](docs/diagrams/network.svg) | home LAN, Tailscale and the camera segment |
| [Rack elevation](docs/diagrams/rack.svg) | what sits in U1–U8, and how it is powered |
| [Camera to dashboard](docs/diagrams/camera-flow.svg) | video in, events through MQTT, alerts out |
| [Wiring and power](docs/diagrams/wiring.svg) | which switch port goes to which patch jack |

Full notes, the hardware inventory and what is still unconfirmed: [system atlas](docs/diagrams/README.md).

## The guides

| If you want to… | Read |
|---|---|
| know how it all fits together | [architecture.md](docs/architecture.md) |
| do something day to day | [operations.md](docs/operations.md) — health checks, common tasks, after a crash |
| rebuild the whole thing | [rebuild.md](docs/rebuild.md) — blank Debian to running, in 14 steps |
| avoid a trap someone already hit | [gotchas.md](docs/gotchas.md) |
| know what is *not* done | [known-gaps.md](docs/known-gaps.md) |
| add or reconfigure a camera | [cameras.md](docs/cameras.md) |
| touch DNS | [dns.md](docs/dns.md) |
| touch the radio | [sdr.md](docs/sdr.md) |
| change what is monitored | [uptime-kuma.md](docs/uptime-kuma.md) |

## The machine

Minisforum M1 Plus — i5-12600H (12C/16T), Iris Xe graphics, 16 GB RAM, 512 GB NVMe, dual 2.5GbE.
Debian 13 (Trixie), kernel 6.12, on bare metal at `192.168.0.13`. Exact image versions at the last
capture are in [VERSIONS.txt](VERSIONS.txt).

Each service is a Docker Compose stack under `stacks/`, deployed to `/opt/stacks` on the server;
`host/` mirrors the handful of files that belong in `/etc`. Passwords and tokens are never in this
repo — the list of what to recreate by hand is at the end of
[rebuild.md](docs/rebuild.md#secrets--not-in-this-repo).
