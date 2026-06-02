# Audio Feeds Setup

This repository contains a Python script and a GitHub Actions workflow to generate and serve audio feeds daily.

## Features
- Fetches audio metadata and constructs valid RSS 2.0 XML feeds.
- Supports iTunes namespaces for podcast clients.
- Runs automatically every day at 06:00 UTC via GitHub Actions.
- Generated XML files are automatically committed and pushed to the repository.

## Requirements
- Python 3.11
- `requests` library

## Local Execution
1. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the generator:
   ```bash
   python main.py
   ```
3. The XML feeds will be generated in the root directory (e.g., `feed_1234.xml`).

## Deployment
The feeds are designed to be served publicly. You can connect this repository to Cloudflare Pages (or any static site hosting service) to serve the generated XML files.

1. Create a project in Cloudflare Pages.
2. Connect it to this GitHub repository.
3. Leave the build command empty, and set the publish directory to the repository root.
4. Feeds will be available at `https://[PROJECT_NAME].pages.dev/feed_[ID].xml`.

## Latest Run Log
<!-- RUN_LOG_START -->
Last Run: 2026-06-02 18:05:04 UTC

### Successfully Generated
- [feed_6108.xml](feed_6108.xml)
- [feed_9887.xml](feed_9887.xml)
- [feed_11099.xml](feed_11099.xml)
- [feed_6327.xml](feed_6327.xml)
- [feed_12095.xml](feed_12095.xml)
- [feed_302.xml](feed_302.xml)
- [feed_6056.xml](feed_6056.xml)
- [feed_7791.xml](feed_7791.xml)

<!-- RUN_LOG_END -->
