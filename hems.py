import socket
import time
import threading
import sqlite3
import requests
import os
from flask import Flask, jsonify, render_template_string
from datetime import datetime, timedelta
from dotenv import load_dotenv
import calendar

# .envファイルから環境変数を読み込む
load_dotenv()

# --- 設定 ---
IP = os.environ.get("HEMS_IP", "192.168.0.146")
PORT = 3610
SOLAR_EOJ = [0x02, 0x79, 0x01]
METER_EOJ = [0x02, 0xA5, 0x01]
MAX_W = 5900  
LAT = float(os.environ.get("HEMS_LAT", "33.46"))
LON = float(os.environ.get("HEMS_LON", "130.54"))

app = Flask(__name__)
latest = {"solar": 0, "buy": 0, "sell": 0, "home": 0, "self_cons": 0, "cloud": 0, "forecast": 0, "cost": 0, "irradiance": 0}

def get_advanced_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&hourly=direct_radiation,diffuse_radiation,cloud_cover&timezone=Asia%2FTokyo&forecast_days=2"
        res = requests.get(url, timeout=5).json()
        f_map = {}
        for i in range(len(res['hourly']['time'])):
            t_str = res['hourly']['time'][i].replace("T", " ") + ":00"
            irradiance = res['hourly']['direct_radiation'][i] + res['hourly']['diffuse_radiation'][i]
            est_w = int(MAX_W * (irradiance / 1000) * 0.85)
            f_map[t_str] = {"w": max(0, est_w), "cloud": res['hourly']['cloud_cover'][i], "irr": irradiance}
        return f_map
    except: return {}

def fetch_echonet(eoj, epc):
    try:
        frame = bytes([0x10, 0x81, 0x00, 0x01, 0x05, 0xff, 0x01, eoj[0], eoj[1], eoj[2], 0x62, 0x01, epc, 0x00])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("", 3610)); s.settimeout(1.0); s.sendto(frame, (IP, PORT))
            data, _ = s.recvfrom(1024); idx = data.find(bytes([epc]))
            return data[idx+2 : idx+2+data[idx+1]]
    except: return None

def collector():
    db_path = "data/energy.db" if os.path.exists("data") else "energy.db"
    if not os.path.exists("data") and "data/" in db_path: os.makedirs("data")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS energy(time TEXT, solar REAL, buy REAL, sell REAL, home REAL, self_cons REAL, cloud REAL, forecast REAL, irradiance REAL)")
    conn.commit(); conn.close()
    while True:
        f_map = get_advanced_forecast()
        now_h = datetime.now().strftime("%Y-%m-%d %H:00:00")
        f_info = f_map.get(now_h, {"w": 0, "cloud": 0, "irr": 0})
        latest["forecast"], latest["cloud"], latest["irradiance"] = f_info["w"], f_info["cloud"], f_info["irr"]
        
        res_s = fetch_echonet(SOLAR_EOJ, 0xE0)
        if res_s: latest["solar"] = int.from_bytes(res_s, "big", signed=True)
        res_m = fetch_echonet(METER_EOJ, 0xF5)
        if res_m and len(res_m) >= 8:
            v1 = int.from_bytes(res_m[0:4], "big", signed=True)
            v2 = int.from_bytes(res_m[4:8], "big", signed=True)
            latest["sell"], latest["buy"] = (v1, 0) if v1 >= 0 else (0, abs(v1))
            latest["home"] = v2
            latest["self_cons"] = max(0, latest["solar"] - latest["sell"])
            
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO energy VALUES (?,?,?,?,?,?,?,?,?)",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     latest["solar"], latest["buy"], latest["sell"], latest["home"], latest["self_cons"], latest["cloud"], latest["forecast"], latest["irradiance"]))
        
        today = datetime.now().strftime("%Y-%m-%d")
        buy_rows = conn.execute("SELECT time, buy FROM energy WHERE date(time) = ?", (today,)).fetchall()
        total_cost = 0.0
        for r_t, r_b in buy_rows:
            dt = datetime.strptime(r_t, "%Y-%m-%d %H:%M:%S")
            rate = 16.6 if (21 <= dt.hour or dt.hour < 7) else (33.8 if dt.month in [7,8,9] else 28.6)
            total_cost += (r_b / 1000 / 60) * rate
        latest["cost"] = int(total_cost)
        conn.commit(); conn.close(); time.sleep(60)

@app.route("/api/live")
def api_live():
    total_cons = latest["self_cons"] + latest["buy"]
    sr = int((latest["self_cons"] / total_cons * 100)) if total_cons > 50 else 0
    return jsonify({**latest, "sr": sr})

@app.route("/api/stats/<mode>")
def api_stats(mode):
    db_path = "data/energy.db" if os.path.exists("data") else "energy.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now = datetime.now()
    res = []

    if mode == "hour":
        f_map = get_advanced_forecast()
        start_t = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute("SELECT * FROM energy WHERE time >= ? ORDER BY time ASC", (start_t,)).fetchall()
        for r in rows:
            dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            b_kwh, s_kwh = r['buy'] / 1000 / 60, r['sell'] / 1000 / 60
            res.append({
                "label": dt.strftime("%H:%M"), "solar": r['solar'], "buy": r['buy'], "sell": r['sell'], "self_cons": r['self_cons'],
                "cloud": r['cloud'], "irr": r['irradiance'], "forecast": f_map.get(dt.strftime("%Y-%m-%d %H:00:00"), {"w":0})["w"],
                "nb": round(b_kwh, 3), "ds": round(s_kwh, 3), "bat": round(min(b_kwh, s_kwh), 2)
            })
        for i in range(1, 13):
            fut = now + timedelta(minutes=i*5)
            f_val = f_map.get(fut.strftime("%Y-%m-%d %H:00:00"), {"w":0, "cloud":0, "irr":0})
            res.append({"label": fut.strftime("%H:%M"), "solar": None, "buy": None, "sell": None, "self_cons": None, "cloud": f_val["cloud"], "irr": f_val["irr"], "forecast": f_val["w"], "nb": 0, "ds": 0, "bat": 0})

    elif mode == "day":
        today_str = now.strftime("%Y-%m-%d")
        f_map = get_advanced_forecast()
        for h in range(24):
            for m in [0, 30]:
                lbl = f"{h:02}:{m:02}"
                start_win = f"{today_str} {h:02}:{m:02}:00"
                end_win = f"{today_str} {h:02}:{m+29:02}:59"
                row = conn.execute("SELECT AVG(solar), AVG(sell), AVG(self_cons), AVG(cloud), AVG(irradiance), AVG(buy), SUM(buy)/60/1000, SUM(sell)/60/1000 FROM energy WHERE time BETWEEN ? AND ?", (start_win, end_win)).fetchone()
                nb, ds = row[6] or 0, row[7] or 0
                f_val = f_map.get(f"{today_str} {h:02}:00:00", {"w":0})["w"]
                res.append({
                    "label": lbl, "solar": int(row[0]) if row[0] else 0, "buy": int(row[5]) if row[5] else 0, "sell": int(row[1]) if row[1] else 0,
                    "self_cons": int(row[2]) if row[2] else 0, "cloud": int(row[3]) if row[3] else 0,
                    "irr": int(row[4]) if row[4] else 0, "forecast": f_val,
                    "nb": round(nb, 2), "ds": round(ds, 2), "bat": round(min(nb, ds), 2)
                })

    elif mode == "month":
        year_month = now.strftime("%Y-%m-")
        days = calendar.monthrange(now.year, now.month)[1]
        for d in range(1, days + 1):
            t_s = f"{year_month}{d:02}%"
            row = conn.execute("SELECT SUM(solar)/60/1000, SUM(sell)/60/1000, SUM(self_cons)/60/1000, AVG(cloud), AVG(irradiance), SUM(forecast)/60/1000, SUM(buy)/60/1000 FROM energy WHERE time LIKE ?", (t_s,)).fetchone()
            row_n = conn.execute("SELECT SUM(buy)/60/1000 FROM energy WHERE time LIKE ? AND (strftime('%H', time) >= '18' OR strftime('%H', time) < '07')", (t_s,)).fetchone()
            row_e = conn.execute("SELECT SUM(sell)/60/1000 FROM energy WHERE time LIKE ? AND (strftime('%H', time) >= '07' AND strftime('%H', time) < '18')", (t_s,)).fetchone()
            nb, ds = row_n[0] or 0, row_e[0] or 0
            res.append({
                "label": f"{d}日", "solar": round(row[0],1) if row[0] else 0, "buy": round(row[6],1) if row[6] else 0, "sell": round(row[1],1) if row[1] else 0,
                "self_cons": round(row[2],1) if row[2] else 0, "cloud": int(row[3]) if row[3] else 0,
                "irr": int(row[4]) if row[4] else 0, "forecast": round(row[5],1) if row[5] else 0,
                "nb": round(nb, 2), "ds": round(ds, 2), "bat": round(min(nb, ds), 1)
            })

    elif mode == "year":
        for m in range(1, 13):
            t_s = now.strftime("%Y-") + f"{m:02}-%"
            row = conn.execute("SELECT SUM(solar)/60/1000, SUM(sell)/60/1000, SUM(self_cons)/60/1000, AVG(cloud), AVG(irradiance), SUM(forecast)/60/1000, SUM(buy)/60/1000 FROM energy WHERE time LIKE ?", (t_s,)).fetchone()
            row_n = conn.execute("SELECT AVG(buy) FROM energy WHERE time LIKE ? AND (strftime('%H', time) >= '18' OR strftime('%H', time) < '07')", (t_s,)).fetchone()
            row_e = conn.execute("SELECT AVG(sell) FROM energy WHERE time LIKE ? AND (strftime('%H', time) >= '07' AND strftime('%H', time) < '18')", (t_s,)).fetchone()
            nb_avg = (row_n[0] or 0) * 13 / 1000
            ds_avg = (row_e[0] or 0) * 11 / 1000
            res.append({
                "label": f"{m}月", "solar": int(row[0]) if row[0] else 0, "buy": int(row[6]) if row[6] else 0, "sell": int(row[1]) if row[1] else 0,
                "self_cons": int(row[2]) if row[2] else 0, "cloud": int(row[3]) if row[3] else 0,
                "irr": int(row[4]) if row[4] else 0, "forecast": int(row[5]) if row[5] else 0,
                "nb": round(nb_avg, 1), "ds": round(ds_avg, 1), "bat": round(min(nb_avg, ds_avg), 1)
            })
    conn.close()
    return jsonify(res)

@app.route("/")
def index():
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8"><title>筑紫野 HEMS PRO</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { font-family: -apple-system, sans-serif; background: #f4f7f9; padding: 20px; color: #333; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 12px; max-width: 1200px; margin: auto; }
            .card { background: white; padding: 20px; border-radius: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); text-align: center; }
            .val { font-size: 28px; font-weight: 800; display: block; margin-top: 8px; }
            .controls { max-width: 1200px; margin: 30px auto; display: flex; gap: 12px; justify-content: center; }
            button { padding: 10px 20px; border-radius: 8px; border: 1px solid #ddd; cursor: pointer; background: white; font-weight: bold; }
            button.active { background: #3498db; color: white; border-color: #3498db; }
            .view-box { background: white; padding: 20px; border-radius: 20px; max-width: 1200px; margin: auto; box-shadow: 0 10px 30px rgba(0,0,0,0.05); overflow-x: auto; }
            #chart-area { height: 60vh; min-height: 400px; }
            table { width: 100%; border-collapse: collapse; font-size: 11px; min-width: 900px; }
            th { background: #f8f9fa; position: sticky; top: 0; }
            th, td { border-bottom: 1px solid #eee; padding: 10px 4px; text-align: center; }
            .bat-val { font-weight: bold; color: #d35400; background: #fff3e0; border-radius: 4px; padding: 2px 4px; }
            .hidden { display: none; }
        </style>
    </head>
    <body>
        <div class="grid">
            <div class="card">☀️ 発電<br><span class="val" id="s">-</span></div>
            <div class="card">🏠 自家消費<br><span class="val" style="color:#9b59b6;" id="sc">-</span></div>
            <div class="card">📤 売電中<br><span class="val" style="color:#2ecc71;" id="sl">-</span></div>
            <div class="card">🔌 買電中<br><span class="val" style="color:#e74c3c;" id="b">-</span></div>
            <div class="card">💰 今日料金<br><span class="val" id="c">-</span></div>
            <div class="card">♻️ 自給率<br><span class="val" id="sr">-</span></div>
        </div>
        <div class="controls">
            <button class="m-btn active" onclick="setMode('hour', this)">前後1時間</button>
            <button class="m-btn" onclick="setMode('day', this)">今日</button>
            <button class="m-btn" onclick="setMode('month', this)">月</button>
            <button class="m-btn" onclick="setMode('year', this)">年</button>
            <span style="width:20px"></span>
            <button class="v-btn active" onclick="setView('chart', this)">グラフ</button>
            <button class="v-btn" onclick="setView('table', this)">リスト</button>
        </div>
        <div class="view-box">
            <div id="chart-area"><canvas id="mainChart"></canvas></div>
            <div id="table-area" class="hidden">
                <table>
                    <thead>
                        <tr>
                            <th>時間軸</th><th>発電実績</th><th>自家消費</th><th>売電量</th>
                            <th>買電量</th><th>発電(予測)</th><th>日照量</th><th>雲量</th>
                            <th>昼間余剰</th><th>夜間買電</th><th>蓄電池必要量</th>
                        </tr>
                    </thead>
                    <tbody id="table-body"></tbody>
                </table>
            </div>
        </div>
        <script>
            let chart; let currentMode = 'hour'; let currentView = 'chart';
            async function updateLive() {
                const res = await fetch('/api/live'); const d = await res.json();
                document.getElementById('s').innerText = d.solar + 'W';
                document.getElementById('sc').innerText = d.self_cons + 'W';
                document.getElementById('sl').innerText = d.sell + 'W';
                document.getElementById('b').innerText = d.buy + 'W';
                document.getElementById('c').innerText = d.cost + '円';
                document.getElementById('sr').innerText = d.sr + '%';
            }
            async function updateStats() {
                const res = await fetch('/api/stats/' + currentMode); const d = await res.json();
                let h = '';
                const unit = (currentMode === 'hour' || currentMode === 'day') ? 'W' : 'kWh';
                d.forEach(x => {
                    h += `<tr>
                        <td>${x.label}</td>
                        <td>${x.solar??'-'}${unit}</td>
                        <td>${x.self_cons??'-'}${unit}</td>
                        <td>${x.sell??'-'}${unit}</td>
                        <td>${x.buy??'-'}${unit}</td>
                        <td style="color:#7f8c8d">${x.forecast}${unit}</td>
                        <td>${x.irr}W/m²</td>
                        <td>${x.cloud}%</td>
                        <td>${x.ds}kWh</td>
                        <td>${x.nb}kWh</td>
                        <td><span class="bat-val">${x.bat}kWh</span></td>
                    </tr>`;
                });
                document.getElementById('table-body').innerHTML = h;
                if (currentView === 'chart') {
                    const ctx = document.getElementById('mainChart').getContext('2d');
                    if(chart) chart.destroy();
                    chart = new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: d.map(x => x.label),
                            datasets: [
                                { label:'発電実績', data:d.map(x=>x.solar), borderColor:'#f1c40f', backgroundColor:'#f1c40f15', fill:true, tension:0.2, spanGaps:true },
                                { label:'発電予測', data:d.map(x=>x.forecast), borderColor:'#95a5a6', borderDash:[5,5], fill:false, tension:0.2, pointRadius:0 },
                                { label:'自家消費', data:d.map(x=>x.self_cons), borderColor:'#9b59b6', tension:0.2, spanGaps:true },
                                { label:'売電', data:d.map(x=>x.sell), borderColor:'#2ecc71', spanGaps:true },
                                { label:'買電', data:d.map(x=>x.buy), borderColor:'#e74c3c', spanGaps:true }
                            ]
                        }, options: { maintainAspectRatio: false, animation: false, plugins: { legend: { position: 'bottom' } }, scales: { y: { beginAtZero: true } } }
                    });
                }
            }
            function setMode(m, btn){ currentMode=m; document.querySelectorAll('.m-btn').forEach(b=>b.classList.remove('active')); btn.classList.add('active'); updateStats(); }
            function setView(v, btn){ currentView=v; document.querySelectorAll('.v-btn').forEach(b=>b.classList.remove('active')); btn.classList.add('active'); document.getElementById('chart-area').classList.toggle('hidden', v!=='chart'); document.getElementById('table-area').classList.toggle('hidden', v!=='table'); updateStats(); }
            setInterval(updateLive, 5000); updateLive(); updateStats();
        </script>
    </body>
    </html>
    """)

if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
