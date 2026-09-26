import os
import sys
import requests
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# 強制 Log 即時印出，不等待 Buffer
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# 從 GitHub Secrets 讀取設定（強制要求，不提供預設值）
# ==========================================
CLIENT_ID = os.environ.get("TDX_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

DAYS_AHEAD = 60
EXCLUDE_TRAINS = []


def get_tdx_token(client_id, client_secret):
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
            raise Exception(f"TDX Token 取得失敗 HTTP {response.status_code}: {response.text}")
    except Exception as e:
        raise Exception(f"取得 Token 時發生連線錯誤: {e}")


def is_extra_train(train_type, note, train_num):
    if "專開列車" in train_type:
        return False
    if 6000 <= train_num <= 6999:
        return True
    return "民國" in note or "加班" in note or "迴送" in note


def fetch_single_day(date_obj, token):
    date_str = date_obj.strftime("%m/%d")
    api_date_str = date_obj.strftime("%Y-%m-%d")
    api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{api_date_str}?$format=JSON"
    headers = {"authorization": f"Bearer {token}", "accept": "json"}
    
    results = []
    attempt = 0
    
    # 無限迴圈，直到成功取得該日資料為止
    while True:
        try:
            attempt += 1
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
                            # 解析起點站、終點站以及對應的開車/到達時間
                            stop_times = item.get("StopTimes", [])
                            start_station, end_station = "", ""
                            start_time, end_time = "", ""
                            
                            if stop_times:
                                first_stop = stop_times[0]
                                last_stop = stop_times[-1]
                                
                                s_name = first_stop.get("StationName", {})
                                start_station = s_name.get("Zh_tw", "") if isinstance(s_name, dict) else str(s_name)
                                start_time = first_stop.get("DepartureTime", "")[:5] # 取 HH:MM
                                
                                e_name = last_stop.get("StationName", {})
                                end_station = e_name.get("Zh_tw", "") if isinstance(e_name, dict) else str(e_name)
                                end_time = last_stop.get("ArrivalTime", "")[:5] # 取 HH:MM
                            
                            results.append((train_no, train_num, date_str, start_station, start_time, end_station, end_time))
                            
                print(f"[V] {api_date_str} 抓取成功，找到 {len(results)} 筆加班車")
                time.sleep(1.0)  # 成功後乖乖休息 1 秒，維護禮貌
                return results
                
            elif response.status_code == 429:
                print(f"[-] {api_date_str} 觸發限速 (429)，無限重試中 (第 {attempt} 次)...")
                time.sleep(3.0)  # 遇到限速時稍微多等 3 秒再挑戰
            else:
                print(f"[X] {api_date_str} HTTP {response.status_code}，重試中...")
                time.sleep(3.0)
                
        except Exception as e:
            print(f"[!] {api_date_str} 發生例外 (第 {attempt} 次): {e}，重試中...")
            time.sleep(3.0)


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


def format_telegram_report(start_str, end_str, total_found, train_dates):
    messages = []
    header = (
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
    # 檢查必要環境變數是否齊全
    missing_vars = []
    if not CLIENT_ID: missing_vars.append("TDX_CLIENT_ID")
    if not CLIENT_SECRET: missing_vars.append("TDX_CLIENT_SECRET")
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

    train_dates = {
        "shun": {},
        "ni": {}
    }
    total_found = 0

    print(f"🚀 開始抓取 TDX 60 天加班車資料 ({start_str} ~ {end_str})...")

    try:
        token = get_tdx_token(CLIENT_ID, CLIENT_SECRET)
        print("🔑 成功取得 TDX Token，開始多執行緒抓取...")

        # 建立 60 天的日期列表
        date_list = [today_taipei + timedelta(days=i) for i in range(DAYS_AHEAD)]

        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_date = {executor.submit(fetch_single_day, d, token): d for d in date_list}
            for future in as_completed(future_to_date):
                day_results = future.result()
                for train_no, train_num, date_str, start_station, start_time, end_station, end_time in day_results:
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
                    train_dates[dir_key][train_no]["dates"].add(date_str)

        print(f"\n[System] 資料抓取完成，總計 {total_found} 筆，準備發送 Telegram 訊息...")

        tg_messages = format_telegram_report(start_str, end_str, total_found, train_dates)
        send_telegram_messages(TG_BOT_TOKEN, TG_CHAT_ID, tg_messages)

    except Exception as e:
        print(f"[Fatal Error] 執行過程發生錯誤: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
