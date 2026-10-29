#!/usr/bin/env python3
"""Parse-check every tracked YAML file outside ansible/ (ansible-lint owns those).

Home Assistant files use custom tags (!include, !secret, !input); those are
accepted as opaque values rather than skipping the files entirely, so a real
syntax error in automations.yaml still fails the build.
"""
import subprocess
import sys

import yaml


class AnyTagLoader(yaml.SafeLoader):
    pass


def _opaque(loader, tag_suffix, node):
    return None


AnyTagLoader.add_multi_constructor("!", _opaque)

tracked = subprocess.run(
    ["git", "ls-files", "*.yml", "*.yaml"],
    capture_output=True, text=True, check=True,
).stdout.split()

failures = 0
for path in tracked:
    if path.startswith("ansible/"):
        continue
    try:
        with open(path, encoding="utf-8") as fh:
            list(yaml.load_all(fh, Loader=AnyTagLoader))
        print(f"ok   {path}")
    except yaml.YAMLError as exc:
        failures += 1
        print(f"FAIL {path}\n     {exc}")

sys.exit(1 if failures else 0)
