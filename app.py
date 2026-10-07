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
    os.environ.get(
        "MQTT_PORT",
        "8883",
    )
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

DEVICE_TOPICS = {
    "light": TOPIC_LIGHT,
    "fan": TOPIC_FAN,
    "geyser": TOPIC_GEYSER,
}


# ============================================================
# LIVE DEVICE STATE
# ============================================================

device_state = {
    "light": "OFF",
    "fan": "OFF",
    "geyser": "OFF",
}


# ============================================================
# LIVE SENSOR STATE
# ============================================================

sensor_state = {
    "temperature": "--",
    "humidity": "--",
    "gas": "--",
    "gas_raw": 0,
}


# ============================================================
# MQTT STATE
# ============================================================

mqtt_state = {
    "connected": False,
    "last_error": "",
    "last_topic": "",
    "last_payload": "",
}


state_lock = threading.Lock()


# ============================================================
# DATABASE
# ============================================================

def get_db():

    conn = sqlite3.connect(
        DATABASE
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = get_db()

    cur = conn.cursor()

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # HISTORY
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # LATEST STATUS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # HISTORY MIGRATION
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # ADMIN ACCOUNT
    # --------------------------------------------------------

    admin = cur.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        (ADMIN_USERNAME,),
    ).fetchone()

    if not admin:

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
# LATEST STATUS HELPERS
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

    if device not in DEVICE_TOPICS:
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
        request.form.get(
            "csrf_token"
        )
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

    user_id = session.get(
        "user_id"
    )

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


# ============================================================
# MQTT CALLBACK: CONNECT
# ============================================================

def on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties=None,
):

    with state_lock:

        if reason_code == 0:

            mqtt_state["connected"] = True
            mqtt_state["last_error"] = ""

        else:

            mqtt_state["connected"] = False
            mqtt_state["last_error"] = str(
                reason_code
            )

    print(
        "========================================",
        flush=True,
    )

    print(
        "MQTT CONNECT CALLBACK",
        flush=True,
    )

    print(
        "Reason code:",
        reason_code,
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    if reason_code != 0:

        print(
            "MQTT CONNECTION FAILED",
            flush=True,
        )

        return

    print(
        "MQTT CONNECTED SUCCESSFULLY",
        flush=True,
    )

    print(
        "Broker:",
        MQTT_BROKER,
        flush=True,
    )

    # --------------------------------------------------------
    # Subscribe to status and command topics.
    # --------------------------------------------------------

    subscriptions = [
        TOPIC_LIGHT,
        TOPIC_FAN,
        TOPIC_GEYSER,
        TOPIC_STATUS,
    ]

    for topic in subscriptions:

        try:

            result, mid = client.subscribe(
                topic
            )

            print(
                "MQTT SUBSCRIBE:",
                topic,
                "result=",
                result,
                "mid=",
                mid,
                flush=True,
            )

        except Exception as exc:

            print(
                "MQTT SUBSCRIBE ERROR:",
                topic,
                exc,
                flush=True,
            )


# ============================================================
# MQTT CALLBACK: DISCONNECT
# ============================================================

def on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None,
):

    with state_lock:

        mqtt_state["connected"] = False

        mqtt_state["last_error"] = str(
            reason_code
        )

    print(
        "MQTT DISCONNECTED:",
        reason_code,
        flush=True,
    )


# ============================================================
# MQTT CALLBACK: MESSAGE
# ============================================================

def on_message(
    client,
    userdata,
    msg,
):

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

        with state_lock:

            mqtt_state[
                "last_topic"
            ] = msg.topic

            mqtt_state[
                "last_payload"
            ] = payload

        if msg.topic != TOPIC_STATUS:

            return

        data = json.loads(
            payload
        )

        print(
            "MQTT STATUS JSON PARSED:",
            data,
            flush=True,
        )

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

        # ----------------------------------------------------
        # DEVICES
        # ----------------------------------------------------

        for device in (
            "light",
            "fan",
            "geyser",
        ):

            value = devices.get(
                device
            )

            if value is not None:

                current_devices[
                    device
                ] = str(
                    value
                ).upper()

        # ----------------------------------------------------
        # SENSORS
        # ----------------------------------------------------

        if "temperature" in sensors:

            current_sensors[
                "temperature"
            ] = sensors[
                "temperature"
            ]

        if "humidity" in sensors:

            current_sensors[
                "humidity"
            ] = sensors[
                "humidity"
            ]

        if "gas" in sensors:

            current_sensors[
                "gas"
            ] = sensors[
                "gas"
            ]

        if "gas_raw" in sensors:

            current_sensors[
                "gas_raw"
            ] = sensors[
                "gas_raw"
            ]

        # ----------------------------------------------------
        # DATABASE
        # ----------------------------------------------------

        write_latest_status(
            current_devices,
            current_sensors,
        )

        # ----------------------------------------------------
        # MEMORY
        # ----------------------------------------------------

        with state_lock:

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

    except json.JSONDecodeError as exc:

        print(
            "MQTT JSON ERROR:",
            exc,
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
# MQTT CLIENT
#
# THIS STRUCTURE MATCHES THE SUCCESSFUL PROJECT.
# ============================================================

mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="flask-home-remote-control"
)

mqtt_client.username_pw_set(
    MQTT_USERNAME,
    MQTT_PASSWORD
)

mqtt_client.tls_set()

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message

mqtt_client.reconnect_delay_set(
    min_delay=2,
    max_delay=30
)


# ============================================================
# MQTT WORKER
#
# This is deliberately based on the previously successful
# execution structure.
# ============================================================

def mqtt_worker():

    print(
        "==============================================",
        flush=True,
    )

    print(
        "STARTING HOME IOT MQTT CLIENT",
        flush=True,
    )

    print(
        "Client ID: flask-home-remote-control",
        flush=True,
    )

    print(
        "Broker:",
        MQTT_BROKER,
        flush=True,
    )

    print(
        "Port:",
        MQTT_PORT,
        flush=True,
    )

    print(
        "Username configured:",
        bool(MQTT_USERNAME),
        flush=True,
    )

    print(
        "Password configured:",
        bool(MQTT_PASSWORD),
        flush=True,
    )

    print(
        "==============================================",
        flush=True,
    )

    while True:

        try:

            # ------------------------------------------------
            # CONNECT WHEN DISCONNECTED
            # ------------------------------------------------

            if not mqtt_client.is_connected():

                print(
                    "MQTT CONNECT ATTEMPT",
                    flush=True,
                )

                try:

                    mqtt_client.connect(
                        MQTT_BROKER,
                        MQTT_PORT,
                        keepalive=60
                    )

                    print(
                        "MQTT TCP/TLS CONNECTION CREATED",
                        flush=True,
                    )

                except Exception as error:

                    with state_lock:

                        mqtt_state[
                            "connected"
                        ] = False

                        mqtt_state[
                            "last_error"
                        ] = str(error)

                    print(
                        "MQTT CONNECT ERROR:",
                        error,
                        flush=True,
                    )

                    time.sleep(5)

                    continue

            # ------------------------------------------------
            # RUN MQTT NETWORK LOOP
            #
            # This is the important difference from the
            # previous implementation.
            # ------------------------------------------------

            mqtt_client.loop(
                timeout=1.0
            )

        except Exception as error:

            with state_lock:

                mqtt_state[
                    "connected"
                ] = False

                mqtt_state[
                    "last_error"
                ] = str(error)

            print(
                "MQTT NETWORK ERROR:",
                error,
                flush=True,
            )

            time.sleep(3)


# ============================================================
# START MQTT WORKER
# ============================================================

mqtt_thread = threading.Thread(
    target=mqtt_worker,
    daemon=True
)

mqtt_thread.start()


# ============================================================
# DEVICE COMMAND
#
# Directly follows the previously successful structure.
# ============================================================

def publish_device_command(
    device,
    action,
):

    device = device.lower().strip()

    action = action.upper().strip()

    print(
        "================================================",
        flush=True,
    )

    print(
        "DEVICE COMMAND REQUEST:",
        device,
        "->",
        action,
        flush=True,
    )

    # --------------------------------------------------------
    # Validate device
    # --------------------------------------------------------

    if device not in DEVICE_TOPICS:

        print(
            "UNKNOWN DEVICE:",
            device,
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # Validate action
    # --------------------------------------------------------

    if action not in (
        "ON",
        "OFF",
    ):

        print(
            "INVALID ACTION:",
            action,
            flush=True,
        )

        return False

    topic = DEVICE_TOPICS[
        device
    ]

    try:

        # ----------------------------------------------------
        # Check MQTT connection
        # ----------------------------------------------------

        if not mqtt_client.is_connected():

            print(
                "MQTT IS NOT CONNECTED.",
                flush=True,
            )

            print(
                "ATTEMPTING IMMEDIATE RECONNECT...",
                flush=True,
            )

            try:

                mqtt_client.reconnect()

                print(
                    "MQTT RECONNECT SUCCESSFUL",
                    flush=True,
                )

            except Exception as error:

                with state_lock:

                    mqtt_state[
                        "connected"
                    ] = False

                    mqtt_state[
                        "last_error"
                    ] = str(error)

                print(
                    "MQTT RECONNECT ERROR:",
                    error,
                    flush=True,
                )

                return False

        # ----------------------------------------------------
        # Direct publish
        # ----------------------------------------------------

        print(
            "MQTT COMMAND TOPIC:",
            topic,
            flush=True,
        )

        print(
            "MQTT COMMAND PAYLOAD:",
            action,
            flush=True,
        )

        print(
            "MQTT COMMAND QOS: 0",
            flush=True,
        )

        print(
            "MQTT COMMAND RETAIN: False",
            flush=True,
        )

        result = mqtt_client.publish(
            topic,
            action,
            qos=0,
            retain=False
        )

        print(
            "MQTT COMMAND PUBLISH RESULT:",
            result.rc,
            flush=True,
        )

        print(
            "MQTT COMMAND MESSAGE ID:",
            result.mid,
            flush=True,
        )

        if result.rc != (
            mqtt.MQTT_ERR_SUCCESS
        ):

            print(
                "MQTT COMMAND PUBLISH FAILED",
                flush=True,
            )

            return False

        print(
            "MQTT COMMAND PUBLISHED SUCCESSFULLY",
            flush=True,
        )

        print(
            "================================================",
            flush=True,
        )

        return True

    except Exception as exc:

        print(
            "MQTT COMMAND ERROR:",
            type(exc).__name__,
            str(exc),
            flush=True,
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

    devices, sensors = (
        read_latest_status()
    )

    with state_lock:

        connected = mqtt_state[
            "connected"
        ]

    return render_template(
        "index.html",
        device_state=devices,
        sensor_state=sensors,
        mqtt_connected=connected,
    )


# ============================================================
# DEVICES
# ============================================================

@app.route("/devices")
@login_required
def devices():

    devices, sensors = (
        read_latest_status()
    )

    with state_lock:

        connected = mqtt_state[
            "connected"
        ]

    return render_template(
        "devices.html",
        device_state=devices,
        sensor_state=sensors,
        mqtt_connected=connected,
    )


# ============================================================
# MONITORING
# ============================================================

@app.route("/monitoring")
@login_required
def monitoring():

    devices, sensors = (
        read_latest_status()
    )

    with state_lock:

        connected = mqtt_state[
            "connected"
        ]

    return render_template(
        "monitoring.html",
        sensor_state=sensors,
        device_state=devices,
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
# ADMIN USER MANAGEMENT
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

    devices, sensors = (
        read_latest_status()
    )

    with state_lock:

        mqtt_info = dict(
            mqtt_state
        )

        device_state.update(
            devices
        )

        sensor_state.update(
            sensors
        )

    return jsonify(
        {
            "mqtt_connected":
                mqtt_info["connected"],

            "mqtt_error":
                mqtt_info["last_error"],

            "devices":
                devices,

            "sensors":
                sensors,
        }
    )


# ============================================================
# MQTT DEBUG API
# ============================================================

@app.route("/api/mqtt")
@login_required
def api_mqtt():

    with state_lock:

        return jsonify(
            {
                "broker":
                    MQTT_BROKER,

                "port":
                    MQTT_PORT,

                "username":
                    MQTT_USERNAME,

                "password_configured":
                    bool(MQTT_PASSWORD),

                "connected":
                    mqtt_state[
                        "connected"
                    ],

                "last_error":
                    mqtt_state[
                        "last_error"
                    ],

                "last_topic":
                    mqtt_state[
                        "last_topic"
                    ],

                "last_payload":
                    mqtt_state[
                        "last_payload"
                    ],

                "topics":
                    DEVICE_TOPICS,
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

    device = device.lower().strip()

    action = action.upper().strip()

    if device not in DEVICE_TOPICS:

        return jsonify(
            {
                "success": False,
                "error": "Invalid device.",
            }
        ), 400

    if action not in (
        "ON",
        "OFF",
    ):

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

    # --------------------------------------------------------
    # IMPORTANT:
    # We update the displayed state only after the MQTT
    # publish was accepted by Paho.
    # --------------------------------------------------------

    if success:

        set_device_status(
            device,
            action,
        )

        with state_lock:

            device_state[
                device
            ] = action

    with state_lock:

        connected = mqtt_state[
            "connected"
        ]

        error = mqtt_state[
            "last_error"
        ]

    return jsonify(
        {
            "success":
                success,

            "device":
                device,

            "action":
                action,

            "mqtt_connected":
                connected,

            "mqtt_error":
                error,
        }
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    with state_lock:

        connected = mqtt_state[
            "connected"
        ]

    return jsonify(
        {
            "status": "ok",
            "mqtt_connected": connected,
        }
    )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

init_db()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
