import eventlet
eventlet.monkey_patch()
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, join_room, emit
import time, uuid, threading
import json, requests, os, pathlib
import re, unicodedata
from pathlib import Path
import random


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
# ===================== MOVEMENT_SERVER COMPAT (merged) =====================
# device_id -> sid  (movement_server used ONLINE_DEVICES)
ONLINE_DEVICES = {}
ROOM_PREFIX = "dev::"

def get_room_for(device_id: str) -> str:
    return ROOM_PREFIX + device_id

# ========================= تابع لجات جي بي تي =========================
# ========== Conversational Memory (short-term + long-term) ==========
MEMORY_FILE = DATA_DIR / "mem_store.json"
MAX_TURNS_PER_USER = 25      # عدد آخر الرسائل المحفوظة (قصيرة المدى)
MAX_RECENT_ITEMS = 5         # آخر عطور ذُكرت
MEM_SUMMARY_EVERY = 6        # كل كم رسالة نحدّث ملخص الشخصية (بدون GPT هنا)
MEM_CLEANUP_DAYS = 120       # نمسح المستخدم الغير نشط بعد X يوم

def _mem_load():
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _mem_save(mem):
    MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")

MEM = _mem_load()

def _now_epoch():
    return int(time.time())

def _ensure_user(uid: str):
    if uid not in MEM:
        MEM[uid] = {
            "created_at": _now_epoch(),
            "updated_at": _now_epoch(),
            "facts": {         # تفضيلات طويلة المدى
                "language": None,      # "ar" | "en"
                "gender_pref": None,   # "male"|"female"|"unisex"
                "season_pref": [],     # ["summer","winter",...]
                "notes_pref": [],      # ["citrus","amber",...]
                "budget_usd": None     # رقم تقريبي
            },
            "recent_items": [],   # آخر عطور ذُكرت/طُلِبت
            "history": [],        # [{role:"user"/"assistant", "text":"..."}]
            "summary": ""         # ملخص تلقائي بسيط من آخر محادثات
        }

def _touch_user(uid: str):
    _ensure_user(uid)
    MEM[uid]["updated_at"] = _now_epoch()

def _push_turn(uid: str, role: str, text: str):
    _ensure_user(uid)
    h = MEM[uid]["history"]
    h.append({"role": role, "text": text, "t": _now_epoch()})
    # قصّ التاريخ للمسموح
    if len(h) > MAX_TURNS_PER_USER:
        del h[0:len(h)-MAX_TURNS_PER_USER]
    MEM[uid]["updated_at"] = _now_epoch()

def _append_recent_item(uid: str, name: str):
    _ensure_user(uid)
    lst = MEM[uid]["recent_items"]
    if name and name not in lst:
        lst.insert(0, name)
        if len(lst) > MAX_RECENT_ITEMS:
            lst.pop()

# ——— اكتشاف تفضيلات بسيطة (AR/EN) ———
AR_SEASONS = ["صيف", "شتاء", "ربيع", "خريف", "مسائي", "مساء", "نهاري", "الكل"]
EN_SEASONS = ["summer","winter","spring","fall","autumn","evening","daytime","all"]

def _extract_prefs(uid: str, user_text: str, lang_hint: str):
    t = user_text.lower()
    f = MEM[uid]["facts"]

    # لغة مفضلة
    if re.search(r"[\u0600-\u06FF]", user_text):  # وجود حروف عربية
        f["language"] = "ar"
    elif re.search(r"[a-zA-Z]", user_text):
        f["language"] = f["language"] or "en"

    # نوع (رجالي/نسائي/يونيسكس)
    if any(k in t for k in ["رجالي","للرجال","male","men","man's","men's"]):
        f["gender_pref"] = "male"
    if any(k in t for k in ["نسائي","للنساء","female","women","ladies"]):
        f["gender_pref"] = "female"
    if any(k in t for k in ["يونيسكس","unisex"]):
        f["gender_pref"] = "unisex"

    # ميزانية
    m1 = re.search(r"(?:budget|price|cost)[^\d]{0,8}(\d{2,4})", t)
    m2 = re.search(r"(?:ميزانيتي|سعر حدود|حدودي)\D{0,6}(\d{2,4})", user_text)
    val = None
    if m1: val = int(m1.group(1))
    if m2: val = int(m2.group(1))
    if val: f["budget_usd"] = val

    # مواسم
    seasons = []
    for s in EN_SEASONS:
        if s in t: seasons.append(s if s!="autumn" else "fall")
    for a,en in zip(AR_SEASONS, ["summer","winter","spring","fall","evening","evening","daytime","all"]):
        if a in user_text: seasons.append(en)
    if seasons:
        f["season_pref"] = sorted(list(set(f["season_pref"] + seasons)))

    # نوتات (مبسّطة)
    note_words = ["citrus","amber","vanilla","woody","incense","saffron","musk","lavender","pepper","aquatic",
                  "حمضي","عنبر","فانيلا","خشبي","بخور","زعفران","مسك","لافندر","فلفل","مائي"]
    found = [w for w in note_words if w in t or w in user_text]
    if found:
        # ترميز عربي -> إنجليزي بسيط
        ar2en = {"حمضي":"citrus","عنبر":"amber","فانيلا":"vanilla","خشبي":"woody","بخور":"incense",
                 "زعفران":"saffron","مسك":"musk","لافندر":"lavender","فلفل":"pepper","مائي":"aquatic"}
        normalized = [ar2en.get(w,w) for w in found]
        f["notes_pref"] = sorted(list(set(f["notes_pref"] + normalized)))

def _maybe_update_summary(uid: str):
    """ملخص تلقائي بسيط من آخر المحادثات بدون استدعاء GPT."""
    _ensure_user(uid)
    h = MEM[uid]["history"]
    if not h: return
    if len(h) % MEM_SUMMARY_EVERY != 0: return
    # التلخيص: أخذ آخر 5 رسائل مستخدم واستخراج نبرة واهتمامات
    last_user_msgs = [x["text"] for x in h if x["role"]=="user"][-5:]
    if not last_user_msgs: return
    blob = " ".join(last_user_msgs).lower()
    tone = []
    if any(k in blob for k in ["شكرا","thank","appreciate"]): tone.append("polite")
    if any(k in blob for k in ["بسرعه","عاجل","urgent","asap"]): tone.append("urgent")
    if any(k in blob for k in ["تفاصيل","details","explain","شرح"]): tone.append("detail-oriented")
    facts = MEM[uid]["facts"]
    tone_txt = ", ".join(tone) if tone else "neutral"
    MEM[uid]["summary"] = (
        f"User tone: {tone_txt}. "
        f"Prefs → gender:{facts['gender_pref'] or '-'}, seasons:{','.join(facts['season_pref']) or '-'}, "
        f"notes:{','.join(facts['notes_pref']) or '-'}, budget:{facts['budget_usd'] or '-'}."
    )

def build_memory_context(uid: str) -> str:
    """نص مختصر يُحقن للنموذج: (ملخص + تفضيلات + آخر 3 تبادلات)."""
    _ensure_user(uid)
    f = MEM[uid]["facts"]
    recent = MEM[uid]["recent_items"]
    h = MEM[uid]["history"][-6:]  # آخر ست رسائل (user/assistant مختلط)

    lines = []
    if MEM[uid]["summary"]:
        lines.append(f"[MEMO-SUMMARY] {MEM[uid]['summary']}")
    lines.append(f"[PREFS] language={f['language'] or '-'}; gender={f['gender_pref'] or '-'}; "
                 f"seasons={','.join(f['season_pref']) or '-'}; notes={','.join(f['notes_pref']) or '-'}; "
                 f"budget_usd={f['budget_usd'] or '-'}")
    if recent:
        lines.append(f"[RECENT-ITEMS] {', '.join(recent)}")
    # آخر 3 تبادلات للمساعدة بالسياق
    tail = []
    for turn in h[-6:]:
        role = "USR" if turn["role"]=="user" else "AST"
        txt = turn["text"].replace("\n"," ").strip()
        tail.append(f"{role}: {txt}")
    if tail:
        lines.append("[RECENT-TURNS]\n" + "\n".join(tail))
    return "\n".join(lines)

# ====== فهارس اتصال الأجهزة ======
# sid -> {"device_id": "...", "device_type": "...", "display_name": "..."}
sid_index = {}
# device_id -> sid
device_index = {}

# أحداث معلّقة لو كان الجهاز أوفلاين
# pending_events["device_id"] = [ (event_name, payload_dict), ... ]
pending_events = {}

# مكالمات جارية: call_id -> dict
ongoing_calls = {}
# شكل السجل:
# ongoing_calls[call_id] = {
#   "caller": "<device_id>",
#   "callee": "<device_id>",
#   "status": "ringing" | "accepted" | "ended",
#   "started_at": <epoch>,
#   "timer": <threading.Timer or None>
# }

RING_TIMEOUT_SEC = 30

def room_of(device_id: str) -> str:
    return f"dev::{device_id}"

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
            lst = ensure_list(pending_events, to_device_id)
            lst.append((event, payload))
    else:
        lst = ensure_list(pending_events, to_device_id)
        lst.append((event, payload))
        print(f"[QUEUE] {event} queued for {to_device_id}")

def push_pending_for(device_id: str):
    """عند تسجيل الدخول ندفُع كل الأحداث المعلقة"""
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

def stop_ring_timer(call_id: str):
    c = ongoing_calls.get(call_id)
    if not c: return
    t = c.get("timer")
    if t:
        try: t.cancel()
        except: pass
        c["timer"] = None

def ring_timeout(call_id: str):
    """يشتغل بعد 30 ثانية إذا ما انقبل الاتصال"""
    c = ongoing_calls.get(call_id)
    if not c or c.get("status") != "ringing":
        return
    caller = c["caller"]; callee = c["callee"]
    c["status"] = "ended"
    # بلغ الطرفين إن المكالمة فائتة
    enqueue_or_emit(caller, "missed_call", {"call_id": call_id, "peer": callee})
    enqueue_or_emit(callee, "missed_call", {"call_id": call_id, "peer": caller})
    print(f"[TIMEOUT] call_id={call_id} caller={caller} callee={callee}")
    ongoing_calls.pop(call_id, None)

# ===================== REST (اختياري للاختبار) =====================
@app.route("/")
def index():
    return jsonify({"status": "ok", "time": int(time.time())})

# ✅ endpoint من movement_server
@app.route("/ping")
def ping():
    return jsonify({"ok": True, "msg": "movement server alive (merged)"})

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
        app.logger.exception("call_robot_failed")
        return jsonify({"ok": False, "error": "call_robot_failed", "detail": str(e)}), 500


# ===================== SOCKET.IO =====================
@socketio.on("connect")
def on_connect():
    # movement_server had [CONNECT] too
    print(f"[CONNECT] sid={request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid

    # --------- existing logic ----------
    info = sid_index.pop(sid, None)
    if info:
        dev = info.get("device_id")
        device_index.pop(dev, None)
        print(f"[DISCONNECT] sid={sid}, device_id={dev}")
        push_online_list()
    else:
        print(f"[DISCONNECT] sid={sid}")

    # --------- movement_server logic ----------
    to_delete = []
    for dev_id, ssid in ONLINE_DEVICES.items():
        if ssid == sid:
            to_delete.append(dev_id)
    for dev_id in to_delete:
        del ONLINE_DEVICES[dev_id]
        print(f"[OFFLINE] {dev_id}")

@socketio.on("register")
def on_register(data):
    """
    data: { device_id, device_type, display_name? }
    """
    dev_id = (data or {}).get("device_id", "").strip() or f"anon_{request.sid}"
    dev_type = (data or {}).get("device_type", "unknown")
    display_name = (data or {}).get("display_name", dev_id)

    # --------- existing logic ----------
    sid_index[request.sid] = {"device_id": dev_id, "device_type": dev_type, "display_name": display_name}
    device_index[dev_id] = request.sid

    join_room(room_of(dev_id))
    print(f"[REGISTER] device_id={dev_id}, type={dev_type}, sid={request.sid}")

    emit("registered", {"ok": True, "device_id": dev_id}, room=request.sid)
    push_online_list()
    push_pending_for(dev_id)

    # --------- movement_server logic ----------
    ONLINE_DEVICES[dev_id] = request.sid
    room = get_room_for(dev_id)
    join_room(room)
    emit("registered", {"ok": True, "device_id": dev_id}, room=request.sid)
    print("[ONLINE]", ONLINE_DEVICES)

@socketio.on("who_is_online")
def on_who_is_online(data):
    push_online_list()

# ====== بدء مكالمة (المتصل يطلب رنين) ======
@socketio.on("call_request")
def on_call_request(data):
    """
    data: { from, to }
    """
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
    # مهلة الرنين
    t = threading.Timer(RING_TIMEOUT_SEC, ring_timeout, args=(call_id,))
    ongoing_calls[call_id]["timer"] = t
    t.start()
    # رجّع للمرسل call_id
    emit("call_created", {"call_id": call_id}, room=request.sid)

# ====== قبول/رفض ======
@socketio.on("call_accepted")
def on_call_accepted(data):
    """
    data: { call_id, by }
    - by هو الجهاز اللي قبل (المتلقي غالبًا)
    """
    call_id = (data or {}).get("call_id")
    by = (data or {}).get("by")
    c = ongoing_calls.get(call_id)
    if not c or c["status"] != "ringing":
        return
    c["status"] = "accepted"
    stop_ring_timer(call_id)
    caller = c["caller"]; callee = c["callee"]
    # أوقف رنين الطرفين
    enqueue_or_emit(caller, "stop_ringing", {"call_id": call_id})
    enqueue_or_emit(callee,  "stop_ringing", {"call_id": call_id})
    # بلّغ المتصل إن الطرف الآخر قبل — المتصل يبدأ بإرسال Offer
    enqueue_or_emit(caller, "call_accepted", {"call_id": call_id, "by": by})
    enqueue_or_emit(callee,  "call_accepted", {"call_id": call_id, "by": by})
    print(f"[ACCEPTED] call_id={call_id} by={by}")

@socketio.on("call_rejected")
def on_call_rejected(data):
    """
    data: { call_id, by }
    """
    call_id = (data or {}).get("call_id")
    by = (data or {}).get("by")
    c = ongoing_calls.pop(call_id, None)
    if not c: return
    stop_ring_timer(call_id)
    caller = c["caller"]; callee = c["callee"]
    enqueue_or_emit(caller, "call_rejected", {"call_id": call_id, "by": by})
    enqueue_or_emit(callee,  "call_rejected", {"call_id": call_id, "by": by})
    print(f"[REJECTED] call_id={call_id} by={by}")

# ====== إنهاء المكالمة ======
@socketio.on("hangup")
def on_hangup(data):
    """
    data: { call_id, by }
    """
    call_id = (data or {}).get("call_id")
    by = (data or {}).get("by")
    c = ongoing_calls.pop(call_id, None)
    if not c: return
    stop_ring_timer(call_id)
    caller = c["caller"]; callee = c["callee"]
    other = caller if by == callee else callee
    enqueue_or_emit(other, "call_ended", {"call_id": call_id, "by": by})
    enqueue_or_emit(by,    "call_ended", {"call_id": call_id, "by": by})
    print(f"[HANGUP] call_id={call_id} by={by}")

# ====== مسار WebRTC (مهم: caller فقط يرسل Offer) ======
@socketio.on("webrtc_offer")
def on_webrtc_offer(data):
    """
    data: { call_id, from, sdp }
    """
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
    """
    data: { call_id, from, sdp }
    """
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
    """
    data: { call_id, from, candidate: {sdpMid, sdpMLineIndex, candidate} }
    """
    call_id = (data or {}).get("call_id")
    frm     = (data or {}).get("from")
    cand    = (data or {}).get("candidate")
    c = ongoing_calls.get(call_id)
    if not c: return
    # وجّه للآخر
    to = c["callee"] if frm == c["caller"] else c["caller"]
    enqueue_or_emit(to, "webrtc_ice", {"call_id": call_id, "from": frm, "candidate": cand})
    print(f"[ICE] {frm} -> {to} call_id={call_id} ok={bool(cand)}")

# ========== NEW: AI Chat (OpenAI + Prompt Dashboard) ==========
PROMPT_FILE = DATA_DIR / "prompt_config.json"

DEFAULT_PROMPT = """\
أنت "كيبي"، موظف استقبال (Receptionist) في "المستشفى الملكي – فرع السيدية".
اذا سألك احد ما عن ماذا توفر المستشفى الملكي اجب فقط ب يوفر كافة أنواع الفحوص الطبية والسريرية بأيدي أفضل الكوادر الطبية.
هدفك السرعة، التنظيم، واللطف بأسلوب مهني قريب للهجة العراقية الخفيفة.

أنماط الإجابة:

1) استقبال عام (مثل: شنو عندكم؟ شنو الخدمات؟):
   - ابدأ بجملة ترحيب قصيرة:
     مثال: "أهلاً وسهلاً بحضرتك في المستشفى الملكي – فرع السيدية 🌿 شلون أگدر أساعدك اليوم؟"
  اذا سالك احد ما عن الخدمات التي توفرها المستشفى اجب فق ب توفر المستشفى الملكي فرع السيدية, كافة انواع الفحوصات الطبية والسريرية, وبايدي افضل الكوادر الطبية
  "
2) حجز موعد:
   - اطلب المعلومات بسطر لكل نقطة:
     • الاسم الكامل  
     • العمر  
     • نوع الاستشارة أو الفحص  
     • اليوم والوقت المناسب  
   - لا تؤكد الحجز فعلياً.
   - اختم بـ: "تمام، راح أنسّق الموضوع لحضرتك 🌿"

3) استفسار عن فحص معيّن:
   - جاوب بإيجاز:
     • ما هو الفحص  
     • هل يحتاج صيام (إذا معروف)  
     • المدة التقريبية  
   - اختم بسؤال: "تحب نحجزلك موعد؟"

4) أسعار أو خصومات:
   - لا تعطي أرقام دقيقة.
   - رد بلطف:
     "الأسعار تختلف حسب نوع الفحص، نكدر نوضّحها بعد تحديد الخدمة 🌿"

5) حالات طارئة:
   - إذا وُجدت أعراض خطيرة (ألم صدر شديد، نزيف، فقدان وعي):
     قل فوراً:
     "إذا الأعراض قوية أو مفاجئة، يُفضّل التوجه للطوارئ فوراً أو الاتصال بالإسعاف."

6) أسئلة خارج الإطار الطبي:
   - رد بلطف:
     "خلّينا نركّز على صحتك أولاً 🌿 شلون أگدر أساعدك طبياً؟"

قواعد عامة:
- جاوب بلغة المستخدم تلقائياً.
- لا تعطي تشخيص طبي نهائي.
- لا تخترع أسماء أطباء.
- لا تنفّذ حجز فعلي.
- الردود قصيرة ومنظمة وواضحة.

----------------------------------------------------------

You are "Kebbi", a receptionist at "Royal Hospital – Saydiya Branch".
The hospital provides all types of medical and clinical examinations handled by top medical professionals.
Your goal is to be fast, organized, and polite with a warm professional tone.

Response Modes:

1) General inquiry (e.g., “What services do you have?”):
   - Start with a short welcome line:
     Example: "Welcome to Royal Hospital – Saydiya Branch 🌿 How may I assist you today?"
   - Briefly mention:
     • Comprehensive laboratory tests  
     • Specialist consultations  
     • Clinical examinations and medical imaging  
   - End with a guiding question: "Would you like to book an appointment or inquire about a specific test?"

2) Appointment booking:
   - Request details clearly, one per line:
     • Full name  
     • Age  
     • Type of consultation or test  
     • Preferred date and time  
   - Do NOT confirm the booking.
   - End with: "Thank you, I will coordinate this for you 🌿"

3) Specific test inquiry:
   - Reply briefly:
     • What the test is  
     • Whether fasting is required (if known)  
     • Approximate duration  
   - End with: "Would you like me to schedule it for you?"

4) Prices or discounts:
   - Do not provide exact numbers.
   - Respond politely:
     "Pricing depends on the specific service. I can clarify once the test or consultation is selected 🌿"

5) Emergency symptoms:
   - If severe symptoms are described (chest pain, bleeding, unconsciousness):
     Respond immediately:
     "If symptoms are severe or sudden, please go to the emergency department immediately or call emergency services."

6) Non-medical or playful requests:
   - Gently redirect:
     "Let’s focus on your health first 🌿 How may I assist you medically?"

Rules:
- Auto-detect user language.
- Do not provide final medical diagnosis.
- Do not invent doctors.
- Do not execute real bookings.
- Keep responses short, structured, and professional.
"""

def _load_prompt() -> str:
    try:
        if PROMPT_FILE.exists():
            data = json.loads(PROMPT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "prompt" in data and data["prompt"].strip():
                return data["prompt"]
    except Exception:
        pass
    return DEFAULT_PROMPT

FAQ_FILE = DATA_DIR / "faq_perfume.json"

DEFAULT_FAQ = [
    {"cat": "about_scent", "qs": [
        "شنو نوع الريحة؟ (خشبي، زهري، فواكه، شرقي، الخ)",
        "بيه لمسة فانيلا أو عود؟",
        "الريحة حلوة بالنهار لو بالليل أكثر؟",
        "يشبه أي عطر مشهور؟",
        "أول ما ترشه شنو تطلع الريحة الأولية؟",
        "بعد شكم دقيقة يتغير؟",
        "الريحة ثقيلة لو خفيفة؟",
        "بيه لمسة سويت (حلوة) لو سبايسي؟",
        "يناسب الصيف لو الشتاء؟",
        "العطر دافئ لو بارد؟"
    ]},
    {"cat": "lasting_projection", "qs": [
        "شكد يثبت تقريباً؟",
        "الفوحان ماله قوي لو ناعم؟",
        "يثبت على الملابس أكثر لو على الجلد؟",
        "إذا رشّيته كم ساعة يظل؟",
        "تنصح بيه للدوام اليومي لو للمناسبات فقط؟"
    ]},
    {"cat": "audience_usage", "qs": [
        "هذا نسائي لو رجالي لو يونيسكس؟",
        "ينفع كهدية؟",
        "يناسب الأعمار الصغيرة لو الكبيرة؟",
        "للطلاب ينفع لو قوي عليهم؟",
        "ينفع للعرايس أو مناسبات رسمية؟",
        "ينفع لعطور الطبقات أو layering ويا عطر ثاني؟"
    ]},
    {"cat": "price_offers", "qs": [
        "شكد سعره؟",
        "أكو حجم أصغر؟",
        "أكو عليه خصم؟",
        "إذا أخذت أكثر من واحد يصير سعر خاص؟",
        "ليش سعره أعلى من غيره؟",
        "شنو الفرق بين هذا الأصلي والنسخة الثانية؟"
    ]},
    {"cat": "ingredients_quality", "qs": [
        "يحتوي على كحول؟",
        "طبيعي لو تركيبة صناعية؟",
        "منو الشركة المصنعة؟",
        "صنع وين؟",
        "الإصدار جديد لو قديم؟",
        "شنو المكونات الأساسية بالعطر؟",
        "يحتوي على المسك أو العنبر؟"
    ]},
    {"cat": "experience_compare", "qs": [
        "أنت جربته بنفسك؟",
        "أكثر عطر ينباع عندكم شنو؟",
        "شنو العطر المفضل عند الزبائن؟",
        "إذا أريد شي يشبه \"ديور سوفاج\"، شنو تنصحني؟",
        "أريد ريحة تظل وتلفت الانتباه، شنو الأفضل؟",
        "أريد شي ناعم وراقي، شنو تقترح؟"
    ]},
    {"cat": "packaging_gift", "qs": [
        "يجي ويا علبة أو بوكس خاص؟",
        "ممكن نكتب اسم الشخص على العلبة؟",
        "أكو تغليف هدية مجاني؟",
        "يجي ويا كيس أو ستيكر؟"
    ]},
    {"cat": "delivery_service", "qs": [
        "توصلونه للبيت؟",
        "التوصيل مجاني؟",
        "كم يوم ياخذ التوصيل؟",
        "أكدر أرجعه إذا ما عجبني؟",
        "أكو ضمان على الأصلية؟",
        "إذا خلص، تكدر تبلغني أول ما يتوفر؟"
    ]}
]

def _load_faq():
    if FAQ_FILE.exists():
        try:
            data = json.loads(FAQ_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return DEFAULT_FAQ

def _save_faq(items: list):
    FAQ_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

FAQ_ITEMS = _load_faq()

def _compose_faq_prompt(items: list) -> str:
    """نص موجّه للنموذج: يشرح طبيعة الأسئلة المتوقعة حتى تكون الإجابات سريعة ومركّزة."""
    lines_en = ["Expected customer questions (grouped). Answer briefly, helpful, product-aware:"]
    lines_ar = ["الأسئلة المتوقعة من الزبون (مجمّعة). أجب بإيجاز ووضوح ووعي بالكتالوج:"]

    for block in items:
        cat = block.get("cat","general")
        qs  = block.get("qs",[])
        if not qs: continue
        lines_en.append(f"- {cat}: {len(qs)} items")
        for q in qs[:6]:
            lines_en.append(f"  • {q}")
        lines_ar.append(f"- {cat}: {len(qs)} سؤال")
        for q in qs[:6]:
            lines_ar.append(f"  • {q}")

    return "\n".join(lines_en) + "\n\n" + "\n".join(lines_ar)

@app.route("/faq", methods=["GET","POST"])
def faq_api():
    """
    GET  -> يرجّع قائمة الأسئلة
    POST -> يستلم قائمة كاملة جديدة ويخزنها (upsert بسيط)
    """
    global FAQ_ITEMS
    if request.method == "GET":
        return jsonify(FAQ_ITEMS)
    data = request.get_json(silent=True) or []
    if not isinstance(data, list) or not data:
        return jsonify({"ok": False, "error": "expect list of {cat, qs[]}"}), 400
    FAQ_ITEMS = data
    _save_faq(FAQ_ITEMS)
    return jsonify({"ok": True, "count": len(FAQ_ITEMS)})

@app.route("/faq_ui")
def faq_ui():
    return """
<!doctype html><meta charset="utf-8">
<title>Kebbi FAQ (Smart Questions)</title>
<style>
body{font-family:system-ui,Arial;margin:24px;max-width:1000px}
textarea{width:100%;height:380px}
button{padding:8px 12px;margin-top:8px}
pre{background:#f6f6f6;padding:12px;white-space:pre-wrap}
</style>
<h2>❓ Kebbi – Smart Questions (FAQ)</h2>
<p>حرّر القائمة كاملةً كـ JSON (مصفوفة من كائنات: {cat, qs:[...]}) ثم احفظ.</p>
<textarea id="box"></textarea><br>
<button onclick="save()">Save</button>
<button onclick="reload()">Reload</button>
<div id="msg"></div>
<script>
async function reload(){
  const r=await fetch('/faq'); const js=await r.json();
  document.getElementById('box').value = JSON.stringify(js, null, 2);
}
async function save(){
  const txt=document.getElementById('box').value;
  let js; try{ js=JSON.parse(txt) }catch(e){ alert('Invalid JSON'); return; }
  const r=await fetch('/faq',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(js)});
  const rs=await r.json(); document.getElementById('msg').textContent = rs.ok?'Saved ✔':'Error';
}
reload();
</script>
""".strip()

def _save_prompt(text: str):
    PROMPT_FILE.write_text(json.dumps({"prompt": text}, ensure_ascii=False, indent=2), encoding="utf-8")

CURRENT_PROMPT = _load_prompt()

# إعدادات OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"

# ====== OpenAI TTS (Streaming) ======
OPENAI_TTS_URL   = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "sage"

@app.route("/tts", methods=["GET"])
def tts_stream():
    """
    يبث صوت TTS تدريجياً حتى يبدأ التشغيل فورًا.
    params:
      - text (مطلوب): النص
      - fmt  (اختياري): aac | mp3 | opus  (الافتراضي aac)
    """
    from flask import Response
    text = (request.args.get("text") or "").strip()
    fmt  = (request.args.get("fmt") or "aac").strip().lower()
    if not text:
        return jsonify({"error": "text is required"}), 400

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": text,
        "format": fmt,
    }

    try:
        r = requests.post(OPENAI_TTS_URL, headers=headers, json=payload, stream=True, timeout=60)
    except Exception as e:
        return jsonify({"error": "OpenAI request error", "detail": str(e)}), 502

    if r.status_code < 200 or r.status_code >= 300:
        try:
            err = r.text
        except Exception:
            err = f"HTTP {r.status_code}"
        return jsonify({"error": "OpenAI TTS failed", "detail": err}), 502

    mime = {
        "aac":  "audio/aac",
        "mp3":  "audio/mpeg",
        "opus": "audio/ogg",
    }.get(fmt, "audio/aac")

    def generate():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    return Response(generate(), mimetype=mime)

def _build_messages(user_text: str, lang: str, uid: str):
    sys_main = CURRENT_PROMPT
    sys_catalog = _load_catalog_prompt_from_disk()
    sys_faq = _compose_faq_prompt(FAQ_ITEMS)
    mem_block = build_memory_context(uid)

    if lang and lang.lower().startswith("ar"):
        user_hint = "اللغة المطلوبة: العربية (ar)."
    else:
        user_hint = "Language requested: English (en)."

    return [
        {"role": "system", "content": sys_main},
        {"role": "system", "content": sys_catalog},
        {"role": "system", "content": sys_faq},
        {"role": "system", "content": f"[CONVERSATION-MEMORY]\n{mem_block}"},
        {"role": "user", "content": f"{user_hint}\n\nUSER SAID:\n{user_text}"}
    ]

def _openai_chat(messages):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 280
    }
    resp = requests.post(OPENAI_BASE, headers=headers, json=body, timeout=30)
    if resp.status_code >= 200 and resp.status_code < 300:
        js = resp.json()
        txt = js["choices"][0]["message"]["content"]
        return txt.strip()
    raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:300]}")

# ========== Product Catalog APIs & Minimal Dashboard ==========
# (مثل ما هو عندك)

@app.route("/perfumes", methods=["GET","POST"])
def perfumes_list_create():
    global PERFUMES
    if request.method == "GET":
        return jsonify(PERFUMES)
    data = request.get_json(silent=True) or {}
    newp = {
        "id": (data.get("id") or str(uuid.uuid4())).strip(),
        "brand": data.get("brand","").strip(),
        "name_en": data.get("name_en","").strip(),
        "name_ar": data.get("name_ar","").strip(),
        "type": data.get("type","unisex").strip(),
        "notes": data.get("notes") or [],
        "season": data.get("season") or [],
        "price": data.get("price") or 0,
        "available": bool(data.get("available", True))
    }
    for i,p in enumerate(PERFUMES):
        if p["id"] == newp["id"]:
            PERFUMES[i] = newp
            save_perfumes(PERFUMES)
            return jsonify({"ok": True, "updated": newp})
    PERFUMES.append(newp)
    save_perfumes(PERFUMES)
    return jsonify({"ok": True, "created": newp})

@app.route("/perfumes/<pid>", methods=["PUT","DELETE"])
def perfumes_update_delete(pid):
    global PERFUMES
    if request.method == "DELETE":
        PERFUMES = [p for p in PERFUMES if p["id"] != pid]
        save_perfumes(PERFUMES)
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    for i,p in enumerate(PERFUMES):
        if p["id"] == pid:
            PERFUMES[i].update({
                "brand": data.get("brand", p["brand"]),
                "name_en": data.get("name_en", p["name_en"]),
                "name_ar": data.get("name_ar", p["name_ar"]),
                "type": data.get("type", p["type"]),
                "notes": data.get("notes", p.get("notes",[])),
                "season": data.get("season", p.get("season",[])),
                "price": data.get("price", p.get("price",0)),
                "available": bool(data.get("available", p.get("available", True))),
            })
            save_perfumes(PERFUMES)
            return jsonify({"ok": True, "updated": PERFUMES[i]})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/catalog", methods=["GET","POST"])
def catalog_api():
    """
    GET  -> يرجع العناصر الخام + نص البرومبت المُولّد
    POST -> upsert عنصر ثم يعيد توليد برومبت الكتالوج
    """
    global CATALOG_ITEMS, CATALOG_PROMPT
    if request.method == "GET":
        return jsonify({"items": CATALOG_ITEMS, "catalog_prompt": _load_catalog_prompt_from_disk()})

    data = request.get_json(silent=True) or {}

    raw_season = data.get("season", [])
    if isinstance(raw_season, str):
        raw_season = [s.strip() for s in raw_season.split(",") if s.strip()]

    item = {
        "name": (data.get("name") or "").strip(),
        "brand": (data.get("brand") or "").strip(),
        "aliases": (data.get("aliases") or []),
        "type": (data.get("type") or "unisex").strip(),
        "notes": (data.get("notes") or "").strip(),
        "season": raw_season,
        "price_usd": data.get("price_usd", None),
        "available": bool(data.get("available", True))
    }
    if not item["name"]:
        return jsonify({"ok": False, "error": "name required"}), 400

    found = None
    for p in CATALOG_ITEMS:
        if p.get("name","").strip().lower() == item["name"].lower():
            found = p
            break
    if found:
        found.update(item)
    else:
        CATALOG_ITEMS.append(item)

    _save_catalog_items(CATALOG_ITEMS)
    CATALOG_PROMPT = _regenerate_and_persist_catalog_prompt(CATALOG_ITEMS)
    return jsonify({"ok": True, "item": item, "catalog_prompt": CATALOG_PROMPT})

@app.route("/catalog_ui")
def catalog_ui():
    return """
<!doctype html><meta charset="utf-8">
<title>Kebbi Catalog → Prompt</title>
<style>
body{font-family:system-ui,Arial;margin:24px;max-width:1000px}
input,textarea{width:100%;margin:6px 0;padding:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
table{border-collapse:collapse;margin-top:16px;width:100%}
td,th{border:1px solid #ddd;padding:8px;text-align:left}
pre{background:#f6f6f6;padding:12px;white-space:pre-wrap}
small{color:#666}
button{padding:8px 12px}
.badge{display:inline-block;padding:2px 6px;border-radius:6px;border:1px solid #ddd;margin:2px 4px}
</style>
<h2>🧴 Catalog → Prompt (LLM)</h2>
<div class="grid">
  <div>
    <label>Name</label><input id="name" placeholder="Bleu de Chanel EDP">
    <label>Brand</label><input id="brand" placeholder="Chanel">
    <label>Aliases (comma-separated)</label><input id="aliases" placeholder="بلو دي شانيل, bleu de chanel">

    <label>Type</label><input id="type" placeholder="male/female/unisex">

    <label>Season (comma-separated)</label>
    <input id="season" placeholder="summer, spring, fall, winter, evening, all">

    <label>Notes</label><textarea id="notes" rows="2" placeholder="Citrus; wood; incense"></textarea>
    <label>Price (USD)</label><input id="price" type="number" step="0.01">
    <label>Available</label><input id="avail" type="checkbox" checked>
    <button onclick="save()">Save/Update & Compose Prompt</button>
    <div id="msg"></div>
  </div>
  <div>
    <button onclick="load()">Reload</button>
    <table id="tbl">
      <thead>
        <tr><th>Name</th><th>Brand</th><th>Type</th><th>Season</th><th>Avail</th><th>Price</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<h3>📄 Current Catalog Prompt</h3>
<pre id="prompt"><small>Loading…</small></pre>

<script>
async function load(){
  const r=await fetch('/catalog'); const js=await r.json();
  const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
  (js.items||[]).forEach(p=>{
    const seasons = Array.isArray(p.season) ? p.season.join(', ') : (p.season||'');
    const tr=document.createElement('tr');
    tr.innerHTML = `
      <td>${p.name||''}</td>
      <td>${p.brand||''}</td>
      <td>${p.type||''}</td>
      <td>${seasons}</td>
      <td>${p.available?'✅':'❌'}</td>
      <td>${(p.price_usd??'')}</td>`;
    tb.appendChild(tr);
  });
  document.getElementById('prompt').textContent = js.catalog_prompt||'(empty)';
}

async function save(){
  const body={
    name:document.getElementById('name').value,
    brand:document.getElementById('brand').value,
    aliases:(document.getElementById('aliases').value||'').split(',').map(s=>s.trim()).filter(Boolean),
    type:(document.getElementById('type').value||'unisex').trim(),
    season:(document.getElementById('season').value||'').split(',').map(s=>s.trim()).filter(Boolean),
    notes:document.getElementById('notes').value,
    price_usd:parseFloat(document.getElementById('price').value||''),
    available:document.getElementById('avail').checked
  };
  const r=await fetch('/catalog',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const js=await r.json();
  document.getElementById('msg').textContent = js.ok?'Saved & Prompt regenerated ✔':'Error: '+(js.error||'');
  await load();
}

load();
</script>
""".strip()

@app.route("/catalog_seed10", methods=["POST"])
def catalog_seed10():
    global CATALOG_ITEMS, CATALOG_PROMPT
    CATALOG_ITEMS = CATALOG_ITEMS_SEED[:]  # copy
    _save_catalog_items(CATALOG_ITEMS)
    CATALOG_PROMPT = _regenerate_and_persist_catalog_prompt(CATALOG_ITEMS)
    return jsonify({"ok": True, "count": len(CATALOG_ITEMS), "catalog_prompt": CATALOG_PROMPT})

# ========== Catalog-as-Prompt (no runtime DB lookups) ==========
CATALOG_JSON = DATA_DIR / "catalog_items.json"
CATALOG_PROMPT_FILE = DATA_DIR / "catalog_prompt.txt"

CATALOG_ITEMS_SEED = [
    {
        "name": "Dior Sauvage EDT",
        "brand": "Dior",
        "aliases": ["سوفاج", "ديور سوفاج", "sauvage"],
        "type": "male",
        "notes": "Fresh spicy; bergamot; ambroxan",
        "season": ["summer", "spring", "all"],
        "price_usd": 98,
        "available": True
    },
    {
        "name": "Bleu de Chanel EDP",
        "brand": "Chanel",
        "aliases": ["بلو دي شانيل", "بلو شانيل", "bleu de chanel"],
        "type": "male",
        "notes": "Citrus; wood; incense",
        "season": ["all"],
        "price_usd": 120,
        "available": True
    },
    {
        "name": "Versace Dylan Blue",
        "brand": "Versace",
        "aliases": ["ديلان بلو", "versace dylan blue", "ديلن بلو"],
        "type": "male",
        "notes": "Aquatic; citrus; ambroxan",
        "season": ["summer", "spring"],
        "price_usd": 80,
        "available": True
    },
    {
        "name": "Montblanc Legend",
        "brand": "Montblanc",
        "aliases": ["ليجند", "legend", "مونت بلانك ليجند"],
        "type": "male",
        "notes": "Lavender; pineapple; sandalwood",
        "season": ["spring", "summer"],
        "price_usd": 85,
        "available": True
    },
    {
        "name": "Acqua di Giò Profumo",
        "brand": "Giorgio Armani",
        "aliases": ["اكوا دي جيو بروفومو", "acqua di gio profumo", "ادج بروفومو"],
        "type": "male",
        "notes": "Aquatic; incense; patchouli",
        "season": ["summer", "evening"],
        "price_usd": 115,
        "available": True
    },
    {
        "name": "Paco Rabanne 1 Million",
        "brand": "Paco Rabanne",
        "aliases": ["ون مليون", "1 مليون", "one million"],
        "type": "male",
        "notes": "Warm spicy; cinnamon; amber",
        "season": ["fall", "winter", "evening"],
        "price_usd": 90,
        "available": True
    },
    {
        "name": "Tom Ford Noir Extreme",
        "brand": "Tom Ford",
        "aliases": ["نوار اكستريم", "noir extreme"],
        "type": "male",
        "notes": "Cardamom; kulfi accord; amber",
        "season": ["winter", "evening"],
        "price_usd": 150,
        "available": False
    },
    {
        "name": "YSL La Nuit de L’Homme",
        "brand": "Yves Saint Laurent",
        "aliases": ["لانوي دي لوم", "la nuit de lhomme", "لانوي"],
        "type": "male",
        "notes": "Cardamom; lavender; cedar",
        "season": ["fall", "winter", "evening"],
        "price_usd": 110,
        "available": True
    },
    {
        "name": "Creed Aventus",
        "brand": "Creed",
        "aliases": ["افنتوس", "aventus", "كريد افنتوس"],
        "type": "male",
        "notes": "Pineapple; birch; musk",
        "season": ["all"],
        "price_usd": 350,
        "available": False
    },
    {
        "name": "Jo Malone Wood Sage & Sea Salt",
        "brand": "Jo Malone",
        "aliases": ["وود سيج اند سي سولت", "wood sage sea salt"],
        "type": "unisex",
        "notes": "Aromatic; sea salt; sage",
        "season": ["summer", "spring", "daytime"],
        "price_usd": 145,
        "available": True
    }
]

def _load_catalog_items():
    if CATALOG_JSON.exists():
        try:
            data = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            print("[CATALOG] load error:", e)
    try:
        CATALOG_JSON.write_text(json.dumps(CATALOG_ITEMS_SEED, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("[CATALOG] seed write error:", e)
    return CATALOG_ITEMS_SEED[:]

def _save_catalog_items(items: list):
    CATALOG_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def _fallback_catalog_prompt(items: list) -> str:
    if not items:
        return "CATALOG:\n(EMPTY)"
    lines = []
    for p in items:
        names = [p.get("name","")] + (p.get("aliases") or [])
        names = ", ".join([n for n in names if n])
        seasons = ", ".join(p.get("season", [])) if isinstance(p.get("season"), list) else (p.get("season") or "")
        lines.append(
            f"- name: {p.get('name','')} | brand: {p.get('brand','')} | type: {p.get('type','')} | "
            f"aliases: {names} | notes: {p.get('notes','')} | season: {seasons} | "
            f"price_usd: {p.get('price_usd','')} | available: { 'yes' if p.get('available') else 'no' }"
        )
    en = "Perfume catalog (ground truth):\n" + "\n".join(lines)
    ar = "كتالوج العطور (مرجع الحقيقة):\n" + "\n".join(lines)
    return en + "\n\n" + ar

def _generate_catalog_prompt_with_gpt(items: list) -> str:
    if not OPENAI_API_KEY or "replace_me" in OPENAI_API_KEY.lower() or "REPLACE_ME" in OPENAI_API_KEY:
        return _fallback_catalog_prompt(items)

    sys = (
        "You are a data composer. Generate a concise bilingual (English first, then Arabic) "
        "knowledge block describing a perfume catalog for a sales assistant. Keep it truthfully "
        "grounded ONLY in the provided items. For EACH item include: name, brand, type (male/female/unisex), "
        "notable notes (short), SEASON tags (e.g., summer/spring/fall/winter/evening/all), availability (yes/no), "
        "approx price in USD, and common aliases for fuzzy matches. "
        "Use compact bullet points. Do not add extra items. Do not invent data."
    )

    user = {"task": "compose_catalog_prompt", "items": items}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}
        ],
        "temperature": 0.2,
        "max_tokens": 700
    }
    try:
        resp = requests.post(OPENAI_BASE, headers=headers, json=body, timeout=40)
        resp.raise_for_status()
        txt = resp.json()["choices"][0]["message"]["content"].strip()
        return txt
    except Exception as e:
        print("[CATALOG] GPT compose error:", e)
        return _fallback_catalog_prompt(items)

def _regenerate_and_persist_catalog_prompt(items: list) -> str:
    text = _generate_catalog_prompt_with_gpt(items)
    CATALOG_PROMPT_FILE.write_text(text, encoding="utf-8")
    return text

CATALOG_ITEMS = _load_catalog_items()

# ✅ لا تسوي GPT call وقت الإقلاع
try:
    if CATALOG_PROMPT_FILE.exists():
        CATALOG_PROMPT = CATALOG_PROMPT_FILE.read_text(encoding="utf-8")
    else:
        CATALOG_PROMPT = _fallback_catalog_prompt(CATALOG_ITEMS)
        CATALOG_PROMPT_FILE.write_text(CATALOG_PROMPT, encoding="utf-8")
except Exception:
    CATALOG_PROMPT = _fallback_catalog_prompt(CATALOG_ITEMS)
    
def _load_catalog_prompt_from_disk() -> str:
    try:
        if CATALOG_PROMPT_FILE.exists():
            return CATALOG_PROMPT_FILE.read_text(encoding="utf-8")
    except Exception:
        pass
    return CATALOG_PROMPT

# ===================== MOVEMENT remote_control (merged) =====================
@socketio.on('remote_control')
def on_remote_control(data):
    """
    data = {
      "from": "owner_phone_1001_move",
      "to": "robot_move_1001",
      "ctrl_type": "move" | "turn" | "stop",
      "value": 0.3,
      "duration_ms": 800
    }
    """
    frm      = (data or {}).get("from")
    to       = (data or {}).get("to")
    ctrl     = (data or {}).get("ctrl_type")

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

    # ✅ نفس سلوك movement_server: اذا الروبوت اوفلاين رجّع ack خطأ
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

# ===================== /chat + dashboards + mem (مثل ما هو) =====================
@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json(silent=True) or {}
        user_text = (data.get("user_text") or "").strip()
        lang = (data.get("lang") or "en-US").strip()
        uid  = (data.get("user_id") or "anon").strip()
        intent_only = bool(data.get("intent_only", False))

        if not user_text:
            return jsonify({"reply": "No input."}), 400

        _touch_user(uid)
        _push_turn(uid, "user", user_text)
        _extract_prefs(uid, user_text, lang)

        def _intent_call_support(text: str) -> bool:
            t = text.lower()
            keys = [
                "call customer service","call support","contact support","helpdesk","help desk",
                "اتصل بخدمة العملاء","اتصل بخدمه العملاء","اتصل بالدعم","كلم خدمة العملاء","دز اتصال"
            ]
            return any(k in t for k in keys)

        if intent_only:
            intent = "call_customer_service" if _intent_call_support(user_text) else "none"
            return jsonify({"intent": intent})

        try:
            matches = [p["name"] for p in CATALOG_ITEMS if p.get("name") and p["name"].lower() in user_text.lower()]
            for n in matches: _append_recent_item(uid, n)
        except Exception:
            pass

        messages = _build_messages(user_text, lang, uid)

        try:
            reply = _openai_chat(messages)
        except Exception as e:
            print("[/chat AI ERROR]", e)
            reply = "تعذّر الحصول على رد من الذكاء الاصطناعي حاليًا." if lang.lower().startswith("ar") else \
                    "Couldn't get an AI reply right now."

        _push_turn(uid, "assistant", reply)
        _maybe_update_summary(uid)
        _mem_save(MEM)

        intent = "call_customer_service" if _intent_call_support(user_text) else "none"
        return jsonify({"reply": reply, "intent": intent})

    except Exception as e:
        print("[/chat ERROR]", e)
        if "ar" in (request.json or {}).get("lang","").lower():
            return jsonify({"reply": "تعذّر معالجة الطلب حالياً."}), 500
        else:
            return jsonify({"reply": "Couldn’t process the request now."}), 500

@app.route("/catalog_prompt", methods=["GET", "POST", "PUT"])
def catalog_prompt_view():
    if request.method in ("POST", "PUT"):
        data = request.get_json(silent=True) or {}
        text = (data.get("catalog_prompt") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "empty catalog_prompt"}), 400
        CATALOG_PROMPT_FILE.write_text(text, encoding="utf-8")
        return jsonify({"ok": True, "length": len(text)})
    return jsonify({"catalog_prompt": _load_catalog_prompt_from_disk()})

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
    _save_prompt(CURRENT_PROMPT)
    return jsonify({"ok": True})

@app.route("/prompt_ui")
def prompt_ui():
    return f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Kebbi Prompt Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body {{ font-family: system-ui, Arial; margin: 24px; }}
textarea {{ width: 100%; height: 320px; }}
button {{ padding: 10px 16px; margin-top: 10px; }}
#msg {{ margin-top: 10px; }}
</style>
</head>
<body>
<h2>🔧 Kebbi Prompt Dashboard</h2>
<p>عدّل البرومبت ثم اضغط حفظ. التغيير فوري ويُحفظ في <code>{PROMPT_FILE.name}</code>.</p>
<textarea id="prompt">{CURRENT_PROMPT.replace("</","&lt;/")}</textarea>
<br/>
<button onclick="save()">Save</button>
<div id="msg"></div>
<script>
async function save(){{
  const p = document.getElementById('prompt').value;
  const r = await fetch('/prompt', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{prompt:p}})}});
  const js = await r.json();
  document.getElementById('msg').textContent = js.ok ? 'Saved ✔' : ('Error: ' + (js.error||''));
}}
</script>
</body>
</html>
""".strip()

@app.route("/mem/<uid>", methods=["GET","DELETE","POST"])
def mem_user(uid):
    _ensure_user(uid)
    if request.method == "GET":
        return jsonify(MEM[uid])
    if request.method == "DELETE":
        MEM.pop(uid, None)
        _mem_save(MEM)
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    facts = data.get("facts", {})
    if isinstance(facts, dict):
        MEM[uid]["facts"].update(facts)
    _mem_save(MEM)
    return jsonify({"ok": True, "facts": MEM[uid]["facts"]})

@app.route("/mem_ui")
def mem_ui():
    return """
<!doctype html><meta charset="utf-8">
<title>Kebbi Memory</title>
<style>
body{font-family:system-ui,Arial;margin:24px;max-width:900px}
input,textarea{width:100%;margin:6px 0;padding:8px}
table{border-collapse:collapse;margin-top:16px;width:100%}
td,th{border:1px solid #ddd;padding:8px;text-align:left}
pre{background:#f6f6f6;padding:12px;white-space:pre-wrap}
</style>
<h2>🧠 Memory Browser</h2>
<input id="uid" placeholder="user_id e.g. phone_0001">
<button onclick="load()">Load</button>
<button onclick="wipe()">Delete</button>

<h3>Facts (long-term)</h3>
<textarea id="facts" rows="6" placeholder='{"language":"ar","gender_pref":"male","budget_usd":100}'></textarea>
<button onclick="saveFacts()">Save Facts</button>

<h3>Raw</h3>
<pre id="raw">(empty)</pre>

<script>
async function load(){
  const uid=document.getElementById('uid').value.trim();
  if(!uid) return;
  const r=await fetch('/mem/'+encodeURIComponent(uid));
  const js=await r.json();
  document.getElementById('raw').textContent = JSON.stringify(js,null,2);
  document.getElementById('facts').value = JSON.stringify(js.facts||{},null,2);
}
async function wipe(){
  const uid=document.getElementById('uid').value.trim();
  if(!uid) return;
  await fetch('/mem/'+encodeURIComponent(uid),{method:'DELETE'});
  document.getElementById('raw').textContent='(deleted)';
}
async function saveFacts(){
  const uid=document.getElementById('uid').value.trim();
  if(!uid) return;
  const facts = JSON.parse(document.getElementById('facts').value||"{}");
  const r=await fetch('/mem/'+encodeURIComponent(uid),{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({facts})
  });
  await load();
}
</script>
""".strip()

def _cleanup_memory():
    try:
        cutoff = _now_epoch() - (MEM_CLEANUP_DAYS*24*3600)
        removed = []
        for uid,rec in list(MEM.items()):
            if rec.get("updated_at",0) < cutoff:
                removed.append(uid); MEM.pop(uid, None)
        if removed:
            print("[MEM] cleanup removed:", removed)
            _mem_save(MEM)
    except Exception as e:
        print("[MEM] cleanup error:", e)
    finally:
        threading.Timer(12*3600, _cleanup_memory).start()

threading.Timer(3, _cleanup_memory).start()

# ====== تشغيل ======
if __name__ == "__main__":
    import socket
    ip = socket.gethostbyname(socket.gethostname())
    print(f"🔥 Aljazari Signaling (Merged) on http://{ip}:5000")
    port = int(os.getenv("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port)
