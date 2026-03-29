import os, socket, time, threading, requests, json, hmac, hashlib, base64, uuid, calendar, math
from flask import Flask, jsonify, render_template_string, request
from datetime import datetime, timedelta
import pytz
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# --- 設定 ---
IP = os.environ.get("ECHONET_IP", "192.168.0.146")
INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN")
INFLUX_ORG = os.environ.get("INFLUX_ORG")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET")
SB_TOKEN = os.environ.get("SB_TOKEN")
SB_SECRET = os.environ.get("SB_SECRET")

jst = pytz.timezone('Asia/Tokyo')

W_MAP = {
    0: "☀️ 快晴",  1: "🌤️ 晴れ",  2: "⛅ 時々曇",  3: "☁️ 曇り",  45: "🌫️ 霧",  48: "🌫️ 霧",  51: "🚿 霧雨",  53: "🚿 霧雨",  55: "🚿 霧雨",  61: "☔ 雨",  63: "☔ 雨",  65: "☔ 強い雨",  66: "🧊 着氷性の雨", 67: "🧊 強い氷雨",  71: "❄️ 軽めの雪", 73: "❄️ 雪",  75: "❄️ 強い雪",  77: "❄️ 霧雪",  80: "🌦️ にわか雨",  81: "🌦️ にわか雨",  82: "🌦️ 激しいにわか雨",  85: "☃️ 雪（にわか）",  86: "☃️ 激しい雪（にわか）",  95: "⚡ 雷雨",  96: "⛈️ 雷雨（ひょう）",  99: "⛈️ 激しい雷雨（ひょう）"
}

COEFF_MAP = {1: 1.72, 2: 1.72, 3: 1.62, 4: 1.56, 5: 1.50, 6: 1.40, 7: 1.30, 8: 1.30, 9: 1.40, 10: 1.56, 11: 1.65, 12: 1.72}
PANEL_CAPACITY = 5.9

def get_unit_price(dt):
    if dt.hour >= 21 or dt.hour < 7: return 16.60
    if dt.month in [7, 8, 9]: return 33.80
    return 28.60

def fetch_echonet(eoj, epc):
    try:
        frame = bytes([0x10, 0x81, 0x00, 0x01, 0x05, 0xff, 0x01, eoj[0], eoj[1], eoj[2], 0x62, 0x01, epc, 0x00])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("", 3610))
            s.settimeout(0.8)
            s.setblocking(False)
            try:
                while True: s.recvfrom(1024)
            except: pass
            s.setblocking(True)
            s.sendto(frame, (IP, 3610))
            data, _ = s.recvfrom(1024)
            if len(data) < 14: return None
            idx = data.find(bytes([epc]))
            if idx == -1: return None
            length = data[idx+1]
            return data[idx+2 : idx+2+length]
    except: return None

latest_instant = {"solar": 0, "buy": 0, "sell": 0, "home": 0}

def collector():
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    last_valid = {"solar": 0, "buy": 0, "sell": 0}
    MAX_WATT = PANEL_CAPACITY * 1500

    while True:
        try:
            res_s = fetch_echonet([0x02, 0x79, 0x01], 0xE0)
            res_m = fetch_echonet([0x02, 0xA5, 0x01], 0xF5)
            if res_s:
                val_s = int.from_bytes(res_s, "big", signed=True)
                if 0 <= val_s < MAX_WATT:
                    solar = val_s
                    last_valid["solar"] = solar
                else: solar = last_valid["solar"]
            else: solar = last_valid["solar"]

            buy, sell = 0, 0
            if res_m and len(res_m) >= 4:
                val_m = int.from_bytes(res_m[0:4], "big", signed=True)
                if -10000 < val_m < 10000:
                    if val_m >= 0: sell = val_m; buy = 0
                    else: sell = 0; buy = abs(val_m)
                    last_valid["buy"] = buy; last_valid["sell"] = sell
                else: buy, sell = last_valid["buy"], last_valid["sell"]
            else: buy, sell = last_valid["buy"], last_valid["sell"]

            home = max(0, solar + buy - sell)
            latest_instant.update({"solar": solar, "buy": buy, "sell": sell, "home": home})
            p = Point("energy").field("solar", float(solar)).field("buy", float(buy)).field("sell", float(sell)).field("home", float(home))
            write_api.write(bucket=INFLUX_BUCKET, record=p)
        except Exception as e: print(f"Collector Error: {e}")
        time.sleep(5)

app = Flask(__name__)

@app.route("/api/live")
def api_live():
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    now = datetime.now(jst)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    query = f'from(bucket:"{INFLUX_BUCKET}") |> range(start: {start.isoformat()}, stop: {now.isoformat()}) |> filter(fn: (r) => r._measurement == "energy") |> integral(unit: 1h) |> map(fn: (r) => ({{ r with _value: r._value / 1000.0 }})) |> pivot(rowKey:["_start"], columnKey: ["_field"], valueColumn: "_value")'
    sums = {"buy_k": 0.0, "sell_k": 0.0, "solar_k": 0.0, "buy_y": 0, "sell_y": 0}
    try:
        tables = client.query_api().query(query)
        for table in tables:
            for r in table.records:
                bk, sk = r.values.get("buy", 0), r.values.get("sell", 0)
                sums["buy_k"] += bk; sums["sell_k"] += sk; sums["solar_k"] += r.values.get("solar", 0)
                sums["buy_y"] += int(bk * get_unit_price(r.get_start()))
                sums["sell_y"] += int(sk * 16.0)
    except: pass
    res = latest_instant.copy()
    res.update({"d_buy_k": round(sums["buy_k"], 2), "d_buy_y": sums["buy_y"], "d_sell_k": round(sums["sell_k"], 2), "d_sell_y": sums["sell_y"], "d_solar_t": round(sums["solar_k"], 2)})
    return jsonify(res)

@app.route("/api/history")
def api_history():
    u = request.args.get("unit", "day")
    d_str = request.args.get("date") or datetime.now(jst).strftime("%Y-%m-%d")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    if u == "day":
        start = jst.localize(datetime.strptime(d_str, "%Y-%m-%d")); stop = start + timedelta(days=1); window = "1h"
        labels = [f"{i:02d}:00" for i in range(24)]
    elif u == "month":
        y, m, _ = map(int, d_str.split("-")); start = jst.localize(datetime(y, m, 1))
        ld = calendar.monthrange(y, m)[1]; stop = start + timedelta(days=ld); window = "1d"
        labels = [f"{i}日" for i in range(1, ld + 1)]
    else:
        y = int(d_str.split("-")[0]); start = jst.localize(datetime(y, 1, 1)); stop = jst.localize(datetime(y+1, 1, 1)); window = "1mo"
        labels = [f"{i}月" for i in range(1, 13)]
    
    query = f'from(bucket:"{INFLUX_BUCKET}") |> range(start: {start.isoformat()}, stop: {stop.isoformat()}) |> filter(fn: (r) => r._measurement == "energy") |> aggregateWindow(every: {window}, fn: mean, createEmpty: true) |> map(fn: (r) => ({{ r with _value: r._value / 1000.0 }})) |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")'
    res_d = {f: [None]*len(labels) for f in ["buy", "sell", "solar", "home"]}
    buy_yen, sell_yen = [None]*len(labels), [None]*len(labels)
    weather_list, radiation_list, forecast_list = [None]*len(labels), [None]*len(labels), [None]*len(labels)
    try:
        tables = client.query_api().query(query)
        for table in tables:
            for i, r in enumerate(table.records):
                if i >= len(labels): break
                for f in ["buy", "sell", "solar", "home"]:
                    val = r.values.get(f)
                    if val is not None:
                        kwh = val if u == "day" else (val * 24 if u == "month" else val * 24 * 30)
                        res_d[f][i] = round(kwh, 2)
                        if f == "buy": buy_yen[i] = int(kwh * (get_unit_price(r.get_time()) if u == "day" else 28.60))
                        if f == "sell": sell_yen[i] = int(kwh * 16.0)
    except: pass
    try:
        w_url = f"https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&hourly=weather_code,shortwave_radiation&timezone=Asia%2FTokyo&start_date={start.strftime('%Y-%m-%d')}&end_date={(stop-timedelta(seconds=1)).strftime('%Y-%m-%d')}"
        w_res = requests.get(w_url).json()
        if u == "day":
            weather_list = [W_MAP.get(c, "-") for c in w_res['hourly']['weather_code']]
            radiation_list = [round(w/1000.0, 2) for w in w_res['hourly']['shortwave_radiation']]
            forecast_list = [round(min((w / 1000.0) * PANEL_CAPACITY * COEFF_MAP.get(int(d_str.split("-")[1]), 1.5), PANEL_CAPACITY), 2) for w in w_res['hourly']['shortwave_radiation']]
    except: pass
    return jsonify({"labels": labels, "buy": res_d["buy"], "sell": res_d["sell"], "solar": res_d["solar"], "home": res_d["home"], "buy_yen": buy_yen, "sell_yen": sell_yen, "forecast": forecast_list, "weather": weather_list, "radiation": radiation_list})

def sb_headers():
    t, nonce = str(int(time.time()*1000)), str(uuid.uuid4())
    sign = base64.b64encode(hmac.new(bytes(SB_SECRET, 'utf-8'), msg=bytes(f"{SB_TOKEN}{t}{nonce}", 'utf-8'), digestmod=hashlib.sha256).digest())
    return {"Authorization": SB_TOKEN, "t": t, "sign": str(sign, 'utf-8'), "nonce": nonce, "Content-Type": "application/json"}

@app.route("/api/devices")
def get_devices(): 
    devices = requests.get("https://api.switch-bot.com/v1.1/devices", headers=sb_headers()).json().get("body", {})
    # デバイスごとの状態（ON/OFF）を並列取得（簡易的に）
    for d in devices.get("deviceList", []):
        try:
            status = requests.get(f"https://api.switch-bot.com/v1.1/devices/{d['deviceId']}/status", headers=sb_headers()).json().get("body", {})
            d['power'] = status.get('power', 'off')
        except: d['power'] = 'off'
    return jsonify(devices)

@app.route("/api/control", methods=["POST"])
def control():
    data = request.json
    return jsonify(requests.post(f"https://api.switch-bot.com/v1.1/devices/{data['deviceId']}/commands", json=data['payload'], headers=sb_headers()).json())

@app.route("/")
def index():
    return render_template_string("""
    <!DOCTYPE html><html lang="ja-jp"><head><meta charset="UTF-8"><title>HEMS Dashboard Pro</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root { --bg: #0b1120; --card: #1e293b; --text: #f8fafc; --buy: #f97316; --sell: #22c55e; --solar: #3b82f6; --home: #a855f7; }
        body { margin: 0; font-family: sans-serif; display: flex; height: 100vh; background: var(--bg); color: var(--text); overflow: hidden; }
        #left, #right { width: 50%; height: 100%; display: flex; flex-direction: column; border-right: 1px solid #334155; }
        .area-live { height: 290px; position: relative; background: #000; border-bottom: 2px solid #334155; }
        .area-data { flex-grow: 1; display: flex; flex-direction: column; min-height: 0; }
        .data-content { flex-grow: 1; position: relative; overflow: hidden; background: #0f172a; }
        .weather-card { height: 110px; padding: 15px; margin: 10px; border-radius: 12px; background: linear-gradient(135deg, #1e40af, #1e293b); border: 1px solid #3b82f6; }
        .radar-box { height: 700px; margin: 0px 10px 0px 10px; border-radius: 12px; overflow: hidden; border: 1px solid #334155; }
        #sb-wrap { flex-grow: 1; overflow-y: auto; padding: 10px; }
        #clock { position: absolute; top: 10px; left: 15px; color: var(--solar); font-weight: bold; font-family: monospace; cursor: pointer; z-index: 100; }
        .node { position: absolute; width: 80px; height: 80px; border-radius: 50%; border: 3px solid #475569; background: #0f172a; display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 10; font-size: 11px; }
        .node.solar { top: 20px; left: 50%; transform: translateX(-50%); border-color: var(--solar); }
        .node.grid { top: 130px; left: 40px; border-color: var(--buy); transition: border-color 0.5s;}
        .node.home { top: 130px; right: 40px; border-color: var(--home); }
        .solar-total { position: absolute; top: 50px; left: calc(50% + 50px); font-size: 20px; font-weight: bold; color: #fff; }
        .acc-info { position: absolute; bottom: 15px; left: 50%; transform: translateX(-50%); width: 90%; display:flex; justify-content:space-around; background:rgba(15,23,42,0.8); padding:10px; border-radius:8px; border:1px solid #334155; }
        table { width: 100%; border-collapse: collapse; font-size: 11px; }
        th { background: #1e293b; padding: 6px; position: sticky; top: 0; z-index: 5; }
        td { padding: 6px; border-bottom: 1px solid #1e293b; text-align: center; }
        .btn { padding: 6px 10px; border: 1px solid #475569; border-radius: 4px; cursor: pointer; background: #1e293b; color: #fff; }
        .btn.active { background: var(--solar); }
        .sb-card { background: var(--card); padding: 8px 12px; border-radius: 10px; margin-bottom: 6px; border: 1px solid #334155; display: flex; align-items: center; justify-content: space-between; }
        .sb-name { font-size: 13px; font-weight: bold; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
        .sb-ctrl { display: flex; align-items: center; gap: 4px; }
        .ac-select { background: #0f172a; color: #fff; border: 1px solid #475569; border-radius: 4px; font-size: 11px; padding: 2px; }
        .ac-temp { width: 35px; background: #0f172a; color: #fff; border: 1px solid #475569; border-radius: 4px; font-size: 11px; text-align: center; }
        .btn-off { padding: 4px 10px; border-radius: 4px; cursor: pointer; background: #334155; color: #888; border: 1px solid #475569; font-size: 11px; }
        .btn-off.active { background: #f87171; color: #fff; border-color: #f87171; font-weight: bold; }
        .btn-on { padding: 4px 10px; border-radius: 4px; cursor: pointer; background: #334155; color: #888; border: 1px solid #475569; font-size: 11px; }
        .btn-on.active { background: #22c55e; color: #fff; border-color: #22c55e; font-weight: bold; }
        
        /* 修正：viewBoxを外すため、SVGが領域いっぱいに広がるように設定 */
        svg { position: absolute; width: 100%; height: 100%; top: 0; left: 0; pointer-events: none; }
    </style>
    </head>
    <body>
        <div id="left">
            <div class="area-live">
                <div id="clock" onclick="toggleFullscreen()"></div>
                <svg>
                    <path id="p-s2h" stroke="#1e293b" stroke-width="6" fill="none" />
                    <path id="p-s2g" stroke="#1e293b" stroke-width="6" fill="none" />
                    <path id="p-g2h" stroke="#1e293b" stroke-width="6" fill="none" />
                    <circle id="d-s2h" r="4" fill="var(--solar)" style="opacity:0;" />
                    <circle id="d-s2g" r="4" fill="var(--solar)" style="opacity:0;" />
                    <circle id="d-g2h" r="4" fill="var(--buy)" style="opacity:0;" />
                </svg>
                <div class="node solar"><small>発電</small><b id="v-solar" style="font-size:18px;">0</b>W</div>
                <div class="solar-total">本日計: <span id="v-s-t" style="color:var(--solar)">0.0</span>kWh</div>
                <div class="node grid"><small>電力網</small><b id="v-grid" style="font-size:18px;">0</b>W</div>
                <div class="node home"><small>家消費</small><b id="v-home" style="font-size:18px;">0</b>W</div>
                <div class="acc-info">
                    <div style="font-size:20px;">買電: <span id="v-bk" style="color:var(--buy)">0</span>kWh (<span id="v-by">0</span>円)</div>
                    <div style="font-size:20px;">売電: <span id="v-sk" style="color:var(--sell)">0</span>kWh (<span id="v-sy">0</span>円)</div>
                </div>
            </div>
            <div class="area-data">
                <div style="padding:10px; display:flex; gap:10px; background:#1e293b;">
                    <button class="btn active" id="b-chart" onclick="setView('chart')">グラフ</button>
                    <button class="btn" id="b-list" onclick="setView('list')">リスト</button>
                    <select id="sel-u" class="btn" style="margin-left:auto;" onchange="loadData()">
                        <option value="day">日</option><option value="month">月</option><option value="year">年</option>
                    </select>
                    <input type="date" id="sel-d" class="btn" onchange="loadData()">
                </div>
                <div class="data-content">
                    <div id="wrap-chart" style="width:100%; height:100%;"><canvas id="mainChart"></canvas></div>
                    <div id="wrap-list" style="display:none; overflow-y:auto; height:100%;">
                        <table><thead id="table-head"></thead><tbody id="list-body"></tbody></table>
                    </div>
                </div>
            </div>
        </div>
        <div id="right">
            <div class="weather-card" id="w-card">
                <div style="display:flex; justify-content:space-between;">
                    <div id="w-txt" style="font-size:18px; margin-top:5px;"></div>
                    <div style="text-align:right"><span id="w-max" style="font-size:30px; font-weight:bold; color:#f87171;">--</span>° / <span id="w-min" style="font-size:20px; opacity:0.8;">--</span>°</div>
                </div>
                <div id="w-weekly" style="display:flex; justify-content:space-between; margin-top:15px; font-size:15px; text-align:center;"></div>
            </div>
            <div class="radar-box"><iframe src="https://webapp.ydits.net/" width="100%" height="100%" scrolling="NO" frameborder="0"></iframe></div>
            <div id="sb-wrap"></div>
        </div>
    <script>
        let mainChart; const rafs = {};
        const W_EMOJI = {0: "☀️ 快晴", 1: "🌤️ 晴れ", 2: "⛅ 時々曇", 3: "☁️ 曇り", 45: "🌫️ 霧", 61: "☔ 雨", 80: "🌦️ にわか雨", 95: "⚡ 雷雨"};
        let currentLoadedDate = new Date().toLocaleDateString('sv-SE');

        function toggleFullscreen() { if (!document.fullscreenElement) document.documentElement.requestFullscreen(); else document.exitFullscreen(); }
function anim(dotId, pathId, val, color) {
            const dot = document.getElementById(dotId), path = document.getElementById(pathId);
            if (!path || val <= 10) { 
                dot.style.opacity = 0; 
                cancelAnimationFrame(rafs[dotId]);
                delete rafs[dotId + "_start"]; // 状態をクリア
                return; 
            }
            
            dot.style.opacity = 1; 
            dot.setAttribute('fill', color);
            
            const len = path.getTotalLength();
            // 発電量に応じた「速度」を一定にする計算式
            let dur = (len * 4000) / (val + 1); 
            dur = Math.min(8000, Math.max(500, dur));

            // すでに動いている場合は開始時間を引き継ぐ（ここがポイント）
            if (!rafs[dotId + "_start"]) {
                rafs[dotId + "_start"] = performance.now();
            }

            function step(ts) { 
                const startTime = rafs[dotId + "_start"];
                // 5秒ごとのデータ更新でdurが変わっても、経過時間から今の位置を再計算する
                const progress = ((ts - startTime) % dur) / dur;
                
                const p = path.getPointAtLength(progress * len); 
                dot.setAttribute('cx', p.x); 
                dot.setAttribute('cy', p.y); 
                rafs[dotId] = requestAnimationFrame(step); 
            }

            // 二重実行防止
            cancelAnimationFrame(rafs[dotId]); 
            rafs[dotId] = requestAnimationFrame(step);
        }

        async function updateLive() {
            try {
                const now = new Date(), todayStr = now.toLocaleDateString('sv-SE');
                if (todayStr !== currentLoadedDate) {
                    currentLoadedDate = todayStr; document.getElementById('sel-d').value = todayStr;
                    loadData(); loadWeather();
                }
                const d = await (await fetch('/api/live')).json();
                document.getElementById('clock').innerText = now.toLocaleString('ja-JP');
                document.getElementById('v-solar').innerText = d.solar;
                document.getElementById('v-home').innerText = d.home;
                document.getElementById('v-s-t').innerText = d.d_solar_t;
                document.getElementById('v-bk').innerText = d.d_buy_k; document.getElementById('v-by').innerText = d.d_buy_y;
                document.getElementById('v-sk').innerText = d.d_sell_k; document.getElementById('v-sy').innerText = d.d_sell_y;
                const gridNode = document.querySelector('.node.grid'), gVal = document.getElementById('v-grid');
                if (d.sell > 0) { gVal.innerText = d.sell; gridNode.style.borderColor = "var(--sell)"; }
                else { gVal.innerText = d.buy; gridNode.style.borderColor = "var(--buy)"; }

                // --- 修正：解像度追従のためのパス更新ロジック ---
                const solar = document.querySelector('.node.solar').getBoundingClientRect();
                const grid = document.querySelector('.node.grid').getBoundingClientRect();
                const home = document.querySelector('.node.home').getBoundingClientRect();
                const area = document.querySelector('.area-live').getBoundingClientRect();

                const sx = (solar.left + solar.width/2) - area.left;
                const sy = (solar.top + solar.height/2) - area.top;
                const gx = (grid.left + grid.width/2) - area.left;
                const hx = (home.left + home.width/2) - area.left;
                const ty = 170; // 分岐点の高さ

                document.getElementById('p-s2h').setAttribute('d', `M ${sx} ${sy} V ${ty} H ${hx}`);
                document.getElementById('p-s2g').setAttribute('d', `M ${sx} ${sy} V ${ty} H ${gx}`);
                document.getElementById('p-g2h').setAttribute('d', `M ${gx} ${ty} H ${hx}`);
                // ---------------------------------------------

                // 修正：色を指定（発電由来：青、買電由来：オレンジ）
                anim('d-s2h', 'p-s2h', Math.min(d.solar, d.home), '#3b82f6'); // 発電→家
                anim('d-s2g', 'p-s2g', d.sell, '#3b82f6');                   // 発電→網
                anim('d-g2h', 'p-g2h', d.buy, '#f97316');                    // 買電→家
            } catch(e) {}
        }

async function loadData() {
            const u = document.getElementById('sel-u').value, d = document.getElementById('sel-d').value;
            try {
                const res = await (await fetch(`/api/history?unit=${u}&date=${d}`)).json();
                mainChart.data.labels = res.labels;
                mainChart.data.datasets = [
                    {label:'買電', type:'bar', backgroundColor:'#f97316', data:res.buy, order:2},
                    {label:'売電', type:'bar', backgroundColor:'#22c55e', data:res.sell, order:2},
                    {label:'発電', type:'line', borderColor:'#3b82f6', data:res.solar, pointRadius:4, tension:0.3, order:1},
                    {label:'家消費', type:'line', borderColor:'#a855f7', data:res.home, pointRadius:4, tension:0.3, order:1}
                ];
                if(u==='day') mainChart.data.datasets.push({label:'予測', type:'line', borderColor:'#94a3b8', borderDash:[5,5], data:res.forecast, pointRadius:0, order:3});
                mainChart.update('none');
                
                const f = (v) => v ?? '-';

                // --- 項目名（ヘッダー）の更新処理を追加 ---
                const unitLabel = u === 'day' ? '時間' : (u === 'month' ? '日付' : '月');
                document.getElementById('table-head').innerHTML = `
                    <tr>
                        <th>${unitLabel}</th>
                        <th>買電(kWh)</th>
                        <th>売電(kWh)</th>
                        <th>発電(kWh)</th>
                        <th>消費(kWh)</th>
                        <th>買電(円)</th>
                        <th>売電(円)</th>
                        <th>天気</th>
                        <th>日射量</th>
                    </tr>`;
                
                // リスト本体の更新
                document.getElementById('list-body').innerHTML = res.labels.map((l, i) => 
                    `<tr>
                        <td>${l}</td>
                        <td>${f(res.buy[i])}</td>
                        <td>${f(res.sell[i])}</td>
                        <td>${f(res.solar[i])}</td>
                        <td>${f(res.home[i])}</td>
                        <td>${f(res.buy_yen[i])}</td>
                        <td>${f(res.sell_yen[i])}</td>
                        <td>${f(res.weather[i])}</td>
                        <td>${f(res.radiation[i])}</td>
                    </tr>`).join('');
            } catch(e) {}
        }

        function setView(v) {
            document.getElementById('wrap-chart').style.display = v==='chart'?'block':'none';
            document.getElementById('wrap-list').style.display = v==='list'?'block':'none';
            document.getElementById('b-chart').classList.toggle('active', v==='chart');
            document.getElementById('b-list').classList.toggle('active', v==='list');
        }

        async function loadWeather() {
            try {
                const res = await (await fetch("https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Asia%2FTokyo")).json();
                document.getElementById('w-max').innerText = Math.round(res.daily.temperature_2m_max[0]);
                document.getElementById('w-min').innerText = Math.round(res.daily.temperature_2m_min[0]);
                document.getElementById('w-txt').innerText = `${W_EMOJI[res.daily.weather_code[0]] || "☁️"} (降水:${res.daily.precipitation_sum[0]}mm)`;
                document.getElementById('w-weekly').innerHTML = res.daily.time.slice(0,7).map((t, i) => `<div>${t.slice(8,10)}日<br>${W_EMOJI[res.daily.weather_code[i]]?.split(' ')[0] || "☁️"}<br>${Math.round(res.daily.temperature_2m_max[i])}°</div>`).join('');
            } catch(e) {}
        }

        async function loadSB() {
            try {
                const d = await (await fetch('/api/devices')).json();
                const wrap = document.getElementById('sb-wrap'); wrap.innerHTML = '';
                (d.deviceList || []).concat(d.infraredRemoteList || []).forEach(v => {
                    const isAC = v.deviceType === "Air Conditioner";
                    const powerStatus = v.power === 'on';
                    const card = document.createElement('div'); card.className = "sb-card";
                    card.innerHTML = `<div class="sb-name">${v.deviceName || v.remoteName}</div><div class="sb-ctrl">
                        ${isAC ? `<select class="ac-select" id="mode-${v.deviceId}"><option value="2">冷</option><option value="3">除</option><option value="5">暖</option></select>
                        <input type="number" class="ac-temp" id="temp-${v.deviceId}" value="26" min="18" max="30">` : ''}
                        <button class="btn-off ${!powerStatus ? 'active' : ''}" id="off-${v.deviceId}" onclick="ctrlAC('${v.deviceId}','off',${isAC})">OFF</button>
                        <button class="btn-on ${powerStatus ? 'active' : ''}" id="on-${v.deviceId}" onclick="ctrlAC('${v.deviceId}','on',${isAC})">ON</button></div>`;
                    wrap.appendChild(card);
                });
            } catch(e) {}
        }

        function ctrlAC(id, action, isAC) {
            let payload;
            const btnOn = document.getElementById(`on-${id}`);
            const btnOff = document.getElementById(`off-${id}`);
            if (!isAC) payload = {command: action === 'off' ? 'turnOff' : 'turnOn', parameter:'default', commandType:'command'};
            else { 
                const t = document.getElementById(`temp-${id}`).value, m = document.getElementById(`mode-${id}`).value; 
                payload = {command:'setAll', parameter:`${t},${m},1,${action}`, commandType:'command'}; 
            }
            fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({deviceId:id, payload:payload})})
            .then(res => {
                if(action === 'on') { btnOn.classList.add('active'); btnOff.classList.remove('active'); } 
                else { btnOff.classList.add('active'); btnOn.classList.remove('active'); }
            });
        }

        mainChart = new Chart(document.getElementById('mainChart').getContext('2d'), { type:'bar', options:{ animation: false, responsive:true, maintainAspectRatio:false, scales:{y:{beginAtZero:true, grid:{color:'#1e293b'}}, x:{grid:{display:false}}} } });
        document.getElementById('sel-d').value = currentLoadedDate;
        updateLive(); loadData(); loadWeather(); loadSB();
        setInterval(updateLive, 5000); setInterval(loadData, 60000); setInterval(loadWeather, 3600000);
        setInterval(loadSB, 30000);
    </script>
    </body></html>
    """)

if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
