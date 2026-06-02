import requests
import time
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://ici.radio-canada.ca/",
    "Origin": "https://ici.radio-canada.ca/"
}

GRAPHQL_URL = "https://services.radio-canada.ca/bff/audio/graphql"

SHOW_IDS = [
    6108,  # Ça s'explique
    9887,  # La journée (est encore jeune)
    11099, # Décrypteurs : le balado
    6327,  # Le bêtisier
    12095, # Olivier Niquet 24/7 (en jaquette)
    302,   # À la une
    6056,  # Moteur de recherche
    7791   # Pouvez-vous répéter la question?
]

SHOW_TITLES = {
    6108: "Ça s'explique",
    9887: "La journée (est encore jeune)",
    11099: "Décrypteurs : le balado",
    6327: "Le bêtisier",
    12095: "Olivier Niquet 24/7 (en jaquette)",
    302: "À la une",
    6056: "Moteur de recherche",
    7791: "Pouvez-vous répéter la question?"
}

def fetch_show_image(show_id):
    """
    Attempts to fetch the show image from the GraphQL metadata endpoint.
    Falls back to scraping the og:image from the HTML page.
    """
    query = """
    query GetShowImage($globalId: ID!) {
      show(globalId: $globalId) {
        image {
          url
        }
      }
    }
    """
    variables = {"globalId": str(show_id)}

    try:
        response = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "data" in data and "show" in data["data"] and data["data"]["show"]:
                image_url = data["data"]["show"].get("image", {}).get("url")
                if image_url:
                    return image_url
    except Exception:
        pass

    time.sleep(0.3)

    try:
        url = f"https://ici.radio-canada.ca/ohdio/balados/{show_id}"
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', response.text)
            if not match:
                match = re.search(r'content="([^"]+)"\s+property="og:image"', response.text)
            if match:
                return match.group(1)
    except Exception:
        pass

    return ""

def fetch_show_rss_data(show_id):
    query = """
    query GetShowEpisodes($params: PodcastByProgrammeIdInput!) {
      podcastByProgrammeId(params: $params) {
        ... on PodcastRss {
           channel {
             title
             description
             image {
               url
             }
             items {
               title
               description
               pubDate
               enclosure {
                 url
                 length
                 type
               }
               itunesDuration
             }
           }
        }
      }
    }
    """
    variables = {
        "params": {
            "programmeId": show_id,
            "withAds": False
        }
    }

    try:
        response = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=HEADERS,
            timeout=15
        )

        if response.status_code == 403:
            return None, f"[{show_id}] 403 Forbidden. Skipping."

        data = response.json()
        if "errors" in data:
            return None, f"[{show_id}] GraphQL Error (Show might be missing). Skipping."

        channel = data.get("data", {}).get("podcastByProgrammeId", {}).get("channel")
        return channel, None
    except Exception as e:
        return None, f"[{show_id}] Network or unexpected error: {e}. Skipping."

def create_rss_xml(show_id, channel_data, fallback_image_url):
    if not channel_data:
        return

    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    rss.set("xmlns:content", "http://purl.org/rss/1.0/modules/content/")

    channel = ET.SubElement(rss, "channel")

    title = ET.SubElement(channel, "title")
    title_text = channel_data.get("title") or SHOW_TITLES.get(show_id, f"Show {show_id}")
    title.text = title_text

    description = ET.SubElement(channel, "description")
    description.text = channel_data.get("description", title_text)

    link = ET.SubElement(channel, "link")
    link.text = f"https://ici.radio-canada.ca/ohdio/balados/{show_id}"

    # Try channel image, then fallback image
    img_data = channel_data.get("image")
    img_url = ""
    if img_data and img_data.get("url"):
        img_url = img_data["url"]
    elif fallback_image_url:
        img_url = fallback_image_url

    if img_url:
        image = ET.SubElement(channel, "image")
        ET.SubElement(image, "url").text = img_url
        ET.SubElement(image, "title").text = title_text
        ET.SubElement(image, "link").text = f"https://ici.radio-canada.ca/ohdio/balados/{show_id}"

        itunes_image = ET.SubElement(channel, "itunes:image")
        itunes_image.set("href", img_url)

    # Add items
    items = channel_data.get("items", [])
    for item_data in items:
        item = ET.SubElement(channel, "item")

        ET.SubElement(item, "title").text = item_data.get("title", "")
        ET.SubElement(item, "description").text = item_data.get("description", "")
        ET.SubElement(item, "pubDate").text = str(item_data.get("pubDate", ""))

        enclosure_data = item_data.get("enclosure")
        if enclosure_data and enclosure_data.get("url"):
            enclosure = ET.SubElement(item, "enclosure")
            enclosure.set("url", enclosure_data["url"])
            enclosure.set("length", str(enclosure_data.get("length", 0)))
            enclosure.set("type", enclosure_data.get("type", "audio/mpeg"))

            guid = ET.SubElement(item, "guid")
            guid.set("isPermaLink", "false")
            guid.text = enclosure_data["url"]

        itunes_duration = item_data.get("itunesDuration")
        if itunes_duration:
            ET.SubElement(item, "itunes:duration").text = str(itunes_duration)

    rough_string = ET.tostring(rss, "utf-8")
    reparsed = minidom.parseString(rough_string)

    for node in reparsed.getElementsByTagName('*'):
        if node.childNodes and all(c.nodeType == minidom.Node.TEXT_NODE and not c.data.strip() for c in node.childNodes):
            node.childNodes = []

    pretty_xml_as_string = reparsed.toprettyxml(indent="  ")

    filename = f"feed_{show_id}.xml"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(pretty_xml_as_string)

    print(f"[{show_id}] Generated {filename} with {len(items)} episodes.")
    return filename

def update_readme_log(logs):
    readme_path = "README.md"
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()

        start_marker = "<!-- RUN_LOG_START -->"
        end_marker = "<!-- RUN_LOG_END -->"

        if start_marker in content and end_marker in content:
            before = content.split(start_marker)[0]
            after = content.split(end_marker)[1]

            # Using timezone-aware datetime per deprecation warning
            try:
                from datetime import timezone
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except ImportError:
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            log_content = f"\nLast Run: {timestamp}\n\n"

            if logs["success"]:
                log_content += "### Successfully Generated\n"
                for success in logs["success"]:
                    log_content += f"- [{success}]({success})\n"
                log_content += "\n"

            if logs["errors"]:
                log_content += "### Errors\n```\n"
                for err in logs["errors"]:
                    log_content += f"{err}\n"
                log_content += "```\n"

            new_content = f"{before}{start_marker}{log_content}{end_marker}{after}"

            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print("README.md updated with run log.")
    except Exception as e:
        print(f"Failed to update README.md: {e}")

def main():
    print(f"Starting feed generation for {len(SHOW_IDS)} shows...")

    logs = {"success": [], "errors": []}

    for show_id in SHOW_IDS:
        print(f"[{show_id}] Processing {SHOW_TITLES.get(show_id, show_id)}...")

        fallback_image_url = fetch_show_image(show_id)

        # 0.3s delay per constraints
        time.sleep(0.3)

        channel_data, error_msg = fetch_show_rss_data(show_id)

        if channel_data:
            filename = create_rss_xml(show_id, channel_data, fallback_image_url)
            if filename:
                logs["success"].append(filename)
            else:
                msg = f"[{show_id}] Failed to create XML file."
                print(msg)
                logs["errors"].append(msg)
        else:
            msg = error_msg if error_msg else f"[{show_id}] No channel data returned. Show may be deleted/unavailable."
            print(msg)
            logs["errors"].append(msg)

    update_readme_log(logs)

if __name__ == "__main__":
    main()
