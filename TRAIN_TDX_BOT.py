import os
import requests
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# 個人自用設定區
# ==========================================
CLIENT_ID = "wuzhifeng1001123-43893dc2-86ec-44f7"
CLIENT_SECRET = "d3d769d7-020e-4c0d-a54b-410cb134e7c5"

TG_BOT_TOKEN = "8801556108:AAGoDW6LtGxvmvElS0ZBEYHKGU5J_XVbY6Q"
TG_CHAT_ID = "8874687159"

# 抓取天數：60 天
DAYS_AHEAD = 60
EXCLUDE_TRAINS = []


def get_tdx_token(session, client_id, client_secret):
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    headers = {"content-type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    response = session.post(auth_url, headers=headers, data=data, timeout=10)
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        raise Exception(f"TDX Token 取得失敗: {response.text}")


def is_extra_train(train_type, note, train_num):
    if "專開列車" in train_type:
        return False
    if 6000 <= train_num <= 6999:
        return True
    return "民國" in note or "加班" in note or "迴送" in note


def send_telegram_messages(session, bot_token, chat_id, messages):
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
            res = session.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                print("[TG] 訊息發送成功！")
            else:
                print(f"[TG Error] 發送失敗: {res.text}")
        except Exception as e:
            print(f"[TG Exception] {e}")
        time.sleep(0.5)


def format_telegram_report(start_str, end_str, total_found, train_dates):
    """
    將收集到的資料格式化為 Telegram 訊息，列出車次與開行日期。
    """
    messages = []
    
    # 標頭訊息
    header = (
        f"60 天內台鐵加班車彙整\n"
        f"統計區間：`{start_str}` ~ `{end_str}`\n"
        f"總計抓取：*{total_found}* 筆加班車紀錄\n"
        f"------------------------------------"
    )
    
    # 處理順行與逆行
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
        
        # 依車次數字排序
        sorted_train_nos = sorted(data_dict.keys(), key=lambda x: int(x))
        
        for train_no in sorted_train_nos:
            dates = sorted(list(data_dict[train_no]))
            dates_str = ", ".join(dates)
            line = f"• *{train_no}次*：{dates_str}\n"
            
            # 避免超過 Telegram 訊息 4096 字數上限，若太長則分段發送
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
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))

    tz_taipei = timezone(timedelta(hours=8))
    today_taipei = datetime.now(tz_taipei).date()
    end_date_taipei = today_taipei + timedelta(days=DAYS_AHEAD - 1)
    
    start_str = today_taipei.strftime("%Y-%m-%d")
    end_str = end_date_taipei.strftime("%Y-%m-%d")

    # 紀錄每個車次出現的日期：{"shun": {車次: set(日期)}, "ni": {車次: set(日期)}}
    train_dates = {
        "shun": defaultdict(set),
        "ni": defaultdict(set)
    }
    total_found = 0

    print(f"🚀 開始抓取 TDX 60 天加班車資料 ({start_str} ~ {end_str})...")

    try:
        token = get_tdx_token(session, CLIENT_ID, CLIENT_SECRET)
        headers = {"authorization": f"Bearer {token}", "accept": "json"}

        curr_dt = today_taipei
        day_count = 0

        while curr_dt <= end_date_taipei:
            day_count += 1
            date_str = curr_dt.strftime("%m/%d")  # Telegram 顯示用簡短日期 MM/DD
            api_date_str = curr_dt.strftime("%Y-%m-%d")
            
            api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{api_date_str}?$format=JSON"
            
            try:
                response = session.get(api_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    day_found = 0
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
                                total_found += 1
                                day_found += 1
                                
                                dir_key = "shun" if train_num % 2 == 0 else "ni"
                                train_dates[dir_key][train_no].add(date_str)

                    print(f"[{day_count}/{DAYS_AHEAD}] {api_date_str} 完成，抓到 {day_found} 筆加班車")
                else:
                    print(f"[{day_count}/{DAYS_AHEAD}] {api_date_str} 抓取失敗，HTTP 狀態碼: {response.status_code}")
            except Exception as req_err:
                print(f"[{day_count}/{DAYS_AHEAD}] {api_date_str} 發生例外: {req_err}")
            
            curr_dt += timedelta(days=1)
            time.sleep(0.15)  # 防觸發 TDX Rate Limit

        print(f"\n[System] 資料抓取完成，總計 {total_found} 筆，準備發送 Telegram 訊息...")

        # 生成並發送 Telegram 訊息
        tg_messages = format_telegram_report(start_str, end_str, total_found, train_dates)
        send_telegram_messages(session, TG_BOT_TOKEN, TG_CHAT_ID, tg_messages)

    except Exception as e:
        print(f"[Fatal Error] 執行過程發生錯誤: {e}")


if __name__ == "__main__":
    main()
