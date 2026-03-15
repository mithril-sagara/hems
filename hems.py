import socket
import time
import threading
import sqlite3
import requests
from flask import Flask, jsonify, render_template_string
from datetime import datetime, timedelta
import os

# --- 設定 ---
# 環境変数 "HEMS_IP" があればそれを使用、なければデフォルト値を使用
IP = os.environ.get("HEMS_IP", "192.168.0.146") 
PORT = 3610

SOLAR_EOJ = [0x02, 0x79, 0x01]
METER_EOJ = [0x02, 0xA5, 0x01]
#パワコンの最大売電量
MAX_W = 5900
# 環境変数から緯度・経度を取得（デフォルトは筑紫野市）
LAT = float(os.environ.get("HEMS_LAT", "33.46"))
LON = float(os.environ.get("HEMS_LON", "130.55"))

app = Flask(__name__)
latest = {"solar": 0, "buy": 0, "sell": 0, "home": 0, "self_cons": 0, "cloud": 0, "forecast": 0, "cost": 0}

def get_advanced_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&hourly=direct_radiation,diffuse_radiation,cloud_cover&timezone=Asia%2FTokyo&forecast_days=2"
        res = requests.get(url, timeout=5).json()
        f_map = {}
        for i in range(len(res['hourly']['time'])):
            t_str = res['hourly']['time'][i].replace("T", " ") + ":00"
            irradiance = res['hourly']['direct_radiation'][i] + res['hourly']['diffuse_radiation'][i]
            est_w = int(MAX_W * (irradiance / 1000) * 0.85)
            f_map[t_str] = {"w": max(0, est_w), "cloud": res['hourly']['cloud_cover'][i]}
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
    conn = sqlite3.connect("energy.db", check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS energy(time TEXT, solar REAL, buy REAL, sell REAL, home REAL, self_cons REAL, cloud REAL, forecast REAL)")
    conn.commit(); conn.close()
    while True:
        f_map = get_advanced_forecast()
        now_h = datetime.now().strftime("%Y-%m-%d %H:00:00")
        f_info = f_map.get(now_h, {"w": 0, "cloud": 0})
        latest["forecast"], latest["cloud"] = f_info["w"], f_info["cloud"]
        res_s = fetch_echonet(SOLAR_EOJ, 0xE0)
        if res_s: latest["solar"] = int.from_bytes(res_s, "big", signed=True)
        res_m = fetch_echonet(METER_EOJ, 0xF5)
        if res_m and len(res_m) >= 8:
            v1 = int.from_bytes(res_m[0:4], "big", signed=True)
            v2 = int.from_bytes(res_m[4:8], "big", signed=True)
            latest["sell"], latest["buy"] = (v1, 0) if v1 >= 0 else (0, abs(v1))
            latest["home"] = v2
            latest["self_cons"] = max(0, latest["solar"] - latest["sell"])
        conn = sqlite3.connect("energy.db")
        conn.execute("INSERT INTO energy VALUES (?,?,?,?,?,?,?,?)",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     latest["solar"], latest["buy"], latest["sell"], latest["home"], latest["self_cons"], latest["cloud"], latest["forecast"]))
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
    conn = sqlite3.connect("energy.db")
    conn.row_factory = sqlite3.Row
    now = datetime.now()
    f_map = get_advanced_forecast()
    res = []

    if mode == "hour":
        start_t = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute("SELECT * FROM energy WHERE time >= ? ORDER BY time ASC", (start_t,)).fetchall()
        for r in rows:
            dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            res.append({"label": dt.strftime("%H:%M"), "solar": r['solar'], "buy": r['buy'], "sell": r['sell'], "self_cons": r['self_cons'], "forecast": f_map.get(dt.strftime("%Y-%m-%d %H:00:00"), {"w":0})["w"]})
        for i in range(1, 13):
            fut = now + timedelta(minutes=i*5)
            res.append({"label": fut.strftime("%H:%M"), "solar": None, "buy": None, "sell": None, "self_cons": None, "forecast": f_map.get(fut.strftime("%Y-%m-%d %H:00:00"), {"w":0})["w"]})

    elif mode == "day":
        today_str = now.strftime("%Y-%m-%d")
        night_buy_total = 0.0
        day_sell_total = 0.0
        for h in range(24):
            for m in [0, 10, 20, 30, 40, 50]:
                lbl = f"{h:02}:{m:02}"
                t_search = f"{today_str} {h:02}:{m:02}%"
                row = conn.execute("SELECT AVG(solar), AVG(buy), AVG(sell), AVG(self_cons) FROM energy WHERE time LIKE ?", (t_search,)).fetchone()
                buy_val = row[1] if row[1] is not None else 0
                sell_val = row[2] if row[2] is not None else 0
                if h >= 18 or h < 7: night_buy_total += (buy_val / 6 / 1000)
                else: day_sell_total += (sell_val / 6 / 1000)
                f_key = f"{today_str} {h:02}:00:00"
                res.append({
                    "label": lbl, "solar": int(row[0]) if row[0] is not None else None, 
                    "buy": buy_val, "sell": sell_val, "self_cons": int(row[3]) if row[3] is not None else None, 
                    "forecast": f_map.get(f_key, {"w": 0})["w"] if m == 0 else None,
                    "summary": {"nb": round(night_buy_total, 2), "ds": round(day_sell_total, 2), "bat": round(min(night_buy_total, day_sell_total), 1)} if (h==23 and m==50) else None
                })

    elif mode == "year":
        for m in range(1, 13):
            t_search = now.strftime("%Y-") + f"{m:02}-%"
            # 月間平均から、夜間と昼間の1日あたり収支を算出
            row_night = conn.execute("SELECT AVG(buy) FROM energy WHERE time LIKE ? AND (strftime('%H', time) >= '18' OR strftime('%H', time) < '07')", (t_search,)).fetchone()
            row_day_sell = conn.execute("SELECT AVG(sell) FROM energy WHERE time LIKE ? AND (strftime('%H', time) >= '07' AND strftime('%H', time) < '18')", (t_search,)).fetchone()
            row_all = conn.execute("SELECT AVG(solar), AVG(buy), AVG(sell), AVG(self_cons) FROM energy WHERE time LIKE ?", (t_search,)).fetchone()
            
            factor = 24 * 30 / 1000
            # 推奨容量の計算（1日あたりの平均夜間買電量 vs 平均昼間売電量）
            avg_nb = (row_night[0] or 0) * 13 / 1000 # 13時間は夜間帯
            avg_ds = (row_day_sell[0] or 0) * 11 / 1000 # 11時間は昼間帯
            
            res.append({
                "label": f"{m}月", 
                "solar": round(row_all[0]*factor) if row_all[0] else 0, 
                "buy": round(row_all[1]*factor) if row_all[1] else 0, 
                "sell": round(row_all[2]*factor) if row_all[2] else 0, 
                "self_cons": round(row_all[3]*factor) if row_all[3] else 0, 
                "forecast": 0,
                "bat_rec": round(min(avg_nb, avg_ds), 1) if avg_nb > 0 else 0
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
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 12px; max-width: 1100px; margin: auto; }
            .card { background: white; padding: 20px; border-radius: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); text-align: center; }
            .val { font-size: 28px; font-weight: 800; display: block; margin-top: 8px; }
            .controls { max-width: 1100px; margin: 30px auto; display: flex; gap: 12px; justify-content: center; align-items: center; }
            button { padding: 10px 20px; border-radius: 8px; border: 1px solid #ddd; cursor: pointer; background: white; font-weight: bold; transition: 0.3s; }
            button.active { background: #3498db; color: white; border-color: #3498db; }
            .view-box { background: white; padding: 25px; border-radius: 20px; max-width: 1100px; margin: auto; box-shadow: 0 10px 30px rgba(0,0,0,0.05); }
            #chart-area { position: relative; height: 65vh; min-height: 500px; width: 100%; }
            .advice-card { padding:15px; background:#e1f5fe; border-radius:12px; margin-bottom:20px; font-size:14px; border-left:5px solid #0288d1; }
            .hidden { display: none; }
            table { width: 100%; border-collapse: collapse; font-size: 13px; }
            th, td { border-bottom: 1px solid #eee; padding: 10px; text-align: center; }
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
            <button class="m-btn active" id="btn-hour" onclick="setMode('hour')">前後1時間</button>
            <button class="m-btn" id="btn-day" onclick="setMode('day')">今日</button>
            <button class="m-btn" id="btn-year" onclick="setMode('year')">今年</button>
            <span style="color:#ccc">|</span>
            <button class="v-btn active" id="btn-chart" onclick="setView('chart')">グラフ</button>
            <button class="v-btn" id="btn-table" onclick="setView('table')">リスト</button>
        </div>
        <div class="view-box">
            <div id="chart-area"><canvas id="mainChart"></canvas></div>
            <div id="table-area" class="hidden">
                <div id="bat-advice" class="advice-card hidden"></div>
                <table><thead id="table-head"></thead><tbody id="table-body"></tbody></table>
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
                if (!d || d.length === 0) return;
                
                const adv = document.getElementById('bat-advice');
                const sm = d.find(x => x.summary)?.summary;
                if(sm && currentMode === 'day') {
                    adv.innerHTML = `<strong>🔋 本日のシミュレーション:</strong> 夜間買電: ${sm.nb}kWh / 昼間余剰: ${sm.ds}kWh <br> 推奨容量: <span style="font-size:1.4em; color:#d35400;">${sm.bat} kWh以上</span>`;
                    adv.classList.remove('hidden');
                } else { adv.classList.add('hidden'); }

                // ヘッダーの切り替え
                let th = '<tr><th>日時</th><th>実績</th><th>予測</th><th>自家消費</th><th>売電</th></tr>';
                if(currentMode === 'year') th = '<tr><th>月</th><th>発電(kWh)</th><th>買電(kWh)</th><th>売電(kWh)</th><th>推奨蓄電池</th></tr>';
                document.getElementById('table-head').innerHTML = th;

                let h = ''; d.slice().reverse().forEach(x => { 
                    if(currentMode === 'year') {
                        h += `<tr><td>${x.label}</td><td>${x.solar}</td><td>${x.buy}</td><td>${x.sell}</td><td style="color:#d35400; font-weight:bold;">${x.bat_rec} kWh</td></tr>`;
                    } else {
                        h += `<tr><td>${x.label}</td><td>${x.solar??'-'}</td><td>${x.forecast??'-'}</td><td>${x.self_cons??'-'}</td><td>${x.sell??'-'}</td></tr>`; 
                    }
                });
                document.getElementById('table-body').innerHTML = h;

                if (currentView === 'chart') {
                    const ctx = document.getElementById('mainChart').getContext('2d');
                    if(chart) chart.destroy();
                    let yL = '電力 (W)'; let sM = 6000;
                    if (currentMode === 'year') { yL = '推定電力量 (kWh)'; sM = null; }

                    chart = new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: d.map(x => x.label),
                            datasets: [
                                { label:'発電実績', data:d.map(x=>x.solar), borderColor:'#f1c40f', backgroundColor:'#f1c40f15', fill:true, tension:0.2, pointRadius:currentMode==='day'?0:3, spanGaps: true },
                                { label:'発電予測', data:d.map(x=>x.forecast), borderColor:'#95a5a6', borderDash:[5,5], fill:false, tension:0.2, pointRadius:0, spanGaps: true },
                                { label:'自家消費', data:d.map(x=>x.self_cons), borderColor:'#9b59b6', fill:false, tension:0.2, pointRadius:currentMode==='day'?0:3, spanGaps: true },
                                { label:'売電', data:d.map(x=>x.sell), borderColor:'#2ecc71', fill:false, pointRadius:currentMode==='day'?0:3, spanGaps: true },
                                { label:'買電', data:d.map(x=>x.buy), borderColor:'#e74c3c', fill:false, pointRadius:currentMode==='day'?0:3, spanGaps: true }
                            ]
                        }, options: { 
                            animation: false, maintainAspectRatio: false,
                            scales: { y: { beginAtZero: true, suggestedMax: sM, title: { display: true, text: yL }, ticks: { stepSize: currentMode==='year'?undefined:500 } } }, 
                            plugins: { legend: { position: 'bottom' } } 
                        }
                    });
                }
            }
            function setMode(m){ currentMode=m; document.querySelectorAll('.m-btn').forEach(b=>b.classList.remove('active')); document.getElementById('btn-'+m).classList.add('active'); updateStats(); }
            function setView(v){ currentView=v; document.querySelectorAll('.v-btn').forEach(b=>b.classList.remove('active')); document.getElementById('btn-'+v).classList.add('active'); document.getElementById('chart-area').classList.toggle('hidden', v!=='chart'); document.getElementById('table-area').classList.toggle('hidden', v!=='table'); updateStats(); }
            setInterval(updateLive, 5000); setInterval(updateStats, 60000);
            updateLive(); updateStats();
        </script>
    </body>
    </html>
    """)

if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)