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
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "EarthquakeBot/1.0"},
        )

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()

        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code <= 599

            if not retryable or attempt == max_attempts:
                raise

            wait_time = 2 * attempt
            print(
                f"XML取得失敗（HTTP {error.code}）。"
                f"{wait_time}秒後に再試行します "
                f"({attempt}/{max_attempts})"
            )
            time.sleep(wait_time)

        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt == max_attempts:
                raise

            wait_time = 2 * attempt
            print(
                f"XML取得エラー: {error}。"
                f"{wait_time}秒後に再試行します "
                f"({attempt}/{max_attempts})"
            )
            time.sleep(wait_time)

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
    observation = find_element(
        root, "Body", "Intensity", "Observation"
    )
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
        to_half_width((intensity.text or "").strip())
        if intensity is not None else ""
    )

    observation_stations = []
    seen_station_codes = set()

    if observation is not None:
        for pref in observation:
            if pref.tag.split("}")[-1] != "Pref":
                continue

            for area_element in pref:
                if area_element.tag.split("}")[-1] != "Area":
                    continue

                for city in area_element:
                    if city.tag.split("}")[-1] != "City":
                        continue

                    city_name = get_text(city, "Name")

                    for station in city:
                        if station.tag.split("}")[-1] != "IntensityStation":
                            continue

                        station_name = get_text(station, "Name")
                        station_code = get_text(station, "Code")
                        station_intensity = to_half_width(
                            get_text(station, "Int")
                        )

                        if not station_name or not station_intensity:
                            continue

                        # 観測点コードを使って同一地点の重複を防ぐ。
                        # コードがない場合は市町村名と観測点名を使う。
                        station_key = (
                            station_code
                            or f"{city_name}|{station_name}"
                        )

                        if station_key in seen_station_codes:
                            continue

                        seen_station_codes.add(station_key)

                        observation_stations.append({
                            "name": station_name,
                            "city": city_name,
                            "code": station_code,
                            "intensity": station_intensity,
                        })

    max_intensity_station_count = None

    if max_intensity and observation_stations:
        max_intensity_station_count = sum(
            1
            for station in observation_stations
            if station["intensity"] == max_intensity
        )

    return {
        "origin_time": origin_time,
        "epicenter": to_half_width(epicenter),
        "depth": depth,
        "magnitude": to_half_width(magnitude),
        "max_intensity": max_intensity,
        "observation_stations": observation_stations,
        "max_intensity_station_count": max_intensity_station_count,
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

def get_entry_id(entry):
    """通知履歴にはXMLのURLを優先して使用する。"""
    return entry.get("link") or entry.get("id", "")


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
        "success": False,
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
        result["success"] = True

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

def load_seen_status():

    if not os.path.exists(SEEN_IDS_FILE):

        return {}

    try:

        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as file:

            data = json.load(file)

        # 旧形式のリストを新形式に変換する。

        if isinstance(data, list):

            return {

                item: {"status": "complete"}

                for item in data

                if isinstance(item, str)

            }

        # 新形式の辞書を読み込む。

        if isinstance(data, dict):

            statuses = {}

            for entry_id, record in data.items():

                if (

                    isinstance(entry_id, str)

                    and isinstance(record, dict)

                    and record.get("status") in ("incomplete", "complete")

                ):

                    statuses[entry_id] = {

                        "status": record["status"]

                    }

            return statuses

        print("通知履歴の形式が不正です。空の履歴として扱います。")

        return {}

    except (json.JSONDecodeError, OSError) as error:

        print("通知履歴の読み込みに失敗しました:", error)

        return {}



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



def send_telegram_in_parts(message, max_length=3500):
    """長文をTelegramの文字数制限内に分割して送信する。"""
    parts = [
        message[i:i + max_length]
        for i in range(0, len(message), max_length)
    ]

    total = len(parts)

    for index, part in enumerate(parts, start=1):
        header = (
            "【テスト・架空の地震情報／"
            f"長文分割 {index}/{total}】\n"
        )

        if not send_telegram(header + part):
            return False

        if index < total:
            time.sleep(1.1)

    return True


def run_notification_test():
    """架空の地震情報をTelegramへ送信して動作確認する。"""

    max_intensity_message = """【テスト・架空の地震情報】
【震源・震度情報】
発生時刻：2026年11月15日 12:00
震央地名：架空県沖
震源の深さ：10km
マグニチュード：M7.5
最大震度：7
最大震度を観測した地点数：2地点

【観測点別震度】
架空市架空町：震度7
架空市架空地区：震度7
"""

    no_intensity_message = """【テスト・架空の地震情報】
【震度速報】
発生時刻：2026年11月15日 12:00
震央地名：架空県沖
マグニチュード：M5.0
最大震度：未入電
"""

    long_message = (
        "【テスト・架空の地震情報】\n"
        "これは長文分割を確認するための架空の文章です。\n"
        * 250
    )

    messages = [
        max_intensity_message,
        no_intensity_message,
    ]

    for message in messages:
        if not send_telegram(message):
            print("テスト通知に失敗しました。")
            return 1
        time.sleep(1.1)

    if not send_telegram_in_parts(long_message):
        print("長文テスト通知に失敗しました。")
        return 1

    print("すべてのテスト通知を送信しました。")
    return 0


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
    if not detail.get("success", False):
        lines.append("※詳細情報の取得に失敗したため、一部情報が未取得です")

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
    seen_status = load_seen_status()

    prepared = []

    # 詳細電文から正式な発表時刻を取得してから、時系列順に並べる。
    for entry in entries:
        entry_id = get_entry_id(entry)
        if not entry_id:
            continue

        previous_status = seen_status.get(entry_id, {}).get("status")

        # 完全版を通知済みならスキップする。
        if previous_status == "complete":
            print("完全版を通知済みのためスキップ:", entry_id)
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

        # 不完全通知済みで、今回も取得失敗なら再通知しない。
        if previous_status == "incomplete" and not detail.get("success", False):
            print("詳細情報の再取得に失敗。次回再試行:", entry_id)
            continue

        prepared.append({
            "entry": entry,
            "detail": detail,
            "title": title,
            "category": category,
            "previous_status": previous_status,
        })

    prepared.sort(key=chronological_key)

    print("通知候補件数:", len(prepared))

    # 送信失敗時はそこで止める。
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

        entry_id = get_entry_id(item["entry"])

        if item["detail"].get("success", False):
            seen_status[entry_id] = {"status": "complete"}
        else:
            seen_status[entry_id] = {"status": "incomplete"}

        # 履歴は最新500件を保持する。
        seen_status = dict(list(seen_status.items())[-500:])

        # 1件成功するごとに履歴ファイルへ保存する。
        if not save_seen_ids(seen_status):
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
        error_time = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M:%S")
        error_message = (
            "【気象庁フィード取得エラー】\n"
            f"発生時刻：{error_time}\n"
            f"エラー内容：{type(error).__name__}: {error}"
        )

        print("フィード取得エラー:", error)
        send_telegram(error_message)
        return 1
    entries = all_entries if args.all else filter_earthquakes(all_entries)

    print("フィード取得件数:", len(all_entries))
    print("地震情報候補件数:", len(entries))

    return process_entries(entries)

def run_with_error_notification():
    try:
        return main()

    except Exception as error:
        error_time = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M:%S")

        error_message = (
            "【プログラム実行エラー】\n"
            f"発生時刻：{error_time}\n"
            f"エラー内容：{type(error).__name__}: {error}"
        )

        print("予期しないプログラムエラー:", error)
        send_telegram(error_message)
        return 1

if __name__ == "__main__":
    raise SystemExit(run_with_error_notification())