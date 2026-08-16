import os
import re
import json
import time
import sqlite3
import hashlib
import threading
import requests
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, session, redirect, url_for

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
app.secret_key = os.getenv("FLASK_SECRET_KEY") or "CHANGE_THIS_SECRET_KEY"

# ---------------------------------------------------------------------------
# Secrets — read ONLY from environment variables (Railway > Variables tab)
# Never hard-code tokens here.
# ---------------------------------------------------------------------------
BOT_TOKEN = "8847474876:AAFI6sSQDiO3HD94CRJVNw_jGd9bUrl-lg4"
CHAT_ID = "7977012474"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR lets you point the database at a persistent volume (e.g. Railway Volume
# mounted at /app/data). If DATA_DIR is not set, it falls back to the app folder —
# fine for local runs or a VPS with a normal persistent disk.
DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "monitor.db")
HISTORY_KEEP_PER_TARGET = 300  # rows kept per target before trimming

db_lock = threading.RLock()
stop_event = threading.Event()
_poll_tracker: dict = {}  # target_id -> last poll timestamp (in-memory)

REQUIRED_TEXT_FIELDS = {
    "name", "site_type", "monitor_condition", "target_text", "source_url",
    "monitor_mode", "source_method", "price_selector", "stock_selector",
    "price_condition", "action_url", "action_method", "action_body", "use_browser",
}
JSON_LIST_FIELDS = {"required_terms", "forbidden_terms"}
INT_FIELDS = {"poll_interval"}
NULLABLE_INT_FIELDS = {"min_stock"}
BOOL_FIELDS = {"auto_action_enabled", "is_active"}

ALLOWED_TARGET_KEYS = (
    REQUIRED_TEXT_FIELDS | JSON_LIST_FIELDS | INT_FIELDS | NULLABLE_INT_FIELDS | BOOL_FIELDS
)

SITE_PRESETS = {
    "generic": {"label": "عام / أي موقع", "hint": ""},
    "amazon": {
        "label": "أمازون (Amazon)",
        "hint": (
            "أمازون يستخدم حماية قوية ضد الروبوتات وقد يحظر الطلبات المتكررة أو يعرض "
            "صفحة تحقق (CAPTCHA). استخدم use_browser=playwright وفاصل زمني لا يقل عن "
            "60 ثانية. أمثلة selectors شائعة (قد تتغير حسب النسخة/الدولة): "
            "السعر: span.a-price-whole | التوفر: #availability span"
        ),
    },
    "noon": {
        "label": "نون (Noon)",
        "hint": "مواقع التجارة الإلكترونية المحلية غالبًا أقل تشددًا من أمازون لكنها تتغير بدون سابق إنذار — راقب السجل بعد أي تحديث.",
    },
    "custom": {"label": "مخصص", "hint": ""},
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db_lock, get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT 'بدون اسم',
            site_type TEXT NOT NULL DEFAULT 'generic',
            monitor_condition TEXT NOT NULL DEFAULT 'available',
            target_text TEXT NOT NULL DEFAULT '',
            required_terms TEXT NOT NULL DEFAULT '[]',
            forbidden_terms TEXT NOT NULL DEFAULT '[]',
            source_url TEXT NOT NULL DEFAULT '',
            monitor_mode TEXT NOT NULL DEFAULT 'polling',
            source_method TEXT NOT NULL DEFAULT 'GET',
            poll_interval INTEGER NOT NULL DEFAULT 30,
            price_selector TEXT NOT NULL DEFAULT '',
            stock_selector TEXT NOT NULL DEFAULT '',
            price_condition TEXT NOT NULL DEFAULT '',
            min_stock INTEGER,
            action_url TEXT NOT NULL DEFAULT '',
            action_method TEXT NOT NULL DEFAULT 'POST',
            action_body TEXT NOT NULL DEFAULT '',
            auto_action_enabled INTEGER NOT NULL DEFAULT 0,
            use_browser TEXT NOT NULL DEFAULT 'auto',
            is_active INTEGER NOT NULL DEFAULT 1,
            match_active INTEGER NOT NULL DEFAULT 0,
            last_status TEXT NOT NULL DEFAULT 'بانتظار أول فحص',
            last_data_time TEXT,
            last_data_hash TEXT NOT NULL DEFAULT '',
            last_source TEXT,
            last_action_time TEXT,
            last_action_status TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_target ON history(target_id, id)")
        conn.commit()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def add_history(target_id: int, event_type: str, message: str = "") -> None:
    with db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO history (target_id, timestamp, event_type, message) VALUES (?,?,?,?)",
            (target_id, now_iso(), event_type, message),
        )
        conn.execute("""
            DELETE FROM history WHERE target_id = ? AND id NOT IN (
                SELECT id FROM history WHERE target_id = ? ORDER BY id DESC LIMIT ?
            )
        """, (target_id, target_id, HISTORY_KEEP_PER_TARGET))
        conn.commit()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["required_terms"] = json.loads(d.get("required_terms") or "[]")
    d["forbidden_terms"] = json.loads(d.get("forbidden_terms") or "[]")
    d["auto_action_enabled"] = bool(d.get("auto_action_enabled"))
    d["is_active"] = bool(d.get("is_active"))
    d["match_active"] = bool(d.get("match_active"))
    return d


def get_target(target_id: int):
    with db_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return row_to_dict(row) if row else None


def get_all_targets() -> list:
    with db_lock, get_conn() as conn:
        rows = conn.execute("SELECT * FROM targets ORDER BY id DESC").fetchall()
        return [row_to_dict(r) for r in rows]


def update_target_fields(target_id: int, fields: dict) -> None:
    if not fields:
        return
    cols, vals = zip(*fields.items())
    set_clause = ", ".join(f"{c} = ?" for c in cols)
    with db_lock, get_conn() as conn:
        conn.execute(f"UPDATE targets SET {set_clause} WHERE id = ?", (*vals, target_id))
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers (validation, http, telegram, extraction)
# ---------------------------------------------------------------------------
def is_valid_http_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def normalize_data(data: Any) -> str:
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(data)


def contains_condition(data: Any, condition: str) -> bool:
    if not isinstance(condition, str) or not condition.strip():
        return False
    return condition.casefold() in normalize_data(data).casefold()


def configured_telegram() -> bool:
    return bool(BOT_TOKEN and CHAT_ID)


def send_telegram_alert(message: str, success: bool = True) -> bool:
    if not configured_telegram():
        return False
    icon = "✅" if success else "❌"
    full_text = f"{icon} {message}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(api_url, json={"chat_id": CHAT_ID, "text": full_text}, timeout=10)
        response.raise_for_status()
        return bool(response.json().get("ok"))
    except (requests.RequestException, ValueError):
        return False


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.8",
}


def try_fetch_page_text(url: str, method: str = "GET", use_browser: str = "auto") -> Any:
    use_browser = (use_browser or "auto").lower()

    if use_browser in ("playwright", "auto"):
        try:
            from playwright.sync_api import sync_playwright
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
                    page = browser.new_page(user_agent=HTTP_HEADERS["User-Agent"])
                    page.goto(url, wait_until="networkidle", timeout=20000)
                    content = page.content()
                    browser.close()
                    return content
            except Exception:
                if use_browser == "playwright":
                    raise
        except Exception:
            pass  # fall back to requests

    if method.upper() == "POST":
        r = requests.post(url, headers=HTTP_HEADERS, timeout=15)
    else:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
    r.raise_for_status()
    return r.text


def _parse_number(value: str) -> float:
    s = str(value).strip().replace('\u00A0', '')
    cleaned = ''.join(ch for ch in s if ch.isdigit() or ch in '.,')
    if cleaned.count(',') > 1 and '.' in cleaned:
        cleaned = cleaned.replace(',', '')
    cleaned = cleaned.replace(',', '.') if cleaned.count('.') == 0 and ',' in cleaned else cleaned.replace(',', '')
    return float(cleaned)


def _check_price_condition(price: float, cond: str) -> bool:
    if not cond or not str(cond).strip():
        return True
    s = str(cond).strip()
    m = re.match(r'^\s*(<=|>=|<|>|==|=)?\s*([0-9.,]+)\s*$', s)
    if not m:
        return False
    op = m.group(1) or '<='
    try:
        target = _parse_number(m.group(2))
    except ValueError:
        return False
    if op in ('<', '<='):
        return price <= target if op == '<=' else price < target
    if op in ('>', '>='):
        return price >= target if op == '>=' else price > target
    if op in ('==', '='):
        return price == target
    return False


def _extract_with_bs4(html: str, selector: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return ''
    try:
        soup = BeautifulSoup(html, 'html.parser')
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ''
    except Exception:
        return ''


def _extract_simple_number_from_html(html: str) -> float:
    m = re.search(r'([0-9][0-9.,]{0,20}[0-9])', html)
    if not m:
        raise ValueError('no number')
    return _parse_number(m.group(1))


def extract_fields(html: str, price_selector: str, stock_selector: str) -> dict:
    out = {'price': None, 'stock': None}
    if not html:
        return out
    if price_selector:
        val = _extract_with_bs4(html, price_selector)
        try:
            out['price'] = _parse_number(val) if val else _extract_simple_number_from_html(html)
        except Exception:
            out['price'] = None
    if stock_selector:
        val = _extract_with_bs4(html, stock_selector)
        try:
            out['stock'] = int(_parse_number(val)) if val else int(_extract_simple_number_from_html(html))
        except Exception:
            out['stock'] = None
    return out


def execute_action(data: Any, action_url: str, method: str = "POST", body_text: str = "") -> dict:
    try:
        if method.upper() == "GET":
            try:
                params = json.loads(body_text) if body_text else {}
            except Exception:
                params = {}
            response = requests.get(action_url, params=params, timeout=15)
        else:
            try:
                json_body = json.loads(body_text) if body_text else data
            except Exception:
                json_body = data
            response = requests.post(action_url, json=json_body, timeout=15)

        status = response.status_code
        ok = 200 <= status < 300
        return {"ok": ok, "status_code": status}
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Core processing (per target)
# ---------------------------------------------------------------------------
def process_target(target: dict, data: Any, source: str) -> dict:
    tid = target["id"]

    page_text = None
    if isinstance(data, dict):
        if "page_text" in data:
            page_text = data["page_text"]
        elif "url" in data:
            try:
                page_text = try_fetch_page_text(str(data["url"]), "GET", target.get("use_browser", "auto"))
            except Exception as exc:
                update_target_fields(tid, {"last_status": f"تعذر جلب الصفحة: {exc}", "last_error": str(exc)})
                add_history(tid, "ERROR", str(exc))
                return {"matched": False, "error": str(exc)}

    data_text = page_text if page_text is not None else normalize_data(data)
    data_hash = hashlib.sha256(str(data_text).encode("utf-8")).hexdigest()

    required_terms = list(target.get("required_terms") or [])
    forbidden_terms = list(target.get("forbidden_terms") or [])
    target_text = (target.get("target_text") or "").strip()

    search_required = list(required_terms)
    if target_text and target_text not in search_required:
        search_required.append(target_text)

    if search_required:
        search_term = ",".join(search_required)
        matched = all(contains_condition(data_text, t) for t in search_required)
    else:
        search_term = target.get("monitor_condition", "")
        matched = contains_condition(data_text, search_term)

    if matched and forbidden_terms:
        for ft in forbidden_terms:
            if contains_condition(data_text, ft):
                matched = False
                add_history(tid, "FORBIDDEN_MATCH", f"مصطلح محظور: {ft}")
                break

    was_active = bool(target.get("match_active"))
    base_update = {"last_data_hash": data_hash, "last_data_time": now_iso(), "last_source": source, "last_error": None}

    if not matched:
        price_selector = target.get("price_selector") or ""
        stock_selector = target.get("stock_selector") or ""
        price_condition = target.get("price_condition") or ""
        min_stock = target.get("min_stock")
        price_ok = stock_ok = True
        if price_selector or stock_selector:
            try:
                extracted = extract_fields(str(data_text), price_selector, stock_selector)
                if price_selector and price_condition:
                    price_ok = extracted.get('price') is not None and _check_price_condition(extracted['price'], price_condition)
                if stock_selector and min_stock is not None:
                    stock_ok = extracted.get('stock') is not None and int(extracted['stock']) >= int(min_stock)
            except Exception as exc:
                add_history(tid, "EXTRACTION_ERROR", str(exc))
                price_ok = stock_ok = False
            if price_ok and stock_ok:
                matched = True

        if not matched:
            update_target_fields(tid, {**base_update, "match_active": 0, "last_status": "وصلت البيانات والشرط غير متحقق"})
            add_history(tid, "NO_MATCH", f"condition='{search_term}'")
            return {"matched": False}

    if was_active:
        update_target_fields(tid, {**base_update, "last_status": "الشرط ما زال متحققًا - تم منع التكرار"})
        return {"matched": True, "duplicate_suppressed": True}

    update_target_fields(tid, {**base_update, "match_active": 1, "last_status": "الشرط متحقق لأول مرة"})
    add_history(tid, "MATCH", f"condition='{search_term}'")

    alert_sent = send_telegram_alert(f"[{target.get('name','')}] تم العثور على الشرط:\n'{search_term}'\nالمصدر: {source}", True)

    if not target.get("auto_action_enabled"):
        add_history(tid, "ACTION_DISABLED", "")
        return {"matched": True, "alert_sent": alert_sent, "action_executed": False}

    action_url = target.get("action_url") or ""
    if not is_valid_http_url(action_url):
        add_history(tid, "ACTION_ERROR", "Invalid action_url")
        send_telegram_alert(f"[{target.get('name','')}] رابط الإجراء غير صالح.", False)
        return {"matched": True, "alert_sent": alert_sent, "action_executed": False, "error": "Invalid action_url"}

    result = execute_action(data_text, action_url, target.get("action_method", "POST"), target.get("action_body", ""))
    update_target_fields(tid, {
        "last_action_time": now_iso(),
        "last_action_status": result.get("status_code"),
        "last_error": None if result.get("ok") else result.get("error", f"HTTP {result.get('status_code')}"),
    })
    if result.get("ok"):
        add_history(tid, "ACTION_SUCCESS", f"HTTP {result.get('status_code')}")
        send_telegram_alert(f"[{target.get('name','')}] تم تنفيذ الإجراء بنجاح.\nHTTP {result.get('status_code')}", True)
    else:
        add_history(tid, "ACTION_FAILED", str(result.get("error") or result.get("status_code")))
        send_telegram_alert(f"[{target.get('name','')}] فشل تنفيذ الإجراء.\n{result.get('error') or result.get('status_code')}", False)

    return {"matched": True, "alert_sent": alert_sent, "action_executed": result.get("ok", False), "action_result": result}


def check_webhook_secret() -> bool:
    if not WEBHOOK_SECRET:
        return True
    return request.headers.get("X-Webhook-Secret", "") == WEBHOOK_SECRET


# ---------------------------------------------------------------------------
# Background monitoring thread — polls every active target independently
# ---------------------------------------------------------------------------
def monitor_loop() -> None:
    print("[*] Monitoring thread started")
    while not stop_event.is_set():
        try:
            targets = [t for t in get_all_targets() if t["is_active"] and t["monitor_mode"] == "polling"]
            now = time.time()
            for t in targets:
                tid = t["id"]
                interval = max(5, int(t.get("poll_interval") or 30))
                last = _poll_tracker.get(tid, 0)
                if now - last < interval:
                    continue
                _poll_tracker[tid] = now

                source_url = (t.get("source_url") or "").strip()
                if not is_valid_http_url(source_url):
                    update_target_fields(tid, {"last_status": "polling مفعل لكن source_url غير صالح"})
                    continue
                try:
                    page_text = try_fetch_page_text(source_url, t.get("source_method", "GET"), t.get("use_browser", "auto"))
                    process_target(t, page_text, "POLLING")
                except Exception as exc:
                    update_target_fields(tid, {"last_status": f"خطأ اتصال بالمصدر: {exc}", "last_error": str(exc)})
                    add_history(tid, "SOURCE_ERROR", str(exc))
        except Exception as exc:
            print(f"[monitor_loop] {exc}")
        stop_event.wait(2)


# ---------------------------------------------------------------------------
# Validation for create/update payloads
# ---------------------------------------------------------------------------
def validate_and_normalize(payload: dict, partial: bool):
    unknown = set(payload) - ALLOWED_TARGET_KEYS
    if unknown:
        return None, f"Unknown field(s): {sorted(unknown)}"

    out = {}
    for k, v in payload.items():
        if k in JSON_LIST_FIELDS:
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return None, f"{k} must be a list of strings"
            out[k] = json.dumps(v, ensure_ascii=False)
        elif k in INT_FIELDS:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return None, f"{k} must be an integer"
            if k == "poll_interval" and iv < 5:
                return None, "poll_interval must be >= 5"
            out[k] = iv
        elif k in NULLABLE_INT_FIELDS:
            out[k] = None if v in (None, "", "null") else int(v)
        elif k in BOOL_FIELDS:
            out[k] = 1 if bool(v) else 0
        else:
            out[k] = "" if v is None else str(v)

    if "monitor_mode" in out and out["monitor_mode"] not in {"webhook", "polling"}:
        return None, "monitor_mode must be webhook or polling"
    if "source_method" in out and out["source_method"] not in {"GET", "POST"}:
        return None, "source_method must be GET or POST"
    if "action_method" in out and out["action_method"] not in {"GET", "POST"}:
        return None, "action_method must be GET or POST"
    if "source_url" in out and out["source_url"] and not is_valid_http_url(out["source_url"]):
        return None, "Invalid source_url"
    if "action_url" in out and out["action_url"] and not is_valid_http_url(out["action_url"]):
        return None, "Invalid action_url"
    if "action_body" in out and out["action_body"]:
        try:
            json.loads(out["action_body"])
        except Exception:
            return None, "action_body must be valid JSON string"

    if not partial:
        if not out.get("name", "").strip():
            return None, "name is required"

    return out, None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def login_required():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("home"))
        return render_template("login.html", login_error="كلمة المرور غير صحيحة")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def home():
    redirect_response = login_required()
    if redirect_response:
        return redirect_response
    return render_template("dashboard.html", telegram_configured=configured_telegram(), site_presets=SITE_PRESETS)


# ---------------------------------------------------------------------------
# API — targets CRUD
# ---------------------------------------------------------------------------
@app.route("/api/targets", methods=["GET"])
def api_list_targets():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    return jsonify({"ok": True, "targets": get_all_targets()}), 200


@app.route("/api/targets", methods=["POST"])
def api_create_target():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid JSON object"}), 400

    normalized, err = validate_and_normalize(data, partial=False)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    defaults = {
        "name": "هدف جديد", "site_type": "generic", "monitor_condition": "available",
        "target_text": "", "required_terms": "[]", "forbidden_terms": "[]",
        "source_url": "", "monitor_mode": "polling", "source_method": "GET",
        "poll_interval": 30, "price_selector": "", "stock_selector": "",
        "price_condition": "", "min_stock": None, "action_url": "", "action_method": "POST",
        "action_body": "", "auto_action_enabled": 0, "use_browser": "auto", "is_active": 1,
    }
    defaults.update(normalized)
    defaults["created_at"] = now_iso()

    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    with db_lock, get_conn() as conn:
        cur = conn.execute(f"INSERT INTO targets ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
        conn.commit()
        new_id = cur.lastrowid

    add_history(new_id, "CREATED", f"تم إنشاء الهدف: {defaults['name']}")
    return jsonify({"ok": True, "target": get_target(new_id)}), 201


@app.route("/api/targets/<int:target_id>", methods=["GET"])
def api_get_target(target_id):
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    t = get_target(target_id)
    if not t:
        return jsonify({"ok": False, "error": "Not found"}), 404
    with db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM history WHERE target_id=? ORDER BY id DESC LIMIT 100", (target_id,)
        ).fetchall()
    return jsonify({"ok": True, "target": t, "history": [dict(r) for r in rows]}), 200


@app.route("/api/targets/<int:target_id>", methods=["PUT", "POST"])
def api_update_target(target_id):
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not get_target(target_id):
        return jsonify({"ok": False, "error": "Not found"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid JSON object"}), 400

    normalized, err = validate_and_normalize(data, partial=True)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    if normalized:
        update_target_fields(target_id, normalized)
        add_history(target_id, "SETTINGS_UPDATED", json.dumps(list(normalized.keys()), ensure_ascii=False))

    return jsonify({"ok": True, "target": get_target(target_id)}), 200


@app.route("/api/targets/<int:target_id>/delete", methods=["POST"])
def api_delete_target(target_id):
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    with db_lock, get_conn() as conn:
        conn.execute("DELETE FROM history WHERE target_id=?", (target_id,))
        conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
        conn.commit()
    _poll_tracker.pop(target_id, None)
    return jsonify({"ok": True}), 200


@app.route("/api/targets/<int:target_id>/toggle", methods=["POST"])
def api_toggle_target(target_id):
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    t = get_target(target_id)
    if not t:
        return jsonify({"ok": False, "error": "Not found"}), 404
    new_state = 0 if t["is_active"] else 1
    update_target_fields(target_id, {"is_active": new_state})
    add_history(target_id, "PAUSED" if not new_state else "RESUMED", "")
    return jsonify({"ok": True, "target": get_target(target_id)}), 200


@app.route("/api/targets/<int:target_id>/test", methods=["POST"])
def api_test_target(target_id):
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    t = get_target(target_id)
    if not t:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if not is_valid_http_url(t.get("source_url") or ""):
        return jsonify({"ok": False, "error": "source_url غير صالح"}), 400
    try:
        page_text = try_fetch_page_text(t["source_url"], t.get("source_method", "GET"), t.get("use_browser", "auto"))
        result = process_target(t, page_text, "TEST")
        return jsonify({"ok": True, "result": result, "target": get_target(target_id)}), 200
    except Exception as exc:
        add_history(target_id, "ERROR", str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/targets/<int:target_id>/history", methods=["GET"])
def api_target_history(target_id):
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    with db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM history WHERE target_id=? ORDER BY id DESC LIMIT 200", (target_id,)
        ).fetchall()
    return jsonify({"ok": True, "history": [dict(r) for r in rows]}), 200


@app.route("/api/summary", methods=["GET"])
def api_summary():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    targets = get_all_targets()
    return jsonify({
        "ok": True,
        "telegram_configured": configured_telegram(),
        "total": len(targets),
        "active": sum(1 for t in targets if t["is_active"]),
        "matched": sum(1 for t in targets if t["match_active"]),
    }), 200


# ---------------------------------------------------------------------------
# Webhook — per target: /webhook/<target_id>
# ---------------------------------------------------------------------------
@app.route("/webhook/<int:target_id>", methods=["POST"])
def webhook(target_id):
    if not check_webhook_secret():
        return jsonify({"ok": False, "error": "Unauthorized webhook"}), 401
    t = get_target(target_id)
    if not t:
        return jsonify({"ok": False, "error": "Target not found"}), 404

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"ok": False, "error": "Expected valid JSON"}), 400

    try:
        result = process_target(t, data, "WEBHOOK")
        return jsonify({"ok": True, "result": result}), 200
    except Exception as exc:
        add_history(target_id, "WEBHOOK_ERROR", str(exc))
        send_telegram_alert(f"خطأ في Webhook: {exc}", False)
        return jsonify({"ok": False, "error": "Internal processing error"}), 500


def main() -> None:
    init_db()
    monitor_thread = threading.Thread(target=monitor_loop, name="source-monitor", daemon=True)
    monitor_thread.start()

    if not configured_telegram():
        print("[!] Telegram BOT_TOKEN / CHAT_ID not set — alerts disabled")

    print("=" * 60)
    print("Multi-Target Monitoring Server")
    print("Dashboard : http://127.0.0.1:5000/")
    print("Webhook   : POST /webhook/<target_id>")
    print("=" * 60)

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
