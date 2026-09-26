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
# 個人自用設定區（優先讀取 GitHub Secrets 環境變數）
# ==========================================
CLIENT_ID = os.environ.get("TDX_CLIENT_ID", "wuzhifeng1001123-43893dc2-86ec-44f7")
CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET", "d3d769d7-020e-4c0d-a54b-410cb134e7c5")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8801556108:AAGoDW6LtGxvmvElS0ZBEYHKGU5J_XVbY6Q")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "8874687159")

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
    # 最多重試 3 次，避免因為瞬間限速而漏掉資料
    for attempt in range(3):
        try:
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
                            results.append((train_no, train_num, date_str))
                print(f"[V] {api_date_str} 抓取成功，找到 {len(results)} 筆加班車")
                return results # 成功就直接返回
            elif response.status_code == 429:
                print(f"[-] {api_date_str} 觸發限速 (429)，等待重試...")
                time.sleep(1.5 * (attempt + 1))
            else:
                print(f"[X] {api_date_str} HTTP {response.status_code}")
                break
        except Exception as e:
            print(f"[!] {api_date_str} 發生例外 (嘗試 {attempt+1}/3): {e}")
            time.sleep(1)
            
    return results



def send_telegram_messages(bot_token, chat_id, messages):
    if not bot_token or "你的_" in bot_token:
        print("[TG Warning] 未設定正確的 Telegram Bot Token，跳過發送。")
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
        f"🚆 *臺鐵 60 天加班車彙整通報*\n"
        f"📅 統計區間：`{start_str}` ~ `{end_str}`\n"
        f"📊 總計抓取：*{total_found}* 筆加班車紀錄\n"
        f"------------------------------------"
    )
    sections = [
        ("🟢 順行（雙數車次）", "shun"),
        ("🔵 逆行（單數車次）", "ni")
    ]
    current_msg = header + "\n\n"
    
    for title, dir_key in sections:
        data_dict = train_dates[dir_key]
        if not data_dict:
            continue
            
        current_msg += f"*{title}*\n"
        sorted_train_nos = sorted(data_dict.keys(), key=lambda x: int(x))
        
        for train_no in sorted_train_nos:
            dates = sorted(list(data_dict[train_no]))
            dates_str = ", ".join(dates)
            line = f"• *{train_no}次*：{dates_str}\n"
            
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
    tz_taipei = timezone(timedelta(hours=8))
    today_taipei = datetime.now(tz_taipei).date()
    end_date_taipei = today_taipei + timedelta(days=DAYS_AHEAD - 1)
    
    start_str = today_taipei.strftime("%Y-%m-%d")
    end_str = end_date_taipei.strftime("%Y-%m-%d")

    train_dates = {
        "shun": defaultdict(set),
        "ni": defaultdict(set)
    }
    total_found = 0

    print(f"🚀 開始抓取 TDX 60 天加班車資料 ({start_str} ~ {end_str})...")

    try:
        token = get_tdx_token(CLIENT_ID, CLIENT_SECRET)
        print("🔑 成功取得 TDX Token，開始多執行緒抓取...")

        # 建立 60 天的日期列表
        date_list = [today_taipei + timedelta(days=i) for i in range(DAYS_AHEAD)]

        # 將原本的 max_workers=10 改小，減少同時發送的請求數
with ThreadPoolExecutor(max_workers=2) as executor:
    future_to_date = {executor.submit(fetch_single_day, d, token): d for d in date_list}
    for future in as_completed(future_to_date):
        day_results = future.result()
        for train_no, train_num, date_str in day_results:
            total_found += 1
            dir_key = "shun" if train_num % 2 == 0 else "ni"
            train_dates[dir_key][train_no].add(date_str)


        print(f"\n[System] 資料抓取完成，總計 {total_found} 筆，準備發送 Telegram 訊息...")

        tg_messages = format_telegram_report(start_str, end_str, total_found, train_dates)
        send_telegram_messages(TG_BOT_TOKEN, TG_CHAT_ID, tg_messages)

    except Exception as e:
        print(f"[Fatal Error] 執行過程發生錯誤: {e}")


if __name__ == "__main__":
    main()
