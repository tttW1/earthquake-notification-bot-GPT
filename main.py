import argparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

try:
    from defusedxml import ElementTree as SafeET
except ImportError:
    SafeET = ET


URL = "https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml"

NS = {
    "atom": "http://www.w3.org/2005/Atom"
}

TIMEOUT = 20

EQ_KEYWORDS = ("地震", "震度", "震源", "津波")


def fetch_xml(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "EarthquakeBot/1.0"}
    )

    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read()


def get_text(element, *path):
    current = element

    for name in path:
        current = next(
            (
                child for child in current
                if child.tag.split("}")[-1] == name
            ),
            None
        )

        if current is None:
            return ""

    return (current.text or "").strip()

def find_element(element, *path):
    current = element

    for name in path:
        current = next(
            (
                child for child in current
                if child.tag.split("}")[-1] == name
            ),
            None
        )

        if current is None:
            return None

    return current

def extract_earthquake_details(root):

    earthquake = find_element(
        root, "Body", "Earthquake"
    )

    intensity = find_element(
        root, "Body", "Intensity",
        "Observation", "MaxInt"
    )

    if earthquake is None:
        earthquake = None

    origin_time = (
        get_text(earthquake, "OriginTime")
        if earthquake is not None else ""
    )

    hypocenter = find_element(
        earthquake, "Hypocenter"
    ) if earthquake is not None else None

    area = find_element(hypocenter, "Area") \
        if hypocenter is not None else None

    epicenter = (
        get_text(area, "Name")
        if area is not None else ""
    )

    magnitude_element = find_element(
        earthquake, "Magnitude"
    ) if earthquake is not None else None

    magnitude = ""

    if magnitude_element is not None:
        magnitude = (
            magnitude_element.get("description")
            or (magnitude_element.text or "").strip()
        )

    max_intensity = (
        (intensity.text or "").strip()
        if intensity is not None else ""
    )

    return {
        "origin_time": origin_time,
        "epicenter": epicenter,
        "magnitude": magnitude,
        "max_intensity": max_intensity
    }

def fetch_feed():
    return fetch_xml(URL)


def parse_entries(xml_data):
    root = SafeET.fromstring(xml_data)

    entries = []

    for entry in root.findall("atom:entry", NS):

        link = entry.find(
            "atom:link[@type='application/xml']", NS
        )

        if link is None:
            link = entry.find("atom:link", NS)

        entries.append({
            "id": entry.findtext(
                "atom:id", default="", namespaces=NS
            ),
            "title": entry.findtext(
                "atom:title", default="", namespaces=NS
            ),
            "updated": entry.findtext(
                "atom:updated", default="", namespaces=NS
            ),
            "link": link.get("href", "") if link is not None else ""
        })

    return entries


def filter_earthquakes(entries):
    return [
        e for e in entries
        if any(k in e["title"] for k in EQ_KEYWORDS)
    ]


def sort_newest_first(entries):
    return sorted(
        entries,
        key=lambda e: e["updated"],
        reverse=True
    )


def classify_information(title):

    if "津波" in title:
        return "津波関連情報"

    if "長周期地震動" in title:
        return "長周期地震動情報"

    if "震度速報" in title:
        return "震度速報"

    if "震源・震度" in title:
        return "震源・震度情報"

    if "震源に関する情報" in title:
        return "震源情報"

    if "地震回数" in title:
        return "地震回数情報"

    if "活動状況" in title:
        return "地震活動状況情報"

    if "地震" in title or "震度" in title:
        return "その他の地震情報"

    return "分類不明"


def fetch_detail(url):

    result = {
        "title": "",
        "origin_time": "",
        "epicenter": "",
        "magnitude": "",
        "max_intensity": ""
    }

    if not url:
        return result

    try:
        xml_data = fetch_xml(url)
        root = SafeET.fromstring(xml_data)

        result["title"] = (
            get_text(root, "Head", "Title")
            or get_text(root, "Control", "Title")
        )

        result.update(
            extract_earthquake_details(root)
        )

    except Exception as e:
        print("詳細情報取得エラー:", e)

    return result


def print_entries(entries):

    for e in entries:

        detail = fetch_detail(e["link"])

        title = detail["title"] or e["title"]

        category = classify_information(title)

        print("-" * 35)
        print("情報名 :", title)
        print("分類   :", category)
        print("更新   :", e["updated"])
        print("発生時刻 :", detail["origin_time"] or "未掲載")
        print("震央地名 :", detail["epicenter"] or "未掲載")
        print("M値     :", detail["magnitude"] or "未掲載")
        print("最大震度 :", detail["max_intensity"] or "未掲載")
        print("ID     :", e["id"])
        print("詳細   :", e["link"])

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=10
    )

    parser.add_argument(
        "--all",
        action="store_true"
    )

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("表示件数は1以上にしてください")

    try:
        xml_data = fetch_feed()
        entries = parse_entries(xml_data)

    except Exception as e:
        print("取得エラー:", e)
        return 1

    total = len(entries)

    if not args.all:
        entries = filter_earthquakes(entries)

    entries = sort_newest_first(entries)
    entries = entries[:args.limit]

    print("取得件数:", total)
    print("表示件数:", len(entries))

    print_entries(entries)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())