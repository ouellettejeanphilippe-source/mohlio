# Audio Feeds Setup

This repository contains a Python script and a GitHub Actions workflow to generate and serve audio feeds daily.

## Features
- Fetches audio metadata and constructs valid RSS 2.0 XML feeds.
- Supports iTunes namespaces for podcast clients.
- Runs automatically at 8:00 AM, 12:00 PM, and 4:00 PM Eastern Time via GitHub Actions (with daylight saving time support).
- Generated XML files are automatically committed and pushed to the repository.

## Manual Update
To manually trigger an immediate update of the feeds:
1. Go to the **[Daily Feed Update workflow page](https://github.com/ouellettejeanphilippe-source/mohlio/actions/workflows/update.yml)**.
2. Click the **Run workflow** dropdown button on the right side.
3. Click the green **Run workflow** button to start the update.

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
Last Run: 2026-08-19 16:27:38 UTC

### Successfully Generated
- [explique](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6108.xml)
- [journee](https://ouellettejeanphilippe-source.github.io/mohlio/feed_9887.xml)
- [decrypteurs](https://ouellettejeanphilippe-source.github.io/mohlio/feed_11099.xml)
- [betisier](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6327.xml)
- [niquet](https://ouellettejeanphilippe-source.github.io/mohlio/feed_12095.xml)
- [une](https://ouellettejeanphilippe-source.github.io/mohlio/feed_302.xml)
- [recherche](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6056.xml)
- [question](https://ouellettejeanphilippe-source.github.io/mohlio/feed_7791.xml)
- [hockey](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6104.xml)
- [changement](https://ouellettejeanphilippe-source.github.io/mohlio/feed_13061.xml)

<!-- RUN_LOG_END -->
