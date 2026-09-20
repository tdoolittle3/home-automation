# DNS — ad filtering on `ladybird`

**AdGuard Home** serving DNS for the whole LAN, with ad and tracker filtering.
It replaces the Pi-hole that ran on the Raspberry Pi at `192.168.0.14`.

Stack lives at `stacks/dns/` -> `/opt/stacks/dns/`.

| What | Where |
|---|---|
| Admin UI | http://192.168.0.13:8053 |
| DNS (plain, LAN) | `192.168.0.13:53` — UDP and TCP |
| Seed config (tracked) | `stacks/dns/AdGuardHome.yaml` |
| Live config (gitignored) | `/opt/stacks/dns/conf/AdGuardHome.yaml` |
| Query log + filter lists | `/opt/stacks/dns/work/` |

Port 8053 rather than AdGuard's default 3000, because the `epg` container
already holds 3000 on this host.

---

## The one rule: this is load-bearing

Once the router hands out `192.168.0.13` as the DNS server, **every device in
the house resolves through this container**. A stopped container, a bad config,
or a `systemctl restart docker` does not look like "DNS is down" to anyone else
— it looks like *the internet is down*, on every device at once, including the
ones belonging to people who cannot fix it.

Three consequences worth internalising before you touch anything:

- **Ladybird's reboots are now everyone's reboots.** This box has a
  kernel-panic history (see the README gotchas). Budget for it.
- **Do maintenance from a device you have pinned to a different resolver**, or
  you will lose the ability to look things up at exactly the moment you need to.
- **Keep the Pi powered and working** until this has run clean for a couple of
  weeks. Rollback is a two-minute router change *only* while the Pi still
  answers — see [Rollback](#rollback).

---

## 1. First deploy

Nothing here disturbs the running Pi-hole. AdGuard binds port 53 on ladybird,
but no client asks ladybird for DNS until the router cutover in step 3. Port 53
is free on this host — `systemd-resolved` is inactive on this build.

Verify that first, because if something *is* on 53 the container will fail to
bind and the reason will not be obvious:

```bash
systemctl is-active systemd-resolved   # expect: inactive
sudo ss -lunp | grep -w 53             # expect: no output
```

Copy the stack up and seed the live config from the tracked one:

```bash
cd /opt/stacks/dns
mkdir -p conf work
cp AdGuardHome.yaml conf/AdGuardHome.yaml
```

Generate the admin password hash. This prompts twice rather than taking the
password on the command line, so it never lands in shell history:

```bash
docker run --rm -it --entrypoint htpasswd httpd:alpine -B -C 10 -n admin
```

It prints `admin:$2y$10$...`. Take everything **after** the colon and paste it
into the `HASH=` line below, keeping the single quotes — they stop the shell
eating the `$` signs:

```bash
HASH='$2y$10$paste-the-hash-here'
sed -i "s|REPLACE_ME_BCRYPT_HASH|$HASH|" conf/AdGuardHome.yaml
grep -c REPLACE_ME conf/AdGuardHome.yaml   # expect: 0
```

Bring it up:

```bash
docker compose up -d
docker compose logs -f adguardhome   # watch until the filter lists finish downloading
```

**Verify before going near the router.** Ask AdGuard directly — the container
has busybox `nslookup`, so this needs nothing installed on the host:

```bash
# resolves normally
docker exec adguardhome nslookup example.com 127.0.0.1

# and actually filters — expect 0.0.0.0
docker exec adguardhome nslookup doubleclick.net 127.0.0.1

# and answers the local name
docker exec adguardhome nslookup ladybird 127.0.0.1
```

Then from a workstation, without changing that machine's DNS settings:

```
nslookup doubleclick.net 192.168.0.13
```

All four must behave before you continue. Log in at
http://192.168.0.13:8053 and confirm the filter lists show non-zero rule
counts — a list that failed to download shows 0 rules and silently blocks
nothing.

---

## 2. Migrating from Pi-hole

The Pi-hole's **blocklists** do not need migrating — the lists in
`AdGuardHome.yaml` supersede them. What *does* matter is the **allowlist**:
every domain you unblocked over the years because something broke. That
knowledge exists nowhere else, and losing it means rediscovering each entry the
hard way, one annoyed household member at a time.

On the Pi at http://192.168.0.14/admin/ go to **Settings -> Teleporter ->
Export** and open the archive. The files that matter are the allowlist entries
(`whitelist.exact.json` / `whitelist.regex.json`, or the domain list in the
Pi-hole UI under **Domains -> Allowed**).

Each allowed domain becomes one line in AdGuard under **Filters -> Custom
filtering rules**:

```
@@||example.com^
```

That unblocks the domain and all its subdomains, at higher priority than every
blocklist. Add them all at once, then mirror them into `user_rules:` in
`stacks/dns/AdGuardHome.yaml` and commit — see [Config drift](#config-drift).

Also copy across any **local DNS records** the Pi-hole held. As of 2026-09-20 it
held none except its own `pi.hole` entry, which is why nothing is listed here —
but check rather than assume, since a record added later would vanish at
cutover with no error anywhere:

**Settings -> Local DNS -> DNS Records** on the Pi-hole; the equivalent in
AdGuard is **Filters -> DNS rewrites**, already seeded with `ladybird`.

---

## 3. Cutover

Two changes, in this order.

**a. Point the router's DHCP at ladybird.** In the router admin, set the DHCP
DNS server to `192.168.0.13` and remove `192.168.0.14`.

List **one** server, not both. Clients do not treat a second DNS server as a
failover to be used only when the first is down — they query whichever they
like, whenever they like. With two filtering resolvers whose rules have drifted
apart, roughly half your queries bypass the list you just edited, and the
resulting "this site is blocked, but only sometimes" is genuinely unpleasant to
diagnose.

**Ladybird's LAN address is DHCP.** The README lists the reservation for MAC
`38:05:25:35:71:69` as an open gap; it stops being optional here. If ladybird
ever gets a different address, the whole house loses DNS. Set it now, in the
same router session.

Devices pick the new server up as their leases renew. To force it: reboot the
router, or on a workstation `ipconfig /release && ipconfig /renew` (Windows) or
re-toggle Wi-Fi.

**b. Repoint the containers.** `host/etc/docker/daemon.json` sends every
container's DNS to `192.168.0.14`. Update it to ladybird:

```json
{
  "dns": ["192.168.0.13", "1.1.1.1", "8.8.8.8"]
}
```

```bash
sudo systemctl restart docker
```

**That restart bounces every container on the box** — Frigate, Home Assistant,
Immich, the lot. Do it in the same maintenance window as the router change, not
casually afterwards. The `1.1.1.1` and `8.8.8.8` fallbacks are what keep
container DNS alive during the seconds before AdGuard is back up.

**Verify the cutover:**

```bash
# from a client whose lease has renewed - expect 192.168.0.13
nslookup doubleclick.net        # Windows: also shows which server answered
```

Then watch **Query log** in the AdGuard UI. Within a minute or two you should
see queries arriving from several distinct client IPs. If every query shows one
source address, host networking is not in effect — see
[Gotchas](#gotchas).

---

## Rollback

While the Pi is still running, rollback is one router change:

1. Set the router's DHCP DNS back to `192.168.0.14`.
2. Revert `daemon.json` to `192.168.0.14` and `sudo systemctl restart docker`.
3. Renew leases (reboot the router).

For an immediate fix on one device without waiting for leases, set that
device's DNS manually to `192.168.0.14` or `1.1.1.1`.

If AdGuard is up but over-blocking, you do not need a rollback — turn
**Protection** off in the UI (top of the dashboard). That keeps it resolving
while it stops filtering, which is usually the right first move when someone
reports a broken site and you are not at a keyboard.

---

## Retiring the Pi

Leave the Pi-hole running and reachable for **at least two weeks** after
cutover. It costs nothing, and it is the only fast rollback.

When you do retire it:

- Take a Teleporter export first and keep it with the backups.
- Power it down but leave it on the switch — the README already earmarks this
  Pi as the future independent watchdog for ladybird, and that role becomes
  *more* valuable now that ladybird owns DNS. See
  [planned Pi watchdog](diagrams/README.md#planned-pi-watchdog).
- Update the README service table and the network diagram.

---

## Config drift

AdGuard **rewrites its own config file** on every change made in the UI, and
strips all comments when it does. So `stacks/dns/AdGuardHome.yaml` is the seed
and the documented intent; `/opt/stacks/dns/conf/AdGuardHome.yaml` is what is
actually running, and it is gitignored.

Same shape as the Frigate `config.yml` problem, and the same discipline:

**After any change in the UI**, pull it back into the repo:

```bash
# on the server
sudo cat /opt/stacks/dns/conf/AdGuardHome.yaml
```

Copy the parts that changed into `stacks/dns/AdGuardHome.yaml` by hand, keeping
the comments, and commit. Do **not** paste the file wholesale — it contains the
admin password hash, and the repo rule is that no credential material lands in
git.

To go the other way (repo -> server), which is what a rebuild does:

```bash
cd /opt/stacks/dns
docker compose down
sudo cp AdGuardHome.yaml conf/AdGuardHome.yaml
# re-insert the password hash, as in First deploy
docker compose up -d
```

---

## Gotchas

- **`bootstrap_dns` must be plain IP addresses.** They are what resolves the
  DoH upstream hostnames before a resolver exists. Put a hostname there — or
  point it at this server — and AdGuard deadlocks at startup with no DNS on the
  LAN at all, and the log does not say so plainly.
- **Host networking, not a `ports:` mapping.** Docker's userland proxy rewrites
  the source address of inbound UDP, so with a port mapping every query appears
  to come from the bridge gateway. Per-client logs, per-client rules and the
  top-clients view all collapse into one row. This is the most common way a
  containerised DNS server ends up useless for diagnosis.
- **AdGuard's stock per-client rate limit is 20 queries/sec**, and that is low
  enough for a single browser to trip. Set to `0` here. The symptom if it is
  ever restored is horrible: a handful of assets on a busy page failing to load,
  intermittently, with nothing that looks like an error anywhere.
- **`/etc/resolv.conf` on ladybird is managed by Tailscale** (`100.100.100.100`)
  and carries a "do not edit" banner it means. The host does not resolve through
  AdGuard, and that is fine — it also means the host keeps working if AdGuard
  does not. If you want the host filtered too, set it in the Tailscale admin
  console under DNS, not in the file.
- **`version.bind` is blocked by default**, so the usual trick for fingerprinting
  a resolver from another machine returns nothing. Use the UI or
  `docker exec adguardhome ...` instead.
- **The `:latest` tag can move across a major version.** The digest that was
  deployed is recorded in `VERSIONS.txt`; pin to it in `docker-compose.yml` if a
  `docker compose pull` ever needs to be reversible on short notice.
- **Files under `conf/` and `work/` are owned by root** — the container writes
  as root. Reading or editing them from the host needs `sudo`, same as the
  Frigate recordings.
- **Blocklists overlap heavily; more lists is not more blocking.** It mostly
  makes a false positive harder to attribute. When something breaks, disable
  **OISD Big** first — it is the broad one, and the most likely culprit.
