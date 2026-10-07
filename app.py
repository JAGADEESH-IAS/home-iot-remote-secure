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

from werkzeug.security import generate_password_hash, check_password_hash
import paho.mqtt.client as mqtt


# ============================================================
# FLASK CONFIGURATION
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
# LIVE DEVICE / SENSOR STATE
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
}


# ============================================================
# MQTT STATE
# ============================================================

mqtt_connected = False
mqtt_client = None
mqtt_lock = threading.Lock()

mqtt_error = None
mqtt_reason = None
mqtt_last_connected = None
mqtt_connection_attempted = False
mqtt_client_created = False
mqtt_connection_started = False


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
                generate_password_hash(ADMIN_PASSWORD),
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            ),
        )

    conn.commit()
    conn.close()


# ============================================================
# CSRF PROTECTION
# ============================================================

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return session["csrf_token"]


@app.context_processor
def inject_template_values():
    return {
        "csrf_token": get_csrf_token(),
        "current_user": current_user(),
    }


@app.before_request
def protect_post_requests():
    if request.method != "POST":
        return None

    token = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
    )

    expected = session.get("csrf_token")

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
            return redirect(url_for("login"))

        if user["status"] != "ACTIVE":
            session.clear()

            flash(
                "Your account is not approved yet.",
                "error",
            )

            return redirect(url_for("login"))

        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()

        if not user:
            return redirect(url_for("login"))

        if (
            user["status"] != "ACTIVE"
            or user["role"] != "ADMIN"
        ):
            flash(
                "Administrator access required.",
                "error",
            )

            return redirect(url_for("dashboard"))

        return view(*args, **kwargs)

    return wrapped


# ============================================================
# MQTT CALLBACKS
# ============================================================

def mqtt_on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties=None,
):
    global mqtt_connected
    global mqtt_error
    global mqtt_reason
    global mqtt_last_connected

    print(
        "MQTT on_connect callback received."
    )

    print(
        "MQTT reason code:",
        reason_code,
    )

    try:
        failure = getattr(
            reason_code,
            "is_failure",
            False,
        )

        if reason_code == 0:
            failure = False

        if failure:
            with mqtt_lock:
                mqtt_connected = False
                mqtt_reason = str(reason_code)
                mqtt_error = (
                    "HiveMQ rejected the MQTT connection."
                )

            print(
                "MQTT CONNECTION FAILED:",
                reason_code,
            )

            return

        with mqtt_lock:
            mqtt_connected = True
            mqtt_error = None
            mqtt_reason = str(reason_code)
            mqtt_last_connected = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        topics = [
            TOPIC_LIGHT,
            TOPIC_FAN,
            TOPIC_GEYSER,
            TOPIC_STATUS,
        ]

        for topic in topics:
            try:
                result, mid = client.subscribe(
                    topic,
                    qos=0,
                )

                print(
                    "MQTT subscribe:",
                    topic,
                    "result=",
                    result,
                    "mid=",
                    mid,
                )

            except Exception as exc:
                print(
                    "MQTT subscribe error:",
                    topic,
                    repr(exc),
                )

        print(
            "MQTT CONNECTED SUCCESSFULLY"
        )

    except Exception as exc:
        with mqtt_lock:
            mqtt_connected = False
            mqtt_error = str(exc)
            mqtt_reason = str(reason_code)

        print(
            "MQTT on_connect callback error:",
            repr(exc),
        )


def mqtt_on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None,
):
    global mqtt_connected
    global mqtt_error
    global mqtt_reason

    with mqtt_lock:
        mqtt_connected = False
        mqtt_reason = str(reason_code)
        mqtt_error = (
            f"MQTT disconnected: {reason_code}"
        )

    print(
        "MQTT DISCONNECTED:",
        reason_code,
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
            "MQTT MESSAGE:",
            msg.topic,
            payload,
        )

        if msg.topic != TOPIC_STATUS:
            return

        data = json.loads(payload)

        with mqtt_lock:

            if "light" in data:
                device_state["light"] = str(
                    data["light"]
                ).upper()

            if "fan" in data:
                device_state["fan"] = str(
                    data["fan"]
                ).upper()

            if "geyser" in data:
                device_state["geyser"] = str(
                    data["geyser"]
                ).upper()

            if "temperature" in data:
                sensor_state["temperature"] = data[
                    "temperature"
                ]

            if "humidity" in data:
                sensor_state["humidity"] = data[
                    "humidity"
                ]

            if "gas" in data:
                sensor_state["gas"] = data[
                    "gas"
                ]

    except Exception as exc:
        print(
            "MQTT message error:",
            repr(exc),
        )


# ============================================================
# MQTT STARTUP
# ============================================================

def start_mqtt():
    global mqtt_client
    global mqtt_connected
    global mqtt_error
    global mqtt_reason
    global mqtt_connection_attempted
    global mqtt_client_created
    global mqtt_connection_started

    with mqtt_lock:
        mqtt_connection_attempted = True
        mqtt_error = None
        mqtt_reason = None
        mqtt_client_created = False
        mqtt_connection_started = False

    print(
        "============================================================"
    )
    print(
        "STARTING MQTT CONNECTION"
    )
    print(
        "MQTT broker:",
        MQTT_BROKER,
    )
    print(
        "MQTT port:",
        MQTT_PORT,
    )
    print(
        "MQTT username configured:",
        bool(MQTT_USERNAME),
    )
    print(
        "MQTT password configured:",
        bool(MQTT_PASSWORD),
    )
    print(
        "============================================================"
    )

    try:
        if not MQTT_BROKER:
            raise RuntimeError(
                "MQTT_BROKER is empty."
            )

        if not MQTT_USERNAME:
            raise RuntimeError(
                "MQTT_USERNAME is empty."
            )

        if not MQTT_PASSWORD:
            raise RuntimeError(
                "MQTT_PASSWORD is empty."
            )

        print(
            "Creating Paho MQTT client..."
        )

        client = mqtt.Client(
            callback_api_version=(
                mqtt.CallbackAPIVersion.VERSION2
            ),
            client_id=(
                "home-iot-flask-"
                + secrets.token_hex(6)
            ),
        )

        with mqtt_lock:
            mqtt_client_created = True

        print(
            "Paho MQTT client created."
        )

        client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD,
        )

        print(
            "MQTT username/password configured."
        )

        client.tls_set()

        print(
            "MQTT TLS configured."
        )

        client.reconnect_delay_set(
            min_delay=2,
            max_delay=30,
        )

        client.on_connect = mqtt_on_connect
        client.on_disconnect = mqtt_on_disconnect
        client.on_message = mqtt_on_message

        mqtt_client = client

        print(
            "MQTT callbacks configured."
        )

        print(
            "Calling MQTT connect()..."
        )

        with mqtt_lock:
            mqtt_connection_started = True

        client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            keepalive=60,
        )

        print(
            "MQTT connect() returned successfully."
        )

        print(
            "Starting MQTT network loop..."
        )

        client.loop_start()

        print(
            "MQTT background client started."
        )

    except Exception as exc:
        error_text = (
            f"{type(exc).__name__}: {exc}"
        )

        with mqtt_lock:
            mqtt_connected = False
            mqtt_error = error_text
            mqtt_reason = error_text

        print(
            "============================================================"
        )
        print(
            "MQTT STARTUP ERROR"
        )
        print(
            error_text
        )
        print(
            "============================================================"
        )


# ============================================================
# REAL MQTT STATUS
# ============================================================

def get_real_mqtt_status():
    global mqtt_connected
    global mqtt_error

    client = mqtt_client

    if client is None:
        with mqtt_lock:
            mqtt_connected = False

            if not mqtt_error:
                mqtt_error = (
                    "MQTT client was not created."
                )

        return False

    try:
        connected = client.is_connected()

        with mqtt_lock:
            mqtt_connected = bool(
                connected
            )

            if connected:
                mqtt_error = None

        return bool(connected)

    except Exception as exc:
        with mqtt_lock:
            mqtt_connected = False
            mqtt_error = (
                f"{type(exc).__name__}: {exc}"
            )

        return False


# ============================================================
# DEVICE COMMAND
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
        return False

    client = mqtt_client

    if client is None:
        print(
            "MQTT publish failed: client unavailable."
        )

        return False

    if not get_real_mqtt_status():
        print(
            "MQTT publish failed: MQTT is not connected."
        )

        return False

    try:
        result = client.publish(
            topic,
            action.upper(),
            qos=0,
        )

        print(
            "MQTT publish result:",
            result.rc,
        )

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(
                "MQTT command published:",
                topic,
                action.upper(),
            )

            return True

        return False

    except Exception as exc:
        print(
            "MQTT publish exception:",
            repr(exc),
        )

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
    connected = get_real_mqtt_status()

    return render_template(
        "index.html",
        device_state=device_state,
        sensor_state=sensor_state,
        mqtt_connected=connected,
    )


# ============================================================
# DEVICES
# ============================================================

@app.route("/devices")
@login_required
def devices():
    connected = get_real_mqtt_status()

    return render_template(
        "devices.html",
        device_state=device_state,
        mqtt_connected=connected,
    )


# ============================================================
# MONITORING
# ============================================================

@app.route("/monitoring")
@login_required
def monitoring():
    connected = get_real_mqtt_status()

    return render_template(
        "monitoring.html",
        sensor_state=sensor_state,
        device_state=device_state,
        mqtt_connected=connected,
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
    connected = get_real_mqtt_status()

    with mqtt_lock:
        devices_copy = dict(
            device_state
        )

        sensors_copy = dict(
            sensor_state
        )

        error_copy = mqtt_error
        reason_copy = mqtt_reason
        last_connected_copy = (
            mqtt_last_connected
        )

        attempted_copy = (
            mqtt_connection_attempted
        )

        client_created_copy = (
            mqtt_client_created
        )

        connection_started_copy = (
            mqtt_connection_started
        )

    return jsonify(
        {
            "mqtt_connected": connected,
            "mqtt_error": error_copy,
            "mqtt_reason": reason_copy,
            "mqtt_last_connected": (
                last_connected_copy
            ),
            "mqtt_connection_attempted": (
                attempted_copy
            ),
            "mqtt_client_created": (
                client_created_copy
            ),
            "mqtt_connection_started": (
                connection_started_copy
            ),
            "devices": devices_copy,
            "sensors": sensors_copy,
        }
    )


# ============================================================
# MQTT DIAGNOSTIC API
# ============================================================

@app.route("/api/mqtt-debug")
@login_required
def mqtt_debug():
    connected = get_real_mqtt_status()

    with mqtt_lock:
        client_exists = (
            mqtt_client is not None
        )

        return jsonify(
            {
                "connected": connected,
                "client_exists": client_exists,
                "broker": MQTT_BROKER,
                "port": MQTT_PORT,
                "username_configured": bool(
                    MQTT_USERNAME
                ),
                "password_configured": bool(
                    MQTT_PASSWORD
                ),
                "connection_attempted": (
                    mqtt_connection_attempted
                ),
                "client_created": (
                    mqtt_client_created
                ),
                "connection_started": (
                    mqtt_connection_started
                ),
                "error": mqtt_error,
                "reason": mqtt_reason,
                "last_connected": (
                    mqtt_last_connected
                ),
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
        "Success" if success else "Failed",
        "Device Control",
    )

    if success:
        with mqtt_lock:
            device_state[device] = action

    connected = get_real_mqtt_status()

    with mqtt_lock:
        error_copy = mqtt_error
        reason_copy = mqtt_reason

    return jsonify(
        {
            "success": success,
            "device": device,
            "action": action,
            "mqtt_connected": connected,
            "mqtt_error": error_copy,
            "mqtt_reason": reason_copy,
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
        "MQTT initialization error:",
        repr(exc),
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
