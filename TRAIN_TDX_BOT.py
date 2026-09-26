import os
import json
import requests
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# 個人自用設定區（金鑰與個人參數直接寫死）
# ==========================================
CLIENT_ID = "wuzhifeng1001123-43893dc2-86ec-44f7"
CLIENT_SECRET = "d3d769d7-020e-4c0d-a54b-410cb134e7c5"

TG_BOT_TOKEN = "8801556108:AAGoDW6LtGxvmvElS0ZBEYHKGU5J_XVbY6Q"
TG_CHAT_ID = "8874687159"
# 你的 GitHub Pages 網址（依你的 GitHub 帳號與 Repo 名稱修改）
PAGES_URL = "https://wuchihfeng.github.io/TDX-Special-Train-Automatic-filtering/"

# 抓取天數：改成 60 天
DAYS_AHEAD = 60

KEY_STATIONS = ["臺北", "板橋", "桃園", "新竹", "苗栗", "臺中", "彰化", "雲林", "嘉義", "臺南", "高雄", "大甲", "花蓮", "臺東", "知本", "屏東", "潮州", "枋寮"]
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


def send_telegram_message(session, bot_token, chat_id, text):
    if not bot_token or "你的_" in bot_token:
        print("[TG Warning] 未設定正確的 Telegram Bot Token，跳過發送。")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        res = session.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("[TG] Telegram 訊息發送成功！")
        else:
            print(f"[TG Error] 發送失敗: {res.text}")
    except Exception as e:
        print(f"[TG Exception] {e}")


def get_line_type(stops):
    station_names = [s.get("StationName", {}).get("Zh_tw", "") for s in stops]
    if any(stn in station_names for stn in ["大甲", "清水", "沙鹿", "龍井", "大肚", "後龍", "通霄", "苑裡"]):
        return "海線"
    if any(stn in station_names for stn in ["豐原", "潭子", "臺中", "大慶", "成功", "三義", "銅鑼"]):
        return "山線"
    if "知本" in station_names:
        return "南迴線"
    if any(stn in station_names for stn in ["花蓮", "臺東", "宜蘭", "羅東"]):
        return "東部幹線"
    if any(stn in station_names for stn in ["屏東", "潮州", "枋寮"]):
        return "屏東線"
    return "西部幹線"


def is_extra_train(train_type, note, train_num):
    if "專開列車" in train_type:
        return False
    if 6000 <= train_num <= 6999:
        return True
    return "民國" in note or "加班" in note or "迴送" in note


def generate_html(categorized_dict, start_str, end_str, total_found):
    json_data = json.dumps(categorized_dict, ensure_ascii=False)
    
    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>臺鐵 60 天加班車查詢系統</title>
    <style>
        :root {{
            --primary: #0056b3;
            --bg: #f4f6f9;
            --card-bg: #ffffff;
            --text: #333333;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 12px;
        }}
        .header {{
            text-align: center;
            background: linear-gradient(135deg, #0056b3, #003366);
            color: white;
            padding: 18px 10px;
            border-radius: 12px;
            margin-bottom: 15px;
            box-shadow: 0 4px 10px rgba(0,0,0,0.12);
        }}
        .header h1 {{ margin: 0 0 6px 0; font-size: 1.4rem; }}
        .header p {{ margin: 0; opacity: 0.9; font-size: 0.85rem; }}
        .tab-buttons {{
            display: flex;
            gap: 8px;
            margin-bottom: 15px;
        }}
        .tab-btn {{
            flex: 1;
            padding: 10px;
            border: none;
            background: #e2e8f0;
            color: #4a5568;
            font-weight: bold;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.95rem;
            transition: 0.2s;
        }}
        .tab-btn.active {{
            background: var(--primary);
            color: white;
        }}
        .series-card {{
            background: var(--card-bg);
            border-radius: 10px;
            padding: 14px;
            margin-bottom: 12px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.06);
            border-left: 5px solid var(--primary);
        }}
        .series-title {{
            font-size: 1.15rem;
            font-weight: bold;
            color: var(--primary);
            margin-bottom: 8px;
            border-bottom: 1px solid #edf2f7;
            padding-bottom: 4px;
        }}
        .train-item {{
            margin-bottom: 10px;
            padding-bottom: 8px;
            border-bottom: 1px dashed #e2e8f0;
        }}
        .train-item:last-child {{ border-bottom: none; margin-bottom: 0; }}
        .train-meta {{
            font-weight: bold;
            margin-bottom: 4px;
            font-size: 0.95rem;
        }}
        .badge {{
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.75rem;
            background: #edf2f7;
            color: #4a5568;
            margin-left: 4px;
        }}
        .stops-container {{
            background: #f8fafc;
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 0.82rem;
            margin-top: 6px;
            line-height: 1.45;
            border: 1px solid #e2e8f0;
        }}
        .key-stops {{
            color: #c53030;
            font-weight: bold;
            margin-bottom: 4px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🚆 臺鐵加班車資訊網 (60 天觀測)</h1>
        <p>統計區間：{start_str} ~ {end_str} （共 {total_found} 筆）</p>
    </div>

    <div class="tab-buttons">
        <button class="tab-btn active" onclick="switchTab('順行（雙數車次）')">順行（雙數）</button>
        <button class="tab-btn" onclick="switchTab('逆行（單數車次）')">逆行（單數）</button>
    </div>

    <div id="content"></div>

    <script>
        const rawData = {json_data};
        let currentTab = '順行（雙數車次）';

        function switchTab(tabName) {{
            currentTab = tabName;
            document.querySelectorAll('.tab-btn').forEach(btn => {{
                btn.classList.toggle('active', btn.innerText.includes(tabName.slice(0, 2)));
            }});
            render();
        }}

        function render() {{
            const contentDiv = document.getElementById('content');
            const data = rawData[currentTab] || {{}};
            let html = '';

            const sortedKeys = Object.keys(data).sort((a, b) => parseInt(a) - parseInt(b));

            if (sortedKeys.length === 0) {{
                contentDiv.innerHTML = '<div class="series-card" style="text-align:center;color:#718096;">此方向無加班車資料</div>';
                return;
            }}

            sortedKeys.forEach(seriesKey => {{
                html += `<div class="series-card"><div class="series-title">【 ${{seriesKey}} 】</div>`;
                
                data[seriesKey].forEach(item => {{
                    let keyStopsText = item.key_stops.map(s => `${{s.name}}: ${{s.time}}`).join(' | ');
                    
                    html += `
                        <div class="train-item">
                            <div class="train-meta">
                                📅 ${{item.date}} | ${{item.start_stn}} ➔ ${{item.end_stn}}
                                <span class="badge">${{item.train_type}}</span>
                                <span class="badge" style="background:#e6fffa;color:#234e52;">${{item.line_type}}</span>
                            </div>
                            ${{item.note_str ? `<div style="font-size:0.82rem;color:#718096;">💬 ${{item.note_str}}</div>` : ''}}
                            <div class="stops-container">
                                ${{keyStopsText ? `<div class="key-stops">📍 定型點時刻：${{keyStopsText}}</div>` : ''}}
                                <div style="color:#4a5568;">🔤 沿途停靠：${{item.all_stops}}</div>
                            </div>
                        </div>
                    `;
                }});

                html += '</div>';
            }});

            contentDiv.innerHTML = html;
        }}

        render();
    </script>
</body>
</html>
"""
    return html_content


def main():
    # 建立具備自動重試功能的 HTTP Session
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))

    tz_taipei = timezone(timedelta(hours=8))
    today_taipei = datetime.now(tz_taipei).date()
    end_date_taipei = today_taipei + timedelta(days=DAYS_AHEAD - 1)
    
    start_str = today_taipei.strftime("%Y-%m-%d")
    end_str = end_date_taipei.strftime("%Y-%m-%d")

    categorized_dict = {
        "順行（雙數車次）": defaultdict(list),
        "逆行（單數車次）": defaultdict(list)
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
            date_str = curr_dt.strftime("%Y-%m-%d")
            api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{date_str}?$format=JSON"
            
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
                                series_key = f"{train_no}次系列"
                                direction_key = "順行（雙數車次）" if train_num % 2 == 0 else "逆行（單數車次）"
                                
                                start_stn = train_info.get("StartingStationName", {}).get("Zh_tw", "")
                                end_stn = train_info.get("EndingStationName", {}).get("Zh_tw", "")
                                stops = item.get("StopTimes", [])
                                line_type = get_line_type(stops)
                                
                                key_stops = []
                                all_stops_list = []
                                for idx, stop in enumerate(stops):
                                    stn_name = stop.get("StationName", {}).get("Zh_tw", "")
                                    dep_time = stop.get("DepartureTime")
                                    arr_time = stop.get("ArrivalTime")
                                    all_stops_list.append(stn_name)
                                    
                                    if idx == 0:
                                        key_stops.append({"name": f"[起] {stn_name}", "time": dep_time})
                                    elif idx == len(stops) - 1:
                                        key_stops.append({"name": f"[終] {stn_name}", "time": arr_time})
                                    elif stn_name in KEY_STATIONS and dep_time:
                                        key_stops.append({"name": stn_name, "time": dep_time})

                                categorized_dict[direction_key][series_key].append({
                                    "date": date_str,
                                    "start_stn": start_stn,
                                    "end_stn": end_stn,
                                    "train_type": train_type,
                                    "line_type": line_type,
                                    "note_str": note,
                                    "key_stops": key_stops,
                                    "all_stops": "、".join(all_stops_list)
                                })
                    print(f"[{day_count}/{DAYS_AHEAD}] {date_str} 完成，抓到 {day_found} 筆加班車")
                else:
                    print(f"[{day_count}/{DAYS_AHEAD}] {date_str} 抓取失敗，HTTP 狀態碼: {response.status_code}")
            except Exception as req_err:
                print(f"[{day_count}/{DAYS_AHEAD}] {date_str} 發生例外: {req_err}")
            
            curr_dt += timedelta(days=1)
            time.sleep(0.15)  # 微幅間隔，防觸發 TDX Rate Limit

        # 1. 寫出網頁檔 index.html
        html_code = generate_html(categorized_dict, start_str, end_str, total_found)
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html_code)
        print("\n[System] index.html 已成功生成！")

        # 2. 發送 Telegram 訊息
        tg_msg = (
            f"🚆 *臺鐵 60 天加班車動態更新*\n"
            f"📅 統計區間：`{start_str}` ~ `{end_str}`\n"
            f"📊 總計抓取：*{total_found}* 筆加班車次\n"
            f"• 順行：{len(categorized_dict['順行（雙數車次）'])} 車次系列\n"
            f"• 逆行：{len(categorized_dict['逆行（單數車次）'])} 車次系列\n\n"
            f"🔗 [點此開啟線上網頁儀表板]({PAGES_URL})"
        )
        send_telegram_message(session, TG_BOT_TOKEN, TG_CHAT_ID, tg_msg)

    except Exception as e:
        print(f"[Fatal Error] 執行過程發生錯誤: {e}")


if __name__ == "__main__":
    main()
