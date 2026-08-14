import os
import json
import time
import hashlib
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, session, redirect, url_for

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))

# ============================================================
# Secrets: set these as environment variables on a real server.
# Never publish real Telegram tokens in source code.
# ============================================================
BOT_TOKEN = "8690826652:AAG5HD9U-Pz0919YSAZkdT_p-LBw2IrXCeU"
CHAT_ID =  "7977012474"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ============================================================
# Files
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
STATE_FILE = os.path.join(BASE_DIR, "runtime_state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs.txt")

DEFAULT_SETTINGS = {
    "monitor_condition": "available",
    "source_url": "",
    "monitor_mode": "webhook",      # webhook | polling
    "source_method": "GET",         # GET | POST (polling only)
    "action_url": "https://httpbin.org/post",
    "auto_action_enabled": False,
    "poll_interval": 10,
}

DEFAULT_STATE = {
    "match_active": False,
    "last_data_hash": "",
    "last_data_time": None,
    "last_status": "النظام بدأ حديثًا",
    "last_action_time": None,
    "last_action_status": None,
    "last_source": None,
    "last_error": None,
}

ALLOWED_SETTING_KEYS = set(DEFAULT_SETTINGS)
settings_lock = threading.RLock()
state_lock = threading.RLock()
process_lock = threading.Lock()
stop_event = threading.Event()

def load_json_file(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return deepcopy(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        result = deepcopy(default)
        result.update(value)
        return result
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[!] Failed to load {path}: {exc}")
        return deepcopy(default)

settings = load_json_file(SETTINGS_FILE, DEFAULT_SETTINGS)
runtime_state = load_json_file(STATE_FILE, DEFAULT_STATE)

def save_json_file(path: str, data: dict) -> bool:
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        return True
    except OSError as exc:
        print(f"[!] Failed to save {path}: {exc}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False

def save_settings() -> bool:
    with settings_lock:
        return save_json_file(SETTINGS_FILE, settings)

def save_state() -> bool:
    with state_lock:
        return save_json_file(STATE_FILE, runtime_state)

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def save_log(message: str, result: str = "INFO") -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            safe = str(message).replace("\n", "\\n")
            f.write(f"{now_iso()} | {result} | {safe}\n")
    except OSError as exc:
        print(f"[!] Failed to write log: {exc}")

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
        save_log("Telegram is not configured", "TELEGRAM_NOT_CONFIGURED")
        return False

    icon = "✅" if success else "❌"
    full_text = f"{icon} {message}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            api_url,
            json={"chat_id": CHAT_ID, "text": full_text},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("ok") is True:
            return True
        save_log(f"Telegram API error: {body}", "TELEGRAM_ERROR")
        return False
    except (requests.RequestException, ValueError) as exc:
        save_log(str(exc), "TELEGRAM_ERROR")
        return False

def execute_action(data: Any, action_url: str) -> dict:
    try:
        response = requests.post(action_url, json=data, timeout=15)
        status = response.status_code
        ok = 200 <= status < 300

        with state_lock:
            runtime_state["last_action_time"] = now_iso()
            runtime_state["last_action_status"] = status
            runtime_state["last_error"] = None if ok else f"HTTP {status}"
        save_state()

        if ok:
            save_log(f"Action HTTP {status}", "ACTION_SUCCESS")
            send_telegram_alert(f"تم تنفيذ الإجراء بنجاح.\nHTTP {status}", True)
            return {"ok": True, "status_code": status}

        save_log(f"Action HTTP {status}", "ACTION_FAILED")
        send_telegram_alert(f"فشل تنفيذ الإجراء.\nHTTP {status}", False)
        return {"ok": False, "status_code": status}

    except requests.RequestException as exc:
        with state_lock:
            runtime_state["last_action_time"] = now_iso()
            runtime_state["last_action_status"] = None
            runtime_state["last_error"] = str(exc)
        save_state()
        save_log(str(exc), "ACTION_ERROR")
        send_telegram_alert(f"خطأ أثناء تنفيذ الإجراء: {exc}", False)
        return {"ok": False, "error": str(exc)}

def process_data(data: Any, source: str) -> dict:
    # Serialize the transition decision so simultaneous webhooks cannot
    # both see match_active=False and execute twice.
    with process_lock:
        with settings_lock:
            condition = str(settings["monitor_condition"])
            auto_action = bool(settings["auto_action_enabled"])
            action_url = str(settings["action_url"])

        data_text = normalize_data(data)
        data_hash = hashlib.sha256(data_text.encode("utf-8")).hexdigest()
        matched = contains_condition(data, condition)

        with state_lock:
            was_active = bool(runtime_state["match_active"])
            runtime_state["last_data_hash"] = data_hash
            runtime_state["last_data_time"] = now_iso()
            runtime_state["last_source"] = source
            runtime_state["last_error"] = None

        if not matched:
            with state_lock:
                runtime_state["match_active"] = False
                runtime_state["last_status"] = "وصلت البيانات والشرط غير متحقق"
            save_state()
            save_log(f"Source={source} | condition='{condition}'", "NO_MATCH")
            return {"matched": False, "alert_sent": False, "action_executed": False}

        if was_active:
            with state_lock:
                runtime_state["last_status"] = "الشرط ما زال متحققًا - تم منع التكرار"
            save_state()
            return {
                "matched": True,
                "alert_sent": False,
                "action_executed": False,
                "duplicate_suppressed": True,
            }

        # New matching state: mark active BEFORE external calls.
        with state_lock:
            runtime_state["match_active"] = True
            runtime_state["last_status"] = "الشرط متحقق لأول مرة"
        save_state()
        save_log(f"Source={source} | condition='{condition}'", "MATCH")

        alert_sent = send_telegram_alert(
            f"تم العثور على الشرط المطلوب:\n'{condition}'\nالمصدر: {source}",
            True,
        )

        if not auto_action:
            save_log("Auto action is disabled", "ACTION_DISABLED")
            return {"matched": True, "alert_sent": alert_sent, "action_executed": False}

        if not is_valid_http_url(action_url):
            save_log("Invalid action_url", "ACTION_ERROR")
            send_telegram_alert("رابط الإجراء غير صالح.", False)
            return {
                "matched": True,
                "alert_sent": alert_sent,
                "action_executed": False,
                "error": "Invalid action_url",
            }

        action_result = execute_action(data, action_url)
        return {
            "matched": True,
            "alert_sent": alert_sent,
            "action_executed": action_result.get("ok", False),
            "action_result": action_result,
        }

def check_webhook_secret() -> bool:
    if not WEBHOOK_SECRET:
        return True
    supplied = request.headers.get("X-Webhook-Secret", "")
    return supplied == WEBHOOK_SECRET

@app.route("/webhook", methods=["POST"])
def webhook():
    if not check_webhook_secret():
        return jsonify({"ok": False, "error": "Unauthorized webhook"}), 401

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"ok": False, "error": "Expected valid JSON"}), 400

    try:
        result = process_data(data, "WEBHOOK")
        return jsonify({"ok": True, "result": result}), 200
    except Exception as exc:
        save_log(str(exc), "WEBHOOK_ERROR")
        send_telegram_alert(f"خطأ في Webhook: {exc}", False)
        return jsonify({"ok": False, "error": "Internal processing error"}), 500

def monitor_source() -> None:
    print("[*] Monitoring thread started")
    while not stop_event.is_set():
        try:
            with settings_lock:
                mode = settings["monitor_mode"]
                source_url = str(settings["source_url"]).strip()
                source_method = str(settings["source_method"]).upper()
                interval = max(2, int(settings["poll_interval"]))

            if mode != "polling":
                stop_event.wait(2)
                continue

            if not is_valid_http_url(source_url):
                with state_lock:
                    runtime_state["last_status"] = "polling مفعل لكن source_url غير صالح"
                save_state()
                stop_event.wait(interval)
                continue

            if source_method == "POST":
                response = requests.post(source_url, timeout=15)
            else:
                response = requests.get(source_url, timeout=15)

            response.raise_for_status()

            try:
                payload = response.json()
            except ValueError:
                payload = response.text

            process_data(payload, "POLLING")

        except requests.RequestException as exc:
            with state_lock:
                runtime_state["last_status"] = f"خطأ اتصال بالمصدر: {exc}"
                runtime_state["last_error"] = str(exc)
            save_state()
            save_log(str(exc), "SOURCE_ERROR")
        except (TypeError, ValueError) as exc:
            with state_lock:
                runtime_state["last_status"] = f"إعدادات polling غير صحيحة: {exc}"
                runtime_state["last_error"] = str(exc)
            save_state()
            save_log(str(exc), "POLLING_CONFIG_ERROR")
        except Exception as exc:
            with state_lock:
                runtime_state["last_status"] = f"خطأ مراقبة: {exc}"
                runtime_state["last_error"] = str(exc)
            save_state()
            save_log(str(exc), "MONITOR_ERROR")

        with settings_lock:
            try:
                wait_seconds = max(2, int(settings["poll_interval"]))
            except (TypeError, ValueError):
                wait_seconds = 10
        stop_event.wait(wait_seconds)

def login_required():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return None

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("home"))
        return render_template("dashboard.html", login_error="كلمة المرور غير صحيحة")
    return render_template("dashboard.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/", methods=["GET"])
def home():
    redirect_response = login_required()
    if redirect_response:
        return redirect_response
    with settings_lock:
        current_settings = deepcopy(settings)
    with state_lock:
        current_state = deepcopy(runtime_state)
    return render_template("dashboard.html", settings=current_settings, state=current_state, logged_in=True)

@app.route("/update_settings", methods=["POST"])
def update_settings():
    redirect_response = login_required()
    if redirect_response:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid JSON object"}), 400

    unknown = set(data) - ALLOWED_SETTING_KEYS
    if unknown:
        return jsonify({"ok": False, "error": f"Unknown setting(s): {sorted(unknown)}"}), 400

    new_values = dict(data)

    if "monitor_mode" in new_values and new_values["monitor_mode"] not in {"webhook", "polling"}:
        return jsonify({"ok": False, "error": "monitor_mode must be webhook or polling"}), 400
    if "source_method" in new_values and new_values["source_method"] not in {"GET", "POST"}:
        return jsonify({"ok": False, "error": "source_method must be GET or POST"}), 400
    if "monitor_condition" in new_values and (
        not isinstance(new_values["monitor_condition"], str)
        or not new_values["monitor_condition"].strip()
    ):
        return jsonify({"ok": False, "error": "monitor_condition must be non-empty text"}), 400

    if "poll_interval" in new_values:
        try:
            new_values["poll_interval"] = int(new_values["poll_interval"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "poll_interval must be an integer"}), 400
        if new_values["poll_interval"] < 2:
            return jsonify({"ok": False, "error": "poll_interval must be >= 2"}), 400

    if "auto_action_enabled" in new_values and not isinstance(new_values["auto_action_enabled"], bool):
        return jsonify({"ok": False, "error": "auto_action_enabled must be boolean"}), 400

    if "source_url" in new_values and new_values["source_url"]:
        if not is_valid_http_url(new_values["source_url"]):
            return jsonify({"ok": False, "error": "Invalid source_url"}), 400
    if "action_url" in new_values:
        if not is_valid_http_url(new_values["action_url"]):
            return jsonify({"ok": False, "error": "Invalid action_url"}), 400

    condition_changed = False
    with settings_lock:
        if "monitor_condition" in new_values:
            condition_changed = str(settings["monitor_condition"]) != str(new_values["monitor_condition"])
        settings.update(new_values)
    save_settings()

    if condition_changed:
        with state_lock:
            runtime_state["match_active"] = False
            runtime_state["last_status"] = "تم تغيير الشرط - إعادة تسليح المراقبة"
        save_state()

    save_log(json.dumps(new_values, ensure_ascii=False), "SETTINGS_UPDATED")
    send_telegram_alert("تم تحديث إعدادات النظام.", True)

    with settings_lock:
        current_settings = deepcopy(settings)
    return jsonify({"ok": True, "settings": current_settings}), 200

@app.route("/get_logs", methods=["GET"])
def get_logs():
    redirect_response = login_required()
    if redirect_response:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"ok": True, "logs": []}), 200
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return jsonify({"ok": True, "logs": lines[-100:]}), 200
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/status", methods=["GET"])
def status():
    redirect_response = login_required()
    if redirect_response:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    with settings_lock:
        current_settings = deepcopy(settings)
    with state_lock:
        current_state = deepcopy(runtime_state)
    return jsonify({
        "ok": True,
        "settings": current_settings,
        "state": current_state,
        "telegram_configured": configured_telegram(),
    }), 200

def main() -> None:
    # Local mode: polling works here.
    # On WSGI hosts such as PythonAnywhere, app.run() is not used.
    monitor_thread = threading.Thread(
        target=monitor_source,
        name="source-monitor",
        daemon=True,
    )
    monitor_thread.start()

    print("=" * 60)
    print("Webhook / Polling Automation Server")
    print("Dashboard : http://127.0.0.1:5000/")
    print("Webhook   : POST /webhook")
    print("Status    : GET  /status")
    print("=" * 60)

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
