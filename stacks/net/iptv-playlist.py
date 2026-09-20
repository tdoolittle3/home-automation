#!/usr/bin/env python3
"""Build Jellyfin's IPTV playlist from iptv-org, keeping only channels that
actually play.

Three stages:
  1. filter  - official ad-supported (FAST) operators only; drop entries the
               upstream list tags as geo-blocked or part-time
  2. probe   - resolve each stream to its first media segment and reject
               placeholder content (Pluto's takedown slate and friends).
               A master playlist can look perfectly healthy while the segments
               behind it are a "where to watch" loop, so the segment path is
               the only reliable check.
  3. publish - atomic replace, with a floor so a bad run can't wipe a good file
"""
import os, re, sys, tempfile, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

SRC = 'https://iptv-org.github.io/iptv/categories/movies.m3u'
DST = '/srv/storage/media/iptv/fast.m3u8'
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0 Safari/537.36')
MIN_CHANNELS = 10
WORKERS = 8

# Operators running official, free, ad-supported services. Edit to taste -
# this is the whole allowlist. Matched case-insensitively on word boundaries,
# NOT as bare substrings: plain 'plex' also matches "Colors Cineplex" and
# "MoviePlex", which are not Plex and not free.
FAST_OPERATORS = (
    'pluto tv', 'samsung tv plus', 'rakuten tv', 'plex', 'tubi', 'xumo',
    'roku channel', 'stirr', 'local now', 'crackle', 'moviedome', 'filmrise',
)
OPERATOR_RE = re.compile(
    '|'.join(r'\b' + re.escape(o) + r'\b' for o in FAST_OPERATORS), re.I)
# Upstream tags these inline in the channel name.
NAME_REJECT = ('[geo-blocked]', '[not 24/7]')
# Placeholder markers seen in segment paths.
SLATE_MARKERS = ('takedownslate', 'unavailable', 'not_available', 'geoblock',
                 'comingsoon', 'placeholder', 'slate')


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read().decode('utf-8', 'replace')


def mask_quoted_commas(s):
    """Blank out commas inside quoted attribute values, preserving length.

    #EXTINF attribute values contain commas - a browser user-agent has
    "(KHTML, like Gecko)" in it - so splitting on the first raw comma cuts the
    line in the wrong place and mangles the channel name.
    """
    out, in_q = [], False
    for ch in s:
        if ch == '"':
            in_q = not in_q
            out.append(ch)
        else:
            out.append('\x00' if (in_q and ch == ',') else ch)
    return ''.join(out)


def parse(lines):
    """-> [(name, [directive lines], url)]"""
    chans, i = [], 0
    while i < len(lines):
        if lines[i].startswith('#EXTINF'):
            ext = lines[i]
            cut = mask_quoted_commas(ext).find(',')
            name = ext[cut + 1:].strip() if cut != -1 else ''
            # Carry #EXTVLCOPT / #EXTHTTP directives - some streams need the
            # user-agent or referrer they set.
            extras, j = [], i + 1
            while j < len(lines) and lines[j].startswith('#'):
                extras.append(lines[j])
                j += 1
            if j < len(lines) and lines[j].startswith('http'):
                chans.append((name, [ext] + extras, lines[j].strip()))
                i = j
        i += 1
    return chans


def wanted(name):
    low = name.lower()
    if any(r in low for r in NAME_REJECT):
        return False
    return bool(OPERATOR_RE.search(name))


def plays(chan):
    """True if the stream resolves to a real media segment."""
    name, _, url = chan
    try:
        final, body = fetch(url)
        rows = [x.strip() for x in body.splitlines()
                if x.strip() and not x.startswith('#')]
        if not rows:
            return name, False, 'empty'
        if '#EXTINF' in body:                      # already a media playlist
            seg, base = rows[0], final
        else:                                      # master -> variant
            final2, vbody = fetch(urllib.parse.urljoin(final, rows[0]))
            segs = [x.strip() for x in vbody.splitlines()
                    if x.strip() and not x.startswith('#')]
            if not segs:
                return name, False, 'empty variant'
            seg, base = segs[0], final2
        path = urllib.parse.urljoin(base, seg).lower()
        if any(m in path for m in SLATE_MARKERS):
            return name, False, 'placeholder'
        return name, True, 'ok'
    except Exception as e:
        return name, False, type(e).__name__


def main():
    try:
        lines = fetch(SRC, 90)[1].splitlines()
    except (urllib.error.URLError, TimeoutError) as e:
        print(f'fetch failed, keeping existing playlist: {e}', file=sys.stderr)
        return 1

    all_ch = parse(lines)
    cands = [c for c in all_ch if wanted(c[0])]
    print(f'upstream={len(all_ch)}  FAST candidates={len(cands)}')

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        verdicts = list(ex.map(plays, cands))

    live = [c for c, (_, good, _) in zip(cands, verdicts) if good]
    rejected = [(n, why) for n, good, why in verdicts if not good]
    print(f'probed={len(cands)}  live={len(live)}  rejected={len(rejected)}')
    for n, why in rejected[:10]:
        print(f'  drop [{why}] {n[:50]}')

    if len(live) < MIN_CHANNELS:
        print(f'only {len(live)} live (floor {MIN_CHANNELS}); '
              'refusing to overwrite', file=sys.stderr)
        return 1

    out = ['#EXTM3U']
    for _, directives, url in live:
        out += directives + [url]
    new = '\n'.join(out) + '\n'

    old = ''
    if os.path.exists(DST):
        with open(DST, encoding='utf-8') as f:
            old = f.read()
    if new == old:
        print(f'unchanged: {len(live)} channels')
        return 0

    os.makedirs(os.path.dirname(DST), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DST), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(new)
    os.chmod(tmp, 0o644)
    os.replace(tmp, DST)
    print(f'updated: {len(live)} channels -> {DST}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
