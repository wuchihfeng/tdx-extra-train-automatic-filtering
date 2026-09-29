import os
import sys
import requests
import time
import json
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# 強制 Log 即時印出，不等待 Buffer
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# 從 GitHub Secrets 讀取設定（支援多金鑰備援與多 Chat ID）
# ==========================================
KEY_PAIRS = []

# 第一組金鑰（主 Key）
client_id_1 = os.environ.get("TDX_CLIENT_ID")
client_secret_1 = os.environ.get("TDX_CLIENT_SECRET")
if client_id_1 and client_secret_1:
    KEY_PAIRS.append({"id": client_id_1.strip(), "secret": client_secret_1.strip(), "name": "主 Key (Key 1)"})

# 第二組金鑰（備援 Key）
client_id_2 = os.environ.get("TDX_CLIENT_ID_2")
client_secret_2 = os.environ.get("TDX_CLIENT_SECRET_2")
if client_id_2 and client_secret_2:
    KEY_PAIRS.append({"id": client_id_2.strip(), "secret": client_secret_2.strip(), "name": "備用 Key (Key 2)"})

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

# 讀取並彙整多個 Telegram Chat ID（支援 TG_CHAT_ID 與 TG_CHAT_ID_2）
tg_ids = []
if os.environ.get("TG_CHAT_ID"):
    tg_ids.append(os.environ.get("TG_CHAT_ID").strip())
if os.environ.get("TG_CHAT_ID_2"):
    tg_ids.append(os.environ.get("TG_CHAT_ID_2").strip())

# 將所有得到的 Chat ID 字串結合
TG_CHAT_ID = ",".join(tg_ids) if tg_ids else None

DAYS_AHEAD = 60
EXCLUDE_TRAINS = []
STATE_FILE = "last_trains.json"

# ==========================================
# 金鑰與 Token 狀態管理
# ==========================================
current_key_index = 0
current_token = None
DEAD_KEYS = set()  # 紀錄驗證失敗、永久無效的金鑰索引


def get_tdx_token(client_id, client_secret):
    """跟 TDX 拿 Token，失敗時回傳 None"""
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    headers = {"content-type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    try:
        response = requests.post(auth_url, headers=headers, data=data, timeout=12)
        if response.status_code == 200:
            return response.json().get("access_token")
        else:
            print(f"[!] 【HTTP {response.status_code}】金鑰驗證失敗: {response.text}")
            return None
    except Exception as e:
        print(f"[!] 取得 Token 時發生連線錯誤: {e}")
        return None


def get_valid_token():
    """
    主備備援邏輯：
    1. 永遠優先驗證並使用 Key 1。
    2. 只有當 Key 1 驗證失敗（非 429，是帳密/權限無效）時，才依序遞補啟用 Key 2。
    3. 若啟用中的 Key 遇到 429，不切換 Key，直接在 API 層級等待 60 秒。
    """
    global current_key_index, current_token

    alive_indices = [i for i in range(len(KEY_PAIRS)) if i not in DEAD_KEYS]

    if not alive_indices:
        print("[Fatal Error] ❌ 所有 TDX 金鑰皆已被標記為無效，請檢查 GitHub Secrets！")
        sys.exit(1)

    # 依序嘗試活著的金鑰（優先 Key 1）
    for idx in alive_indices:
        key_info = KEY_PAIRS[idx]
        print(f"[Key System] 嘗試驗證【{key_info['name']}】...")
        token = get_tdx_token(key_info["id"], key_info["secret"])

        if token:
            current_key_index = idx
            current_token = token
            print(f"[Key System] 🎉 【{key_info['name']}】驗證成功並啟用！")
            return current_token
        else:
            print(f"[Key System] ❌ 【{key_info['name']}】驗證失敗，標記為永久無效！")
            DEAD_KEYS.add(idx)

    print("[Fatal Error] ❌ 無可用金鑰！")
    sys.exit(1)


def is_extra_train(train_type, note, train_num):
    if "專開" in train_type:
        return False
    return "民國" in note or (6000 <= train_num <= 6999)


def fetch_single_day(date_obj):
    global current_token, current_key_index
    date_str = date_obj.strftime("%m/%d")
    api_date_str = date_obj.strftime("%Y-%m-%d")
    api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{api_date_str}?$format=JSON"

    results = []
    attempt = 0

    while True:
        try:
            attempt += 1
            headers = {"authorization": f"Bearer {current_token}", "accept": "json"}
            response = requests.get(api_url, headers=headers, timeout=15)

            if response.status_code == 200:
                data = response.json()
                for item in data.get("TrainTimetables", []):
                    train_info = item.get("TrainInfo", {})
                    train_no = train_info.get("TrainNo")

                    if train_no and train_no.isdigit():
                        train_num = int(train_no)
                        raw_note = train_info.get("Note", "")
                        note = raw_note.get("Zh_tw", "") if isinstance(raw_note, dict) else str(raw_note or "")

                        train_type_dict = train_info.get("TrainTypeName", {})
                        train_type = train_type_dict.get("Zh_tw", "") if isinstance(train_type_dict, dict) else str(train_type_dict)

                        if is_extra_train(train_type, note, train_num) and train_no not in EXCLUDE_TRAINS:
                            status_sign = ""
                            if "行駛" in note:
                                status_sign = "+"
                            elif "停駛" in note:
                                status_sign = "-"

                            stop_times = item.get("StopTimes", [])
                            start_station, end_station = "", ""
                            start_time, end_time = "", ""

                            if stop_times:
                                first_stop = stop_times[0]
                                last_stop = stop_times[-1]

                                s_name = first_stop.get("StationName", {})
                                start_station = s_name.get("Zh_tw", "") if isinstance(s_name, dict) else str(s_name)
                                start_time = first_stop.get("DepartureTime", "")[:5]

                                e_name = last_stop.get("StationName", {})
                                end_station = e_name.get("Zh_tw", "") if isinstance(e_name, dict) else str(e_name)
                                end_time = last_stop.get("ArrivalTime", "")[:5]

                            results.append((train_no, train_num, date_str, start_station, start_time, end_station, end_time, status_sign))

                print(f"[V] {api_date_str} 抓取成功，找到 {len(results)} 筆加班車")
                time.sleep(0.3)
                return results

            elif response.status_code in [401, 403]:
                # Token 過期或權限異常：嘗試重新驗證 Key
                print(f"[X] {api_date_str} 遇到 HTTP {response.status_code} (Token 失效)，嘗試重新驗證 Key...")
                DEAD_KEYS.add(current_key_index)  # 標記當前 Key 失效，強迫切換
                current_token = get_valid_token()
                time.sleep(0.5)

            elif response.status_code == 429:
                # 觸發 429 流量限制
                print(f"[-] {api_date_str} 觸發 429 限速，【{KEY_PAIRS[current_key_index]['name']}】等待 60 秒後重試 (第 {attempt} 次)...")
                time.sleep(60)

            else:
                print(f"[X] {api_date_str} HTTP {response.status_code}，等待 8 秒後重試...")
                time.sleep(8.0)

        except Exception as e:
            print(f"[!] {api_date_str} 發生例外 (第 {attempt} 次): {e}，等待 8 秒後重試...")
            time.sleep(8.0)


def load_previous_trains():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] 讀取上一期 JSON 失敗: {e}")
            return {}
    return {}


def save_current_trains(train_dates):
    serializable = {}
    for dir_key, trains in train_dates.items():
        serializable[dir_key] = {}
        for train_no, info in trains.items():
            serializable[dir_key][train_no] = {
                "dates": list(info["dates"]),
                "route": info["route"]
            }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        print("[System] 成功儲存目前狀態至 last_trains.json")
    except Exception as e:
        print(f"[!] 儲存 JSON 失敗: {e}")


def compare_new_trains(prev_data, current_train_dates):
    if not prev_data:
        return []

    new_items = []
    for dir_key in ["shun", "ni"]:
        current_dict = current_train_dates.get(dir_key, {})
        prev_dict = prev_data.get(dir_key, {})

        for train_no, curr_info in current_dict.items():
            curr_dates = curr_info["dates"]
            route = curr_info["route"]

            if train_no not in prev_dict:
                sorted_dates = ", ".join(sorted(list(curr_dates)))
                new_items.append(f"*{train_no}次* ({route}) [全新車次]：{sorted_dates}")
            else:
                prev_dates = set(prev_dict[train_no].get("dates", []))
                added_dates = curr_dates - prev_dates
                if added_dates:
                    sorted_added = ", ".join(sorted(list(added_dates)))
                    new_items.append(f"*{train_no}次* ({route}) 新增日期：{sorted_added}")

    return new_items


def send_telegram_messages(bot_token, chat_id, messages):
    """支援單個或多個 Telegram Chat ID 的發送機制"""
    if not bot_token:
        print("[TG Warning] 未設定 Telegram Bot Token，跳過發送。")
        return

    # 自動解析傳入的 chat_id（支援以逗號、分號或空格分隔多個 ID）
    if isinstance(chat_id, str):
        chat_ids = [c.strip() for c in chat_id.replace(";", ",").replace(" ", ",").split(",") if c.strip()]
    elif isinstance(chat_id, list):
        chat_ids = chat_id
    else:
        chat_ids = [str(chat_id)] if chat_id else []

    if not chat_ids:
        print("[TG Warning] 未提供有效的 Chat ID，跳過發送。")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # 針對每一個 Chat ID 發送所有訊息區段
    for target_id in chat_ids:
        print(f"[TG] 開始發送訊息至 Chat ID: {target_id}")
        for msg in messages:
            payload = {
                "chat_id": target_id,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }
            try:
                res = requests.post(url, json=payload, timeout=8)
                if res.status_code == 200:
                    print(f"[TG] 訊息發送至 {target_id} 成功！")
                else:
                    print(f"[TG Error] 發送至 {target_id} 失敗: {res.text}")
            except Exception as e:
                print(f"[TG Exception] 發送至 {target_id} 發生錯誤: {e}")
            time.sleep(0.3)


def format_telegram_report(start_str, end_str, total_found, train_dates, new_items):
    messages = []

    header = ""
    if new_items:
        header += "相較前一報，異動了：\n"
        for item in new_items:
            header += f"• {item}\n"
        header += "------------------------------------\n\n"

    header += (
        f"臺鐵 60 天內加班車彙整\n"
        f"統計區間：`{start_str}` ~ `{end_str}`\n"
        f"總計抓取：*{total_found}* 筆加班車紀錄\n"
        f"------------------------------------"
    )

    sections = [
        ("順行（雙數車次）", "shun"),
        ("逆行（單數車次）", "ni")
    ]
    current_msg = header + "\n\n"

    for title, dir_key in sections:
        data_dict = train_dates[dir_key]
        if not data_dict:
            continue

        current_msg += f"*{title}*\n"
        sorted_train_nos = sorted(data_dict.keys(), key=lambda x: int(x))

        for train_no in sorted_train_nos:
            info = data_dict[train_no]
            dates = sorted(list(info["dates"]))
            dates_str = ", ".join(dates)
            route = info["route"]
            line = f"• *{train_no}次* ({route})：{dates_str}\n"

            if len(current_msg) + len(line) > 3800:
                messages.append(current_msg)
                current_msg = f"*{title}（續）*\n" + line
            else:
                current_msg += line
        current_msg += "\n"

    if current_msg.strip():
        messages.append(current_msg)

    return messages


def main():
    missing_vars = []
    if not KEY_PAIRS: missing_vars.append("TDX_CLIENT_ID / TDX_CLIENT_SECRET")
    if not TG_BOT_TOKEN: missing_vars.append("TG_BOT_TOKEN")
    if not TG_CHAT_ID: missing_vars.append("TG_CHAT_ID")

    if missing_vars:
        print(f"[Fatal Error] 缺少必要的環境變數 (GitHub Secrets): {', '.join(missing_vars)}")
        sys.exit(1)

    tz_taipei = timezone(timedelta(hours=8))
    today_taipei = datetime.now(tz_taipei).date()
    end_date_taipei = today_taipei + timedelta(days=DAYS_AHEAD - 1)

    start_str = today_taipei.strftime("%Y-%m-%d")
    end_str = end_date_taipei.strftime("%Y-%m-%d")

    prev_data = load_previous_trains()

    train_dates = {
        "shun": {},
        "ni": {}
    }
    total_found = 0

    print(f"開始抓取 TDX 60 天加班車資料 ({start_str} ~ {end_str})...")
    print(f"檢測到已設定 {len(KEY_PAIRS)} 組 API 金鑰機制。")

    try:
        # 啟動時先取得 Token（預設 Key 1）
        get_valid_token()

        date_list = [today_taipei + timedelta(days=i) for i in range(DAYS_AHEAD)]

        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_date = {executor.submit(fetch_single_day, d): d for d in date_list}
            for future in as_completed(future_to_date):
                day_results = future.result()
                if day_results:
                    for train_no, train_num, date_str, start_station, start_time, end_station, end_time, status_sign in day_results:
                        total_found += 1
                        dir_key = "shun" if train_num % 2 == 0 else "ni"

                        if train_no not in train_dates[dir_key]:
                            start_part = f"{start_station} {start_time}" if start_time else start_station
                            end_part = f"{end_station} {end_time}" if end_time else end_station
                            route_str = f"{start_part} -> {end_part}" if start_station and end_station else "未知區間"

                            train_dates[dir_key][train_no] = {
                                "dates": set(),
                                "route": route_str
                            }

                        date_entry = f"{date_str}{status_sign}"
                        train_dates[dir_key][train_no]["dates"].add(date_entry)

        new_items = compare_new_trains(prev_data, train_dates)
        save_current_trains(train_dates)

        print(f"\n[System] 資料抓取與比對完成，發現 {len(new_items)} 項新增項目。準備發送 Telegram 訊息...")

        tg_messages = format_telegram_report(start_str, end_str, total_found, train_dates, new_items)
        send_telegram_messages(TG_BOT_TOKEN, TG_CHAT_ID, tg_messages)

    except Exception as e:
        print(f"[Fatal Error] 執行過程發生錯誤: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
w
