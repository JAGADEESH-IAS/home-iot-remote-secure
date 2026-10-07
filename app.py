import os
import json
import sqlite3
import threading
import time
import secrets
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    flash,
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)

import paho.mqtt.client as mqtt


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key",
)

DATABASE = "home_iot.db"


# ============================================================
# MQTT CONFIGURATION
# ============================================================

MQTT_BROKER = os.environ.get(
    "MQTT_BROKER",
    "bravetawny-af88996b.a02.usw2.aws.hivemq.cloud",
)

MQTT_PORT = int(
    os.environ.get("MQTT_PORT", "8883")
)

MQTT_USERNAME = os.environ.get(
    "MQTT_USERNAME",
    "",
)

MQTT_PASSWORD = os.environ.get(
    "MQTT_PASSWORD",
    "",
)


# ============================================================
# ADMIN CONFIGURATION
# ============================================================

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "admin",
)

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "admin",
)


# ============================================================
# MQTT TOPICS
# ============================================================

TOPIC_LIGHT = "home/light"
TOPIC_FAN = "home/fan"
TOPIC_GEYSER = "home/geyser"
TOPIC_STATUS = "home/status"


# ============================================================
# LOCAL STATE
# ============================================================

device_state = {
    "light": "OFF",
    "fan": "OFF",
    "geyser": "OFF",
}

sensor_state = {
    "temperature": "--",
    "humidity": "--",
    "gas": "--",
    "gas_raw": 0,
}


# ============================================================
# MQTT RUNTIME
# ============================================================

mqtt_client = None
mqtt_connected = False
mqtt_last_error = ""

mqtt_lock = threading.Lock()


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'USER',
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            username TEXT NOT NULL,
            device TEXT NOT NULL,
            action TEXT NOT NULL,
            result TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'Device Control'
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS latest_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            light TEXT NOT NULL DEFAULT 'OFF',
            fan TEXT NOT NULL DEFAULT 'OFF',
            geyser TEXT NOT NULL DEFAULT 'OFF',
            temperature TEXT NOT NULL DEFAULT '--',
            humidity TEXT NOT NULL DEFAULT '--',
            gas TEXT NOT NULL DEFAULT '--',
            gas_raw INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
        """
    )

    cur.execute(
        """
        INSERT OR IGNORE INTO latest_status (id)
        VALUES (1)
        """
    )

    columns = {
        row["name"]
        for row in cur.execute(
            "PRAGMA table_info(history)"
        ).fetchall()
    }

    if "event_type" not in columns:
        cur.execute(
            """
            ALTER TABLE history
            ADD COLUMN event_type TEXT
            NOT NULL DEFAULT 'Device Control'
            """
        )

    existing_admin = cur.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        (ADMIN_USERNAME,),
    ).fetchone()

    if not existing_admin:

        cur.execute(
            """
            INSERT INTO users
            (
                username,
                password_hash,
                role,
                status,
                created_at
            )
            VALUES (?, ?, 'ADMIN', 'ACTIVE', ?)
            """,
            (
                ADMIN_USERNAME,
                generate_password_hash(
                    ADMIN_PASSWORD
                ),
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            ),
        )

    conn.commit()
    conn.close()


# ============================================================
# SHARED STATUS
# ============================================================

def read_latest_status():

    conn = get_db()

    row = conn.execute(
        """
        SELECT
            light,
            fan,
            geyser,
            temperature,
            humidity,
            gas,
            gas_raw
        FROM latest_status
        WHERE id = 1
        """
    ).fetchone()

    conn.close()

    if not row:
        return (
            dict(device_state),
            dict(sensor_state),
        )

    devices = {
        "light": row["light"],
        "fan": row["fan"],
        "geyser": row["geyser"],
    }

    sensors = {
        "temperature": row["temperature"],
        "humidity": row["humidity"],
        "gas": row["gas"],
        "gas_raw": row["gas_raw"],
    }

    return devices, sensors


def write_latest_status(
    devices,
    sensors,
):

    conn = get_db()

    conn.execute(
        """
        UPDATE latest_status
        SET
            light = ?,
            fan = ?,
            geyser = ?,
            temperature = ?,
            humidity = ?,
            gas = ?,
            gas_raw = ?,
            updated_at = ?
        WHERE id = 1
        """,
        (
            str(
                devices.get(
                    "light",
                    "OFF",
                )
            ).upper(),
            str(
                devices.get(
                    "fan",
                    "OFF",
                )
            ).upper(),
            str(
                devices.get(
                    "geyser",
                    "OFF",
                )
            ).upper(),
            str(
                sensors.get(
                    "temperature",
                    "--",
                )
            ),
            str(
                sensors.get(
                    "humidity",
                    "--",
                )
            ),
            str(
                sensors.get(
                    "gas",
                    "--",
                )
            ),
            int(
                sensors.get(
                    "gas_raw",
                    0,
                )
                or 0
            ),
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        ),
    )

    conn.commit()
    conn.close()


def set_device_status(
    device,
    action,
):

    if device not in {
        "light",
        "fan",
        "geyser",
    }:
        return

    conn = get_db()

    conn.execute(
        f"""
        UPDATE latest_status
        SET
            {device} = ?,
            updated_at = ?
        WHERE id = 1
        """,
        (
            action.upper(),
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# CSRF
# ============================================================

def get_csrf_token():

    if "csrf_token" not in session:
        session["csrf_token"] = (
            secrets.token_urlsafe(32)
        )

    return session["csrf_token"]


@app.before_request
def protect_post_requests():

    if request.method != "POST":
        return None

    token = (
        request.form.get("csrf_token")
        or request.headers.get(
            "X-CSRF-Token"
        )
    )

    expected = session.get(
        "csrf_token"
    )

    if (
        not expected
        or not token
        or not secrets.compare_digest(
            token,
            expected,
        )
    ):
        return jsonify(
            {
                "success": False,
                "error": (
                    "Invalid security token. "
                    "Please refresh the page."
                ),
            }
        ), 403

    return None


# ============================================================
# HISTORY
# ============================================================

def add_history(
    username,
    device,
    action,
    result,
    event_type="Device Control",
):

    conn = get_db()

    conn.execute(
        """
        INSERT INTO history
        (
            created_at,
            username,
            device,
            action,
            result,
            event_type
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            username,
            device,
            action,
            result,
            event_type,
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# AUTHENTICATION
# ============================================================

def current_user():

    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    user = conn.execute(
        """
        SELECT *
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    return user


def login_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        user = current_user()

        if not user:
            return redirect(
                url_for("login")
            )

        if user["status"] != "ACTIVE":

            session.clear()

            flash(
                "Your account is not approved yet.",
                "error",
            )

            return redirect(
                url_for("login")
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


def admin_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        user = current_user()

        if not user:
            return redirect(
                url_for("login")
            )

        if (
            user["status"] != "ACTIVE"
            or user["role"] != "ADMIN"
        ):

            flash(
                "Administrator access required.",
                "error",
            )

            return redirect(
                url_for("dashboard")
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


@app.context_processor
def inject_template_values():

    return {
        "csrf_token": get_csrf_token(),
        "current_user": current_user(),
    }


# ============================================================
# MQTT STATUS CALLBACKS
# ============================================================

def mqtt_on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties=None,
):

    global mqtt_connected
    global mqtt_last_error

    if reason_code == 0:

        mqtt_connected = True
        mqtt_last_error = ""

        for topic in [
            TOPIC_LIGHT,
            TOPIC_FAN,
            TOPIC_GEYSER,
            TOPIC_STATUS,
        ]:

            try:

                result, _ = client.subscribe(
                    topic
                )

                print(
                    "MQTT subscribe:",
                    topic,
                    result,
                    flush=True,
                )

            except Exception as exc:

                print(
                    "MQTT subscribe error:",
                    topic,
                    exc,
                    flush=True,
                )

        print(
            "MQTT connected successfully:",
            reason_code,
            flush=True,
        )

    else:

        mqtt_connected = False
        mqtt_last_error = str(
            reason_code
        )

        print(
            "MQTT connection failed:",
            reason_code,
            flush=True,
        )


def mqtt_on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None,
):

    global mqtt_connected
    global mqtt_last_error

    mqtt_connected = False
    mqtt_last_error = str(
        reason_code
    )

    print(
        "MQTT disconnected:",
        reason_code,
        flush=True,
    )


def mqtt_on_message(
    client,
    userdata,
    msg,
):

    global device_state
    global sensor_state

    try:

        payload = msg.payload.decode(
            "utf-8"
        )

        print(
            "MQTT MESSAGE RECEIVED:",
            msg.topic,
            flush=True,
        )

        print(
            "MQTT PAYLOAD:",
            payload,
            flush=True,
        )

        if msg.topic != TOPIC_STATUS:
            return

        data = json.loads(payload)

        devices = data.get(
            "devices",
            {}
        )

        sensors = data.get(
            "sensors",
            {}
        )

        current_devices, current_sensors = (
            read_latest_status()
        )

        if "light" in devices:
            current_devices["light"] = str(
                devices["light"]
            ).upper()

        if "fan" in devices:
            current_devices["fan"] = str(
                devices["fan"]
            ).upper()

        if "geyser" in devices:
            current_devices["geyser"] = str(
                devices["geyser"]
            ).upper()

        if "temperature" in sensors:
            current_sensors["temperature"] = (
                sensors["temperature"]
            )

        if "humidity" in sensors:
            current_sensors["humidity"] = (
                sensors["humidity"]
            )

        if "gas" in sensors:
            current_sensors["gas"] = (
                sensors["gas"]
            )

        if "gas_raw" in sensors:
            current_sensors["gas_raw"] = (
                sensors["gas_raw"]
            )

        write_latest_status(
            current_devices,
            current_sensors,
        )

        with mqtt_lock:

            device_state.update(
                current_devices
            )

            sensor_state.update(
                current_sensors
            )

        print(
            "MQTT STATUS UPDATED SUCCESSFULLY",
            flush=True,
        )

        print(
            "TEMPERATURE:",
            current_sensors.get(
                "temperature"
            ),
            flush=True,
        )

        print(
            "HUMIDITY:",
            current_sensors.get(
                "humidity"
            ),
            flush=True,
        )

        print(
            "GAS:",
            current_sensors.get(
                "gas"
            ),
            flush=True,
        )

    except Exception as exc:

        print(
            "MQTT MESSAGE ERROR:",
            type(exc).__name__,
            str(exc),
            flush=True,
        )


# ============================================================
# MQTT CLIENT FOR STATUS MONITORING
# ============================================================

def create_mqtt_client():

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=(
            f"home-iot-status-"
            f"{os.getpid()}-"
            f"{int(time.time())}-"
            f"{secrets.token_hex(3)}"
        ),
    )

    client.username_pw_set(
        MQTT_USERNAME,
        MQTT_PASSWORD,
    )

    client.tls_set()

    client.on_connect = mqtt_on_connect
    client.on_disconnect = mqtt_on_disconnect
    client.on_message = mqtt_on_message

    return client


def ensure_mqtt_connection():

    global mqtt_connected
    global mqtt_last_error

    client = mqtt_client

    if client is None:

        mqtt_connected = False

        if not mqtt_last_error:
            mqtt_last_error = (
                "MQTT client is not initialized."
            )

        return False

    try:

        connected = client.is_connected()

        mqtt_connected = connected

        if connected:
            mqtt_last_error = ""

        return connected

    except Exception as exc:

        mqtt_connected = False

        mqtt_last_error = (
            f"{type(exc).__name__}: {exc}"
        )

        print(
            "MQTT STATE CHECK ERROR:",
            mqtt_last_error,
            flush=True,
        )

        return False


def start_mqtt():

    global mqtt_client
    global mqtt_connected
    global mqtt_last_error

    print(
        "Starting MQTT background client",
        flush=True,
    )

    if not MQTT_BROKER:
        mqtt_connected = False
        mqtt_last_error = (
            "MQTT_BROKER is empty."
        )
        return

    if not MQTT_USERNAME:
        mqtt_connected = False
        mqtt_last_error = (
            "MQTT_USERNAME is empty."
        )
        return

    if not MQTT_PASSWORD:
        mqtt_connected = False
        mqtt_last_error = (
            "MQTT_PASSWORD is empty."
        )
        return

    try:

        client = create_mqtt_client()

        mqtt_client = client

        print(
            "Connecting MQTT to:",
            MQTT_BROKER,
            MQTT_PORT,
            flush=True,
        )

        client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60,
        )

        client.loop_start()

        print(
            "MQTT NETWORK LOOP STARTED.",
            flush=True,
        )

        deadline = time.time() + 5

        while time.time() < deadline:

            if client.is_connected():

                mqtt_connected = True
                mqtt_last_error = ""

                print(
                    "MQTT CONNECTED SUCCESSFULLY.",
                    flush=True,
                )

                return

            time.sleep(0.1)

        mqtt_connected = client.is_connected()

        if not mqtt_connected:

            mqtt_last_error = (
                "MQTT connection did not complete."
            )

            print(
                "MQTT CONNECTION TIMEOUT:",
                mqtt_last_error,
                flush=True,
            )

    except Exception as exc:

        mqtt_connected = False

        mqtt_last_error = (
            f"{type(exc).__name__}: {exc}"
        )

        print(
            "MQTT START ERROR:",
            mqtt_last_error,
            flush=True,
        )


# ============================================================
# DEDICATED MQTT COMMAND PUBLISHER
# ============================================================

def publish_device_command(
    device,
    action,
):

    topics = {
        "light": TOPIC_LIGHT,
        "fan": TOPIC_FAN,
        "geyser": TOPIC_GEYSER,
    }

    topic = topics.get(device)

    if not topic:

        print(
            "MQTT COMMAND ERROR: invalid device =",
            device,
            flush=True,
        )

        return False

    payload = action.upper()

    if payload not in {
        "ON",
        "OFF",
    }:

        print(
            "MQTT COMMAND ERROR: invalid action =",
            payload,
            flush=True,
        )

        return False

    command_client = None

    client_id = (
        f"home-iot-command-"
        f"{os.getpid()}-"
        f"{int(time.time() * 1000)}-"
        f"{secrets.token_hex(4)}"
    )

    try:

        print(
            "================================================",
            flush=True,
        )

        print(
            "MQTT DEDICATED COMMAND START",
            flush=True,
        )

        print(
            "Device:",
            device,
            flush=True,
        )

        print(
            "Topic:",
            topic,
            flush=True,
        )

        print(
            "Payload:",
            payload,
            flush=True,
        )

        print(
            "QoS: 0",
            flush=True,
        )

        print(
            "Retain: True",
            flush=True,
        )

        print(
            "Command Client ID:",
            client_id,
            flush=True,
        )

        # ----------------------------------------------------
        # CREATE DEDICATED COMMAND CLIENT
        # ----------------------------------------------------

        command_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        command_client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD,
        )

        command_client.tls_set()

        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------

        print(
            "MQTT COMMAND: connecting...",
            flush=True,
        )

        command_client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60,
        )

        print(
            "MQTT COMMAND: connected.",
            flush=True,
        )

        # ----------------------------------------------------
        # START NETWORK LOOP
        # ----------------------------------------------------

        command_client.loop_start()

        deadline = time.time() + 5

        while time.time() < deadline:

            if command_client.is_connected():
                break

            time.sleep(0.05)

        if not command_client.is_connected():

            print(
                "MQTT COMMAND: connection not established.",
                flush=True,
            )

            command_client.loop_stop()

            try:
                command_client.disconnect()
            except Exception:
                pass

            return False

        print(
            "MQTT COMMAND: network loop running.",
            flush=True,
        )

        # ----------------------------------------------------
        # PUBLISH
        # ----------------------------------------------------

        result = command_client.publish(
            topic,
            payload=payload,
            qos=0,
            retain=True,
        )

        print(
            "MQTT COMMAND PUBLISH RC:",
            result.rc,
            flush=True,
        )

        print(
            "MQTT COMMAND MESSAGE ID:",
            result.mid,
            flush=True,
        )

        if result.rc != mqtt.MQTT_ERR_SUCCESS:

            print(
                "MQTT COMMAND: publish failed.",
                flush=True,
            )

            command_client.loop_stop()

            try:
                command_client.disconnect()
            except Exception:
                pass

            return False

        # ----------------------------------------------------
        # WAIT FOR PUBLISH
        # ----------------------------------------------------

        print(
            "MQTT COMMAND: waiting for network publish...",
            flush=True,
        )

        try:

            result.wait_for_publish(
                timeout=5
            )

        except Exception as exc:

            print(
                "MQTT COMMAND WAIT ERROR:",
                type(exc).__name__,
                str(exc),
                flush=True,
            )

        # Give the network loop time to flush
        # the outgoing MQTT packet.
        time.sleep(0.5)

        try:

            published = result.is_published()

        except Exception:

            published = False

        print(
            "MQTT COMMAND IS PUBLISHED:",
            published,
            flush=True,
        )

        # ----------------------------------------------------
        # STOP NETWORK LOOP
        # ----------------------------------------------------

        command_client.loop_stop()

        print(
            "MQTT COMMAND: network loop stopped.",
            flush=True,
        )

        # ----------------------------------------------------
        # DISCONNECT
        # ----------------------------------------------------

        try:

            command_client.disconnect()

        except Exception:
            pass

        print(
            "MQTT COMMAND: disconnected.",
            flush=True,
        )

        print(
            "MQTT DEDICATED COMMAND SUCCESS",
            flush=True,
        )

        print(
            "================================================",
            flush=True,
        )

        return True

    except Exception as exc:

        print(
            "================================================",
            flush=True,
        )

        print(
            "MQTT DEDICATED COMMAND FAILED",
            flush=True,
        )

        print(
            "Error type:",
            type(exc).__name__,
            flush=True,
        )

        print(
            "Error:",
            str(exc),
            flush=True,
        )

        print(
            "================================================",
            flush=True,
        )

        try:

            if command_client is not None:

                command_client.loop_stop()

                command_client.disconnect()

        except Exception:
            pass

        return False


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    if current_user():

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "home.html"
    )


# ============================================================
# SIGN UP
# ============================================================

@app.route(
    "/signup",
    methods=["GET", "POST"],
)
def signup():

    if request.method == "POST":

        username = request.form.get(
            "username",
            "",
        ).strip()

        password = request.form.get(
            "password",
            "",
        )

        confirm_password = request.form.get(
            "confirm_password",
            "",
        )

        if not username or not password:

            flash(
                "Username and password are required.",
                "error",
            )

            return redirect(
                url_for("signup")
            )

        if len(password) < 8:

            flash(
                "Password must contain at least 8 characters.",
                "error",
            )

            return redirect(
                url_for("signup")
            )

        if (
            confirm_password
            and password != confirm_password
        ):

            flash(
                "Passwords do not match.",
                "error",
            )

            return redirect(
                url_for("signup")
            )

        conn = get_db()

        existing = conn.execute(
            """
            SELECT id
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

        if existing:

            conn.close()

            flash(
                "Username already exists.",
                "error",
            )

            return redirect(
                url_for("signup")
            )

        conn.execute(
            """
            INSERT INTO users
            (
                username,
                password_hash,
                role,
                status,
                created_at
            )
            VALUES (?, ?, 'USER', 'PENDING', ?)
            """,
            (
                username,
                generate_password_hash(
                    password
                ),
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            ),
        )

        conn.commit()
        conn.close()

        return render_template(
            "signup_success.html",
            username=username,
        )

    return render_template(
        "signup.html"
    )


# ============================================================
# SIGN IN
# ============================================================

def signin_handler():

    if request.method == "POST":

        username = request.form.get(
            "username",
            "",
        ).strip()

        password = request.form.get(
            "password",
            "",
        )

        conn = get_db()

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

        conn.close()

        if (
            not user
            or not check_password_hash(
                user["password_hash"],
                password,
            )
        ):

            flash(
                "Invalid username or password.",
                "error",
            )

            return redirect(
                url_for("login")
            )

        if user["status"] != "ACTIVE":

            flash(
                "Your account is waiting for administrator approval.",
                "error",
            )

            return redirect(
                url_for("login")
            )

        session.clear()

        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]

        get_csrf_token()

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "login.html"
    )


@app.route(
    "/login",
    methods=["GET", "POST"],
)
def login():

    return signin_handler()


@app.route(
    "/signin",
    methods=["GET", "POST"],
)
def signin():

    return signin_handler()


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/logout",
    methods=["POST"],
)
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():

    ensure_mqtt_connection()

    devices, sensors = (
        read_latest_status()
    )

    return render_template(
        "index.html",
        device_state=devices,
        sensor_state=sensors,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# DEVICES
# ============================================================

@app.route("/devices")
@login_required
def devices():

    ensure_mqtt_connection()

    devices, sensors = (
        read_latest_status()
    )

    return render_template(
        "devices.html",
        device_state=devices,
        sensor_state=sensors,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# MONITORING
# ============================================================

@app.route("/monitoring")
@login_required
def monitoring():

    ensure_mqtt_connection()

    devices, sensors = (
        read_latest_status()
    )

    return render_template(
        "monitoring.html",
        sensor_state=sensors,
        device_state=devices,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# HISTORY
# ============================================================

@app.route("/history")
@login_required
def history():

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            id,
            created_at,
            username,
            device,
            action,
            event_type,
            result
        FROM history
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()

    conn.close()

    return render_template(
        "history.html",
        history=rows,
    )


# ============================================================
# SETTINGS
# ============================================================

@app.route("/settings")
@login_required
def settings():

    return render_template(
        "settings.html"
    )


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin_users():

    conn = get_db()

    users = conn.execute(
        """
        SELECT
            id,
            username,
            role,
            status,
            created_at
        FROM users
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return render_template(
        "admin_users.html",
        users=users,
    )


@app.route(
    "/admin/user/<int:user_id>/<action>",
    methods=["POST"],
)
@admin_required
def admin_user_action(
    user_id,
    action,
):

    if action not in {
        "approve",
        "reject",
        "disable",
    }:

        flash(
            "Invalid user action.",
            "error",
        )

        return redirect(
            url_for("admin_users")
        )

    conn = get_db()

    user = conn.execute(
        """
        SELECT username, role
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()

    if not user:

        conn.close()

        flash(
            "User not found.",
            "error",
        )

        return redirect(
            url_for("admin_users")
        )

    if user["role"] == "ADMIN":

        conn.close()

        flash(
            "Administrator accounts cannot be changed here.",
            "error",
        )

        return redirect(
            url_for("admin_users")
        )

    new_status = {
        "approve": "ACTIVE",
        "reject": "REJECTED",
        "disable": "DISABLED",
    }[action]

    conn.execute(
        """
        UPDATE users
        SET status = ?
        WHERE id = ?
        """,
        (
            new_status,
            user_id,
        ),
    )

    conn.commit()
    conn.close()

    add_history(
        session.get(
            "username",
            "Unknown",
        ),
        "USER",
        action.upper(),
        "Success",
        "User Management",
    )

    flash(
        f"User {user['username']} is now {new_status}.",
        "success",
    )

    return redirect(
        url_for("admin_users")
    )


# ============================================================
# STATUS API
# ============================================================

@app.route("/api/status")
@login_required
def api_status():

    connected = ensure_mqtt_connection()

    devices, sensors = (
        read_latest_status()
    )

    with mqtt_lock:

        device_state.update(
            devices
        )

        sensor_state.update(
            sensors
        )

    return jsonify(
        {
            "mqtt_connected": connected,
            "mqtt_error": mqtt_last_error,
            "devices": devices,
            "sensors": sensors,
        }
    )


# ============================================================
# DEVICE CONTROL API
# ============================================================

@app.route(
    "/api/device/<device>/<action>",
    methods=["POST"],
)
@login_required
def api_device(
    device,
    action,
):

    device = device.lower()
    action = action.upper()

    allowed_devices = {
        "light",
        "fan",
        "geyser",
    }

    allowed_actions = {
        "ON",
        "OFF",
    }

    if device not in allowed_devices:

        return jsonify(
            {
                "success": False,
                "error": "Invalid device.",
            }
        ), 400

    if action not in allowed_actions:

        return jsonify(
            {
                "success": False,
                "error": "Invalid action.",
            }
        ), 400

    success = publish_device_command(
        device,
        action,
    )

    username = session.get(
        "username",
        "Unknown",
    )

    add_history(
        username,
        device.upper(),
        action,
        (
            "Success"
            if success
            else "Failed"
        ),
        "Device Control",
    )

    if success:

        set_device_status(
            device,
            action,
        )

        with mqtt_lock:

            device_state[
                device
            ] = action

    return jsonify(
        {
            "success": success,
            "device": device,
            "action": action,
            "mqtt_connected": mqtt_connected,
            "mqtt_error": mqtt_last_error,
        }
    )


# ============================================================
# MQTT DEBUG API
# ============================================================

@app.route("/api/mqtt")
@login_required
def api_mqtt():

    connected = ensure_mqtt_connection()

    return jsonify(
        {
            "connected": connected,
            "mqtt_connected": connected,
            "error": mqtt_last_error,
            "broker": MQTT_BROKER,
            "port": MQTT_PORT,
            "topics": {
                "light": TOPIC_LIGHT,
                "fan": TOPIC_FAN,
                "geyser": TOPIC_GEYSER,
                "status": TOPIC_STATUS,
            },
        }
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    connected = ensure_mqtt_connection()

    return jsonify(
        {
            "status": "ok",
            "mqtt_connected": connected,
        }
    )


# ============================================================
# STARTUP
# ============================================================

init_db()

try:

    start_mqtt()

except Exception as exc:

    print(
        "MQTT INITIALIZATION ERROR:",
        type(exc).__name__,
        str(exc),
        flush=True,
    )


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "5000",
            )
        ),
        debug=False,
    )
