import os
import json
import uuid
import time
import threading
import sqlite3
import secrets
from functools import wraps
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import paho.mqtt.client as mqtt


app = Flask(__name__)

# ============================================================
# SECURITY / DATABASE CONFIGURATION
# ============================================================

app.secret_key = os.getenv("SECRET_KEY", "")
if not app.secret_key:
    # Development fallback only. Set SECRET_KEY in Render for deployment.
    app.secret_key = secrets.token_hex(32)

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(__file__), "data", "homeiot.db")
)

os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)


def db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'USER',
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                approved_at TEXT,
                approved_by TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                device TEXT,
                action TEXT,
                event_type TEXT NOT NULL,
                result TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()

        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        admin_password = os.getenv("ADMIN_PASSWORD", "")

        if admin_username and admin_password:
            existing = conn.execute(
                "SELECT id FROM users WHERE username = ?",
                (admin_username,)
            ).fetchone()
            now = datetime.now(timezone.utc).isoformat()
            if existing is None:
                conn.execute(
                    """INSERT INTO users
                       (username, password_hash, role, status, created_at, approved_at, approved_by)
                       VALUES (?, ?, 'ADMIN', 'ACTIVE', ?, ?, ?)""",
                    (admin_username, generate_password_hash(admin_password), now, now, admin_username)
                )
                conn.commit()
            else:
                conn.execute(
                    "UPDATE users SET role='ADMIN', status='ACTIVE' WHERE username = ?",
                    (admin_username,)
                )
                conn.commit()
    finally:
        conn.close()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = db_connection()
    try:
        return conn.execute(
            "SELECT id, username, role, status FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
    finally:
        conn.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login", next=request.path))
        if user["status"] != "ACTIVE":
            session.clear()
            flash("Your account is not currently approved for access.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login", next=request.path))
        if user["status"] != "ACTIVE" or user["role"] != "ADMIN":
            return jsonify({"success": False, "error": "Administrator access required"}), 403
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def valid_csrf():
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(supplied and secrets.compare_digest(supplied, session.get("csrf_token", "")))


def record_history(username, event_type, result, device=None, action=None, details=None):
    conn = db_connection()
    try:
        conn.execute(
            """INSERT INTO history
               (username, device, action, event_type, result, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, device, action, event_type, result, details, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()


@app.context_processor
def inject_user_context():
    user = current_user()
    return {
        "current_user": user,
        "csrf_token": csrf_token()
    }


init_database()


# ============================================================
# HIVEMQ CONFIGURATION
# ============================================================

MQTT_BROKER = os.getenv(
    "MQTT_BROKER",
    "bravetawny-af88996b.a02.usw2.aws.hivemq.cloud"
)

MQTT_PORT = int(
    os.getenv("MQTT_PORT", "8883")
)

MQTT_USERNAME = os.getenv(
    "MQTT_USERNAME",
    ""
)

MQTT_PASSWORD = os.getenv(
    "MQTT_PASSWORD",
    ""
)


# ============================================================
# MQTT TOPICS
# ============================================================

LIGHT_TOPIC = "home/light"
FAN_TOPIC = "home/fan"
GEYSER_TOPIC = "home/geyser"
STATUS_TOPIC = "home/status"


# ============================================================
# DEVICE STATE
# ============================================================

device_states = {
    "light": "OFF",
    "fan": "OFF",
    "geyser": "OFF"
}


# ============================================================
# SENSOR STATE
# ============================================================

sensor_data = {
    "temperature": None,
    "humidity": None,
    "gas": "NORMAL",
    "gas_raw": None
}


# ============================================================
# APPLICATION STATE
# ============================================================

mqtt_connected = False
last_error = None
last_status_time = None
last_command = None

status_client = None
status_client_started = False

state_lock = threading.Lock()
startup_lock = threading.Lock()
mqtt_connection_event = threading.Event()


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(message, flush=True)


# ============================================================
# MQTT CONNECT CALLBACK
# ============================================================

def on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties
):

    global mqtt_connected
    global last_error

    log("")
    log("========================================")
    log("MQTT CONNECT CALLBACK")
    log(f"Reason code: {reason_code}")
    log("========================================")

    if reason_code == 0:

        mqtt_connected = True
        last_error = None

        mqtt_connection_event.set()

        log("MQTT CONNECTED SUCCESSFULLY")
        log(f"Broker: {MQTT_BROKER}")
        log(f"Port: {MQTT_PORT}")

        result, mid = client.subscribe(
            STATUS_TOPIC,
            qos=0
        )

        log(
            f"STATUS SUBSCRIBE RESULT: "
            f"rc={result}, mid={mid}"
        )

    else:

        mqtt_connected = False

        mqtt_connection_event.clear()

        last_error = (
            f"MQTT connection failed: "
            f"{reason_code}"
        )

        log("MQTT CONNECTION FAILED")


# ============================================================
# MQTT DISCONNECT CALLBACK
# ============================================================

def on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties
):

    global mqtt_connected

    mqtt_connected = False

    mqtt_connection_event.clear()

    log("")
    log("========================================")
    log("MQTT DISCONNECTED")
    log(f"Reason code: {reason_code}")
    log("========================================")


# ============================================================
# MQTT MESSAGE CALLBACK
# ============================================================

def on_message(
    client,
    userdata,
    message
):

    global last_status_time

    if message.topic != STATUS_TOPIC:
        return

    try:

        payload = message.payload.decode(
            "utf-8"
        )

        data = json.loads(payload)

    except Exception as error:

        log("")
        log("========================================")
        log("MQTT STATUS JSON ERROR")
        log(str(error))
        log("========================================")

        return

    log("")
    log("========================================")
    log("ESP32 STATUS RECEIVED")
    log(f"Topic: {message.topic}")
    log(f"Payload: {payload}")
    log("========================================")

    with state_lock:

        # ----------------------------------------------------
        # DEVICE STATES
        # ----------------------------------------------------

        devices = data.get(
            "devices",
            {}
        )

        if "light" in devices:

            device_states["light"] = str(
                devices["light"]
            ).upper()

        if "fan" in devices:

            device_states["fan"] = str(
                devices["fan"]
            ).upper()

        if "geyser" in devices:

            device_states["geyser"] = str(
                devices["geyser"]
            ).upper()

        # ----------------------------------------------------
        # SENSOR DATA
        # ----------------------------------------------------

        sensors = data.get(
            "sensors",
            {}
        )

        if "temperature" in sensors:

            sensor_data["temperature"] = (
                sensors["temperature"]
            )

        if "humidity" in sensors:

            sensor_data["humidity"] = (
                sensors["humidity"]
            )

        if "gas" in sensors:

            sensor_data["gas"] = (
                sensors["gas"]
            )

        if "gas_raw" in sensors:

            sensor_data["gas_raw"] = (
                sensors["gas_raw"]
            )

        # ----------------------------------------------------
        # STATUS TIMESTAMP
        # ----------------------------------------------------

        last_status_time = time.time()


# ============================================================
# START MQTT STATUS CLIENT
#
# IMPORTANT:
# This function is intentionally NOT called when the module
# is imported.
#
# Gunicorn imports app.py first and then creates its worker.
# Starting MQTT during import would start it in the wrong
# process.
#
# We start it lazily from inside the actual Flask worker.
# ============================================================

def ensure_status_client():

    global status_client
    global status_client_started
    global mqtt_connected
    global last_error

    if status_client_started:
        return

    with startup_lock:

        if status_client_started:
            return

        log("")
        log("========================================")
        log("STARTING MQTT INSIDE FLASK WORKER")
        log(f"Process ID: {os.getpid()}")
        log("========================================")

        client_id = (
            "homeiot-status-" +
            uuid.uuid4().hex[:12]
        )

        log(
            f"MQTT Client ID: {client_id}"
        )

        try:

            status_client = mqtt.Client(
                callback_api_version=(
                    mqtt.CallbackAPIVersion.VERSION2
                ),
                client_id=client_id
            )

            status_client.username_pw_set(
                MQTT_USERNAME,
                MQTT_PASSWORD
            )

            status_client.tls_set()

            status_client.reconnect_delay_set(
                min_delay=2,
                max_delay=30
            )

            status_client.on_connect = on_connect
            status_client.on_disconnect = on_disconnect
            status_client.on_message = on_message

            log("CONNECTING TO HIVEMQ...")

            result = status_client.connect(
                MQTT_BROKER,
                MQTT_PORT,
                keepalive=60
            )

            log(
                f"MQTT CONNECT RETURNED: "
                f"{result}"
            )

            status_client.loop_start()

            log(
                "MQTT NETWORK LOOP STARTED"
            )

            status_client_started = True

            connected = mqtt_connection_event.wait(
                timeout=10
            )

            if connected:

                log("")
                log("========================================")
                log("MQTT CONNECTION CONFIRMED")
                log(
                    f"Flask worker PID: "
                    f"{os.getpid()}"
                )
                log("========================================")

            else:

                mqtt_connected = False

                log("")
                log("========================================")
                log("MQTT CONNECTION TIMEOUT")
                log("========================================")

        except Exception as error:

            mqtt_connected = False
            last_error = str(error)

            log("")
            log("========================================")
            log("MQTT START ERROR")
            log(str(error))
            log("========================================")


# ============================================================
# COMMAND PUBLISHER
# ============================================================

def publish_command(
    topic,
    payload
):

    global last_error
    global last_command

    command_client = None

    client_id = (
        "homeiot-command-" +
        uuid.uuid4().hex[:12]
    )

    log("")
    log("########################################")
    log("NEW DEVICE COMMAND")
    log(f"Client ID: {client_id}")
    log(f"Worker PID: {os.getpid()}")
    log(f"Topic: {topic}")
    log(f"Payload: {payload}")
    log("QoS: 0")
    log("########################################")

    try:

        # ----------------------------------------------------
        # CREATE FRESH MQTT CLIENT
        # ----------------------------------------------------

        command_client = mqtt.Client(
            callback_api_version=(
                mqtt.CallbackAPIVersion.VERSION2
            ),
            client_id=client_id
        )

        command_client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD
        )

        command_client.tls_set()

        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------

        log(
            "COMMAND CLIENT CONNECTING..."
        )

        connect_result = command_client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            keepalive=60
        )

        log(
            f"COMMAND CONNECT RETURNED: "
            f"{connect_result}"
        )

        # ----------------------------------------------------
        # START NETWORK LOOP
        # ----------------------------------------------------

        command_client.loop_start()

        log(
            "COMMAND NETWORK LOOP STARTED"
        )

        # ----------------------------------------------------
        # WAIT FOR CONNECTION
        # ----------------------------------------------------

        connected = False

        for attempt in range(40):

            time.sleep(0.25)

            if command_client.is_connected():

                connected = True

                log(
                    "COMMAND MQTT CONNECTION CONFIRMED"
                )

                break

        if not connected:

            last_error = (
                "Command MQTT client "
                "could not connect to HiveMQ."
            )

            log(
                "COMMAND MQTT CONNECTION FAILED"
            )

            return False

        # ----------------------------------------------------
        # PUBLISH
        # ----------------------------------------------------

        log(
            "PUBLISHING COMMAND..."
        )

        result = command_client.publish(
            topic=topic,
            payload=payload,
            qos=0,
            retain=False
        )

        log(
            f"COMMAND PUBLISH RESULT: "
            f"rc={result.rc}, "
            f"mid={result.mid}"
        )

        if result.rc != mqtt.MQTT_ERR_SUCCESS:

            last_error = (
                f"MQTT publish failed: "
                f"rc={result.rc}"
            )

            return False

        # ----------------------------------------------------
        # ALLOW TRANSMISSION
        # ----------------------------------------------------

        time.sleep(1)

        last_command = {
            "topic": topic,
            "payload": payload,
            "qos": 0,
            "success": True,
            "timestamp": time.time()
        }

        last_error = None

        log("")
        log("########################################")
        log("COMMAND PUBLISHED TO HIVEMQ")
        log(f"Topic: {topic}")
        log(f"Payload: {payload}")
        log("########################################")

        return True

    except Exception as error:

        last_error = str(error)

        log("")
        log("COMMAND MQTT EXCEPTION")
        log(str(error))

        return False

    finally:

        if command_client is not None:

            try:
                command_client.loop_stop()
            except Exception:
                pass

            try:
                command_client.disconnect()
            except Exception:
                pass

            log(
                "COMMAND MQTT CLIENT CLOSED"
            )


# ============================================================
# PUBLIC HOME / AUTHENTICATION
# ============================================================

@app.route("/")
def home():
    if current_user() is not None:
        return redirect(url_for("dashboard"))
    return render_template("home.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        if not valid_csrf():
            return render_template("signup.html", error="Security check failed. Please try again.")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(username) < 3 or len(username) > 50:
            return render_template("signup.html", error="Username must be between 3 and 50 characters.")
        if len(password) < 8:
            return render_template("signup.html", error="Password must contain at least 8 characters.")
        if password != confirm:
            return render_template("signup.html", error="Passwords do not match.")

        conn = db_connection()
        try:
            exists = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if exists:
                return render_template("signup.html", error="That username is already registered.")
            conn.execute(
                """INSERT INTO users (username, password_hash, role, status, created_at)
                   VALUES (?, ?, 'USER', 'PENDING', ?)""",
                (username, generate_password_hash(password), datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        finally:
            conn.close()

        record_history(username, "ACCOUNT", "PENDING", details="New user registration awaiting administrator approval")
        return render_template("signup_success.html", username=username)

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        if not valid_csrf():
            return render_template("login.html", error="Security check failed. Please try again.")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = db_connection()
        try:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        finally:
            conn.close()

        if user is None or not check_password_hash(user["password_hash"], password):
            return render_template("login.html", error="Invalid username or password.")

        if user["status"] == "PENDING":
            return render_template("login.html", error="Your account is awaiting administrator approval.")
        if user["status"] in ("REJECTED", "DISABLED"):
            return render_template("login.html", error="Your account is not approved for access.")

        session.clear()
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        record_history(user["username"], "LOGIN", "SUCCESS")

        next_url = request.args.get("next") or request.form.get("next")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for("dashboard"))

    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    if not valid_csrf():
        return jsonify({"success": False, "error": "Security check failed"}), 400
    user = current_user()
    if user:
        record_history(user["username"], "LOGOUT", "SUCCESS")
    session.clear()
    return redirect(url_for("home"))


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    ensure_status_client()
    return render_template("index.html")


# ============================================================
# STATUS API
# ============================================================

@app.route("/api/status")
@login_required
def api_status():

    ensure_status_client()

    # --------------------------------------------------------
    # Give the MQTT connection a moment to receive the first
    # ESP32 status packet if this is the first request.
    # --------------------------------------------------------

    if last_status_time is None:

        mqtt_connection_event.wait(
            timeout=2
        )

        # Small additional window for ESP32 status.
        time.sleep(0.2)

    with state_lock:

        light = device_states["light"]
        fan = device_states["fan"]
        geyser = device_states["geyser"]

        temperature = sensor_data["temperature"]
        humidity = sensor_data["humidity"]
        gas = sensor_data["gas"]
        gas_raw = sensor_data["gas_raw"]

        status_time = last_status_time

    # --------------------------------------------------------
    # RECENT STATUS
    # --------------------------------------------------------

    recently_received = False

    if status_time is not None:

        recently_received = (
            time.time() - status_time < 15
        )

    online = (
        mqtt_connected or
        recently_received
    )

    # --------------------------------------------------------
    # RETURN BOTH NESTED AND FLAT DATA
    # --------------------------------------------------------

    return jsonify({

        "mqtt": {
            "connected": online
        },

        "devices": {
            "light": light,
            "fan": fan,
            "geyser": geyser
        },

        "sensors": {
            "temperature": temperature,
            "humidity": humidity,
            "gas": gas,
            "gas_raw": gas_raw
        },

        "light": light,
        "fan": fan,
        "geyser": geyser,

        "temperature": temperature,
        "humidity": humidity,
        "gas": gas,
        "gas_raw": gas_raw,

        "last_status_received": status_time,

        "last_error": last_error,

        "last_command": last_command
    })


# ============================================================
# DEVICE CONTROL
# ============================================================

@app.route(
    "/api/device/<device>/<action>",
    methods=["POST"]
)
@login_required
def control_device(
    device,
    action
):

    ensure_status_client()

    device = device.lower()
    action = action.upper()

    topics = {
        "light": LIGHT_TOPIC,
        "fan": FAN_TOPIC,
        "geyser": GEYSER_TOPIC
    }

    log("")
    log("========================================")
    log("DASHBOARD DEVICE COMMAND")
    log(f"Device: {device}")
    log(f"Action: {action}")
    log("========================================")

    if device not in topics:

        return jsonify({
            "success": False,
            "error": "Unknown device"
        }), 400

    if action not in ["ON", "OFF"]:

        return jsonify({
            "success": False,
            "error": "Invalid action"
        }), 400

    if not valid_csrf():

        return jsonify({
            "success": False,
            "error": "Security check failed"
        }), 400

    success = publish_command(
        topics[device],
        action
    )

    user = current_user()

    if not success:

        record_history(
            user["username"],
            "DEVICE",
            "FAILED",
            device=device,
            action=action,
            details=last_error or "MQTT command failed"
        )
        return jsonify({
            "success": False,
            "error": (
                last_error or
                "MQTT command failed"
            )
        }), 503

    record_history(
        user["username"],
        "DEVICE",
        "SUCCESS",
        device=device,
        action=action,
        details="MQTT command published"
    )

    return jsonify({

        "success": True,

        "device": device,

        "action": action,

        "topic": topics[device],

        "qos": 0,

        "message": "Command sent successfully"
    })


# ============================================================
# HISTORY / SETTINGS / ADMIN
# ============================================================

@app.route("/history")
@login_required
def history():
    user = current_user()
    conn = db_connection()
    try:
        if user["role"] == "ADMIN":
            rows = conn.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT 200"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM history WHERE username = ? ORDER BY id DESC LIMIT 200",
                (user["username"],)
            ).fetchall()
    finally:
        conn.close()
    return render_template("history.html", history=rows)


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


@app.route("/admin/users")
@admin_required
def admin_users():
    conn = db_connection()
    try:
        users = conn.execute(
            "SELECT id, username, role, status, created_at, approved_at, approved_by FROM users ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/<action>", methods=["POST"])
@admin_required
def admin_user_action(user_id, action):
    if not valid_csrf():
        return jsonify({"success": False, "error": "Security check failed"}), 400

    admin = current_user()
    if action not in ("approve", "reject", "disable"):
        return jsonify({"success": False, "error": "Invalid action"}), 400

    conn = db_connection()
    try:
        target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            return jsonify({"success": False, "error": "User not found"}), 404
        if target["role"] == "ADMIN":
            return jsonify({"success": False, "error": "Admin accounts cannot be changed here"}), 400

        now = datetime.now(timezone.utc).isoformat()
        if action == "approve":
            conn.execute(
                "UPDATE users SET status='ACTIVE', approved_at=?, approved_by=? WHERE id=?",
                (now, admin["username"], user_id)
            )
            result = "APPROVED"
        elif action == "reject":
            conn.execute(
                "UPDATE users SET status='REJECTED', approved_at=NULL, approved_by=? WHERE id=?",
                (admin["username"], user_id)
            )
            result = "REJECTED"
        else:
            conn.execute("UPDATE users SET status='DISABLED' WHERE id=?", (user_id,))
            result = "DISABLED"
        conn.commit()
    finally:
        conn.close()

    record_history(admin["username"], "USER", result, details=f"User: {target['username']}")
    return redirect(url_for("admin_users"))


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
@login_required
def health():

    ensure_status_client()

    try:

        connected = (
            status_client.is_connected()
            if status_client is not None
            else False
        )

    except Exception:

        connected = False

    return jsonify({

        "status": "ok",

        "mqtt_connected": connected,

        "last_error": last_error,

        "last_status_received": (
            last_status_time
        ),

        "last_command": last_command
    })


# ============================================================
# START LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "5000")
        ),
        debug=False
    )
