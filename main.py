"""
100 PSI pressure transducer -> Seeed XIAO ESP32C3 (MicroPython)

Sensor: 3-wire, 5V supply, 0.5V (0 psi) .. 4.5V (100 psi) output.
  Red    -> XIAO 5V pin  (live only when powered over USB / 5V)
  Black  -> XIAO GND
  Signal -> voltage divider -> XIAO A0 (GPIO2)

The ESP32C3 ADC reads accurately only up to ~2.5V, so halve the signal:

    signal ---[ R1 = 10k ]---+---[ R2 = 10k ]--- GND
                             |
                         A0 (GPIO2)

4.5V becomes 2.25V, and 5V (fault) becomes 2.5V.
A 0.1uF capacitor from A0 to GND helps reduce noise.

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
SENSOR_PIN = 2          # XIAO ESP32C3 A0 = GPIO2
R1 = 10_000             # ohms, signal -> A0
R2 = 10_000             # ohms, A0 -> GND
V_MIN = 0.5             # sensor output at 0 psi (volts)
V_MAX = 4.5             # sensor output at full scale (volts)
P_MAX_PSI = 100.0       # sensor full-scale range
SAMPLES = 32            # readings averaged per measurement
PERIOD_MS = 250         # time between readings

# Measure the 0 psi offset at boot.
# The sensor must be open to the air (no pressure) when the board starts.
AUTO_ZERO = True

HOSTNAME = "pressure-sensor"
HTTP_PORT = 80
WIFI_TIMEOUT_S = 20

adc = ADC(Pin(SENSOR_PIN), atten=ADC.ATTN_11DB)  # ~0-2.5V range
zero_offset_v = 0.0
latest = {}             # most recent reading, served at /data


def read_sensor_voltage():
    """Sensor output voltage, averaged, with the divider undone."""
    total = 0
    for _ in range(SAMPLES):
        total += adc.read_uv()  # factory-calibrated microvolts
        time.sleep_us(200)
    pin_v = total / SAMPLES / 1_000_000
    return pin_v * (R1 + R2) / R2


def voltage_to_psi(v):
    psi = (v - V_MIN - zero_offset_v) * P_MAX_PSI / (V_MAX - V_MIN)
    return max(psi, 0.0)  # hide small negative readings caused by noise


def connect_wifi():
    """Join the network from secrets.py. Returns the IP, or None."""
    if secrets is None:
        print("No secrets.py on board - WiFi off, serial only")
        return None
    network.hostname(HOSTNAME)
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        deadline = time.time() + WIFI_TIMEOUT_S
        while not wlan.isconnected() and time.time() < deadline:
            time.sleep_ms(250)
    if not wlan.isconnected():
        print("WiFi failed (status {}) - serial only".format(wlan.status()))
        return None
    ip = wlan.ifconfig()[0]
    print("WiFi connected: http://{}/".format(ip))
    return ip


def take_reading():
    """Read the sensor, update `latest`, and print a CSV line."""
    v = read_sensor_voltage()
    # Under ~0.2V usually means a broken signal wire or no 5V;
    # above ~4.8V usually means a short or a faulty sensor.
    if v < 0.2:
        fault = "signal too low - check wiring / 5V supply"
    elif v > 4.8:
        fault = "signal too high - check sensor"
    else:
        fault = None

    if fault:
        print("FAULT: " + fault)
        latest.update(volts=round(v, 3), psi=None, bar=None, kpa=None,
                      fault=fault)
    else:
        psi = voltage_to_psi(v)
        bar, kpa = psi * 0.0689476, psi * 6.89476
        print("{:.3f},{:.2f},{:.3f},{:.1f}".format(v, psi, bar, kpa))
        latest.update(volts=round(v, 3), psi=round(psi, 2),
                      bar=round(bar, 3), kpa=round(kpa, 1), fault=None)
    latest["zero"] = round(zero_offset_v, 3)


async def sensor_loop():
    while True:
        take_reading()
        await asyncio.sleep_ms(PERIOD_MS)


# ---------- Web page ----------
# The page polls /data twice a second and plots the last two minutes.
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pressure Sensor</title><style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#15181c;--mute:#646d77;--line:#2563eb;--bad:#c62828;--grid:#e3e6ea}
@media(prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b1f24;--fg:#e8eaed;--mute:#9aa3ad;--line:#6ea8ff;--bad:#ff6b6b;--grid:#2b3037}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
main{max-width:520px;margin:0 auto;padding:16px}
.card{background:var(--card);border-radius:12px;padding:20px;margin-bottom:12px}
.psi{font-size:64px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}
.unit{font-size:20px;color:var(--mute);margin-left:6px}
.row{display:flex;flex-wrap:wrap;gap:16px;margin-top:12px;color:var(--mute);font-variant-numeric:tabular-nums}
#status{font-size:14px;color:var(--mute)}#status.bad{color:var(--bad)}
canvas{width:100%;height:160px;display:block}
</style></head><body><main>
<div class="card"><div><span class="psi" id="psi">--</span><span class="unit">psi</span></div>
<div class="row"><span id="bar">-- bar</span><span id="kpa">-- kPa</span><span id="v">-- V</span></div></div>
<div class="card"><canvas id="c"></canvas></div>
<div id="status">Connecting...</div>
</main><script>
const N=240,hist=[],$=id=>document.getElementById(id);
function draw(){
 const c=$('c'),d=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;
 c.width=w*d;c.height=h*d;const g=c.getContext('2d');g.scale(d,d);
 const cs=getComputedStyle(document.documentElement),col=n=>cs.getPropertyValue(n);
 const hi=Math.max(10,...hist.filter(x=>x!=null))*1.1;
 g.strokeStyle=col('--grid');g.lineWidth=1;
 for(let i=0;i<=4;i++){const y=h-h*i/4+.5;g.beginPath();g.moveTo(0,y);g.lineTo(w,y);g.stroke()}
 g.fillStyle=col('--mute');g.font='11px system-ui';g.fillText(hi.toFixed(0)+' psi',4,12);
 g.strokeStyle=col('--line');g.lineWidth=2;g.beginPath();let pen=false;
 hist.forEach((p,i)=>{if(p==null){pen=false;return}
  const x=w*i/(N-1),y=h-h*p/hi;pen?g.lineTo(x,y):g.moveTo(x,y);pen=true});
 g.stroke()}
function status(t,bad){const s=$('status');s.textContent=t;s.className=bad?'bad':''}
async function tick(){
 try{const d=await (await fetch('/data',{cache:'no-store'})).json();
  $('v').textContent=d.volts.toFixed(3)+' V';
  if(d.fault){$('psi').textContent='--';status('FAULT: '+d.fault,1);hist.push(null)}
  else{$('psi').textContent=d.psi.toFixed(2);
   $('bar').textContent=d.bar.toFixed(3)+' bar';$('kpa').textContent=d.kpa.toFixed(1)+' kPa';
   status('Live - zero offset '+d.zero.toFixed(3)+' V');hist.push(d.psi)}
 }catch(e){status('Lost connection to sensor',1);hist.push(null)}
 if(hist.length>N)hist.shift();draw();setTimeout(tick,500)}
tick();
</script></body></html>"""


async def handle_http(reader, writer):
    try:
        request = (await reader.readline()).split()
        while True:  # skip the request headers
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
        path = request[1] if len(request) > 1 else b"/"

        if path == b"/":
            status, ctype, body = "200 OK", "text/html; charset=utf-8", PAGE
        elif path == b"/data":
            status, ctype, body = "200 OK", "application/json", json.dumps(latest)
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
    global zero_offset_v

    # Join WiFi before zeroing, so the zero is taken with the radio running.
    ip = connect_wifi()

    if AUTO_ZERO:
        time.sleep_ms(500)  # let the sensor settle after power-up
        v = read_sensor_voltage()
        # Only trust the zero if it's close to the expected 0.5V
        if 0.3 < v < 0.8:
            zero_offset_v = v - V_MIN
        print("Zero offset (V): {:.3f}".format(zero_offset_v))

    print("volts,psi,bar,kPa")
    take_reading()  # so /data has a value before the first request
    asyncio.run(run(ip))


main()
