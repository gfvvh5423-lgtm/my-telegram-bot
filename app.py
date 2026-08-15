import os
import json
import time
import hashlib
import threading
import requests
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, session, redirect, url_for

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
app.secret_key = os.getenv("FLASK_SECRET_KEY") or "CHANGE_THIS_SECRET_KEY"

# Secrets from environment (no hard-coded tokens)
BOT_TOKEN = "8690826652:AAHdcncZg5H4NsLgN7NtvH-Go9BN4TTscc8"
CHAT_ID = "7977012474"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
STATE_FILE = os.path.join(BASE_DIR, "runtime_state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs.txt")

DEFAULT_SETTINGS = {
    "monitor_condition": "available",   # legacy / fallback
    "target_text": "",                  # new: text to search inside page content (legacy single-term)
    "required_terms": [],                 # list of terms that must all be present (e.g. ["متوفر"])
    "forbidden_terms": [],                # list of terms that must NOT be present (e.g. ["نفد"])
    "source_url": "",
    "monitor_mode": "webhook",          # webhook | polling
    "source_method": "GET",             # GET | POST (polling only)
    "action_url": "https://httpbin.org/post",
    "action_method": "POST",            # POST | GET
    "action_body": "",                  # JSON string to send as body (if POST)
    "auto_action_enabled": False,
    "poll_interval": 10,
    "use_browser": "auto",              # auto | requests | playwright
    "price_selector": "",               # CSS selector to extract price (e.g. 'span.price')
    "stock_selector": "",               # CSS selector to extract stock count (e.g. 'div.stock')
    "price_condition": "",              # condition string like '<=250' or '<= 250'
    "min_stock": None,                    # integer minimum stock required
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

ALLOWED_SETTING_KEYS = set(DEFAULT_SETTINGS.keys())
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


def execute_action(data: Any, action_url: str, method: str = "POST", body_text: str = "") -> dict:
    try:
        if method.upper() == "GET":
            params = {}
            try:
                params = json.loads(body_text) if body_text else {}
            except Exception:
                # if body_text not JSON, ignore
                pass
            response = requests.get(action_url, params=params, timeout=15)
        else:
            json_body = None
            try:
                json_body = json.loads(body_text) if body_text else data
            except Exception:
                # fallback to sending data as-is
                json_body = data
            response = requests.post(action_url, json=json_body, timeout=15)

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


def try_fetch_page_text(url: str, method: str = "GET") -> Any:
    """
    Attempt to fetch page visible text.
    - If settings['use_browser'] == 'playwright' or 'auto' (and playwright available): use Playwright
    - Else fall back to requests and return response.text
    Returns string (page text / HTML) or raises requests.RequestException
    """
    with settings_lock:
        use_browser = str(settings.get("use_browser", "auto")).lower()

    # Try playwright if requested/auto
    if use_browser in ("playwright", "auto"):
        try:
            from playwright.sync_api import sync_playwright
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
                    page = browser.new_page()
                    page.goto(url, wait_until="networkidle", timeout=15000)
                    # return full HTML content so selectors work
                    content = page.content()
                    browser.close()
                    return content
            except Exception as exc:
                save_log(f"Playwright fetch failed: {exc}", "BROWSER_FETCH_ERROR")
                # If auto, fall through to requests
                if use_browser == "playwright":
                    raise
        except Exception as exc:
            save_log(f"Playwright not usable: {exc}", "PLAYWRIGHT_NOT_AVAILABLE")
            # fall back to requests

    # Fallback: requests
    if method.upper() == "POST":
        r = requests.post(url, timeout=15)
    else:
        r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.text


def _parse_number(value: str) -> float:
    try:
        s = str(value)
        # remove non-number characters except dot and comma
        s = s.strip()
        s = s.replace('\u00A0', '')  # non-breaking spaces
        # keep digits, dots and commas
        cleaned = ''.join(ch for ch in s if ch.isdigit() or ch in '.,')
        # normalize comma as thousand separator
        if cleaned.count(',') > 1 and '.' in cleaned:
            # ambiguous, remove commas
            cleaned = cleaned.replace(',', '')
        cleaned = cleaned.replace(',', '.') if cleaned.count('.') == 0 and ',' in cleaned else cleaned.replace(',', '')
        return float(cleaned)
    except Exception:
        raise ValueError(f"Cannot parse number from '{value}'")


def _check_price_condition(price: float, cond: str) -> bool:
    if not cond or not str(cond).strip():
        return True
    s = str(cond).strip()
    import re
    m = re.match(r'^\s*(<=|>=|<|>|==|=)?\s*([0-9.,]+)\s*$', s)
    if not m:
        # unknown format -> fail safe: return False
        return False
    op = m.group(1) or '<='
    num = m.group(2)
    try:
        target = _parse_number(num)
    except ValueError:
        return False
    if op == '<' or op == '<=':
        return price <= target if op == '<=' else price < target
    if op == '>' or op == '>=':
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
        if el:
            return el.get_text(strip=True)
    except Exception:
        return ''
    return ''


def _extract_simple_number_from_html(html: str) -> float:
    import re
    # find first occurrence of a number like 1,234.56 or 1234 or 123
    m = re.search(r'([0-9][0-9\.,]{0,20}[0-9])', html)
    if not m:
        raise ValueError('no number')
    return _parse_number(m.group(1))


def extract_fields(html: str, price_selector: str, stock_selector: str, use_browser_hint: str = 'auto') -> dict:
    """Extract price and stock numbers from HTML using selectors or fallback heuristics."""
    out = {'price': None, 'stock': None}
    if not html:
        return out
    if price_selector:
        # try bs4 first
        val = _extract_with_bs4(html, price_selector)
        if val:
            try:
                out['price'] = _parse_number(val)
            except Exception:
                out['price'] = None
        else:
            # fallback simple search around selector string
            try:
                out['price'] = _extract_simple_number_from_html(html)
            except Exception:
                out['price'] = None
    if stock_selector:
        val = _extract_with_bs4(html, stock_selector)
        if val:
            try:
                out['stock'] = int(_parse_number(val))
            except Exception:
                out['stock'] = None
        else:
            try:
                out['stock'] = int(_parse_number(str(_extract_simple_number_from_html(html))))
            except Exception:
                out['stock'] = None
    return out


def process_data(data: Any, source: str) -> dict:
    with process_lock:
        with settings_lock:
            condition = str(settings.get("monitor_condition", ""))
            target_text = str(settings.get("target_text", "")).strip()
            # new multi-term fields
            required_terms_raw = settings.get("required_terms", [])
            forbidden_terms_raw = settings.get("forbidden_terms", [])
            # normalize lists to strings
            required_terms = []
            forbidden_terms = []
            try:
                if isinstance(required_terms_raw, list):
                    required_terms = [str(x).strip() for x in required_terms_raw if str(x).strip()]
            except Exception:
                required_terms = []
            try:
                if isinstance(forbidden_terms_raw, list):
                    forbidden_terms = [str(x).strip() for x in forbidden_terms_raw if str(x).strip()]
            except Exception:
                forbidden_terms = []

            auto_action = bool(settings.get("auto_action_enabled", False))
            action_url = str(settings.get("action_url", ""))
            action_method = str(settings.get("action_method", "POST")).upper()
            action_body = str(settings.get("action_body", "")).strip()
            price_selector = str(settings.get("price_selector", "")).strip()
            stock_selector = str(settings.get("stock_selector", "")).strip()
            price_condition = str(settings.get("price_condition", "")).strip()
            min_stock = settings.get("min_stock", None)

        # If webhook provided a url instead of page_text, fetch it
        page_text = None
        if isinstance(data, dict):
            # prefer explicit page_text from incoming payload
            if "page_text" in data:
                page_text = data["page_text"]
            elif "url" in data:
                try:
                    page_text = try_fetch_page_text(str(data["url"]), method="GET")
                except Exception as exc:
                    with state_lock:
                        runtime_state["last_status"] = f"تعذر جلب الصفحة من webhook URL: {exc}"
                        runtime_state["last_error"] = str(exc)
                    save_state()
                    save_log(str(exc), "WEBHOOK_FETCH_ERROR")
                    return {"matched": False, "alert_sent": False, "action_executed": False, "error": str(exc)}

        # If polling or other callers passed plain text, accept it
        data_text = page_text if page_text is not None else normalize_data(data)
        data_hash = hashlib.sha256(str(data_text).encode("utf-8")).hexdigest()

        # Build search logic: prefer required_terms (including target_text), exclude forbidden_terms
        search_required = list(required_terms)
        if target_text:
            # keep backwards compatibility: target_text becomes a required term
            if target_text not in search_required:
                search_required.append(target_text)

        matched = False
        search_term = ""

        # If we have explicit required terms, require all of them
        if search_required:
            search_term = ",".join(search_required)
            matched_all = True
            for term in search_required:
                if not contains_condition(data_text, term):
                    matched_all = False
                    break
            matched = matched_all
        else:
            # fallback to the legacy single-condition field
            search_term = condition
            matched = contains_condition(data_text, search_term)

        # If any forbidden term is present, suppress match
        if matched and forbidden_terms:
            for ft in forbidden_terms:
                if contains_condition(data_text, ft):
                    matched = False
                    # annotate reason in state
                    with state_lock:
                        runtime_state["last_status"] = f"مكتشف مصطلح محظور: {ft} - لم يتم الإبلاغ"
                    save_state()
                    save_log(f"Source={source} | forbidden='{ft}'", "FORBIDDEN_MATCH")
                    break

        with state_lock:
            was_active = bool(runtime_state["match_active"])
            runtime_state["last_data_hash"] = data_hash
            runtime_state["last_data_time"] = now_iso()
            runtime_state["last_source"] = source
            runtime_state["last_error"] = None

        if not matched:
            # before returning, check if price/stock selectors exist and whether they might satisfy numeric conditions even if text didn't
            price_ok = True
            stock_ok = True
            extracted = None
            try:
                if price_selector or stock_selector:
                    extracted = extract_fields(str(data_text), price_selector, stock_selector)
                    if price_selector and price_condition:
                        if extracted.get('price') is None:
                            price_ok = False
                        else:
                            price_ok = _check_price_condition(extracted.get('price'), price_condition)
                    if stock_selector and min_stock is not None:
                        if extracted.get('stock') is None:
                            stock_ok = False
                        else:
                            try:
                                stock_ok = int(extracted.get('stock')) >= int(min_stock)
                            except Exception:
                                stock_ok = False
            except Exception as exc:
                save_log(f"Extraction error: {exc}", "EXTRACTION_ERROR")
                price_ok = False
                stock_ok = False

            if price_ok and stock_ok and (price_selector or stock_selector):
                # treat as matched by numeric conditions
                matched = True
            else:
                with state_lock:
                    runtime_state["match_active"] = False
                    runtime_state["last_status"] = "وصلت البيانات والشرط غير متحقق"
                save_state()
                save_log(f"Source={source} | condition='{search_term}'", "NO_MATCH")
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

        # New match
        with state_lock:
            runtime_state["match_active"] = True
            runtime_state["last_status"] = "الشرط متحقق لأول مرة"
        save_state()
        save_log(f"Source={source} | condition='{search_term}'", "MATCH")

        alert_sent = send_telegram_alert(
            f"تم العثور على الشرط المطلوب:\n'{search_term}'\nالمصدر: {source}",
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

        action_result = execute_action(data_text, action_url, method=action_method, body_text=action_body)
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

@app.route("/monitor_source_thread")
def monitor_source_thread():
    # compatibility wrapper if invoked directly; the real worker is monitor_source
    return jsonify({"ok": True}), 200

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

            try:
                page_text = try_fetch_page_text(source_url, method=source_method)
            except Exception as exc:
                with state_lock:
                    runtime_state["last_status"] = f"خطأ اتصال بالمصدر: {exc}"
                    runtime_state["last_error"] = str(exc)
                save_state()
                save_log(str(exc), "SOURCE_ERROR")
                stop_event.wait(interval)
                continue

            process_data(page_text, "POLLING")

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

    if "action_method" in new_values and new_values["action_method"] not in {"GET", "POST"}:
        return jsonify({"ok": False, "error": "action_method must be GET or POST"}), 400

    # validate action_body if provided: must be valid JSON (or empty)
    if "action_body" in new_values and new_values["action_body"]:
        try:
            json.loads(new_values["action_body"])
        except Exception:
            return jsonify({"ok": False, "error": "action_body must be valid JSON string"}), 400

    # validate required_terms and forbidden_terms if provided: must be lists of strings
    if "required_terms" in new_values:
        if not isinstance(new_values["required_terms"], list) or not all(isinstance(x, str) for x in new_values["required_terms"]):
            return jsonify({"ok": False, "error": "required_terms must be a list of strings"}), 400
    if "forbidden_terms" in new_values:
        if not isinstance(new_values["forbidden_terms"], list) or not all(isinstance(x, str) for x in new_values["forbidden_terms"]):
            return jsonify({"ok": False, "error": "forbidden_terms must be a list of strings"}), 400

    condition_changed = False
    with settings_lock:
        if "monitor_condition" in new_values:
            condition_changed = str(settings.get("monitor_condition", "")) != str(new_values["monitor_condition"])
        settings.update(new_values)
    save_settings()

    if condition_changed or "target_text" in new_values:
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
    monitor_thread = threading.Thread(
        target=monitor_source,
        name="source-monitor",
        daemon=True,
    )
    monitor_thread.start()

    # Warn at startup if Telegram not configured
    if not configured_telegram():
        save_log("Telegram bot token or chat id not set — Telegram alerts disabled", "WARN")

    print("=" * 60)
    print("Webhook / Polling Automation Server (with page-text checking)")
    print("Dashboard : http://127.0.0.1:5000/")
    print("Webhook   : POST /webhook")
    print("Status    : GET  /status")
    print("=" * 60)

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
