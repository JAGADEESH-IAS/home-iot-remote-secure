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


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key",
)

DATABASE = "home_iot.db"

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

TOPIC_LIGHT = "home/light"
TOPIC_FAN = "home/fan"
TOPIC_GEYSER = "home/geyser"
TOPIC_STATUS = "home/status"

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

mqtt_client = None
mqtt_connected = False
mqtt_last_error = ""

mqtt_lock = threading.Lock()


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

    return (
        {
            "light": row["light"],
            "fan": row["fan"],
            "geyser": row["geyser"],
        },
        {
            "temperature": row["temperature"],
            "humidity": row["humidity"],
            "gas": row["gas"],
            "gas_raw": row["gas_raw"],
        },
    )


def write_latest_status(devices, sensors):
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


def set_device_status(device, action):
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


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = (
            secrets.token_urlsafe(32)
        )

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

        subscriptions = [
            TOPIC_LIGHT,
            TOPIC_FAN,
            TOPIC_GEYSER,
            TOPIC_STATUS,
        ]

        for topic in subscriptions:
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

        current_devices.update(
            {
                key: str(value).upper()
                for key, value in devices.items()
                if key in {
                    "light",
                    "fan",
                    "geyser",
                }
            }
        )

        current_sensors.update(
            {
                key: value
                for key, value in sensors.items()
                if key in {
                    "temperature",
                    "humidity",
                    "gas",
                    "gas_raw",
                }
            }
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
            "MQTT message error:",
            exc,
            flush=True,
        )


def create_mqtt_client():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=(
            "home-iot-flask-"
            + str(os.getpid())
            + "-"
            + str(int(time.time()))
            + "-"
            + secrets.token_hex(3)
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
    global mqtt_client
    global mqtt_connected
    global mqtt_last_error

    with mqtt_lock:
        if mqtt_client is not None:
            try:
                if mqtt_client.is_connected():
                    mqtt_connected = True
                    return True

            except Exception as exc:
                print(
                    "MQTT state check error:",
                    exc,
                    flush=True,
                )

        if not MQTT_BROKER:
            mqtt_connected = False
            mqtt_last_error = (
                "MQTT_BROKER is empty."
            )

            return False

        if not MQTT_USERNAME:
            mqtt_connected = False
            mqtt_last_error = (
                "MQTT_USERNAME is empty."
            )

            return False

        if not MQTT_PASSWORD:
            mqtt_connected = False
            mqtt_last_error = (
                "MQTT_PASSWORD is empty."
            )

            return False

        try:
            print(
                "Creating MQTT client for current worker...",
                flush=True,
            )

            client = create_mqtt_client()

            mqtt_client = client

            print(
                "Connecting MQTT worker to:",
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

            deadline = time.time() + 5

            while time.time() < deadline:
                try:
                    if client.is_connected():
                        mqtt_connected = True
                        mqtt_last_error = ""

                        print(
                            "MQTT worker connected successfully.",
                            flush=True,
                        )

                        return True

                except Exception:
                    pass

                time.sleep(0.1)

            if client.is_connected():
                mqtt_connected = True
                mqtt_last_error = ""

                return True

            mqtt_connected = False

            if not mqtt_last_error:
                mqtt_last_error = (
                    "MQTT connection did not complete."
                )

            print(
                "MQTT worker connection timeout:",
                mqtt_last_error,
                flush=True,
            )

            return False

        except Exception as exc:
            mqtt_connected = False

            mqtt_last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            print(
                "MQTT worker connection error:",
                mqtt_last_error,
                flush=True,
            )

            return False


def start_mqtt():
    print(
        "Starting MQTT background client",
        flush=True,
    )

    ensure_mqtt_connection()


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

    if not ensure_mqtt_connection():
        print(
            "MQTT command blocked:",
            mqtt_last_error,
            flush=True,
        )

        return False

    client = mqtt_client

    if client is None:
        return False

    try:
        if not client.is_connected():
            return False

        result = client.publish(
            topic,
            action.upper(),
            qos=0,
        )

        print(
            "MQTT publish:",
            topic,
            action.upper(),
            "result=",
            result.rc,
            flush=True,
        )

        return (
            result.rc
            == mqtt.MQTT_ERR_SUCCESS
        )

    except Exception as exc:
        print(
            "MQTT publish error:",
            exc,
            flush=True,
        )

        return False


@app.route("/")
def home():
    if current_user():
        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "home.html"
    )


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


@app.route(
    "/logout",
    methods=["POST"],
)
def logout():
    session.clear()

    return redirect(
        url_for("home")
    )


@app.route("/dashboard")
@login_required
def dashboard():
    ensure_mqtt_connection()

    devices_state, sensors_state = (
        read_latest_status()
    )

    return render_template(
        "index.html",
        device_state=devices_state,
        sensor_state=sensors_state,
        mqtt_connected=mqtt_connected,
    )


@app.route("/devices")
@login_required
def devices():
    ensure_mqtt_connection()

    devices_state, _ = (
        read_latest_status()
    )

    return render_template(
        "devices.html",
        device_state=devices_state,
        mqtt_connected=mqtt_connected,
    )


@app.route("/monitoring")
@login_required
def monitoring():
    ensure_mqtt_connection()

    devices_state, sensors_state = (
        read_latest_status()
    )

    return render_template(
        "monitoring.html",
        sensor_state=sensors_state,
        device_state=devices_state,
        mqtt_connected=mqtt_connected,
    )


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


@app.route("/settings")
@login_required
def settings():
    return render_template(
        "settings.html"
    )


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


@app.route("/api/status")
@login_required
def api_status():
    connected = ensure_mqtt_connection()

    devices, sensors = (
        read_latest_status()
    )

    with mqtt_lock:
        device_state.update(devices)
        sensor_state.update(sensors)

    return jsonify(
        {
            "mqtt_connected": connected,
            "mqtt_error": mqtt_last_error,
            "devices": devices,
            "sensors": sensors,
        }
    )


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
            device_state[device] = action

    return jsonify(
        {
            "success": success,
            "device": device,
            "action": action,
            "mqtt_connected": (
                mqtt_connected
            ),
            "mqtt_error": (
                mqtt_last_error
            ),
        }
    )


init_db()

try:
    start_mqtt()

except Exception as exc:
    print(
        "MQTT initialization error:",
        exc,
        flush=True,
    )


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
