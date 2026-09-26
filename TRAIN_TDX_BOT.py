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
PAGES_URL = "https://wuchihfeng.github.io/tdx-train-automatic-filtering/"

# 抓取天數：60 天
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
    <!-- Bootstrap 5 CSS CDN -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {{
            background-color: #f8f9fa;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft JhengHei", sans-serif;
        }}
        .card-header-custom {{
            background-color: #003366;
            color: #ffffff;
        }}
        .badge-direction {{
            font-size: 0.85rem;
        }}
        .badge-shun {{
            background-color: #198754;
        }}
        .badge-ni {{
            background-color: #0d6efd;
        }}
        .stop-badge {{
            margin-right: 4px;
            margin-bottom: 6px;
            display: inline-block;
        }}
        .key-stops-list {{
            background-color: #f1f3f5;
            border-radius: 6px;
            padding: 8px 12px;
            list-style-type: none;
            margin-bottom: 0;
        }}
        .key-stops-list li {{
            padding: 2px 0;
            font-size: 0.88rem;
        }}
    </style>
</head>
<body>

<div class="container my-4">
    <!-- Header 區域 -->
    <div class="text-center p-4 mb-4 rounded-3 text-white" style="background: linear-gradient(135deg, #003366, #0056b3);">
        <h2 class="fw-bold mb-1">🚆 臺鐵加班車資訊網 (60 天觀測)</h2>
        <p class="mb-0 opacity-75">統計區間：{start_str} ~ {end_str} （共計 {total_found} 筆加班車）</p>
    </div>

    <!-- 頁籤按鈕 -->
    <ul class="nav nav-pills nav-justified mb-4" id="pills-tab" role="tablist">
        <li class="nav-item" role="presentation">
            <button class="nav-link active fw-bold fs-5" id="tab-shun" onclick="switchTab('順行（雙數車次）')">🟢 順行（雙數車次）</button>
        </li>
        <li class="nav-item" role="presentation">
            <button class="nav-link fw-bold fs-5" id="tab-ni" onclick="switchTab('逆行（單數車次）')">🔵 逆行（單數車次）</button>
        </li>
    </ul>

    <!-- 主要資料卡片區域 -->
    <div id="content" class="row g-4"></div>
</div>

<script>
    const rawData = {json_data};
    let currentTab = '順行（雙數車次）';

    function switchTab(tabName) {{
        currentTab = tabName;
        document.getElementById('tab-shun').classList.toggle('active', tabName.includes('順行'));
        document.getElementById('tab-ni').classList.toggle('active', tabName.includes('逆行'));
        render();
    }}

    function render() {{
        const contentDiv = document.getElementById('content');
        const data = rawData[currentTab] || {{}};
        let html = '';

        const sortedKeys = Object.keys(data).sort((a, b) => parseInt(a) - parseInt(b));

        if (sortedKeys.length === 0) {{
            contentDiv.innerHTML = '<div class="col-12 text-center text-muted my-5"><h5>此方向當前無加班車資料</h5></div>';
            return;
        }}

        sortedKeys.forEach(seriesKey => {{
            const items = data[seriesKey];
            items.forEach(item => {{
                const isShun = currentTab.includes('順行');
                const stopsArray = item.all_stops ? item.all_stops.split('、') : [];

                // 組合 key stops 列表
                let keyStopsHtml = '';
                if (item.key_stops && item.key_stops.length > 0) {{
                    keyStopsHtml = item.key_stops.map(s => `<li>• ${{s.name}} | 時間: ${{s.time}}</li>`).join('');
                }}

                // 組合完整停靠站標籤
                let stopsBadgesHtml = '';
                stopsArray.forEach((stop, idx) => {{
                    if (idx === 0) {{
                        stopsBadgesHtml += `<span class="badge bg-success stop-badge">${{stop}} (起點)</span>`;
                    }} else if (idx === stopsArray.length - 1) {{
                        stopsBadgesHtml += `<span class="badge bg-danger stop-badge">${{stop}} (終點)</span>`;
                    }} else {{
                        stopsBadgesHtml += `<span class="badge bg-light text-dark border stop-badge">${{stop}}</span>`;
                    }}
                }});

                html += `
                <div class="col-12 col-lg-6">
                    <div class="card h-100 shadow-sm border-0">
                        <!-- 卡片標頭 -->
                        <div class="card-header card-header-custom d-flex justify-content-between align-items-center">
                            <div>
                                <span class="badge badge-direction ${{isShun ? 'badge-shun' : 'badge-ni'}} me-2">
                                    ${{isShun ? '順行' : '逆行'}}
                                </span>
                                <strong class="fs-5">${{seriesKey}}</strong>
                            </div>
                            <span class="badge bg-warning text-dark fs-6">${{item.date}}</span>
                        </div>

                        <div class="card-body">
                            <!-- 基本行駛資訊 -->
                            <div class="mb-3">
                                <p class="mb-1"><strong>📌 營運區間：</strong><span class="text-primary fw-bold">${{item.start_stn}} ➔ ${{item.end_stn}}</span></p>
                                <p class="mb-1"><strong>🚆 車種車型：</strong>${{item.train_type}}</p>
                                ${{item.line_type ? `<p class="mb-1"><strong>🗺️ 幹線路線：</strong><span class="badge bg-secondary">${{item.line_type}}</span></p>` : ''}}
                            </div>

                            <!-- 備註 -->
                            ${{item.note_str ? `
                            <div class="alert alert-info py-2 px-3 mb-3 fs-6" role="alert">
                                <strong>ℹ️ 備註：</strong>${{item.note_str}}
                            </div>` : ''}}

                            <!-- 重點停靠站開車時間 -->
                            ${{keyStopsHtml ? `
                            <div class="mb-3">
                                <h6 class="fw-bold text-secondary mb-2">⏱️ 定型點 / 重點站時刻：</h6>
                                <ul class="key-stops-list">
                                    ${{keyStopsHtml}}
                                </ul>
                            </div>` : ''}}

                            <!-- 完整停靠站點 -->
                            ${{stopsBadgesHtml ? `
                            <div>
                                <h6 class="fw-bold text-secondary mb-2">📍 沿途停靠站（共 ${{stopsArray.length}} 站）：</h6>
                                <div>
                                    ${{stopsBadgesHtml}}
                                </div>
                            </div>` : ''}}
                        </div>
                    </div>
                </div>
                `;
            }});
        }});

        contentDiv.innerHTML = html;
    }}

    render();
</script>
<!-- Bootstrap 5 JS Bundle -->
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/bootstrap.bundle.min.js"></script>
</body>
</html>
"""
    return html_content


def main():
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
            time.sleep(0.15)  # 防觸發 TDX Rate Limit

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
