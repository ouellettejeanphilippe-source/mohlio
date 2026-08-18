import requests
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Create a session with retry for more robust requests
session = requests.Session()
retry = Retry(connect=3, backoff_factor=0.5)
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)


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
    7791,  # Pouvez-vous répéter la question?
    6104,  # Tellement hockey
    13061  # Changement de ligne
]

SHOW_TITLES = {
    6108: "Ça s'explique",
    9887: "La journée (est encore jeune)",
    11099: "Décrypteurs : le balado",
    6327: "Le bêtisier",
    12095: "Olivier Niquet 24/7 (en jaquette)",
    302: "À la une",
    6056: "Moteur de recherche",
    7791: "Pouvez-vous répéter la question?",
    6104: "Tellement hockey",
    13061: "Changement de ligne"
}

SHOW_SHORT_NAMES = {
    6108: "explique",
    9887: "journee",
    11099: "decrypteurs",
    6327: "betisier",
    12095: "niquet",
    302: "une",
    6056: "recherche",
    7791: "question",
    6104: "hockey",
    13061: "changement"
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
        response = session.post(GRAPHQL_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "data" in data and "show" in data["data"] and data["data"]["show"]:
                image_url = data["data"]["show"].get("image", {}).get("url")
                if image_url:
                    return image_url
    except Exception:
        pass

    time.sleep(0.3)

    # Try getting the canonical URL first, which is needed to load the correct HTML on Ohdio
    canonical_url = None
    query_prog = """
    query GetProgramme($params: ProgrammeByIdInput!) {
      programmeById(params: $params) {
        ... on EmissionBalado {
          canonicalUrl
        }
        ... on EmissionPremiere {
          canonicalUrl
        }
        ... on EmissionMusique {
          canonicalUrl
        }
        ... on EmissionGrandesSeries {
          canonicalUrl
        }
      }
    }
    """
    variables_prog = {"params": {"id": show_id, "forceWithoutCueSheet": False}}
    try:
        resp_prog = session.post(GRAPHQL_URL, json={"query": query_prog, "variables": variables_prog}, headers=HEADERS, timeout=10)
        if resp_prog.status_code == 200:
            data_prog = resp_prog.json()
            canonical_url = data_prog.get('data', {}).get('programmeById', {}).get('canonicalUrl')
    except Exception:
        pass

    # Fallback to base url if canonicalUrl not found
    url = f"https://ici.radio-canada.ca/ohdio/balados/{show_id}"
    if canonical_url:
        # Use the ohdio root + canonicalUrl, requests will follow the 301/302 redirect
        url = f"https://ici.radio-canada.ca/ohdio{canonical_url}"

    try:
        response = session.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            # 1. Try og:image first
            match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', response.text)
            if not match:
                match = re.search(r'content="([^"]+)"\s+property="og:image"', response.text)
            og_image = match.group(1) if match else None

            # 2. Extract from page HTML (excluding fallbacks)
            imgs = re.findall(r'https://images\.radio-canada\.ca[^"\']+', response.text)
            clean_imgs = []
            for img in imgs:
                # filter out curly brackets (from templates like {ratio}) and generic fallbacks
                if '{' not in img and '\\' not in img and 'fallback' not in img and 'erreur' not in img and 'molecule' not in img and 'tuile-rechercher' not in img and 'balado' in img:
                    # Clean up any trailing HTML parts if regex captured too much
                    img_clean = img.split('>')[0].split('<')[0]
                    clean_imgs.append(img_clean)

            # Prefer 1x1 image, ideally 600w or 300w
            for img in clean_imgs:
                if '1x1' in img:
                    return img

            # If no 1x1 image is found, but we have an og_image, use that
            if og_image:
                return og_image

            # Fallback to first available clean image if 1x1 and og_image not found
            if clean_imgs:
                return clean_imgs[0]
    except Exception:
        pass

    return ""


import json
from email.utils import format_datetime
from datetime import datetime

def fetch_aac_url_from_media_id(media_id):
    validation_url = f"https://services.radio-canada.ca/media/validation/v2/?appCode=medianet&deviceType=ipad&connectionType=wifi&idMedia={media_id}&output=json"
    try:
        resp = session.get(validation_url, headers=HEADERS, timeout=10)
        if resp.status_code == 429:
            print(f"429 Too Many Requests for media {media_id}. Sleeping 2 seconds.")
            time.sleep(5)
            resp = session.get(validation_url, headers=HEADERS, timeout=10)

        try:
            data = resp.json()
        except ValueError:
            print(f"Invalid JSON for media {media_id}. Status: {resp.status_code}")
            return None, 0
        m3u8_url = data.get("url")
        if not m3u8_url:
            return None, 0

        return m3u8_url, 0
    except Exception as e:
        print(f"Error fetching media {media_id}: {e}")
    return None, 0

def fetch_all_media_from_page(show_id):
    """
    Finds all media ids on the page by recursively searching window._rcState_
    """
    items = []

    # First find canonical URL
    query = """
    query GetProgramme($params: ProgrammeByIdInput!) {
      programmeById(params: $params) {
        ... on EmissionBalado {
          id
          canonicalUrl
        }
        ... on EmissionPremiere {
          id
          canonicalUrl
        }
        ... on EmissionMusique {
          id
          canonicalUrl
        }
        ... on EmissionGrandesSeries {
          id
          canonicalUrl
        }
      }
    }
    """
    variables = {"params": {"id": show_id, "forceWithoutCueSheet": False}}
    try:
        resp = session.post(GRAPHQL_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=10)
        data = resp.json()
        canonical_url = data.get('data', {}).get('programmeById', {}).get('canonicalUrl')

        if canonical_url:
            # Use the ohdio root + canonicalUrl, requests will follow the 301/302 redirect
            page_url = f"https://ici.radio-canada.ca/ohdio{canonical_url}"
        else:
            # Fallback to generic URL if canonicalUrl is not available
            page_url = f"https://ici.radio-canada.ca/ohdio/balados/{show_id}"

        page_resp = session.get(page_url, headers=HEADERS, timeout=15)
        m = re.search(r'window\._rcState_\s*=\s*(.*?);</script>', page_resp.text)
        if not m:
            return items, f"[{show_id}] Could not find state data in page HTML."

        import json
        state = json.loads(m.group(1))

        # recursive search for objects with mediaIds
        media_objs = []
        def find_media_ids(obj):
            if isinstance(obj, dict):
                if 'mediaIds' in obj and obj['mediaIds']:
                    media_objs.append(obj)
                for k, v in obj.items():
                    find_media_ids(v)
            elif isinstance(obj, list):
                for v in obj:
                    find_media_ids(v)

        find_media_ids(state)

        for obj in media_objs:
            media_ids = obj.get('mediaIds', [])
            if not media_ids:
                continue

            media_id = media_ids[0]
            time.sleep(1.0)
            aac_url, size = fetch_aac_url_from_media_id(media_id)
            if not aac_url:
                continue

            # Find date
            pub_date_str = obj.get('broadcastedFirstTimeAt') or obj.get('publishedAt') or obj.get('updatedAt')
            rfc2822_date = pub_date_str
            if pub_date_str:
                try:
                    from datetime import datetime
                    from email.utils import format_datetime
                    d = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                    rfc2822_date = format_datetime(d)
                except:
                    pass

            # Find duration
            duration = 0
            if 'duration' in obj:
                d = obj['duration']
                if isinstance(d, dict):
                    duration = d.get('durationInSeconds', 0)
                elif isinstance(d, (int, float)):
                    duration = int(d)

            item = {
                "title": obj.get('title', ''),
                "description": obj.get('summary') or obj.get('description') or '',
                "pubDate": rfc2822_date,
                "itunesDuration": duration,
                "enclosure": {
                    "url": aac_url,
                    "length": size,
                    "type": "audio/mpeg"
                },
                "_media_id": media_id
            }
            items.append(item)

        return items, None
    except Exception as e:
        return items, f"[{show_id}] Fallback error: {e}"

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

    graphql_items = []
    channel_info = {
        "title": SHOW_TITLES.get(show_id, f"Show {show_id}"),
        "description": SHOW_TITLES.get(show_id, ""),
        "image": {"url": ""}
    }

    try:
        response = session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=HEADERS,
            timeout=15
        )

        if response.status_code == 200:
            data = response.json()
            channel = data.get("data", {}).get("podcastByProgrammeId", {}).get("channel")
            if channel:
                channel_info["title"] = channel.get("title") or channel_info["title"]
                channel_info["description"] = channel.get("description") or channel_info["description"]
                if channel.get("image"):
                    channel_info["image"] = channel.get("image")
                if channel.get("items"):
                    graphql_items = channel.get("items")
    except Exception as e:
        print(f"[{show_id}] GraphQL request failed: {e}")
        pass

    # Fetch from page
    page_items, page_err = fetch_all_media_from_page(show_id)
    if page_err:
        print(page_err)

    # Combine items uniquely
    all_items = []
    seen_titles = set()

    def normalize_title(t):
        if not t:
            return ""
        # Remove HTML tags, convert to lowercase, strip whitespace
        t = re.sub(r'<[^>]+>', '', t)
        return t.lower().strip()

    # Process page items first as they are more up to date and might have extra clips
    for item in page_items:
        title = item.get("title", "")
        norm_title = normalize_title(title)

        # Still make sure we have a valid URL
        url = item.get("enclosure", {}).get("url")
        if url and norm_title not in seen_titles:
            seen_titles.add(norm_title)
            all_items.append(item)

    for item in graphql_items:
        title = item.get("title", "")
        norm_title = normalize_title(title)

        url = item.get("enclosure", {}).get("url")
        if url and norm_title not in seen_titles:
            seen_titles.add(norm_title)
            all_items.append(item)

    if not all_items:
        return None, f"[{show_id}] No items found from GraphQL or page."

    channel_info["items"] = all_items
    return channel_info, None

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

    feed_url = f"https://ouellettejeanphilippe-source.github.io/mohlio/feed_{show_id}.xml"

    link = ET.SubElement(channel, "link")
    link.text = feed_url

    # Try channel image, then fallback image
    img_data = channel_data.get("image")
    img_url = ""
    if img_data and img_data.get("url"):
        img_url = img_data["url"]
    elif fallback_image_url:
        img_url = fallback_image_url
    else:
        img_url = "https://example.com/image.jpg"

    if img_url:
        image = ET.SubElement(channel, "image")
        ET.SubElement(image, "url").text = img_url
        ET.SubElement(image, "title").text = title_text
        ET.SubElement(image, "link").text = feed_url

        itunes_image = ET.SubElement(channel, "itunes:image")
        itunes_image.set("href", img_url)


    items = channel_data.get("items", [])

    # Sort items chronologically by pubDate if possible (newest first)
    def get_date(item):
        d_str = item.get("pubDate", "")
        if d_str:
            try:
                # e.g., "Wed, 01 Jan 2025 12:00:00 -0000"
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(d_str)
                return dt.timestamp()
            except:
                pass
        return 0

    items.sort(key=get_date, reverse=True)

    for item_data in items:
        item = ET.SubElement(channel, "item")

        ET.SubElement(item, "title").text = item_data.get("title", "")
        ET.SubElement(item, "description").text = item_data.get("description", "")
        ET.SubElement(item, "pubDate").text = str(item_data.get("pubDate", ""))

        enclosure_data = item_data.get("enclosure")
        if enclosure_data and enclosure_data.get("url"):
            enclosure = ET.SubElement(item, "enclosure")
            enclosure.set("url", enclosure_data["url"])
            enclosure.set("length", "100000000")
            enclosure.set("type", "audio/mpeg")

            guid = ET.SubElement(item, "guid")
            guid.set("isPermaLink", "false")
            guid.text = enclosure_data["url"] + "?v=2"

        itunes_duration = item_data.get("itunesDuration")
        if itunes_duration:
            try:
                seconds = int(itunes_duration)
                h = seconds // 3600
                m = (seconds % 3600) // 60
                s = seconds % 60
                if h > 0:
                    formatted_duration = f"{h:02d}:{m:02d}:{s:02d}"
                else:
                    formatted_duration = f"{m:02d}:{s:02d}"
                ET.SubElement(item, "itunes:duration").text = formatted_duration
            except ValueError:
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
                    # Try to parse the show_id out of the filename (e.g. feed_6104.xml)
                    short_name = success
                    try:
                        m = re.search(r'feed_(\d+)\.xml', success)
                        if m:
                            show_id = int(m.group(1))
                            short_name = SHOW_SHORT_NAMES.get(show_id, success)
                    except Exception:
                        pass

                    full_url = f"https://ouellettejeanphilippe-source.github.io/mohlio/{success}"
                    log_content += f"- [{short_name}]({full_url})\n"
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
