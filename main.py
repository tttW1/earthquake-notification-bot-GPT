import re
from datetime import datetime, timezone, timedelta
import urllib.parse
import os
import json
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

def to_half_width(text):
    table = str.maketrans(
        "０１２３４５６７８９",
        "0123456789"
    )
    return str(text).translate(table)


def format_time(value):

    if not value:
        return "未掲載"

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone(timedelta(hours=9))
            )

        dt = dt.astimezone(
            timezone(timedelta(hours=9))
        )

        result = (
            f"{dt.year}年{dt.month}月{dt.day}日 "
            f"{dt.hour:02d}:{dt.minute:02d}"
        )

        # 元データに秒が含まれている場合だけ表示
        if re.search(r"T\d{2}:\d{2}:\d{2}", value):
            result += f":{dt.second:02d}"

        return result

    except ValueError:
        return to_half_width(value)

def extract_earthquake_details(root):

    earthquake = find_element(
        root, "Body", "Earthquake"
    )

    intensity = find_element(
        root, "Body", "Intensity",
        "Observation", "MaxInt"
    )

    origin_time = (
        get_text(earthquake, "OriginTime")
        if earthquake is not None else ""
    )

    hypocenter = (
        find_element(earthquake, "Hypocenter")
        if earthquake is not None else None
    )

    area = (
        find_element(hypocenter, "Area")
        if hypocenter is not None else None
    )

    epicenter = (
        get_text(area, "Name")
        if area is not None else ""
    )

    # マグニチュード
    magnitude_element = (
        find_element(earthquake, "Magnitude")
        if earthquake is not None else None
    )

    magnitude = ""

    if magnitude_element is not None:
        magnitude = (
            magnitude_element.get("description")
            or (magnitude_element.text or "").strip()
        )

    # 震源の深さ
    coordinate = (
        find_element(area, "Coordinate")
        if area is not None else None
    )

    depth = ""

    if coordinate is not None:
        description = to_half_width(
            coordinate.get("description", "")
        )

        match = re.search(
            r"深さ\s*([0-9]+)\s*km",
            description
        )

        if match:
            depth = match.group(1) + "km"

        elif "ごく浅い" in description:
            depth = "ごく浅い"

    # 最大震度
    max_intensity = (
        (intensity.text or "").strip()
        if intensity is not None else ""
    )

    return {
        "origin_time": origin_time,
        "epicenter": to_half_width(epicenter),
        "magnitude": to_half_width(magnitude),
        "depth": depth,
        "max_intensity": to_half_width(max_intensity)
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

    seen_ids = load_seen_ids()
    new_seen_ids = list(seen_ids)

    for e in entries:

        entry_id = e["id"]

        if entry_id in seen_ids:
            print("通知済みのためスキップ:", entry_id)
            continue

        detail = fetch_detail(e["link"])

        title = detail["title"] or e["title"]
        category = classify_information(title)

        origin_time = format_time(
            detail["origin_time"]
        )

        updated_time = format_time(
            e["updated"]
        )

        message = (
            f"【地震情報】\n"
            f"情報名：{to_half_width(title)}\n"
            f"分類：{to_half_width(category)}\n"
            f"発生時刻：{origin_time}\n"
            f"震央地名：{to_half_width(detail['epicenter']) or '未掲載'}\n"
            f"震源の深さ：{detail['depth'] or '未掲載'}\n"
            f"マグニチュード：{to_half_width(detail['magnitude']) or '未掲載'}\n"
            f"最大震度：{to_half_width(detail['max_intensity']) or '未掲載'}\n"
            f"更新：{updated_time}\n"
            f"詳細：{e['link']}"
        )

        print("-" * 35)
        print(message)

        if send_telegram(message):
            new_seen_ids.append(entry_id)

    new_seen_ids = list(dict.fromkeys(new_seen_ids))[-500:]

    save_seen_ids(new_seen_ids)

def send_telegram(message):

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegramの設定が見つかりません")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    data = {
        "chat_id": chat_id,
        "text": message
    }

    encoded_data = urllib.parse.urlencode(data).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=encoded_data,
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT
        ) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("ok"):
            print("Telegram送信成功")
            return True

        print("Telegram送信失敗:", result)
        return False

    except Exception as e:
        print("Telegram送信エラー:", e)
        return False


def load_seen_ids():

    filename = "seen_ids.json"

    if not os.path.exists(filename):
        return []

    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)

    except (json.JSONDecodeError, OSError):
        print("通知履歴の読み込みに失敗しました")
        return []

def save_seen_ids(seen_ids):

    filename = "seen_ids.json"

    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(
                seen_ids,
                f,
                ensure_ascii=False,
                indent=2
            )

        print("通知履歴を保存しました")

    except OSError as e:
        print("通知履歴の保存に失敗しました:", e)

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