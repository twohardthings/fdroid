#!/usr/bin/env python3
"""Downloads and verifies the APKs listed in apps.yml.

Usage: fetch_releases.py [apps.yml]

For each app it takes the latest `keep_releases` GitHub Releases (no drafts or
pre-releases) and, for every asset matching `assets`:
  1. downloads it and checks it against the SHA-256 digest GitHub reports,
  2. checks with aapt2 that the package name is the app's `id`,
  3. checks with apksigner that the APK verifies and is signed by exactly the
     pinned `signer` certificate,
and only then moves it into repo/. It also copies the store listing (fastlane
metadata) of the latest release into metadata/<id>/.

Any failed check stops the script with an error, so the workflow publishes
nothing and the live repo keeps its previous state.

GITHUB_TOKEN, if set, is sent to api.github.com only (for the rate limit),
never to download hosts.
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import yaml

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAX_APK_BYTES = 200 * 1024 * 1024
MAX_LISTING_BYTES = 8 * 1024 * 1024
MAX_RELEASE_PAGES = 10  # of 100 releases each
# Files of a fastlane listing that F-Droid uses; everything else is ignored.
LISTING_FILE = re.compile(
    r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*/"
    r"(title\.txt|short_description\.txt|full_description\.txt"
    r"|changelogs/[0-9]{1,10}\.txt"
    r"|images/(icon|featureGraphic|promoGraphic|tvBanner)\.(png|jpg|jpeg)"
    r"|images/(phoneScreenshots|sevenInchScreenshots|tenInchScreenshots"
    r"|tvScreenshots|wearScreenshots)/[A-Za-z0-9_.-]{1,100}\.(png|jpg|jpeg))$"
)


def fail(message):
    raise SystemExit(f"error: {message}")


def api(path):
    req = urllib.request.Request(API + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def download(url, dest, limit):
    """Downloads url to dest (no credentials) and returns its SHA-256."""
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(url, timeout=300) as response, open(dest, "wb") as f:
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > limit:
                fail(f"{url} is larger than {limit} bytes")
            digest.update(chunk)
            f.write(chunk)
    return digest.hexdigest()


def build_tool(name):
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not home:
        fail("ANDROID_HOME is not set")
    versions = sorted(
        glob.glob(os.path.join(home, "build-tools", "*", name)),
        key=lambda p: [int(x) if x.isdigit() else x
                       for x in re.split(r"[.-]", p.split(os.sep)[-2])],
    )
    if not versions:
        fail(f"{name} not found in {home}/build-tools")
    return versions[-1]


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"{os.path.basename(args[0])} failed on {args[-1]}:\n"
             f"{result.stdout}{result.stderr}")
    return result.stdout


def check_apk(path, app_id, signer):
    badging = run(build_tool("aapt2"), "dump", "badging", path)
    match = re.search(r"^package: name='([^']+)' versionCode='(\d+)'", badging, re.M)
    if not match:
        fail(f"{path}: no package name in aapt2 output")
    if match.group(1) != app_id:
        fail(f"{path}: package {match.group(1)}, expected {app_id}")
    certs = run(build_tool("apksigner"), "verify", "--print-certs", path)
    signers = set(re.findall(r"certificate SHA-256 digest: ([0-9a-f]{64})", certs))
    if signers != {signer}:
        fail(f"{path}: signed by {sorted(signers) or 'nothing'}, expected {signer}")
    return int(match.group(2))


def fetch_app(app, keep, repo_dir, tmp):
    app_id, github, signer = app["id"], app["github"], app["signer"].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", signer):
        fail(f"{app_id}: signer must be 64 lowercase hex characters")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github):
        fail(f"{app_id}: bad github repository {github!r}")
    pattern = re.compile(app["assets"])

    releases = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        batch = api(f"/repos/{github}/releases?per_page=100&page={page}")
        releases += [r for r in batch
                     if not r["draft"] and not r["prerelease"]
                     and any(pattern.search(a["name"]) for a in r["assets"])]
        if len(releases) >= keep or len(batch) < 100:
            break
    else:
        fail(f"{app_id}: only {len(releases)} of {keep} releases found in the "
             f"first {MAX_RELEASE_PAGES * 100}; raise MAX_RELEASE_PAGES")
    releases = releases[:keep]
    if not releases:
        fail(f"{app_id}: no published release with matching assets")

    for release in releases:
        for asset in release["assets"]:
            name = asset["name"]
            if not pattern.search(name):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.apk", name):
                fail(f"{app_id}: unexpected asset name {name!r}")
            expected = (asset.get("digest") or "").removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                fail(f"{app_id} {name}: GitHub reports no SHA-256 digest")
            tmp_path = os.path.join(tmp, name)
            actual = download(asset["browser_download_url"], tmp_path, MAX_APK_BYTES)
            if actual != expected:
                fail(f"{app_id} {name}: SHA-256 {actual}, GitHub reports {expected}")
            version_code = check_apk(tmp_path, app_id, signer)
            # Releases may reuse an asset name (app-release.apk), so the
            # repo file name also has the app id and versionCode.
            dest = os.path.join(repo_dir, f"{app_id}_{version_code}_{name}")
            if os.path.exists(dest):
                fail(f"{app_id} {name}: versionCode {version_code} exists twice")
            shutil.move(tmp_path, dest)
            print(f"{app_id} {release['tag_name']}: {name} "
                  f"(versionCode {version_code}) verified", file=sys.stderr)

    if app.get("fastlane"):
        fetch_listing(app_id, github, releases[0]["tag_name"], app["fastlane"])


def fetch_listing(app_id, github, tag, base):
    base = base.strip("/")
    ref = urllib.parse.quote(tag, safe="")
    tree = api(f"/repos/{github}/git/trees/{ref}?recursive=1")
    if tree.get("truncated"):
        fail(f"{app_id}: repository tree too large to list")
    dest_root = os.path.join("metadata", app_id)
    shutil.rmtree(dest_root, ignore_errors=True)
    count = 0
    for entry in tree["tree"]:
        path = entry["path"]
        if entry["type"] != "blob" or not path.startswith(base + "/"):
            continue
        rel = path[len(base) + 1:]
        if not LISTING_FILE.match(rel):
            continue
        dest = os.path.join(dest_root, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        url = f"{RAW}/{github}/{ref}/{urllib.parse.quote(path)}"
        download(url, dest, MAX_LISTING_BYTES)
        # Git's blob id is sha1("blob <size>\0" + content): check it, so the
        # file is exactly the one in the tagged tree.
        with open(dest, "rb") as f:
            content = f.read()
        blob = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
        if blob != entry["sha"]:
            fail(f"{app_id}: {path} doesn't match the tagged tree")
        count += 1
    print(f"{app_id} {tag}: {count} store listing files", file=sys.stderr)


def main(config_path):
    with open(config_path) as f:
        config = yaml.safe_load(f)
    keep = int(config.get("keep_releases", 3))
    os.makedirs("repo", exist_ok=True)
    ids = [app["id"] for app in config["apps"]]
    if len(ids) != len(set(ids)):
        fail("an app id is listed twice")
    with tempfile.TemporaryDirectory() as tmp:
        for app in config["apps"]:
            if not os.path.isfile(os.path.join("metadata", app["id"] + ".yml")):
                fail(f"metadata/{app['id']}.yml is missing")
            fetch_app(app, keep, "repo", tmp)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "apps.yml")
