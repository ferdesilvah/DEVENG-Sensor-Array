"""
Two 100 PSI pressure transducers -> Seeed XIAO ESP32C3 (MicroPython)

Sensors: 3-wire, 5V supply, 0.5V (0 psi) .. 4.5V (100 psi) output.
  Red    -> XIAO 5V pin  (live only when powered over USB / 5V)
  Black  -> XIAO GND
  Signal -> voltage divider -> sensor 1: XIAO A0 (GPIO2)
                               sensor 2: XIAO D1/A1 (GPIO3)

The ESP32C3 ADC reads accurately only up to ~2.5V, so each sensor gets
its own divider to halve the signal:

    signal ---[ R1 = 10k ]---+---[ R2 = 10k ]--- GND
                             |
                   A0 (GPIO2) / A1 (GPIO3)

4.5V becomes 2.25V, and 5V (fault) becomes 2.5V.
A 0.1uF capacitor from each ADC pin to GND helps reduce noise.

WiFi: credentials live in secrets.py (WIFI_SSID, WIFI_PASSWORD).
At boot the board joins the network and prints its address over USB
serial; open http://<that address>/ for a live readout. Without WiFi it
keeps printing CSV over serial as before.
"""

import json
import time
import asyncio
import network
from machine import ADC, Pin

try:
    import secrets
except ImportError:
    secrets = None

# ---------- Configuration ----------
SENSOR_PINS = (2, 3)    # XIAO ESP32C3 A0 = GPIO2, D1/A1 = GPIO3
R1 = 10_000             # ohms, signal -> ADC pin
R2 = 10_000             # ohms, ADC pin -> GND
V_MIN = 0.5             # sensor output at 0 psi (volts)
V_MAX = 4.5             # sensor output at full scale (volts)
P_MAX_PSI = 100.0       # sensor full-scale range
SAMPLES = 32            # readings averaged per measurement
PERIOD_MS = 250         # time between readings
HEADER_EVERY = 40       # reprint the CSV header every N readings (~10 s)
RECENT_ROWS = 240       # readings kept for the web recorder (~60 s)

# Measure each sensor's 0 psi offset at boot.
# The sensors must be open to the air (no pressure) when the board starts.
AUTO_ZERO = True

HOSTNAME = "pressure-sensor"
HTTP_PORT = 80
WIFI_TIMEOUT_S = 20

adcs = [ADC(Pin(p), atten=ADC.ATTN_11DB) for p in SENSOR_PINS]  # ~0-2.5V range
zero_offsets_v = [0.0] * len(SENSOR_PINS)
latest = {"sensors": []}  # most recent readings, served at /data
# Every reading as [seq, t_s, s1_psi, s1_volts, s2_psi, ...]; the web page
# fetches the ones it hasn't seen so a recording has no gaps between polls.
recent = []
seq = 0
start_ms = time.ticks_ms()  # reset in main() once zeroing is done
wlan = None             # set by connect_wifi() once the board is on the network
readings_since_header = HEADER_EVERY  # print the header with the first reading
CSV_HEADER = "t_s," + ",".join("s{0}_volts,s{0}_psi,s{0}_bar,s{0}_kPa".format(i + 1)
                               for i in range(len(SENSOR_PINS)))


def read_sensor_voltage(adc):
    """Sensor output voltage, averaged, with the divider undone."""
    total = 0
    for _ in range(SAMPLES):
        total += adc.read_uv()  # factory-calibrated microvolts
        time.sleep_us(200)
    pin_v = total / SAMPLES / 1_000_000
    return pin_v * (R1 + R2) / R2


def voltage_to_psi(v, zero_offset_v):
    psi = (v - V_MIN - zero_offset_v) * P_MAX_PSI / (V_MAX - V_MIN)
    return max(psi, 0.0)  # hide small negative readings caused by noise


def connect_wifi():
    """Join the network from secrets.py. Returns the IP, or None."""
    global wlan
    if secrets is None:
        print("No secrets.py on board - WiFi off, serial only")
        return None
    network.hostname(HOSTNAME)
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if not w.isconnected():
        w.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        deadline = time.time() + WIFI_TIMEOUT_S
        while not w.isconnected() and time.time() < deadline:
            time.sleep_ms(250)
    if not w.isconnected():
        print("WiFi failed (status {}) - serial only".format(w.status()))
        return None
    wlan = w
    ip = wlan.ifconfig()[0]
    print("WiFi connected: http://{}/".format(ip))
    return ip


def web_status():
    """Where to open the web page right now (the IP can change on reconnect)."""
    if wlan is None:
        return "off (WiFi failed at boot - press reset to retry)"
    if not wlan.isconnected():
        return "WiFi dropped - reconnecting..."
    return "http://{}/".format(wlan.ifconfig()[0])


def read_one(adc, zero_offset_v):
    """Read one sensor. Returns (reading dict, CSV fields)."""
    v = read_sensor_voltage(adc)
    # Under ~0.2V usually means a broken signal wire or no 5V;
    # above ~4.8V usually means a short or a faulty sensor.
    if v < 0.2:
        fault = "signal too low - check wiring / 5V supply"
    elif v > 4.8:
        fault = "signal too high - check sensor"
    else:
        fault = None

    reading = {"volts": round(v, 3), "zero": round(zero_offset_v, 3),
               "fault": fault}
    if fault:
        reading.update(psi=None, bar=None, kpa=None)
        return reading, "{:.3f},,,".format(v)
    psi = voltage_to_psi(v, zero_offset_v)
    bar, kpa = psi * 0.0689476, psi * 6.89476
    reading.update(psi=round(psi, 2), bar=round(bar, 3), kpa=round(kpa, 1))
    return reading, "{:.3f},{:.2f},{:.3f},{:.1f}".format(v, psi, bar, kpa)


def take_reading():
    """Read every sensor, update `latest`, and print a CSV line."""
    global readings_since_header, seq
    # The header and web link are repeated so a serial monitor opened after
    # boot still sees them.
    if readings_since_header >= HEADER_EVERY:
        print("# Web page: " + web_status())
        print(CSV_HEADER)
        readings_since_header = 0
    readings_since_header += 1

    t = round(time.ticks_diff(time.ticks_ms(), start_ms) / 1000, 3)
    readings, fields = [], ["{:.3f}".format(t)]
    seq += 1
    row = [seq, t]
    for i, adc in enumerate(adcs):
        reading, csv = read_one(adc, zero_offsets_v[i])
        if reading["fault"]:
            print("FAULT sensor {}: {}".format(i + 1, reading["fault"]))
        readings.append(reading)
        fields.append(csv)
        row += [reading["psi"], reading["volts"]]
    print(",".join(fields))
    latest.update(sensors=readings, seq=seq, t=t)
    recent.append(row)
    if len(recent) > RECENT_ROWS:
        recent.pop(0)


async def sensor_loop():
    while True:
        take_reading()
        await asyncio.sleep_ms(PERIOD_MS)


# ---------- Web page ----------
# A live diagram of the rig: inlet pipe -> sensor 1 -> flow meter -> hose
# -> chamber holding sensor 2. The page polls /data twice a second and
# plots the last two minutes below the diagram.
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pressure Rig</title><style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#15181c;--mute:#646d77;--l0:#2563eb;--l1:#d97706;--bad:#c62828;--ok:#16a34a;--grid:#e3e6ea;
--pipe:#ececec;--edge:#5f6770;--blk:#6b7280;--fm:#7a939c;--box:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b1f24;--fg:#e8eaed;--mute:#9aa3ad;--l0:#6ea8ff;--l1:#f5a524;--bad:#ff6b6b;--ok:#4ade80;--grid:#2b3037;
--pipe:#2c3138;--edge:#8b949e;--blk:#4b5563;--fm:#4f6b74;--box:#161a1f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px;margin:4px 0 12px}
h1{font-size:20px;margin:0;font-weight:600}
#status{font-size:14px;color:var(--mute);display:flex;align-items:center;gap:6px}
#status i{width:8px;height:8px;border-radius:50%;background:var(--mute)}
#status.live i{background:var(--ok)}#status.bad{color:var(--bad)}#status.bad i{background:var(--bad)}
.card{background:var(--card);border-radius:12px;padding:16px;margin-bottom:12px}
svg{width:100%;height:auto;display:block}
.pipe,.hose{fill:var(--pipe);stroke:var(--edge);stroke-width:2}
.box{fill:var(--box);stroke:var(--edge);stroke-width:2}
.blk{fill:var(--blk);stroke:var(--edge);stroke-width:2;transition:stroke .2s}
.blk.bad{stroke:var(--bad);stroke-width:4}
.fm{fill:var(--fm);stroke:var(--edge);stroke-width:2}
.dial{fill:none;stroke:#fff;stroke-opacity:.7;stroke-width:3}
.lead{stroke:var(--mute);stroke-width:1.5;fill:none}
.flow{fill:none;stroke:var(--l0);stroke-width:4;stroke-linecap:round;stroke-dasharray:2 26;opacity:0}
.flow.on{opacity:.8;animation:run 1.5s linear infinite}
@keyframes run{to{stroke-dashoffset:-28}}
.led{fill:var(--mute)}.led.live{fill:var(--ok)}.led.bad{fill:var(--bad)}
.name{fill:var(--mute);font-size:19px;letter-spacing:.06em}
.val{fill:var(--fg);font-size:44px;font-weight:600;font-variant-numeric:tabular-nums}
.val .u{fill:var(--mute);font-size:22px;font-weight:400}
.val.bad{fill:var(--bad)}
.dp{fill:var(--mute);font-size:20px;font-variant-numeric:tabular-nums}
#tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:12px}
#tiles .card{margin:0}
.tn{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--mute);margin-bottom:6px}
.tn i{width:10px;height:10px;border-radius:50%}
.big{font-size:40px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.1}
.big small{font-size:18px;color:var(--mute);font-weight:400;margin-left:4px}
.row{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;font-size:14px;color:var(--mute);font-variant-numeric:tabular-nums}
.fault{margin-top:6px;font-size:14px;color:var(--bad)}.fault:empty{display:none}
.pvh{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;font-size:14px;color:var(--mute);margin-bottom:8px}
.pvh b{color:var(--fg);font-weight:600;margin-right:6px}
.pv{max-height:320px;overflow:auto;border:1px solid var(--grid);border-radius:8px}
.pv table{border-collapse:collapse;width:100%;font:13px ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.pv th,.pv td{padding:4px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--grid)}
.pv th:nth-child(2),.pv td:nth-child(2){text-align:left}
.pv th{position:sticky;top:0;background:var(--card);color:var(--mute);font-weight:600}
.pv td.e{text-align:center;color:var(--mute);font-family:system-ui,sans-serif;padding:24px;white-space:normal}
.rec{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
button{font:inherit;font-size:15px;padding:8px 14px;border-radius:8px;border:1px solid var(--grid);background:var(--bg);color:var(--fg);cursor:pointer}
button:disabled{opacity:.45;cursor:default}
#recb.on{background:var(--bad);border-color:var(--bad);color:#fff}
#recinfo{color:var(--mute);font-size:14px;font-variant-numeric:tabular-nums}
</style></head><body><main>
<header><h1>Pressure rig</h1><div id="status"><i></i><span id="st">Connecting...</span></div></header>

<div class="card"><svg viewBox="0 0 1200 430" role="img" aria-label="Rig diagram with live readings">
 <rect id="tint" x="702" y="42" width="246" height="336" fill="var(--l1)" opacity="0"/>
 <rect class="box" x="700" y="40" width="250" height="340" rx="6" fill-opacity="0"/>
 <rect class="pipe" x="40" y="96" width="292" height="56" rx="3"/>
 <path class="hose" d="M520,82 C600,82 640,132 702,134 L702,170 L520,166 Z"/>
 <line id="f0" class="flow" x1="48" y1="124" x2="330" y2="124"/>
 <path id="f1" class="flow" d="M522,124 C600,124 640,150 700,152"/>
 <rect id="b0" class="blk" x="330" y="80" width="96" height="88" rx="4"/>
 <circle id="led0" class="led" cx="412" cy="94" r="6"/>
 <rect class="fm" x="424" y="80" width="96" height="88" rx="4"/>
 <circle class="dial" cx="472" cy="124" r="24"/><path class="dial" d="M472,124 L486,110"/>
 <rect id="b1" class="blk" x="776" y="216" width="100" height="84" rx="4"/>
 <circle id="led1" class="led" cx="862" cy="230" r="6"/>
 <text class="dp" x="612" y="58" text-anchor="middle">&Delta;P <tspan id="dp">--</tspan></text>

 <path class="lead" d="M372,168 L250,276"/><circle cx="250" cy="276" r="3" fill="var(--mute)"/>
 <rect x="40" y="290" width="14" height="14" rx="7" fill="var(--l0)"/>
 <text class="name" x="62" y="303">PRESSURE SENSOR 1</text>
 <text id="v0" class="val" x="40" y="354"><tspan id="p0">--</tspan><tspan class="u" id="u0"> psi</tspan></text>

 <path class="lead" d="M472,168 L452,322"/><circle cx="452" cy="322" r="3" fill="var(--mute)"/>
 <text class="name" x="452" y="350" text-anchor="middle">FLOW METER</text>
 <text class="val" x="452" y="398" text-anchor="middle"><tspan id="fv">--</tspan><tspan class="u" id="fu"> not wired</tspan></text>

 <path class="lead" d="M876,262 L968,290"/><circle cx="968" cy="290" r="3" fill="var(--mute)"/>
 <rect x="980" y="290" width="14" height="14" rx="7" fill="var(--l1)"/>
 <text class="name" x="1002" y="303">PRESSURE SENSOR 2</text>
 <text id="v1" class="val" x="980" y="354"><tspan id="p1">--</tspan><tspan class="u" id="u1"> psi</tspan></text>
</svg></div>

<div class="card rec"><button id="recb">&#9679; Record</button><button id="dl" disabled>Download CSV</button>
 <span id="recinfo">Press Record to start a run</span></div>

<div id="tiles">
 <div class="card" id="t0"><div class="tn"><i style="background:var(--l0)"></i>Sensor 1 &middot; inlet</div>
  <div class="big"><span class="psi">--</span><small>psi</small></div>
  <div class="row"><span class="bar">-- bar</span><span class="kpa">-- kPa</span><span class="v">-- V</span></div><div class="fault"></div></div>
 <div class="card" id="t1"><div class="tn"><i style="background:var(--l1)"></i>Sensor 2 &middot; chamber</div>
  <div class="big"><span class="psi">--</span><small>psi</small></div>
  <div class="row"><span class="bar">-- bar</span><span class="kpa">-- kPa</span><span class="v">-- V</span></div><div class="fault"></div></div>
 <div class="card"><div class="tn">Pressure drop (1 &minus; 2)</div>
  <div class="big"><span id="dpt">--</span><small>psi</small></div>
  <div class="row"><span id="zero">zero offsets --</span></div></div>
</div>

<div class="card"><div class="pvh"><span><b>CSV preview</b><span id="pvname"></span></span><span id="pvnote"></span></div>
 <div class="pv" id="pvbox"><table><thead><tr id="pvhead"></tr></thead><tbody id="pv"></tbody></table></div></div>
</main><script>
const $=id=>document.getElementById(id);
function status(t,cls){$('st').textContent=t;$('status').className=cls}
function sensor(i,s){
 const t=$('t'+i),q=c=>t.querySelector(c),bad=!s||!!s.fault;
 $('b'+i).classList.toggle('bad',!!(s&&s.fault));
 $('led'+i).setAttribute('class','led'+(s?(s.fault?' bad':' live'):''));
 $('v'+i).classList.toggle('bad',!!(s&&s.fault));
 $('p'+i).textContent=!s?'--':s.fault?'FAULT':s.psi.toFixed(2);
 $('u'+i).style.display=s&&s.fault?'none':'';
 q('.psi').textContent=bad?'--':s.psi.toFixed(2);
 q('.bar').textContent=(bad?'--':s.bar.toFixed(3))+' bar';
 q('.kpa').textContent=(bad?'--':s.kpa.toFixed(1))+' kPa';
 q('.v').textContent=(s?s.volts.toFixed(3):'--')+' V';
 q('.fault').textContent=s&&s.fault?'FAULT: '+s.fault:'';
 return bad?null:s.psi}
function rig(p){
 const ok=p[0]!=null&&p[1]!=null,dp=ok?p[0]-p[1]:null;
 $('dp').textContent=ok?dp.toFixed(2)+' psi':'--';$('dpt').textContent=ok?dp.toFixed(2):'--';
 // Animate flow while there is pressure at the inlet; faster for a bigger drop.
 const on=p[0]!=null&&p[0]>0.5,dur=Math.max(0.35,2.5-Math.abs(dp||0)/8)+'s';
 ['f0','f1'].forEach(id=>{const f=$(id);f.classList.toggle('on',on);f.style.animationDuration=dur});
 $('tint').setAttribute('opacity',p[1]==null?0:Math.min(p[1]/100,1)*0.35)}
// ---- Recorder: every board reading of a run, timed from the run's start ----
// Board rows are [seq, t_s since boot, s1_psi, s1_volts, s2_psi, s2_volts].
let last=null,rec=false,run=0,rows=[],t0=null,wall0=0,missed=0,saved=true,note='';
const COLS=['t_s','clock_time','s1_psi','s1_volts','s2_psi','s2_volts','dp_psi'],PV_MAX=500;
function csvRow(r){const f=(v,n)=>v==null?'':v.toFixed(n),t=r[1]-t0;
 return [t.toFixed(3),stamp(wall0+t*1000,1),f(r[2],2),f(r[3],3),f(r[4],2),f(r[5],3),
  r[2]!=null&&r[4]!=null?(r[2]-r[4]).toFixed(2):'']}
function fname(){return 'run'+run+'-'+stamp(wall0)+'.csv'}
// The preview shows exactly what the download will contain (newest PV_MAX rows).
$('pvhead').innerHTML=COLS.map(c=>'<th>'+c+'</th>').join('');
function pvEmpty(msg){$('pv').innerHTML='<tr><td class="e" colspan="'+COLS.length+'">'+msg+'</td></tr>'}
function pvAdd(list){
 const box=$('pvbox'),tb=$('pv'),stick=box.scrollTop+box.clientHeight>=box.scrollHeight-8;
 if(tb.querySelector('.e'))tb.innerHTML='';
 list.forEach(r=>{const tr=document.createElement('tr');
  csvRow(r).forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.appendChild(td)});tb.appendChild(tr)});
 while(tb.children.length>PV_MAX)tb.firstChild.remove();
 if(stick)box.scrollTop=box.scrollHeight}  // follow new rows unless scrolled up
pvEmpty('No run yet - press Record and rows will appear here as they are recorded');
function recUI(){
 $('pvname').textContent=rows.length?fname():'';
 $('pvnote').textContent=!rows.length?'':rows.length>PV_MAX?'showing last '+PV_MAX+' of '+rows.length+' rows':rows.length+' rows';
 const b=$('recb');b.classList.toggle('on',rec);b.innerHTML=rec?'&#9632; Stop':'&#9679; Record';
 $('dl').disabled=rec||!rows.length;
 const dur=rows.length?rows[rows.length-1][1]-t0:0;
 $('recinfo').textContent=!run?(note||'Press Record to start a run'):
  'Run '+run+(rec?' recording':saved?' saved':' stopped')+' - '+dur.toFixed(1)+' s - '+rows.length+' rows'+
  (missed?' - '+missed+' readings missed':'')+(note?' - '+note:'')}
$('recb').onclick=()=>{
 if(rec){rec=false;recUI();return}
 if(!saved&&rows.length&&!confirm('Run '+run+' has not been downloaded. Discard it?'))return;
 rows=[];t0=null;missed=0;note='';run++;rec=true;saved=false;
 pvEmpty('Recording - waiting for the first reading...');recUI()};
function collect(d){
 if(last!=null&&d.seq<last){  // board restarted: its clock and counter reset
  if(rec){rec=false;note='board restarted, run stopped'}
  last=null}
 if(rec&&d.rows&&last!=null){
  if(d.rows.length&&d.rows[0][0]>last+1)missed+=d.rows[0][0]-last-1;  // fell out of the board's buffer
  d.rows.forEach(r=>{if(t0==null){t0=r[1];wall0=Date.now()-(d.t-r[1])*1000}rows.push(r)});
  if(d.rows.length)pvAdd(d.rows)}
 last=d.seq}
function stamp(ms,sep){const d=new Date(ms),p=(n,w)=>String(n).padStart(w||2,'0');
 return sep?d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds())+'.'+p(d.getMilliseconds(),3)
  :''+d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+'-'+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds())}
$('dl').onclick=()=>{
 let out=COLS.join(',')+'\n';
 rows.forEach(r=>{out+=csvRow(r).join(',')+'\n'});
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([out],{type:'text/csv'}));
 a.download=fname();a.click();URL.revokeObjectURL(a.href);saved=true;recUI()};
addEventListener('beforeunload',e=>{if(rec||(!saved&&rows.length)){e.preventDefault();e.returnValue=''}});

async function tick(){
 try{const d=await (await fetch(last==null?'/data':'/data?since='+last,{cache:'no-store'})).json(),s=d.sensors;
  collect(d);
  const p=[sensor(0,s[0]),sensor(1,s[1])];rig(p);
  if(d.flow!=null){$('fv').textContent=d.flow.toFixed(2);$('fu').textContent=' '+(d.flow_unit||'L/min')}
  $('zero').textContent='zero offsets '+s.map(x=>x.zero.toFixed(3)+' V').join(' / ');
  s.some(x=>x.fault)?status('Live - sensor fault','bad'):status('Live','live')
 }catch(e){status('Lost connection to sensors','bad');sensor(0,null);sensor(1,null);rig([null,null])}
 recUI();setTimeout(tick,500)}
tick();
</script></body></html>"""


async def handle_http(reader, writer):
    try:
        request = (await reader.readline()).split()
        while True:  # skip the request headers
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
        path, _, query = (request[1] if len(request) > 1 else b"/").partition(b"?")

        if path == b"/":
            status, ctype, body = "200 OK", "text/html; charset=utf-8", PAGE
        elif path == b"/data":
            # /data?since=N also returns every buffered reading after seq N
            data = dict(latest)
            if query.startswith(b"since="):
                try:
                    since = int(query[6:])
                except ValueError:
                    since = seq
                data["rows"] = [r for r in recent if r[0] > since]
            status, ctype, body = "200 OK", "application/json", json.dumps(data)
        else:
            status, ctype, body = "404 Not Found", "text/plain", "Not found"

        body = body.encode()
        writer.write("HTTP/1.0 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n"
                     "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                     .format(status, ctype, len(body)).encode())
        writer.write(body)
        await writer.drain()
    except OSError:
        pass  # client went away mid-request
    finally:
        writer.close()
        await writer.wait_closed()


async def run(ip):
    if ip:
        await asyncio.start_server(handle_http, "0.0.0.0", HTTP_PORT)
    await sensor_loop()


def main():
    global start_ms

    # Join WiFi before zeroing, so the zero is taken with the radio running.
    ip = connect_wifi()

    if AUTO_ZERO:
        time.sleep_ms(500)  # let the sensors settle after power-up
        for i, adc in enumerate(adcs):
            v = read_sensor_voltage(adc)
            # Only trust the zero if it's close to the expected 0.5V
            if 0.3 < v < 0.8:
                zero_offsets_v[i] = v - V_MIN
            print("Sensor {} zero offset (V): {:.3f}".format(i + 1, zero_offsets_v[i]))

    start_ms = time.ticks_ms()  # t_s counts from here
    take_reading()  # so /data has a value before the first request
    asyncio.run(run(ip))


main()
