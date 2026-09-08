# -*- coding: utf-8 -*-
import json
from pathlib import Path
from datetime import datetime
from collections import deque
import logging
import threading
import time

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

APP_TITLE = "Mission API Console"
SETTINGS_FILE = Path("settings.json")
EXAMPLES_FILE = Path("examples.json")
DEFAULTS = {"baseUrl": "", "orgId": "KUKA", "authHeader": "", "theme": "dark"}
REQUEST_CONFIG = {
    "ip_address": "192.168.15.107",
    "method": "POST",
    "path": "/interfaces/api/amr/robotQuery",
    "port": 5000,
    "authorization_token": "5d3db3e159d5b20951361b92d286b7fd78c3d4c415c1bc2dc7c5c59862a16184",
    "body": {
        
    },
}
REQUEST_INTERVAL_SECONDS = 0.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "change-me-for-production"

received_callbacks = deque(maxlen=100)
received_responses = deque(maxlen=100)
repeat_stop = threading.Event()
repeat_thread = None
repeat_lock = threading.Lock()


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text("utf-8"))}
        except Exception:
            return DEFAULTS.copy()
    return DEFAULTS.copy()


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


settings = load_settings()


def load_examples():
    if EXAMPLES_FILE.exists():
        try:
            return json.loads(EXAMPLES_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def save_example(key, text):
    data = load_examples()
    data[key] = text
    EXAMPLES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def now_stamp():
    return datetime.now().strftime("%Y%m%d%H%M%S")


def request_url():
    return f"http://{REQUEST_CONFIG['ip_address']}:{REQUEST_CONFIG['port']}{REQUEST_CONFIG['path']}"


def request_host():
    return f"{REQUEST_CONFIG['ip_address']}:{REQUEST_CONFIG['port']}"


def request_body():
    body = REQUEST_CONFIG["body"]
    if isinstance(body, str):
        return body.replace("{{requestId}}", f"request{now_stamp()}").replace("{{missionCode}}", f"mission{now_stamp()}")
    return body


def send_configured_request():
    headers = {
        "Content-Type": "application/json",
        "Authorization": REQUEST_CONFIG["authorization_token"],
    }
    body = request_body()
    try:
        response = requests.request(
            REQUEST_CONFIG["method"],
            request_url(),
            json=body,
            headers=headers,
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError:
            data = response.text
        received_responses.appendleft(
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "status": response.status_code,
                "data": data,
            }
        )
    except requests.RequestException as error:
        received_responses.appendleft(
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "status": "ERROR",
                "data": str(error),
            }
        )


def repeat_requests():
    while not repeat_stop.is_set():
        started = time.monotonic()
        send_configured_request()
        repeat_stop.wait(max(0, REQUEST_INTERVAL_SECONDS - (time.monotonic() - started)))


def start_repeating_requests():
    global repeat_thread
    with repeat_lock:
        if repeat_thread and repeat_thread.is_alive():
            return False
        repeat_stop.clear()
        repeat_thread = threading.Thread(target=repeat_requests, daemon=True)
        repeat_thread.start()
        return True


def stop_repeating_requests():
    repeat_stop.set()


@app.route("/")
def index():
    return render_template("index.html", title=APP_TITLE, request_host=request_host())


@app.route("/api/repeat/start", methods=["POST"])
def start_repeat():
    started = start_repeating_requests()
    return jsonify({"running": True, "started": started})


@app.route("/api/repeat/stop", methods=["POST"])
def stop_repeat():
    stop_repeating_requests()
    return jsonify({"running": False})


@app.route("/api/repeat/status")
def repeat_status():
    running = bool(repeat_thread and repeat_thread.is_alive())
    return jsonify({"running": running, "responses": list(received_responses)})


@app.route("/settings", methods=["GET", "POST"])
def configure():
    if request.method == "POST":
        for k in ["baseUrl", "orgId", "authHeader", "theme"]:
            if k in request.form:
                settings[k] = request.form.get(k, "")
        save_settings(settings)
        flash("Settings saved", "success")
        return redirect(url_for("configure"))
    return render_template("settings.html", settings=settings, example_default=load_examples().get("default", ""), title=APP_TITLE)


@app.route("/api/ids")
def api_ids():
    s = now_stamp()
    return jsonify({"requestId": f"request{s}", "missionCode": f"mission{s}"})


@app.route("/api/example", methods=["GET", "POST"])
def api_example():
    key = request.args.get("key") or request.form.get("key") or "default"
    if request.method == "POST":
        save_example(key, request.form.get("text", ""))
        return jsonify({"ok": True})
    return jsonify({"key": key, "text": load_examples().get(key, "")})


@app.route("/missionStateCallback", methods=["GET", "POST"])
def mission_state_callback():
    if request.method == "POST":
        data = request.get_json(silent=True, force=False)
        logger.info("Received missionStateCallback: %s", data)
        received_callbacks.appendleft(
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "data": data,
            }
        )
        response = jsonify(
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "data": None,
                "code": "0",
                "msg": "Success",
                "errMsg": None,
            }
        )
        response.headers["Content-Type"] = "application/json;charset=UTF-8"
        return response, 200

    return render_template("mission_state_callback.html", items=list(received_callbacks), title=APP_TITLE)


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200


@app.route("/send", methods=["POST"])
def send():
    method = request.form["method"]
    url = request.form["url"].replace("{{baseUrl}}", settings.get("baseUrl", ""))
    body = request.form.get("body", "")

    headers = {}
    headers_text = request.form.get("headers_text", "")
    for line in headers_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()

    if settings.get("authHeader"):
        if ":" in settings["authHeader"]:
            k, v = settings["authHeader"].split(":", 1)
        else:
            k, v = "Authorization", settings["authHeader"]
        headers[k.strip()] = v.strip()

    s = now_stamp()
    fresh_request = f"request{s}"
    fresh_mission = f"mission{s}"

    body_to_send = body.replace("{{requestId}}", fresh_request).replace("{{missionCode}}", fresh_mission)

    try:
        payload = json.loads(body_to_send) if body_to_send.strip() else None
        if isinstance(payload, (dict, list)):
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
            r = requests.request(method.upper(), url, json=payload, headers=headers, timeout=30)
        else:
            r = requests.request(method.upper(), url, data=body_to_send.encode("utf-8"), headers=headers, timeout=30)
    except Exception:
        r = requests.request(method.upper(), url, data=body_to_send.encode("utf-8"), headers=headers, timeout=30)

    status = getattr(r, "status_code", "ERROR")
    try:
        pretty = json.dumps(r.json(), indent=2, ensure_ascii=False)
    except Exception:
        pretty = getattr(r, "text", "Request failed")

    return render_template("send_result.html", method=method, url=url, status=status, response_text=pretty, headers=headers, title=APP_TITLE)


if __name__ == "__main__":
    start_repeating_requests()
    logger.info("Starting Flask server on 0.0.0.0:5002 ...")
    app.run(host="0.0.0.0", port=5003, debug=False, use_reloader=False)
