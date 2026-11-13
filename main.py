import argparse
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    from defusedxml import ElementTree as SafeET
except ImportError:
    SafeET = ET


FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml"
TIMEOUT = 20
SEEN_IDS_FILE = "seen_ids.json"
JST = timezone(timedelta(hours=9))

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# 津波は対象外。地震・震度・震源に関係する電文だけを候補にする。
EARTHQUAKE_KEYWORDS = ("地震", "震度", "震源")


def fetch_xml(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "EarthquakeBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def get_text(element, *path):
    current = element
    if current is None:
        return ""

    for name in path:
        current = next(
            (
                child for child in current
                if child.tag.split("}")[-1] == name
            ),
            None,
        )
        if current is None:
            return ""

    return (current.text or "").strip()


def find_element(element, *path):
    current = element
    if current is None:
        return None

    for name in path:
        current = next(
            (
                child for child in current
                if child.tag.split("}")[-1] == name
            ),
            None,
        )
        if current is None:
            return None

    return current


def to_half_width(value):
    return unicodedata.normalize("NFKC", str(value))


def parse_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt.astimezone(JST)
    except (TypeError, ValueError):
        return None


def format_time(value, include_seconds=False):
    """日付と時刻の間は半角スペース。秒は指定された場合だけ表示。"""
    if not value:
        return ""

    dt = parse_datetime(value)
    if dt is None:
        return to_half_width(value)

    result = (
        f"{dt.year}年{dt.month}月{dt.day}日 "
        f"{dt.hour:02d}:{dt.minute:02d}"
    )

    if include_seconds and re.search(r"T\d{2}:\d{2}:\d{2}", value):
        result += f":{dt.second:02d}"

    return result


def extract_earthquake_details(root):
    earthquake = find_element(root, "Body", "Earthquake")
    intensity = find_element(
        root, "Body", "Intensity", "Observation", "MaxInt"
    )

    origin_time = get_text(earthquake, "OriginTime")

    hypocenter = find_element(earthquake, "Hypocenter")
    area = find_element(hypocenter, "Area")

    epicenter = get_text(area, "Name")

    magnitude_element = find_element(earthquake, "Magnitude")
    magnitude = ""
    if magnitude_element is not None:
        magnitude = (
            magnitude_element.get("description")
            or (magnitude_element.text or "").strip()
        )

    coordinate = find_element(area, "Coordinate")
    depth = ""
    if coordinate is not None:
        description = to_half_width(
            coordinate.get("description", "")
        )
        match = re.search(r"深さ\s*([0-9]+)\s*km", description)
        if match:
            depth = match.group(1) + "km"
        elif "ごく浅い" in description:
            depth = "ごく浅い"

    max_intensity = (
        (intensity.text or "").strip()
        if intensity is not None else ""
    )

    return {
        "origin_time": origin_time,
        "epicenter": to_half_width(epicenter),
        "depth": depth,
        "magnitude": to_half_width(magnitude),
        "max_intensity": to_half_width(max_intensity),
    }


def fetch_feed():
    return fetch_xml(FEED_URL)


def parse_entries(xml_data):
    root = SafeET.fromstring(xml_data)
    entries = []

    for entry in root.findall("atom:entry", ATOM_NS):
        link = entry.find(
            "atom:link[@type='application/xml']", ATOM_NS
        )
        if link is None:
            link = entry.find("atom:link", ATOM_NS)

        entries.append({
            "id": entry.findtext(
                "atom:id", default="", namespaces=ATOM_NS
            ),
            "title": entry.findtext(
                "atom:title", default="", namespaces=ATOM_NS
            ),
            "updated": entry.findtext(
                "atom:updated", default="", namespaces=ATOM_NS
            ),
            "link": link.get("href", "") if link is not None else "",
        })

    return entries


def is_tsunami_information(title):
    return "津波" in (title or "")


def filter_earthquakes(entries):
    filtered = []
    for entry in entries:
        title = entry.get("title", "")
        if is_tsunami_information(title):
            continue
        if any(keyword in title for keyword in EARTHQUAKE_KEYWORDS):
            filtered.append(entry)
    return filtered


def classify_information(title):
    """分類名は指定された5種類の名称をそのまま使用する。"""
    if "津波" in title:
        return None

    if "震度速報" in title:
        return "震度速報"

    if "震源・震度" in title:
        return "震源・震度情報"

    if "震源に関する情報" in title:
        return "震源に関する情報"

    if "長周期地震動に関する観測情報" in title or "長周期地震動" in title:
        return "長周期地震動に関する観測情報"

    if "遠地地震に関する情報" in title or "遠地地震" in title:
        return "遠地地震に関する情報"

    return "その他の地震情報"


def fetch_detail(url):
    result = {
        "title": "",
        "report_time": "",
        "origin_time": "",
        "epicenter": "",
        "depth": "",
        "magnitude": "",
        "max_intensity": "",
    }

    if not url:
        return result

    try:
        root = SafeET.fromstring(fetch_xml(url))

        result["title"] = (
            get_text(root, "Head", "Title")
            or get_text(root, "Control", "Title")
        )
        # 気象庁電文の発表時刻。取得できない場合はAtomのupdatedを使う。
        result["report_time"] = get_text(root, "Head", "ReportDateTime")
        result.update(extract_earthquake_details(root))

    except Exception as error:
        print("詳細情報取得エラー:", error)

    return result


def load_seen_ids():
    if not os.path.exists(SEEN_IDS_FILE):
        return []

    try:
        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            print("通知履歴の形式が不正です。空の履歴として扱います。")
            return []

        return [item for item in data if isinstance(item, str)]

    except (json.JSONDecodeError, OSError) as error:
        print("通知履歴の読み込みに失敗しました:", error)
        return []


def save_seen_ids(seen_ids):
    temporary_file = SEEN_IDS_FILE + ".tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(
                seen_ids,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")

        os.replace(temporary_file, SEEN_IDS_FILE)
        print("通知履歴を保存しました")
        return True

    except OSError as error:
        print("通知履歴の保存に失敗しました:", error)
        try:
            if os.path.exists(temporary_file):
                os.remove(temporary_file)
        except OSError:
            pass
        return False


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegramの設定が見つかりません")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("ok"):
            print("Telegram送信成功")
            return True

        print("Telegram送信失敗:", result)
        return False

    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        print("Telegram送信エラー:", error)
        return False


def build_message(entry, detail, category):
    lines = [f"【{category}】"]

    origin_time = format_time(
        detail.get("origin_time", ""),
        include_seconds=False,
    )
    if origin_time:
        lines.append(f"発生時刻：{origin_time}")

    fields = (
        ("震央地名", detail.get("epicenter", "")),
        ("震源の深さ", detail.get("depth", "")),
        ("マグニチュード", detail.get("magnitude", "")),
        ("最大震度", detail.get("max_intensity", "")),
    )

    for label, value in fields:
        if value:
            lines.append(f"{label}：{to_half_width(value)}")

    report_time_source = (
        detail.get("report_time") or entry.get("updated", "")
    )
    report_time = format_time(
        report_time_source,
        include_seconds=True,
    )
    if report_time:
        lines.append(f"発表時刻：{report_time}")

    if entry.get("link"):
        lines.append(f"詳細：{entry['link']}")

    return "\n".join(lines)


def chronological_key(prepared_entry):
    value = (
        prepared_entry.get("detail", {}).get("report_time")
        or prepared_entry.get("entry", {}).get("updated", "")
    )
    dt = parse_datetime(value)

    if dt is not None:
        return (0, dt.timestamp())

    # 日時を解釈できない場合はAtomのupdated文字列を補助キーにする。
    updated = prepared_entry.get("entry", {}).get("updated", "")
    return (1, updated)


def process_entries(entries):
    seen_ids = load_seen_ids()
    seen_set = set(seen_ids)

    prepared = []

    # 詳細電文から正式な発表時刻を取得してから、時系列順に並べる。
    for entry in entries:
        entry_id = entry.get("id", "")
        if not entry_id or entry_id in seen_set:
            if entry_id in seen_set:
                print("通知済みのためスキップ:", entry_id)
            continue

        if is_tsunami_information(entry.get("title", "")):
            continue

        detail = fetch_detail(entry.get("link", ""))
        title = detail.get("title") or entry.get("title", "")

        if is_tsunami_information(title):
            continue

        category = classify_information(title)
        if category is None:
            continue

        prepared.append({
            "entry": entry,
            "detail": detail,
            "title": title,
            "category": category,
        })

    prepared.sort(key=chronological_key)

    print("通知候補件数:", len(prepared))

    # 送信失敗時はそこで止める。後の時刻の情報を先に通知しないため。
    for index, item in enumerate(prepared):
        message = build_message(
            item["entry"],
            item["detail"],
            item["category"],
        )

        print("-" * 35)
        print(message)

        if not send_telegram(message):
            print("送信に失敗したため、後続の通知を次回実行に持ち越します。")
            return 1

        seen_ids.append(item["entry"]["id"])
        seen_ids = list(dict.fromkeys(seen_ids))[-500:]

        # 1件成功するごとに履歴ファイルへ保存する。
        if not save_seen_ids(seen_ids):
            print("履歴を保存できないため、後続の通知を停止します。")
            return 1

        # Telegramの同一チャットへの連続送信を避ける。
        if index < len(prepared) - 1:
            time.sleep(1.1)

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="気象庁の地震情報をTelegramへ通知します。"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="地震関連以外も含めてフィード項目を処理候補にします（津波は常に除外）。",
    )
    args = parser.parse_args()

    try:
        xml_data = fetch_feed()
        all_entries = parse_entries(xml_data)
    except Exception as error:
        print("フィード取得エラー:", error)
        return 1

    entries = all_entries if args.all else filter_earthquakes(all_entries)

    print("フィード取得件数:", len(all_entries))
    print("地震情報候補件数:", len(entries))

    return process_entries(entries)


if __name__ == "__main__":
    raise SystemExit(main())
