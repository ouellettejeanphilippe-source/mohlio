#!/usr/bin/env python3
"""Download GTA V radio station audio from YouTube and upload to GitHub Releases.

This is a one-time setup script.  It downloads each station's audio as MP3
using yt-dlp, creates a GitHub Release (if needed), and uploads each file as
a release asset.  The permanent download URLs are what the podcast feed
references in its enclosures.

Requirements:
    pip install yt-dlp
    gh auth login          # GitHub CLI, authenticated

Usage:
    python youtube_setup.py                 # all stations
    python youtube_setup.py --station ls-rock channel-x   # specific ones
    python youtube_setup.py --dry-run       # see what would happen
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile

LOG = logging.getLogger("youtube_setup")

REPO = "ouellettejeanphilippe-source/mohlio"
TAG = "youtube-audio"
RELEASE_NAME = "YouTube audio files for podcast feeds"

# Imported from main.py so the station list stays in one place.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import YOUTUBE_SHOWS, YouTubeStation  # noqa: E402


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    LOG.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ensure_release() -> None:
    """Create the release if it does not exist yet."""
    result = gh("release", "view", TAG, "-R", REPO, check=False)
    if result.returncode == 0:
        LOG.info("Release %s already exists.", TAG)
        return
    LOG.info("Creating release %s...", TAG)
    gh(
        "release", "create", TAG,
        "-R", REPO,
        "--title", RELEASE_NAME,
        "--notes", "Audio files extracted from YouTube for the GTA V Radio podcast feed.",
    )
    LOG.info("Release created.")


def existing_assets() -> set[str]:
    """Return the set of asset filenames already in the release."""
    result = gh(
        "release", "view", TAG,
        "-R", REPO,
        "--json", "assets",
        check=False,
    )
    if result.returncode != 0:
        return set()
    data = json.loads(result.stdout)
    return {asset["name"] for asset in data.get("assets", [])}


def download_audio(station: YouTubeStation, out_dir: str) -> str:
    """Download the audio of a YouTube video as MP3.  Returns the file path."""
    output_path = os.path.join(out_dir, f"{station.slug}.mp3")
    url = f"https://www.youtube.com/watch?v={station.video_id}"
    LOG.info("[%s] Downloading %s ...", station.slug, url)
    subprocess.run(
        [
            "yt-dlp",
            "-x", "--audio-format", "mp3",
            "--audio-quality", "3",  # ~128-160 kbps, good balance of size/quality
            "-o", output_path,
            "--no-playlist",
            "--retries", "3",
            url,
        ],
        check=True,
    )
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    LOG.info("[%s] Downloaded: %.1f MB", station.slug, size_mb)
    return output_path


def upload_asset(filepath: str) -> None:
    """Upload a file as a release asset."""
    filename = os.path.basename(filepath)
    LOG.info("Uploading %s ...", filename)
    gh(
        "release", "upload", TAG,
        filepath,
        "-R", REPO,
        "--clobber",
    )
    LOG.info("Uploaded %s.", filename)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--station",
        nargs="*",
        default=[],
        help="station slugs to process (default: all)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    all_stations: list[YouTubeStation] = []
    for show in YOUTUBE_SHOWS:
        all_stations.extend(show.stations)

    if args.station:
        by_slug = {s.slug: s for s in all_stations}
        stations = []
        for slug in args.station:
            if slug not in by_slug:
                LOG.error("Unknown station: %s", slug)
                return 1
            stations.append(by_slug[slug])
    else:
        stations = all_stations

    LOG.info("%d station(s) to process.", len(stations))

    if args.dry_run:
        for s in stations:
            LOG.info(
                "  [%s] %s (%d:%02d) — %s",
                s.slug, s.title, s.duration // 60, s.duration % 60,
                s.asset_url,
            )
        return 0

    ensure_release()
    already = existing_assets()
    LOG.info("%d asset(s) already in release.", len(already))

    with tempfile.TemporaryDirectory(prefix="mohlio-yt-") as tmp:
        for station in stations:
            filename = f"{station.slug}.mp3"
            if filename in already:
                LOG.info("[%s] Already uploaded, skipping.", station.slug)
                continue
            try:
                filepath = download_audio(station, tmp)
                upload_asset(filepath)
                os.unlink(filepath)
            except (subprocess.CalledProcessError, OSError) as exc:
                LOG.error("[%s] Failed: %s", station.slug, exc)
                continue

    LOG.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
