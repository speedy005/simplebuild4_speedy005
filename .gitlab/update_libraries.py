#!/usr/bin/env python3
"""Auto-update library versions in defaults/libraries.yaml.

Checks each library's version_check source for newer versions and updates
the YAML file. Designed to run in GitLab CI or locally.

Usage:
    python3 .gitlab/update_libraries.py [--dry-run]
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

import yaml

LIBRARIES_YAML = Path(__file__).resolve().parent.parent / "defaults" / "libraries.yaml"
PRERELEASE_TAGS = ("alpha", "beta", "preview", "rc", "dev", "pre")


def fetch_json(url: str) -> list | dict:
    """Fetch JSON from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "s4-updater/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_html(url: str) -> str:
    """Fetch HTML from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "s4-updater/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def is_prerelease(version: str) -> bool:
    """Check if version string contains pre-release indicators."""
    v_lower = version.lower()
    return any(tag in v_lower for tag in PRERELEASE_TAGS)


# Pre-release rank — lower = less stable. Used by parse_version().
_PRERELEASE_RANK = {
    "dev": 1, "alpha": 2, "a": 2,
    "beta": 3, "b": 3,
    "pre": 4, "preview": 4,
    "rc": 5, "c": 5,
}


def parse_version(version: str) -> tuple:
    """Parse version string into comparable tuple.

    SemVer-aware: pre-release segments (beta, rc, alpha …) sort *before*
    the corresponding release — e.g. 4.0.0-beta1 < 4.0.0.
    Letter-suffix patches like '1.1.1w' still sort *after* '1.1.1'
    (OpenSSL LTS convention).

    Implementation: every tuple ends with a release sentinel so that
    Python's shorter-tuple-is-less rule can never make a release look
    older than a pre-release of the same version.
      release   → last element (0, 9999, 0)
      pre-rel   → last element (-1, rank, patch_num)
    """
    v = re.sub(r'^[a-zA-Z_-]+', '', version)
    parts = []
    release_sentinel = (0, 9999, 0)   # overridden when a pre-release tag is found
    for p in re.split(r'[.\-]', v):
        if not p:
            continue
        try:
            parts.append((0, int(p)))
        except ValueError:
            # Pure letters + optional digits → pre-release tag (beta1, rc2, alpha)
            tag_match = re.match(r'^([a-zA-Z]+)(\d*)$', p)
            if tag_match:
                tag = tag_match.group(1).lower()
                num = int(tag_match.group(2)) if tag_match.group(2) else 0
                rank = _PRERELEASE_RANK.get(tag, 5)
                release_sentinel = (-1, rank, num)   # pre-release < release
            else:
                # digit+letter like "1w" (OpenSSL 1.1.1w patch suffix)
                parts.append((1, p))   # sorts after pure-numeric parts ✓
    parts.append(release_sentinel)
    return tuple(parts)


def get_branch(version: str) -> str:
    """Extract branch from version by removing the last numeric/alpha segment.

    Examples:
        '3.6.1'  → '3.6'    (major.minor branch)
        '1.1.1w' → '1.1.1'  (OpenSSL 1.1.1 branch, 'w' is the patch)
        '1.1.0l' → '1.1.0'  (OpenSSL 1.1.0 branch, 'l' is the patch)
        '1.0.29' → '1.0'    (libusb branch)
        '8.19.0' → '8'      (libcurl major branch)
        '2.4.1'  → '2.4'    (pcsclite branch)
    """
    # Split into parts
    parts = version.split(".")
    if len(parts) <= 1:
        return version

    # Check if last part is purely numeric or has trailing alpha (like '1w', '19')
    last = parts[-1]

    # If last part has letters mixed with numbers (like "1w"), the branch includes
    # everything except the trailing letters: "1.1.1w" → branch "1.1.1"
    alpha_match = re.match(r'^(\d+)([a-zA-Z]+)$', last)
    if alpha_match:
        return ".".join(parts[:-1] + [alpha_match.group(1)])

    # Otherwise branch = everything except last part: "3.6.1" → "3.6"
    return ".".join(parts[:-1])


# ---------------------------------------------------------------------------
# Version fetchers
# ---------------------------------------------------------------------------

def fetch_github_versions(repo: str) -> list[dict]:
    """Fetch versions from GitHub releases API.

    Returns list of {version, prerelease, tag} dicts.
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    try:
        releases = fetch_json(url)
    except Exception as e:
        print(f"  WARNING: GitHub API failed for {repo}: {e}")
        return []

    versions = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        # Normalize tag to version
        version = tag
        # Strip common prefixes
        for prefix in ["openssl-", "OpenSSL_", "curl-", "v"]:
            if version.startswith(prefix):
                version = version[len(prefix):]
        # curl uses underscores: curl-8_19_0 → 8.19.0
        if repo == "curl/curl":
            version = version.replace("_", ".")
        # OpenSSL old style: 1_1_1w → 1.1.1w
        version = re.sub(r'^(\d+)_(\d+)_(\d+)', r'\1.\2.\3', version)

        if not version or not version[0].isdigit():
            continue

        versions.append({
            "version": version,
            "prerelease": rel.get("prerelease", False) or is_prerelease(version),
            "tag": tag,
        })

    return versions


def fetch_url_versions(url: str, lib_name: str) -> list[dict]:
    """Fetch versions by scraping a directory listing page.

    Used for pcsclite (pcsclite.apdu.fr) and ccid (ccid.apdu.fr).
    """
    try:
        html = fetch_html(url)
    except Exception as e:
        print(f"  WARNING: URL fetch failed for {url}: {e}")
        return []

    versions = []
    # Find links to tar files
    pattern = re.compile(r'href="[^"]*?(\d+\.\d+\.\d+)[^"]*?\.tar\.')
    seen = set()
    for match in pattern.finditer(html):
        version = match.group(1)
        if version not in seen:
            seen.add(version)
            versions.append({
                "version": version,
                "prerelease": is_prerelease(version),
                "tag": "",
            })

    return versions


def fetch_versions(version_check: str, lib_name: str) -> list[dict]:
    """Dispatch to appropriate version fetcher."""
    if version_check.startswith("github:"):
        repo = version_check[7:]
        return fetch_github_versions(repo)
    elif version_check.startswith("url:"):
        url = version_check[4:]
        return fetch_url_versions(url, lib_name)
    return []


# Cache: lib_name → set of branch strings currently marked LTS upstream.
_LTS_BRANCHES_CACHE: dict[str, set[str]] = {}


def fetch_lts_branches(lib_name: str) -> set[str]:
    """Which upstream branches are currently flagged as LTS?

    GitHub release metadata doesn't carry this — OpenSSL publishes it
    only on their release-policy page. For other libs we have no
    reliable source, so return empty.
    """
    if lib_name in _LTS_BRANCHES_CACHE:
        return _LTS_BRANCHES_CACHE[lib_name]
    out: set[str] = set()
    if lib_name == "OpenSSL":
        try:
            html = fetch_html("https://openssl-library.org/source/")
            for m in re.finditer(r'(\d+\.\d+)\s*\[LTS\]', html):
                out.add(m.group(1))
        except Exception as e:
            print(f"  WARNING: OpenSSL LTS scrape failed: {e}")
    _LTS_BRANCHES_CACHE[lib_name] = out
    return out


# ---------------------------------------------------------------------------
# Update logic
# ---------------------------------------------------------------------------

def find_updates(lib: dict, all_versions: list[dict]) -> list[tuple[str, str]]:
    """Find version updates for a library's variants.

    Returns list of (old_version, new_version) tuples.
    Does NOT modify the lib dict — changes are applied via string replacement.
    """
    lib_name = lib["name"]
    variants = lib.get("variants", [])
    if not variants:
        return []

    allow_prerelease = True
    updates = []
    all_variant_versions = {v.get("version", "") for v in variants}

    for variant in variants:
        v = variant.get("version", "")
        if v in ("auto", "master", "custom"):
            continue
        # Skip fixed bitslice variants like 1.1.0-64, 1.1.0-32
        if re.match(r'^\d+\.\d+\.\d+-\d+$', v):
            continue

        # Determine branch from label or version.
        # Convention:
        #   "X.Y.x (...)" — auto-bump within X.Y branch
        #   "X.Y.Z (...)" where X.Y.Z equals the variant's version —
        #                 pinned anchor, no bumps (used e.g. to hold the
        #                 last pre-C11 pcsclite release even though newer
        #                 patches exist in the same 1.9 branch).
        #   "X.Y.Z (...)" where X.Y.Z differs from the variant's version —
        #                 branch marker only (OpenSSL "1.1.1" label on
        #                 variant "1.1.1w"); fall through to get_branch().
        label = variant.get("label", "")
        label_branch = re.match(r'^([\d.]+)\.x', label)
        if label_branch:
            branch = label_branch.group(1)
        else:
            label_exact = re.match(r'^([\d.]+)', label)
            if label_exact and label_exact.group(1) == v:
                continue
            branch = get_branch(v)
        is_pre = is_prerelease(v)

        # Find best version in same branch
        candidates = []
        for av in all_versions:
            av_branch = get_branch(av["version"])
            if not (av_branch == branch or av_branch.startswith(branch + ".")):
                continue
            if av["prerelease"] and not allow_prerelease:
                continue
            if av["prerelease"] and not is_pre:
                continue  # never bump stable → pre
            if not av["prerelease"] and is_pre:
                continue  # never promote pre → stable (handled by removal)
            candidates.append(av)

        if not candidates:
            continue

        candidates.sort(key=lambda x: parse_version(x["version"]), reverse=True)
        newest = candidates[0]["version"]

        if newest != v and parse_version(newest) > parse_version(v) and newest not in all_variant_versions:
            updates.append((v, newest))
            # Mark the target as taken so a second variant in the same
            # branch (e.g. a deliberate "no C11 needed" anchor pinned
            # to an older version) doesn't get bumped to the same
            # version on a later iteration — that would duplicate the
            # entry and erase the anchor.
            all_variant_versions.add(newest)
            all_variant_versions.discard(v)

    return updates


def _release_form(version: str) -> str:
    """``1.0.30-rc1`` → ``1.0.30``; ``4.1.0-beta1`` → ``4.1.0``."""
    return re.sub(
        r'[-_.]?(rc|beta|alpha|preview|dev|pre|a|b)\d*.*$',
        '', version, flags=re.IGNORECASE,
    )


def find_obsolete_prereleases(lib: dict, all_versions: list[dict]) -> list[str]:
    """Pre-release variants whose stable equivalent now exists upstream."""
    upstream_stable = {av["version"] for av in all_versions if not av["prerelease"]}
    obsolete = []
    for v in lib.get("variants", []):
        ver = v.get("version", "")
        if not is_prerelease(ver):
            continue
        if _release_form(ver) in upstream_stable:
            obsolete.append(ver)
    return obsolete


def find_new_versions(lib: dict, all_versions: list[dict],
                      updates: list[tuple[str, str]] | None = None) -> list[dict]:
    """Propose new variants to add — stable branches we don't yet cover,
    plus pre-releases not already superseded by any stable.

    Returns a list of dicts: ``{"version", "prerelease", "branch"}``.

    ``updates`` is the output of ``find_updates`` for the same lib —
    we apply it conceptually so that a version about to be bumped in
    the YAML isn't proposed again as a new variant (otherwise the
    pipeline both rewrites rc1→rc2 *and* inserts a fresh rc2 → dup).
    """
    variants = lib.get("variants", [])
    existing_versions = {v.get("version", "") for v in variants}
    # Apply pending updates conceptually so the dedup matches the
    # post-pipeline state.
    for old, new in updates or []:
        existing_versions.discard(old)
        existing_versions.add(new)

    # Branch-max from EXISTING stable variants + branches that already
    # have a pre-release representative we don't want to duplicate.
    existing_stable_branch_max: dict[str, tuple] = {}
    existing_pre_branches: set[str] = set()
    for ver in existing_versions:
        if ver in ("auto", "master", "custom"):
            continue
        # Skip fixed bitslice/anchor variants like 1.1.0-64 — not real pres.
        if re.match(r'^\d+\.\d+\.\d+-\d+$', ver):
            continue
        if is_prerelease(ver):
            existing_pre_branches.add(get_branch(_release_form(ver)))
            continue
        b = get_branch(ver)
        pv = parse_version(ver)
        if pv > existing_stable_branch_max.get(b, ()):
            existing_stable_branch_max[b] = pv

    # Branch-max from UPSTREAM stable — needed so pre-releases get
    # filtered the moment the equivalent stable ships upstream, even
    # before our YAML catches up. Also used to pick the newest stable
    # of each newly discovered upstream branch.
    upstream_stable_newest: dict[str, dict] = {}
    for av in all_versions:
        if av["prerelease"]:
            continue
        b = get_branch(av["version"])
        pv = parse_version(av["version"])
        cur = upstream_stable_newest.get(b)
        if cur is None or pv > parse_version(cur["version"]):
            upstream_stable_newest[b] = av

    new_entries: list[dict] = []
    seen_new_branches: set[str] = set()

    # Highest existing stable overall — the floor below which we never
    # auto-add historical branches. Otherwise an initial run would
    # flood the YAML with every ancient minor branch upstream ever had.
    highest_existing_stable: tuple = max(
        existing_stable_branch_max.values(), default=(),
    )

    # (1) NEW stable branches that are strictly newer than everything
    # we currently cover. find_updates handles bumps within an existing
    # branch; find_obsolete_prereleases handles pre→stable graduation
    # of a tracked pre-release. We only step in when upstream shipped
    # a brand-new, higher branch (e.g. 4.1.x once we only had 4.0.x).
    #
    # A note on branch granularity: get_branch strips one trailing
    # segment, so "1.3.2" → "1.3" but "1.3" → "1". When upstream
    # occasionally publishes a short tag (zlib ships "1.3" as a tag,
    # not "1.3.0"), its coarse branch like "1" would look brand-new
    # to us even though we track its finer-grained children (1.3.x).
    # Treat an upstream branch as already covered when any existing
    # branch has it as a prefix.
    def _covered(upstream_branch: str) -> bool:
        if upstream_branch in existing_stable_branch_max:
            return True
        prefix = upstream_branch + "."
        return any(eb.startswith(prefix) for eb in existing_stable_branch_max)

    for branch, av in upstream_stable_newest.items():
        if _covered(branch):
            continue  # bump path handles it, or finer branch already covers
        if parse_version(av["version"]) <= highest_existing_stable:
            continue  # older than our newest — don't backfill history
        new_entries.append({
            "version": av["version"],
            "prerelease": False,
            "branch": branch,
        })
        seen_new_branches.add(branch)

    # (2) Pre-releases — only if not already superseded by any stable
    # (existing OR upstream) and not already represented in our YAML.
    for av in all_versions:
        if not av["prerelease"]:
            continue
        if av["version"] in existing_versions:
            continue
        b = get_branch(_release_form(av["version"]))
        if b in existing_pre_branches:
            continue  # one pre per branch is enough
        if b in seen_new_branches:
            continue  # we just added a stable for this branch — don't also add a pre
        pv = parse_version(av["version"])
        if pv <= existing_stable_branch_max.get(b, ()):
            continue
        upstream_cur = upstream_stable_newest.get(b)
        if upstream_cur is not None and pv <= parse_version(upstream_cur["version"]):
            continue  # upstream has shipped a stable >= this pre
        new_entries.append({
            "version": av["version"],
            "prerelease": True,
            "branch": b,
        })
        seen_new_branches.add(b)

    return new_entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def apply_updates(content: str, updates: list[tuple[str, str]], lib_name: str) -> tuple[str, list[str]]:
    """Apply version updates via string replacement in YAML content.

    Preserves comments and formatting.
    Returns (new_content, list_of_change_descriptions).
    """
    changes = []
    for old_ver, new_ver in updates:
        # 1. Replace version: field
        for old_p, new_p in [
            (f'version: "{old_ver}"', f'version: "{new_ver}"'),
            (f'version: {old_ver}',   f'version: {new_ver}'),
        ]:
            if old_p in content:
                content = content.replace(old_p, new_p, 1)
                break

        # 2. Update source: URLs that bake the version inline. Some
        #    upstreams (curl, OpenSSL) put the version into the
        #    GitHub release tag with underscores (e.g. ``curl-8_19_0``);
        #    if we only bumped ``version:`` the next build would 404.
        #    Bump both forms in any source: line that follows the
        #    version: line up to the next variant boundary.
        old_under = old_ver.replace(".", "_")
        new_under = new_ver.replace(".", "_")
        # Limit substitution to the variant block we just touched —
        # search forward from the just-replaced version: marker until
        # the next ``- version:`` or library block start.
        version_marker = f'version: "{new_ver}"' if f'version: "{new_ver}"' in content else f'version: {new_ver}'
        block_start = content.find(version_marker)
        if block_start >= 0:
            block_end = content.find("- version:", block_start + len(version_marker))
            if block_end < 0:
                block_end = content.find("\n  - name:", block_start)
            if block_end < 0:
                block_end = len(content)
            block = content[block_start:block_end]
            new_block = block
            new_block = new_block.replace(old_ver, new_ver)
            if old_under != old_ver:
                new_block = new_block.replace(old_under, new_under)
            if new_block != block:
                content = content[:block_start] + new_block + content[block_end:]

        # 3. Update label field — search for both the exact version string and the
        #    space-normalised form (labels may use "4.0.0 beta1" for "4.0.0-beta1").
        old_forms = [re.escape(old_ver)]
        old_space = old_ver.replace("-", " ")
        if old_space != old_ver:
            old_forms.append(re.escape(old_space))

        if is_prerelease(old_ver) and not is_prerelease(new_ver):
            # Pre-release → release: replace the whole label with "X.Y.x"
            # (strips "(experimental)" and the old pre-release tag)
            branch = get_branch(new_ver)
            new_label = f"{branch}.x"
            for old_form in old_forms:
                replaced = re.sub(
                    r'(label:\s*["\']?)[^"\'\n]*' + old_form + r'[^"\'\n]*(["\']?)',
                    r'\g<1>' + new_label + r'\g<2>',
                    content, count=1,
                )
                if replaced != content:
                    content = replaced
                    break
        else:
            # Same release type (pre→pre or release→release): update version in label.
            # Write the new version in space-normalised form for readability.
            new_in_label = new_ver.replace("-", " ")
            for old_form in old_forms:
                replaced = re.sub(
                    r'(label:\s*["\']?)([^"\'\n]*)' + old_form + r'([^"\'\n]*["\']?)',
                    r'\g<1>\g<2>' + new_in_label + r'\g<3>',
                    content, count=1,
                )
                if replaced != content:
                    content = replaced
                    break

        changes.append(f"{lib_name}: {old_ver} → {new_ver}")

    return content, changes


def apply_removals(content: str, removals: list[str], lib_name: str) -> tuple[str, list[str]]:
    """Strip variant blocks for each version in ``removals``."""
    changes = []
    for ver in removals:
        marker = f'- version: "{ver}"'
        marker_pos = content.find(marker)
        if marker_pos < 0:
            marker = f'- version: {ver}'
            marker_pos = content.find(marker)
        if marker_pos < 0:
            continue
        line_start = content.rfind("\n", 0, marker_pos) + 1
        next_var = content.find("\n      - version:", marker_pos)
        next_lib = content.find("\n  - name:", marker_pos)
        end = len(content)
        for cand in (next_var, next_lib):
            if 0 <= cand < end:
                end = cand + 1
        content = content[:line_start] + content[end:]
        changes.append(f"{lib_name}: REMOVE {ver} (superseded by stable)")
    return content, changes


def apply_new_versions(content: str, new_entries: list[dict], lib: dict) -> tuple[str, list[str]]:
    """Insert new variants into YAML content.

    Each entry is a dict ``{"version", "prerelease", "branch"}`` as
    returned by ``find_new_versions``. Pre-releases get an
    "(experimental)" label; stable branches that we didn't cover yet
    get an "X.Y.x (latest)" or "X.Y.x (LTS)" label (LTS detected via
    ``fetch_lts_branches``). When a new (latest) is inserted, any
    existing "(latest)" label in the same library is downgraded to
    "(current)" so only the newest stable carries the top marker.
    ``requires`` is inherited from the closest existing stable variant
    so unrelated anchors like 1.1.0 don't drag their own requirements
    onto a brand-new 4.x branch.

    Insertion is always at the top (first variant position), so a new
    stable branch like 4.1.0 lands above the previously-newest 4.0.x.
    """
    lib_name = lib["name"]
    changes = []

    # Pre-compute "requires:" default — reuse the value from the
    # highest existing stable variant (bitslice anchors ignored).
    inherited_requires = ""
    best_pv = ()
    for v in lib.get("variants", []):
        ver = v.get("version", "")
        if ver in ("auto", "master", "custom"):
            continue
        if re.match(r'^\d+\.\d+\.\d+-\d+$', ver):
            continue
        if is_prerelease(ver):
            continue
        pv = parse_version(ver)
        if pv > best_pv:
            best_pv = pv
            inherited_requires = str(v.get("requires", "") or "")

    lts_branches = fetch_lts_branches(lib_name) if any(
        not e["prerelease"] for e in new_entries) else set()

    # Insert in descending version order so the newest variant ends
    # up topmost. apply_new_versions always inserts at the first
    # variant position; inserting in reverse order means each later
    # insert lands above the previous.
    sorted_entries = sorted(
        new_entries,
        key=lambda e: parse_version(e["version"]),
    )

    for entry in sorted_entries:
        ver = entry["version"]
        is_pre = entry["prerelease"]
        branch = entry.get("branch", "")

        # Build label
        if is_pre:
            label = f'{ver.replace("-", " ")} (experimental)'
            tag = "prerelease"
        else:
            suffix = "LTS" if branch in lts_branches else "latest"
            label = f'{branch}.x ({suffix})'
            tag = suffix.lower()

        # Downgrade any pre-existing "(latest)" label in this library's
        # block before computing insert positions, so the new stable
        # top is the only one carrying it. Scoped to the library block
        # to leave other libraries alone; (LTS) and (current) are
        # untouched.
        if suffix == "latest":
            lib_block_start = content.find(f"name: {lib_name}")
            if lib_block_start != -1:
                next_lib_pos = content.find("\n  - name:", lib_block_start + 1)
                block_end = next_lib_pos if next_lib_pos != -1 else len(content)
                block = content[lib_block_start:block_end]
                relabeled = block.replace(' (latest)"', ' (current)"')
                if relabeled != block:
                    content = content[:lib_block_start] + relabeled + content[block_end:]
                    changes.append(f"{lib_name}: RELABEL previous (latest) -> (current)")

        # Find insertion point: first "- version:" after the library's
        # "variants:" marker. New entry is placed before that line, so
        # it becomes the new topmost variant.
        lib_pattern = f"name: {lib_name}"
        lib_pos = content.find(lib_pattern)
        if lib_pos == -1:
            continue
        variants_pos = content.find("variants:", lib_pos)
        if variants_pos == -1:
            continue
        first_variant_pos = content.find("- version:", variants_pos)
        if first_variant_pos == -1:
            continue
        line_start = content.rfind("\n", 0, first_variant_pos) + 1
        existing_line = content[line_start:content.find("\n", first_variant_pos)]
        indent = existing_line[:len(existing_line) - len(existing_line.lstrip())]

        lines = [
            f'{indent}- version: "{ver}"',
            f'{indent}  label: "{label}"',
        ]
        if inherited_requires:
            lines.append(f'{indent}  requires: "{inherited_requires}"')
        new_entry = "\n".join(lines) + "\n"

        content = content[:line_start] + new_entry + content[line_start:]
        changes.append(f"{lib_name}: NEW {ver} ({tag})")

    return content, changes


def main():
    dry_run = "--dry-run" in sys.argv

    print(f"Loading {LIBRARIES_YAML}")
    content = LIBRARIES_YAML.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    if not data or "libraries" not in data:
        print("ERROR: Invalid libraries.yaml")
        sys.exit(1)

    all_changes = []

    for lib in data["libraries"]:
        name = lib["name"]
        version_check = lib.get("version_check", "")
        if not version_check:
            print(f"  {name}: no version_check, skipping")
            continue

        print(f"  {name}: checking {version_check}...")
        versions = fetch_versions(version_check, name)
        if not versions:
            print(f"  {name}: no versions found")
            continue

        print(f"  {name}: found {len(versions)} releases")

        # Find updates
        updates = find_updates(lib, versions)
        new_vers = find_new_versions(lib, versions, updates=updates)
        obsolete = find_obsolete_prereleases(lib, versions)

        if updates or new_vers or obsolete:
            if not dry_run:
                content, rem_changes = apply_removals(content, obsolete, name)
                content, changes = apply_updates(content, updates, name)
                content, new_changes = apply_new_versions(content, new_vers, lib)
                all_changes.extend(rem_changes + changes + new_changes)
            else:
                for ov in obsolete:
                    print(f"    {name}: REMOVE {ov} (superseded by stable)")
                    all_changes.append(f"{name}: REMOVE {ov} (superseded by stable)")
                for old_v, new_v in updates:
                    print(f"    {name}: {old_v} → {new_v}")
                    all_changes.append(f"{name}: {old_v} → {new_v}")
                for nv in new_vers:
                    kind = "prerelease" if nv["prerelease"] else "stable"
                    print(f"    {name}: NEW {nv['version']} ({kind}, branch {nv['branch']})")
                    all_changes.append(f"{name}: NEW {nv['version']} ({kind})")
        else:
            print(f"    up to date")

    if all_changes:
        if dry_run:
            print(f"\nDRY RUN — {len(all_changes)} changes would be made:")
            for c in all_changes:
                print(f"  {c}")
        else:
            LIBRARIES_YAML.write_text(content, encoding="utf-8")
            print(f"\nUpdated {LIBRARIES_YAML} with {len(all_changes)} changes")
            for c in all_changes:
                print(f"  {c}")
    else:
        print("\nAll libraries up to date")

    return 0


if __name__ == "__main__":
    sys.exit(main())
