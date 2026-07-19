import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from arduino.app_utils import Bridge
except ImportError:
    Bridge = None


def load_local_env() -> None:
    candidates = [
        Path(os.getcwd()) / ".env",
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path("/app/.env"),
        Path("/home/arduino/ArduinoApps/led-matrix-voice-lab/.env"),
    ]
    env_path = next((path for path in candidates if path.exists()), None)
    if env_path is None:
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


load_local_env()

PORT = int(os.getenv("MATRIX_APP_PORT", "5002"))
MAX_TEXT_LENGTH = 80
last_text = "HELLO ARDUINO"
last_bridge_error = ""
last_scroll_enabled = True
last_speed_ms = 150
current_mode_comm = False  # Track toggle state globally


def clean_matrix_text(value: str) -> str:
    text = " ".join(value.strip().split())
    text = re.sub(r"[^A-Za-z0-9 .,!?@#:+\\-_/]", "", text)
    return text[:MAX_TEXT_LENGTH].upper()


def clamp_speed(value: object) -> int:
    try:
        speed = int(value)
    except (TypeError, ValueError):
        speed = 150
    return max(90, min(320, speed))


def bridge_call(method: str, value: object) -> tuple[bool, str]:
    if Bridge is None:
        return False, "Bridge is available only when this runs in Arduino App Lab on Arduino Uno Q."
    try:
        result = Bridge.call(method, str(value))
        return True, str(result)
    except Exception as exc:
        return False, str(exc)


def send_display_state(text: str, scroll_enabled: bool, speed_ms: int) -> tuple[bool, str]:
    if Bridge is None:
        return False, "Bridge is available only when this runs in Arduino App Lab on Arduino Uno Q."
    calls = [
        bridge_call("set_scroll", "1" if scroll_enabled else "0"),
        bridge_call("set_speed", speed_ms),
        bridge_call("display_text", text),
    ]
    sent = all(ok for ok, _message in calls)
    messages = [message for _ok, message in calls if message]
    return sent, "; ".join(messages)


def send_settings(scroll_enabled: bool, speed_ms: int) -> tuple[bool, str]:
    if Bridge is None:
        return False, "Bridge is available only when this runs in Arduino App Lab on Arduino Uno Q."
    calls = [
        bridge_call("set_scroll", "1" if scroll_enabled else "0"),
        bridge_call("set_speed", speed_ms),
    ]
    sent = all(ok for ok, _message in calls)
    messages = [message for _ok, message in calls if message]
    return sent, "; ".join(messages)


class MatrixHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            self.send_text(HTML, "text/html; charset=utf-8")
            return
            
        # Route to serve the local logo asset from the same working directory
        if self.path == "/logo.png":
            logo_path = Path(os.getcwd()) / "logo.png"
            if not logo_path.exists():
                logo_path = Path(__file__).resolve().parent / "logo.png"
            
            if logo_path.exists():
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    with open(logo_path, "rb") as f:
                        self.wfile.write(f.read())
                    return
                except OSError:
                    pass
            self.send_error(404)
            return

        if self.path == "/health":
            global last_text
            if current_mode_comm and Bridge is not None:
                try:
                    incoming = Bridge.call("get_last_text", "")
                    if incoming and incoming.strip():
                        last_text = clean_matrix_text(incoming)
                except Exception:
                    pass

            self.send_json(
                {
                    "ok": True,
                    "matrix": "Arduino Uno Q built-in 13x8",
                    "bridge_available": Bridge is not None,
                    "last_text": last_text,
                    "scroll_enabled": last_scroll_enabled,
                    "speed_ms": last_speed_ms,
                    "comm_mode_active": current_mode_comm,
                    "last_bridge_error": last_bridge_error,
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        global last_text, last_bridge_error, last_scroll_enabled, last_speed_ms, current_mode_comm
        if self.path not in {"/display", "/settings", "/mode"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self.send_json({"ok": False, "error": "Could not read request."})
            return

        if self.path == "/mode":
            current_mode_comm = bool(data.get("comm_mode", False))
            val_to_send = "1" if current_mode_comm else "0"
            sent, bridge_message = bridge_call("set_mode", val_to_send)
            self.send_json({"ok": True, "comm_mode_active": current_mode_comm, "bridge_message": bridge_message})
            return

        scroll_enabled = bool(data.get("scroll_enabled", last_scroll_enabled))
        speed_ms = clamp_speed(data.get("speed_ms", last_speed_ms))
        last_scroll_enabled = scroll_enabled
        last_speed_ms = speed_ms

        if self.path == "/settings":
            sent, bridge_message = send_settings(scroll_enabled, speed_ms)
            last_bridge_error = "" if sent else bridge_message
            self.send_json(
                {
                    "ok": True,
                    "sent_to_matrix": sent,
                    "scroll_enabled": scroll_enabled,
                    "speed_ms": speed_ms,
                    "bridge_message": bridge_message,
                }
            )
            return

        raw_text = data.get("text", "")
        text = clean_matrix_text(raw_text)
        if not text:
            text = " "

        last_text = text
        sent, bridge_message = send_display_state(text, scroll_enabled, speed_ms)
        last_bridge_error = "" if sent else bridge_message
        self.send_json(
            {
                "ok": True,
                "text": text,
                "sent_to_matrix": sent,
                "scroll_enabled": scroll_enabled,
                "speed_ms": speed_ms,
                "bridge_message": bridge_message,
            }
        )

    def send_json(self, data: dict) -> None:
        self.send_text(json.dumps(data), "application/json; charset=utf-8")

    def send_text(self, text: str, content_type: str) -> None:
        encoded = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), MatrixHandler)
    print(f"Sanchaar Mitra Lab is running on http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sanchaar Mitra Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Arial, Helvetica, sans-serif;
      background: #0A1118;
      color: #E2E8F0;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(90deg, rgba(0, 229, 255, 0.02) 1px, transparent 1px),
        linear-gradient(180deg, rgba(0, 229, 255, 0.02) 1px, transparent 1px),
        linear-gradient(135deg, #0A1118 0%, #121E2B 100%);
      background-size: 28px 28px, 28px 28px, auto;
    }
    
    header {
      width: 100%;
      max-width: 100%;
      margin: 0;
      padding: 35px 40px 0 40px;
      display: flex;
      justify-content: flex-start;
      align-items: center;
    }
    
    .header-brand-group {
      display: flex;
      align-items: center;
      gap: 35px;
    }
    
    .brand-logo {
      height: auto;
      max-height: 160px;
      width: auto;
      max-width: 75vw;
      object-fit: contain;
      filter: drop-shadow(0 0 35px rgba(0, 229, 255, 0.3));
      transition: transform 0.3s ease;
    }
    .brand-logo:hover {
      transform: scale(1.01);
    }
    
    .hackathon-tag {
      font-weight: 800;
      font-size: 1.35rem;
      letter-spacing: 0.5px;
      color: #F8FAFC;
      text-transform: uppercase;
      white-space: nowrap;
    }

    main {
      width: min(1080px, calc(100vw - 28px));
      margin: 0 auto;
      display: grid;
      grid-template-columns: minmax(310px, 0.86fr) minmax(320px, 1fr);
      align-items: center;
      gap: 35px;
      padding: 0px 0 40px 0;
    }
    .controls, .preview {
      display: grid;
      gap: 20px;
    }
    h1 {
      margin: 0;
      font-size: clamp(2.2rem, 4.5vw, 4.3rem);
      line-height: 1.05;
      letter-spacing: -0.5px;
      color: #00E5FF;
      text-shadow: 0 0 20px rgba(0, 229, 255, 0.2);
      white-space: nowrap;
    }
    .subtitle {
      margin: 0;
      font-size: 1.05rem;
      line-height: 1.5;
      color: #94A3B8;
    }
    
    .mode-box {
      background: rgba(20, 35, 51, 0.7);
      padding: 16px;
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border: 1px solid rgba(0, 229, 255, 0.15);
      backdrop-filter: blur(8px);
    }
    .switch-label { font-weight: bold; color: #F8FAFC; }
    .switch { position: relative; display: inline-block; width: 62px; height: 32px; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #334155; transition: .3s; border-radius: 32px; }
    .slider:before { position: absolute; content: ""; height: 24px; width: 24px; left: 4px; bottom: 4px; background-color: #F8FAFC; transition: .3s; border-radius: 50%; }
    input:checked + .slider { background-color: #00E5FF; }
    input:checked + .slider:before { transform: translateX(30px); background-color: #0A1118; }

    label {
      font-weight: 700;
      color: #CBD5E1;
      text-transform: uppercase;
      font-size: 0.85rem;
      letter-spacing: 0.5px;
    }
    textarea {
      width: 100%;
      min-height: 120px;
      resize: vertical;
      padding: 16px;
      border: 2px solid rgba(0, 229, 255, 0.2);
      border-radius: 10px;
      font: inherit;
      font-size: 1.12rem;
      background: rgba(15, 23, 42, 0.8);
      color: #F8FAFC;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
      transition: border-color 0.2s;
    }
    textarea:focus {
      outline: none;
      border-color: #00E5FF;
    }
    .buttons {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    button {
      border: 0;
      border-radius: 8px;
      min-height: 54px;
      padding: 12px 14px;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
      color: #0A1118;
      background: #00E5FF;
      box-shadow: 0 8px 20px rgba(0, 229, 255, 0.15);
      transition: all 0.2s;
    }
    button.warn { 
      background: #EF4444; 
      color: #F8FAFC;
      box-shadow: 0 8px 20px rgba(239, 68, 68, 0.15);
    }
    button:hover { 
      filter: brightness(1.1);
      transform: translateY(-1px);
    }
    button:disabled {
      opacity: 0.4;
      cursor: wait;
      transform: none;
    }
    .status {
      min-height: 52px;
      padding: 14px;
      border-left: 5px solid #007BFF;
      background: rgba(0, 123, 255, 0.1);
      color: #93C5FD;
      border-radius: 6px;
      font-weight: 600;
      line-height: 1.4;
    }
    .matrix-shell {
      background: #090F14;
      border: 8px solid #1E293B;
      border-radius: 12px;
      padding: clamp(12px, 2.4vw, 22px);
      box-shadow: 0 30px 60px rgba(0, 0, 0, 0.5), inset 0 0 20px rgba(0,0,0,0.6);
      overflow: hidden;
    }
    .matrix {
      display: grid;
      grid-template-columns: repeat(13, minmax(14px, 1fr));
      grid-template-rows: repeat(8, minmax(14px, 1fr));
      gap: clamp(5px, 1vw, 10px);
      aspect-ratio: 13 / 8;
    }
    .dot {
      border-radius: 50%;
      background: #1E293B;
      box-shadow: inset 0 0 4px rgba(0, 0, 0, 0.5);
    }
    .dot.on {
      background: #00E5FF;
      box-shadow: 0 0 12px rgba(0, 229, 255, 0.95), 0 0 22px rgba(0, 229, 255, 0.4);
    }
    .readout {
      min-height: 58px;
      padding: 14px;
      border: 2px solid rgba(255, 255, 255, 0.08);
      background: rgba(15, 23, 42, 0.6);
      border-radius: 10px;
      font-size: clamp(1.2rem, 3vw, 1.85rem);
      font-weight: 900;
      color: #00E5FF;
      overflow-wrap: anywhere;
      letter-spacing: 1px;
    }
    .toggles {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      color: #94A3B8;
      font-weight: 700;
    }
    input[type="range"] {
      width: min(240px, 100%);
      accent-color: #00E5FF;
    }
    @media (max-width: 820px) {
      header {
        padding: 20px 20px 0 20px;
      }
      .header-brand-group {
        gap: 16px;
        flex-direction: column;
        align-items: flex-start;
      }
      .brand-logo {
        max-height: 80px;
      }
      main {
        grid-template-columns: 1fr;
        align-content: start;
        padding-top: 10px;
      }
      .buttons {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-brand-group">
      <img src="/logo.png" alt="Sanchaar Mitra Logo" class="brand-logo" onerror="this.style.display='none';">
      <div class="hackathon-tag">Multiverse Hackathon</div>
    </div>
  </header>

  <main>
    <section class="controls">
      <h1>Sanchaar Mitra</h1>
      
      <!-- Mode Switch Widget -->
      <div class="mode-box">
        <span class="switch-label" id="modeTitleText">🔄 Mode: Reverse Communication</span>
        <label class="switch">
          <input type="checkbox" id="modeSwitcher">
          <span class="slider"></span>
        </label>
      </div>

      <p class="subtitle">Sanchaar Mitra is an edge-powered, standalone assistive wearable device designed to translate sign language gestures into real-time text and speech pipelines, bridging communication gaps seamlessly.</p>
      
      <label for="textInput">Word to display</label>
      <textarea id="textInput" maxlength="80">HELLO ARDUINO</textarea>
      
      <div class="buttons">
        <button id="talkButton" title="Use microphone">Talk</button>
        <button id="sendButton" title="Display typed word">Display</button>
        <button id="clearButton" class="warn" title="Clear matrix">Clear</button>
      </div>
      <div class="status" id="status">Ready.</div>
    </section>

    <section class="preview">
      <div class="matrix-shell" aria-label="Arduino Uno Q 13 by 8 LED matrix preview">
        <div class="matrix" id="matrix"></div>
      </div>
      <div class="readout" id="readout">HELLO ARDUINO</div>
      <div class="toggles">
        <label><input type="checkbox" id="scrollToggle" checked> Scroll preview</label>
        <label for="speed">Speed</label>
        <input type="range" id="speed" min="90" max="320" value="150">
      </div>
    </section>
  </main>

  <script>
    const COLS = 13;
    const ROWS = 8;
    const matrix = document.getElementById("matrix");
    const textInput = document.getElementById("textInput");
    const readout = document.getElementById("readout");
    const statusBox = document.getElementById("status");
    const talkButton = document.getElementById("talkButton");
    const sendButton = document.getElementById("sendButton");
    const clearButton = document.getElementById("clearButton");
    const scrollToggle = document.getElementById("scrollToggle");
    const speed = document.getElementById("speed");
    const modeSwitcher = document.getElementById("modeSwitcher");
    const modeTitleText = document.getElementById("modeTitleText");
    const dots = [];
    let columns = [];
    let offset = 0;
    let timer = null;
    let settingsTimer = null;
    let commPollingInterval = null;

    const FONT = {
      " ": ["00000","00000","00000","00000","00000","00000","00000"],
      "0": ["01110","10001","10011","10101","11001","10001","01110"],
      "1": ["00100","01100","00100","00100","00100","00100","01110"],
      "2": ["01110","10001","00001","00010","00100","01000","11111"],
      "3": ["11110","00001","00001","01110","00001","00001","11110"],
      "4": ["00010","00110","01010","10010","11111","00010","00010"],
      "5": ["11111","10000","11110","00001","00001","10001","01110"],
      "6": ["00110","01000","10000","11110","10001","10001","01110"],
      "7": ["11111","00001","00010","00100","01000","01000","01000"],
      "8": ["01110","10001","10001","01110","10001","10001","01110"],
      "9": ["01110","10001","10001","01111","00001","00010","11100"],
      "A": ["01110","10001","10001","11111","10001","10001","10001"],
      "B": ["11110","10001","10001","11110","10001","10001","11110"],
      "C": ["01111","10000","10000","10000","10000","10000","01111"],
      "D": ["11110","10001","10001","10001","10001","10001","11110"],
      "E": ["11111","10000","10000","11110","10000","10000","11111"],
      "F": ["11111","10000","10000","11110","10000","10000","10000"],
      "G": ["01111","10000","10000","10011","10001","10001","01111"],
      "H": ["10001","10001","10001","11111","10001","10001","10001"],
      "I": ["01110","00100","00100","00100","00100","00100","01110"],
      "J": ["00111","00010","00010","00010","00010","10010","01100"],
      "K": ["10001","10010","10100","11000","10100","10010","10001"],
      "L": ["10000","10000","10000","10000","10000","10000","11111"],
      "M": ["10001","11011","10101","10101","10001","10001","10001"],
      "N": ["10001","11001","10101","10011","10001","10001","10001"],
      "O": ["01110","10001","10001","10001","10001","10001","01110"],
      "P": ["11110","10001","10001","11110","10000","10000","10000"],
      "Q": ["01110","10001","10001","10001","10101","10010","01101"],
      "R": ["11110","10001","10001","11110","10100","10010","10001"],
      "S": ["01111","10000","10000","01110","00001","00001","11110"],
      "T": ["11111","00100","00100","00100","00100","00100","00100"],
      "U": ["10001","10001","10001","10001","10001","10001","01110"],
      "V": ["10001","10001","10001","10001","10001","01010","00100"],
      "W": ["10001","10001","10001","10101","10101","10101","01010"],
      "X": ["10001","10001","01010","00100","01010","10001","10001"],
      "Y": ["10001","10001","01010","00100","00000","00000","00000"],
      "Z": ["11111","00001","00010","00100","01000","10000","11111"],
      ".": ["00000","00000","00000","00000","00000","01100","01100"],
      ",": ["00000","00000","00000","00000","01100","01100","01000"],
      "!": ["00100","00100","00100","00100","00100","00000","00100"],
      "?": ["01110","10001","00001","00010","00100","00000","00100"],
      ":": ["00000","01100","01100","00000","01100","01100","00000"],
      "-": ["00000","00000","00000","11111","00000","00000","00000"],
      "_": ["00000","00000","00000","00000","00000","00000","11111"],
      "/": ["00001","00010","00010","00100","01000","01000","10000"],
      "@": ["01110","10001","10111","10101","10111","10000","01110"],
      "#": ["01010","01010","11111","01010","11111","01010","01010"],
      "+": ["00000","00100","00100","11111","00100","00100","00000"]
    };

    for (let i = 0; i < ROWS * COLS; i += 1) {
      const dot = document.createElement("span");
      dot.className = "dot";
      matrix.appendChild(dot);
      dots.push(dot);
    }

    function cleanText(value) {
      return value.trim().replace(/\s+/g, " ").replace(/[^A-Za-z0-9 .,!?@#:+\-_/]/g, "").slice(0, 80).toUpperCase();
    }

    function buildColumns(text) {
      const result = Array(COLS).fill(0);
      for (const char of text || " ") {
        const glyph = FONT[char] || FONT["?"];
        for (let x = 0; x < 5; x += 1) {
          let column = 0;
          for (let y = 0; y < 7; y += 1) {
            if (glyph[y][x] === "1") column |= 1 << (y + 1);
          }
          result.push(column);
        }
        result.push(0);
      }
      return result.concat(Array(COLS).fill(0));
    }

    function draw() {
      for (let x = 0; x < COLS; x += 1) {
        const source = columns[(offset + x) % columns.length] || 0;
        for (let y = 0; y < ROWS; y += 1) {
          dots[y * COLS + x].classList.toggle("on", Boolean(source & (1 << y)));
        }
      }
    }

    function refreshPreview(reset = false) {
      const text = cleanText(textInput.value) || " ";
      readout.textContent = text.trim() || " ";
      columns = buildColumns(text);
      if (reset) offset = 0;
      draw();
    }

    function startScroll() {
      if (timer) window.clearInterval(timer);
      timer = window.setInterval(() => {
        if (scrollToggle.checked && columns.length) {
          offset = (offset + 1) % columns.length;
          draw();
        }
      }, Number(speed.value));
    }

    function currentSettings() {
      return {
        scroll_enabled: scrollToggle.checked,
        speed_ms: Number(speed.value)
      };
    }

    function scheduleHardwareSettingsSync() {
      if (settingsTimer) window.clearTimeout(settingsTimer);
      settingsTimer = window.setTimeout(syncHardwareSettings, 220);
    }

    async function syncHardwareSettings() {
      const settings = currentSettings();
      try {
        const response = await fetch("/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(settings)
        });
        const data = await response.json();
        statusBox.textContent = data.sent_to_matrix
          ? `Hardware updated: scroll ${settings.scroll_enabled ? "on" : "off"}, speed ${settings.speed_ms} ms.`
          : `Preview updated. ${data.bridge_message}`;
      } catch (error) {
        statusBox.textContent = `Settings connection problem: ${error}`;
      }
    }

    async function sendDisplayText(text) {
      const savedState = { sendDisabled: sendButton.disabled, talkDisabled: talkButton.disabled };
      sendButton.disabled = true;
      talkButton.disabled = true;
      
      // Update status string contextually depending on active mode state
      if (!modeSwitcher.checked) {
          statusBox.textContent = "Sending to Arduino Uno Q matrix...";
      }
      
      try {
        const response = await fetch("/display", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, ...currentSettings() })
        });
        const data = await response.json();
        textInput.value = data.text;
        refreshPreview(true);
        
        if (modeSwitcher.checked) {
            statusBox.textContent = `Streaming live sign language text from hardware pipeline...`;
        } else {
            statusBox.textContent = data.sent_to_matrix
              ? `Displaying on Arduino Uno Q: ${data.text}`
              : `Previewing: ${data.text}. ${data.bridge_message}`;
        }
      } catch (error) {
        statusBox.textContent = `App connection problem: ${error}`;
      } finally {
        // Re-lock fields explicitly if still sitting in automated hardware tracking mode
        if (modeSwitcher.checked) {
            textInput.disabled = sendButton.disabled = talkButton.disabled = clearButton.disabled = true;
        } else {
            sendButton.disabled = savedState.sendDisabled;
            talkButton.disabled = savedState.talkDisabled;
        }
      }
    }

    async function pollHardwareState() {
      try {
        const response = await fetch("/health");
        const data = await response.json();
        if (data.comm_mode_active) {
          // Check if the serial string incoming from the STM32 route has updated
          if (textInput.value !== data.last_text) {
            // FIX: Feeds incoming COM port string directly to Reverse Communication processing routine
            await sendDisplayText(data.last_text);
          }
        }
      } catch (error) {
        console.error("Hardware polling tracking error:", error);
      }
    }

    async function toggleMode() {
      const isCommMode = modeSwitcher.checked;
      if (isCommMode) {
        modeTitleText.textContent = "🖐️ Mode: Communication Mode";
        textInput.disabled = sendButton.disabled = talkButton.disabled = clearButton.disabled = true;
        statusBox.textContent = "Locked. Running on Serial pipeline...";
        
        if (commPollingInterval) window.clearInterval(commPollingInterval);
        commPollingInterval = window.setInterval(pollHardwareState, 300);
      } else {
        modeTitleText.textContent = "🔄 Mode: Reverse Communication";
        textInput.disabled = sendButton.disabled = talkButton.disabled = clearButton.disabled = false;
        statusBox.textContent = "Ready.";
        
        if (commPollingInterval) {
          window.clearInterval(commPollingInterval);
          commPollingInterval = null;
        }
      }

      try {
        await fetch("/mode", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ comm_mode: isCommMode })
        });
      } catch (error) {
        statusBox.textContent = `Mode sync failure: ${error}`;
      }
    }

    function startListening() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        statusBox.textContent = "Speech recognition is not available here. Type the word and press Display.";
        return;
      }
      const recognition = new SpeechRecognition();
      recognition.lang = "en-US";
      recognition.interimResults = false;
      recognition.maxAlternatives = 1;
      statusBox.textContent = "Listening...";
      recognition.onresult = (event) => {
        const spoken = event.results[0][0].transcript;
        textInput.value = spoken;
        sendDisplayText(spoken);
      };
      recognition.onerror = (event) => {
        statusBox.textContent = `Listening error: ${event.error}`;
      };
      recognition.start();
    }

    textInput.addEventListener("input", () => refreshPreview(true));
    sendButton.addEventListener("click", () => sendDisplayText(textInput.value));
    talkButton.addEventListener("click", startListening);
    clearButton.addEventListener("click", () => sendDisplayText(" "));
    modeSwitcher.addEventListener("change", toggleMode);
    speed.addEventListener("input", () => {
      startScroll();
      scheduleHardwareSettingsSync();
    });
    scrollToggle.addEventListener("change", () => {
      draw();
      scheduleHardwareSettingsSync();
    });
    refreshPreview(true);
    startScroll();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
