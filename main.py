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
# 從 GitHub Secrets 讀取設定（支援雙金鑰備援）
# ==========================================
KEY_PAIRS = []

# 第一組金鑰（主 Key）
client_id_1 = os.environ.get("TDX_CLIENT_ID")
client_secret_1 = os.environ.get("TDX_CLIENT_SECRET")
if client_id_1 and client_secret_1:
    KEY_PAIRS.append({"id": client_id_1, "secret": client_secret_1, "name": "主 Key (Key 1)"})

# 第二組金鑰（備援 Key）
client_id_2 = os.environ.get("TDX_CLIENT_ID_2")
client_secret_2 = os.environ.get("TDX_CLIENT_SECRET_2")
if client_id_2 and client_secret_2:
    KEY_PAIRS.append({"id": client_id_2, "secret": client_secret_2, "name": "備用 Key (Key 2)"})

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

DAYS_AHEAD = 60
EXCLUDE_TRAINS = []
STATE_FILE = "last_trains.json"

# 全域變數：管理當前使用的 Key 與 Token
current_key_index = 0
current_token = None


def get_tdx_token(client_id, client_secret):
    """跟 TDX 拿 Token，失敗時回傳 None 不崩潰"""
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    headers = {"content-type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    try:
        response = requests.post(auth_url, headers=headers, data=data, timeout=8)
        if response.status_code == 200:
            return response.json().get("access_token")
        else:
            print(f"[!] Key 驗證失敗 (HTTP {response.status_code}): {response.text}")
            return None
    except Exception as e:
        print(f"[!] 取得 Token 時發生連線錯誤: {e}")
        return None


def refresh_or_switch_token():
    """自動輪詢所有 Key，直到拿到有效 Token 為止"""
    global current_key_index, current_token
    
    total_keys = len(KEY_PAIRS)
    for _ in range(total_keys):
        key_info = KEY_PAIRS[current_key_index]
        print(f"[Key System] 嘗試使用【{key_info['name']}】取得 Token...")
        
        token = get_tdx_token(key_info["id"], key_info["secret"])
        if token:
            current_token = token
            print(f"[Key System] 🎉 【{key_info['name']}】驗證成功，取得 Token！")
            return current_token
        
        # 第一組失敗，自動切換到下一組
        print(f"[Key Warning] 【{key_info['name']}】無效或已被停權，自動切換至下一組金鑰...")
        current_key_index = (current_key_index + 1) % total_keys
        time.sleep(1)

    raise Exception("所有設定的 TDX API 金鑰均無效（Invalid credentials），請檢查 GitHub Secrets 設定！")


def switch_to_next_key():
    """強制切換到下一組 Key 並取得 Token"""
    global current_key_index
    if len(KEY_PAIRS) > 1:
        current_key_index = (current_key_index + 1) % len(KEY_PAIRS)
        print(f"[Key System] 切換至備用金鑰：【{KEY_PAIRS[current_key_index]['name']}】")
    return refresh_or_switch_token()


def is_extra_train(train_type, note, train_num):
    if "專開" in train_type:
        return False
    return "民國" in note or (6000 <= train_num <= 6999)


def fetch_single_day(date_obj):
    global current_token
    date_str = date_obj.strftime("%m/%d")
    api_date_str = date_obj.strftime("%Y-%m-%d")
    api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{api_date_str}?$format=JSON"
    
    results = []
    attempt = 0
    auth_errors_count = 0  # 紀錄權限相關錯誤次數
    
    while True:
        try:
            attempt += 1
            headers = {"authorization": f"Bearer {current_token}", "accept": "json"}
            response = requests.get(api_url, headers=headers, timeout=8)
            
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
                time.sleep(1.0)
                return results
                
            elif response.status_code in [401, 403]:
                # Token 過期或 Key 被停權，啟動備用 Key 切換機制
                print(f"[X] {api_date_str} 遇到 HTTP {response.status_code} (金鑰/Token 失效)，啟動切換備援 Key...")
                switch_to_next_key()
                time.sleep(2.0)

            elif response.status_code in [400, 429]:
                auth_errors_count += 1
                # 如果連續打同一個日期爆 429 超過 3 次，自動換下一張 Key 試試看
                if auth_errors_count >= 3 and len(KEY_PAIRS) > 1:
                    print(f"[-] {api_date_str} 連續 429 限速，切換至備用 Key 繼續戰鬥...")
                    switch_to_next_key()
                    auth_errors_count = 0
                else:
                    print(f"[-] {api_date_str} 觸發限速/異常 (HTTP {response.status_code})，等待 12 秒後重試 (第 {attempt} 次)...")
                time.sleep(12.0)
                
            else:
                print(f"[X] {api_date_str} HTTP {response.status_code}，等待 12 秒後重試...")
                time.sleep(12.0)
                
        except Exception as e:
            print(f"[!] {api_date_str} 發生例外 (第 {attempt} 次): {e}，等待 12 秒後重試...")
            time.sleep(12.0)


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
    if not bot_token:
        print("[TG Warning] 未設定 Telegram Bot Token，跳過發送。")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    for msg in messages:
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        try:
            res = requests.post(url, json=payload, timeout=8)
            if res.status_code == 200:
                print("[TG] 訊息發送成功！")
            else:
                print(f"[TG Error] 發送失敗: {res.text}")
        except Exception as e:
            print(f"[TG Exception] {e}")
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
        # 初始化 Token
        refresh_or_switch_token()

        date_list = [today_taipei + timedelta(days=i) for i in range(DAYS_AHEAD)]

        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_date = {executor.submit(fetch_single_day, d): d for d in date_list}
            for future in as_completed(future_to_date):
                day_results = future.result()
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
