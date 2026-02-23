# ========================= kebbicall.py (MINIMAL) =========================
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, join_room, emit
import time, uuid, threading, os, json
from pathlib import Path
import requests

# ===================== App =====================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'aljazari-move-only'

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=25,
    ping_interval=10,
)

DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ===================== Device Registry / Rooms =====================
ONLINE_DEVICES = {}  # device_id -> sid  (keep as in your merged movement_server)
ROOM_PREFIX = "dev::"

def get_room_for(device_id: str) -> str:
    return ROOM_PREFIX + device_id

def room_of(device_id: str) -> str:
    return f"dev::{device_id}"

# sid -> {"device_id": "...", "device_type": "...", "display_name": "..."}
sid_index = {}
# device_id -> sid
device_index = {}
# pending events while offline
pending_events = {}

def ensure_list(dct, key):
    if key not in dct:
        dct[key] = []
    return dct[key]

def online(device_id: str) -> bool:
    return device_id in device_index

def enqueue_or_emit(to_device_id: str, event: str, payload: dict):
    rid = room_of(to_device_id)
    if online(to_device_id):
        try:
            socketio.emit(event, payload, room=rid)
            print(f"[EMIT] {event} -> {rid} ONLINE")
        except Exception as e:
            print(f"[EMIT ERROR] {event} -> {rid}: {e}")
            ensure_list(pending_events, to_device_id).append((event, payload))
    else:
        ensure_list(pending_events, to_device_id).append((event, payload))
        print(f"[QUEUE] {event} queued for {to_device_id}")

def push_pending_for(device_id: str):
    if device_id in pending_events and pending_events[device_id]:
        rid = room_of(device_id)
        for ev_name, payload in pending_events[device_id]:
            socketio.emit(ev_name, payload, room=rid)
            print(f"[FLUSH] {ev_name} -> {rid}")
        pending_events[device_id].clear()

def push_online_list():
    lst = [{"device_id": d, "sid": s} for d, s in device_index.items()]
    socketio.emit("online_list", {"devices": lst})
    print(f"[online_list] {lst}")

# ===================== Calls / WebRTC =====================
ongoing_calls = {}
RING_TIMEOUT_SEC = 30

def stop_ring_timer(call_id: str):
    c = ongoing_calls.get(call_id)
    if not c:
        return
    t = c.get("timer")
    if t:
        try:
            t.cancel()
        except:
            pass
        c["timer"] = None

def ring_timeout(call_id: str):
    c = ongoing_calls.get(call_id)
    if not c or c.get("status") != "ringing":
        return
    caller = c["caller"]
    callee = c["callee"]
    c["status"] = "ended"
    enqueue_or_emit(caller, "missed_call", {"call_id": call_id, "peer": callee})
    enqueue_or_emit(callee, "missed_call", {"call_id": call_id, "peer": caller})
    print(f"[TIMEOUT] call_id={call_id} caller={caller} callee={callee}")
    ongoing_calls.pop(call_id, None)

# ===================== REST =====================
@app.route("/")
def index():
    return jsonify({"status": "ok", "time": int(time.time())})

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "msg": "signal server alive"})

# (اختياري) نفس endpoint اللي عندك لاختبار الاتصال
@app.route("/call_robot_dry", methods=["POST"])
def call_robot_dry():
    data = request.get_json(silent=True) or {}
    caller = data.get("caller")
    target = data.get("target")
    return jsonify({"would_call": True, "caller": caller, "target": target}), 200

@app.route("/call_robot", methods=["POST"])
def call_robot():
    try:
        data = request.get_json(silent=True) or {}
        caller = data.get("caller", "phone_0001")
        target = data.get("target", "robot_0001")
        call_id = str(uuid.uuid4())
        print(f"[HTTP] call_robot {caller} -> {target} call_id={call_id}")

        ongoing_calls[call_id] = {
            "caller": caller,
            "callee": target,
            "status": "ringing",
            "started_at": time.time(),
            "timer": None
        }

        enqueue_or_emit(target, "incoming_call", {"call_id": call_id, "from": caller})

        t = threading.Timer(RING_TIMEOUT_SEC, ring_timeout, args=(call_id,))
        ongoing_calls[call_id]["timer"] = t
        t.start()

        return jsonify({"status": "calling", "call_id": call_id}), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": "call_robot_failed", "detail": str(e)}), 500

# ===================== Socket.IO =====================
@socketio.on("connect")
def on_connect():
    print(f"[CONNECT] sid={request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid

    info = sid_index.pop(sid, None)
    if info:
        dev = info.get("device_id")
        device_index.pop(dev, None)
        print(f"[DISCONNECT] sid={sid}, device_id={dev}")
        push_online_list()
    else:
        print(f"[DISCONNECT] sid={sid}")

    # keep movement_server ONLINE_DEVICES behavior
    to_delete = []
    for dev_id, ssid in ONLINE_DEVICES.items():
        if ssid == sid:
            to_delete.append(dev_id)
    for dev_id in to_delete:
        del ONLINE_DEVICES[dev_id]
        print(f"[OFFLINE] {dev_id}")

@socketio.on("register")
def on_register(data):
    dev_id = (data or {}).get("device_id", "").strip() or f"anon_{request.sid}"
    dev_type = (data or {}).get("device_type", "unknown")
    display_name = (data or {}).get("display_name", dev_id)

    sid_index[request.sid] = {"device_id": dev_id, "device_type": dev_type, "display_name": display_name}
    device_index[dev_id] = request.sid

    join_room(room_of(dev_id))
    print(f"[REGISTER] device_id={dev_id}, type={dev_type}, sid={request.sid}")

    emit("registered", {"ok": True, "device_id": dev_id}, room=request.sid)
    push_online_list()
    push_pending_for(dev_id)

    # movement_server logic
    ONLINE_DEVICES[dev_id] = request.sid
    join_room(get_room_for(dev_id))
    emit("registered", {"ok": True, "device_id": dev_id}, room=request.sid)
    print("[ONLINE]", ONLINE_DEVICES)

@socketio.on("who_is_online")
def on_who_is_online(data):
    push_online_list()

# ====== Calls ======
@socketio.on("call_request")
def on_call_request(data):
    frm = (data or {}).get("from")
    to  = (data or {}).get("to")
    if not frm or not to:
        return
    call_id = str(uuid.uuid4())
    print(f"[CALL_REQUEST] {frm} -> {to} call_id={call_id}")

    ongoing_calls[call_id] = {
        "caller": frm,
        "callee": to,
        "status": "ringing",
        "started_at": time.time(),
        "timer": None
    }

    enqueue_or_emit(to, "incoming_call", {"call_id": call_id, "from": frm})

    t = threading.Timer(RING_TIMEOUT_SEC, ring_timeout, args=(call_id,))
    ongoing_calls[call_id]["timer"] = t
    t.start()

    emit("call_created", {"call_id": call_id}, room=request.sid)

@socketio.on("call_accepted")
def on_call_accepted(data):
    call_id = (data or {}).get("call_id")
    by = (data or {}).get("by")
    c = ongoing_calls.get(call_id)
    if not c or c["status"] != "ringing":
        return

    c["status"] = "accepted"
    stop_ring_timer(call_id)

    caller = c["caller"]; callee = c["callee"]
    enqueue_or_emit(caller, "stop_ringing", {"call_id": call_id})
    enqueue_or_emit(callee, "stop_ringing", {"call_id": call_id})

    enqueue_or_emit(caller, "call_accepted", {"call_id": call_id, "by": by})
    enqueue_or_emit(callee, "call_accepted", {"call_id": call_id, "by": by})
    print(f"[ACCEPTED] call_id={call_id} by={by}")

@socketio.on("call_rejected")
def on_call_rejected(data):
    call_id = (data or {}).get("call_id")
    by = (data or {}).get("by")
    c = ongoing_calls.pop(call_id, None)
    if not c:
        return
    stop_ring_timer(call_id)

    caller = c["caller"]; callee = c["callee"]
    enqueue_or_emit(caller, "call_rejected", {"call_id": call_id, "by": by})
    enqueue_or_emit(callee, "call_rejected", {"call_id": call_id, "by": by})
    print(f"[REJECTED] call_id={call_id} by={by}")

@socketio.on("hangup")
def on_hangup(data):
    call_id = (data or {}).get("call_id")
    by = (data or {}).get("by")
    c = ongoing_calls.pop(call_id, None)
    if not c:
        return
    stop_ring_timer(call_id)

    caller = c["caller"]; callee = c["callee"]
    other = caller if by == callee else callee
    enqueue_or_emit(other, "call_ended", {"call_id": call_id, "by": by})
    enqueue_or_emit(by,    "call_ended", {"call_id": call_id, "by": by})
    print(f"[HANGUP] call_id={call_id} by={by}")

# ====== WebRTC Signaling ======
@socketio.on("webrtc_offer")
def on_webrtc_offer(data):
    call_id = (data or {}).get("call_id")
    frm     = (data or {}).get("from")
    sdp     = (data or {}).get("sdp")
    c = ongoing_calls.get(call_id)
    if not c or c.get("caller") != frm:
        print(f"[OFFER] Rejected (not caller) call_id={call_id}, from={frm}")
        return
    to = c["callee"]
    enqueue_or_emit(to, "webrtc_offer", {"call_id": call_id, "from": frm, "sdp": sdp})
    print(f"[OFFER] {frm} -> {to} call_id={call_id} len={len(sdp) if sdp else 0}")

@socketio.on("webrtc_answer")
def on_webrtc_answer(data):
    call_id = (data or {}).get("call_id")
    frm     = (data or {}).get("from")
    sdp     = (data or {}).get("sdp")
    c = ongoing_calls.get(call_id)
    if not c or c.get("callee") != frm:
        print(f"[ANSWER] Rejected (not callee) call_id={call_id}, from={frm}")
        return
    to = c["caller"]
    enqueue_or_emit(to, "webrtc_answer", {"call_id": call_id, "from": frm, "sdp": sdp})
    print(f"[ANSWER] {frm} -> {to} call_id={call_id} len={len(sdp) if sdp else 0}")

@socketio.on("webrtc_ice")
def on_webrtc_ice(data):
    call_id = (data or {}).get("call_id")
    frm     = (data or {}).get("from")
    cand    = (data or {}).get("candidate")
    c = ongoing_calls.get(call_id)
    if not c:
        return
    to = c["callee"] if frm == c["caller"] else c["caller"]
    enqueue_or_emit(to, "webrtc_ice", {"call_id": call_id, "from": frm, "candidate": cand})
    print(f"[ICE] {frm} -> {to} call_id={call_id} ok={bool(cand)}")

# ====== Remote Control (movement) ======
@socketio.on('remote_control')
def on_remote_control(data):
    frm  = (data or {}).get("from")
    to   = (data or {}).get("to")
    ctrl = (data or {}).get("ctrl_type")

    try:
        value = float((data or {}).get("value", 0.0))
    except Exception:
        value = 0.0

    try:
        duration = int((data or {}).get("duration_ms", 0))
    except Exception:
        duration = 0

    print(f"[REMOTE_CTRL] from={frm} -> to={to} type={ctrl} value={value} dur={duration}")

    if not to:
        return

    if to not in ONLINE_DEVICES:
        print(f"[REMOTE_CTRL] target {to} OFFLINE")
        emit("remote_ack", {"ok": False, "reason": "robot_offline"}, room=request.sid)
        return

    room = get_room_for(to)
    emit("remote_control", {
        "from": frm,
        "to": to,
        "ctrl_type": ctrl,
        "value": value,
        "duration_ms": duration
    }, room=room)

    emit("remote_ack", {"ok": True, "target_room": room}, room=request.sid)

# ===================== GPT Chat (Minimal) =====================
PROMPT_FILE = DATA_DIR / "prompt_config.json"

DEFAULT_PROMPT = """أنت مساعد ودود وسريع. جاوب باختصار وبأسلوب بسيط.
إذا المستخدم عربي جاوب عربي، إذا إنكليزي جاوب إنكليزي."""

def load_prompt() -> str:
    try:
        if PROMPT_FILE.exists():
            data = json.loads(PROMPT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and (data.get("prompt") or "").strip():
                return data["prompt"].strip()
    except Exception:
        pass
    return DEFAULT_PROMPT

def save_prompt(text: str):
    PROMPT_FILE.write_text(json.dumps({"prompt": text}, ensure_ascii=False, indent=2), encoding="utf-8")

CURRENT_PROMPT = load_prompt()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

def openai_chat(user_text: str, lang: str) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY is missing."

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    # hint بسيط للغة
    if (lang or "").lower().startswith("ar"):
        user_hint = "اللغة المطلوبة: العربية."
    else:
        user_hint = "Language requested: English."

    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": CURRENT_PROMPT},
            {"role": "user", "content": f"{user_hint}\n\n{user_text}"}
        ],
        "temperature": 0.4,
        "max_tokens": 280
    }

    r = requests.post(OPENAI_BASE, headers=headers, json=body, timeout=35)
    if 200 <= r.status_code < 300:
        js = r.json()
        return (js["choices"][0]["message"]["content"] or "").strip()

    return f"OpenAI error {r.status_code}: {r.text[:200]}"

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_text = (data.get("user_text") or "").strip()
    lang = (data.get("lang") or "en-US").strip()

    if not user_text:
        return jsonify({"reply": "No input."}), 400

    reply = openai_chat(user_text, lang)
    return jsonify({"reply": reply})

@app.route("/prompt", methods=["GET", "POST"])
def prompt_api():
    global CURRENT_PROMPT
    if request.method == "GET":
        return jsonify({"prompt": CURRENT_PROMPT})

    data = request.get_json(silent=True) or {}
    newp = (data.get("prompt") or "").strip()
    if not newp:
        return jsonify({"ok": False, "error": "empty prompt"}), 400
    CURRENT_PROMPT = newp
    save_prompt(CURRENT_PROMPT)
    return jsonify({"ok": True})

@app.route("/prompt_ui")
def prompt_ui():
    safe_prompt = CURRENT_PROMPT.replace("</", "&lt;/")
    return f"""
<!doctype html><meta charset="utf-8">
<title>Kebbi Prompt</title>
<style>
body{{font-family:system-ui,Arial;margin:24px;max-width:1000px}}
textarea{{width:100%;height:320px}}
button{{padding:10px 16px;margin-top:10px}}
#msg{{margin-top:10px}}
</style>
<h2>🔧 Prompt Dashboard</h2>
<textarea id="p">{safe_prompt}</textarea><br>
<button onclick="save()">Save</button>
<div id="msg"></div>
<script>
async function save(){{
  const p=document.getElementById('p').value;
  const r=await fetch('/prompt',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{prompt:p}})}});
  const js=await r.json();
  document.getElementById('msg').textContent = js.ok?'Saved ✔':('Error: '+(js.error||''));
}}
</script>
""".strip()

# ===================== Run =====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"🔥 Minimal Signaling + GPT on 0.0.0.0:{port}")
    socketio.run(app, host="0.0.0.0", port=port)
