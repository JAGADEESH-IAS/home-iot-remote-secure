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
# FLASK APPLICATION
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
# MQTT RUNTIME STATE
# ============================================================

mqtt_client = None

mqtt_connected = False

mqtt_last_error = ""

mqtt_lock = threading.Lock()


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
    # CREATE ADMIN ACCOUNT
    # --------------------------------------------------------

    existing_admin = cur.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        (
            ADMIN_USERNAME,
        ),
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
            VALUES (
                ?,
                ?,
                'ADMIN',
                'ACTIVE',
                ?
            )
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
# CSRF PROTECTION
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
# CURRENT USER
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
        (
            user_id,
        ),
    ).fetchone()

    conn.close()

    return user


# ============================================================
# LOGIN REQUIRED
# ============================================================

def login_required(view):

    @wraps(view)
    def wrapped(
        *args,
        **kwargs,
    ):

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


# ============================================================
# ADMIN REQUIRED
# ============================================================

def admin_required(view):

    @wraps(view)
    def wrapped(
        *args,
        **kwargs,
    ):

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
# MQTT CONNECT CALLBACK
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

        print(
            "MQTT CONNECTED:",
            MQTT_BROKER,
            MQTT_PORT,
            flush=True,
        )

        subscriptions = [
            TOPIC_LIGHT,
            TOPIC_FAN,
            TOPIC_GEYSER,
            TOPIC_STATUS,
        ]

        for topic in subscriptions:

            try:

                result, mid = client.subscribe(
                    topic,
                    qos=0,
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

    else:

        mqtt_connected = False

        mqtt_last_error = str(
            reason_code
        )

        print(
            "MQTT CONNECTION FAILED:",
            reason_code,
            flush=True,
        )


# ============================================================
# MQTT DISCONNECT CALLBACK
# ============================================================

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
        "MQTT DISCONNECTED:",
        reason_code,
        flush=True,
    )


# ============================================================
# MQTT MESSAGE CALLBACK
# ============================================================

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

        # ----------------------------------------------------
        # Only process home/status
        # ----------------------------------------------------

        if msg.topic != TOPIC_STATUS:

            return

        # ----------------------------------------------------
        # Convert JSON text into Python object
        # ----------------------------------------------------

        data = json.loads(
            payload
        )

        # ----------------------------------------------------
        # ESP32 sends:
        #
        # {
        #   "devices": {
        #       "light": "OFF",
        #       "fan": "OFF",
        #       "geyser": "OFF"
        #   },
        #   "sensors": {
        #       "temperature": 30.7,
        #       "humidity": 65.8,
        #       "gas": "NORMAL",
        #       "gas_raw": 0
        #   }
        # }
        # ----------------------------------------------------

        devices = data.get(
            "devices",
            {},
        )

        sensors = data.get(
            "sensors",
            {},
        )

        with mqtt_lock:

            # ------------------------------------------------
            # DEVICE STATES
            # ------------------------------------------------

            if "light" in devices:

                device_state[
                    "light"
                ] = str(
                    devices["light"]
                ).upper()

            if "fan" in devices:

                device_state[
                    "fan"
                ] = str(
                    devices["fan"]
                ).upper()

            if "geyser" in devices:

                device_state[
                    "geyser"
                ] = str(
                    devices["geyser"]
                ).upper()

            # ------------------------------------------------
            # SENSOR VALUES
            # ------------------------------------------------

            if "temperature" in sensors:

                sensor_state[
                    "temperature"
                ] = sensors[
                    "temperature"
                ]

            if "humidity" in sensors:

                sensor_state[
                    "humidity"
                ] = sensors[
                    "humidity"
                ]

            if "gas" in sensors:

                sensor_state[
                    "gas"
                ] = sensors[
                    "gas"
                ]

            if "gas_raw" in sensors:

                sensor_state[
                    "gas_raw"
                ] = sensors[
                    "gas_raw"
                ]

        print(
            "MQTT STATUS UPDATED SUCCESSFULLY",
            flush=True,
        )

        print(
            "TEMPERATURE:",
            sensor_state["temperature"],
            flush=True,
        )

        print(
            "HUMIDITY:",
            sensor_state["humidity"],
            flush=True,
        )

        print(
            "GAS:",
            sensor_state["gas"],
            flush=True,
        )

    except Exception as exc:

        print(
            "MQTT MESSAGE ERROR:",
            exc,
            flush=True,
        )


# ============================================================
# CREATE MQTT CLIENT
# ============================================================

def create_mqtt_client():

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=(
            "home-iot-flask-"
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

    client.on_connect = (
        mqtt_on_connect
    )

    client.on_disconnect = (
        mqtt_on_disconnect
    )

    client.on_message = (
        mqtt_on_message
    )

    return client


# ============================================================
# ENSURE MQTT CONNECTION
# ============================================================

def ensure_mqtt_connection():

    global mqtt_client
    global mqtt_connected
    global mqtt_last_error

    with mqtt_lock:

        # ----------------------------------------------------
        # Existing client
        # ----------------------------------------------------

        if mqtt_client is not None:

            try:

                if mqtt_client.is_connected():

                    mqtt_connected = True

                    return True

            except Exception as exc:

                print(
                    "MQTT STATE CHECK ERROR:",
                    exc,
                    flush=True,
                )

        # ----------------------------------------------------
        # Configuration check
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Create MQTT client
        # ----------------------------------------------------

        try:

            print(
                "CREATING MQTT CLIENT...",
                flush=True,
            )

            client = (
                create_mqtt_client()
            )

            mqtt_client = client

            print(
                "CONNECTING TO HIVEMQ:",
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

            # ------------------------------------------------
            # Wait for connection
            # ------------------------------------------------

            deadline = (
                time.time() + 5
            )

            while (
                time.time() < deadline
            ):

                try:

                    if (
                        client.is_connected()
                    ):

                        mqtt_connected = True

                        mqtt_last_error = ""

                        print(
                            "MQTT WORKER CONNECTED SUCCESSFULLY",
                            flush=True,
                        )

                        return True

                except Exception:
                    pass

                time.sleep(
                    0.1
                )

            # ------------------------------------------------
            # Final connection check
            # ------------------------------------------------

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
                "MQTT CONNECTION TIMEOUT:",
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
                "MQTT CONNECTION ERROR:",
                mqtt_last_error,
                flush=True,
            )

            return False


# ============================================================
# START MQTT
# ============================================================

def start_mqtt():

    print(
        "STARTING MQTT BACKGROUND CLIENT",
        flush=True,
    )

    ensure_mqtt_connection()


# ============================================================
# PUBLISH DEVICE COMMAND
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

    topic = topics.get(
        device
    )

    if not topic:

        return False

    # --------------------------------------------------------
    # Make sure current worker has MQTT
    # --------------------------------------------------------

    if not ensure_mqtt_connection():

        print(
            "MQTT COMMAND BLOCKED:",
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
            "MQTT PUBLISH:",
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
            "MQTT PUBLISH ERROR:",
            exc,
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
    methods=[
        "GET",
        "POST",
    ],
)
def signup():

    if request.method == "POST":

        username = (
            request.form.get(
                "username",
                "",
            ).strip()
        )

        password = (
            request.form.get(
                "password",
                "",
            )
        )

        confirm_password = (
            request.form.get(
                "confirm_password",
                "",
            )
        )

        if (
            not username
            or not password
        ):

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
            and password
            != confirm_password
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
            (
                username,
            ),
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
            VALUES (
                ?,
                ?,
                'USER',
                'PENDING',
                ?
            )
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

        username = (
            request.form.get(
                "username",
                "",
            ).strip()
        )

        password = (
            request.form.get(
                "password",
                "",
            )
        )

        conn = get_db()

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
            """,
            (
                username,
            ),
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

        session["user_id"] = (
            user["id"]
        )

        session["username"] = (
            user["username"]
        )

        session["role"] = (
            user["role"]
        )

        get_csrf_token()

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "login.html"
    )


@app.route(
    "/login",
    methods=[
        "GET",
        "POST",
    ],
)
def login():

    return signin_handler()


@app.route(
    "/signin",
    methods=[
        "GET",
        "POST",
    ],
)
def signin():

    return signin_handler()


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/logout",
    methods=[
        "POST",
    ],
)
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route(
    "/dashboard"
)
@login_required
def dashboard():

    ensure_mqtt_connection()

    return render_template(
        "index.html",
        device_state=device_state,
        sensor_state=sensor_state,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# DEVICES
# ============================================================

@app.route(
    "/devices"
)
@login_required
def devices():

    ensure_mqtt_connection()

    return render_template(
        "devices.html",
        device_state=device_state,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# MONITORING
# ============================================================

@app.route(
    "/monitoring"
)
@login_required
def monitoring():

    ensure_mqtt_connection()

    return render_template(
        "monitoring.html",
        sensor_state=sensor_state,
        device_state=device_state,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# HISTORY
# ============================================================

@app.route(
    "/history"
)
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

@app.route(
    "/settings"
)
@login_required
def settings():

    return render_template(
        "settings.html"
    )


# ============================================================
# ADMIN USER MANAGEMENT
# ============================================================

@app.route(
    "/admin"
)
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
    methods=[
        "POST",
    ],
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
        SELECT
            username,
            role
        FROM users
        WHERE id = ?
        """,
        (
            user_id,
        ),
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
        (
            f"User {user['username']} "
            f"is now {new_status}."
        ),
        "success",
    )

    return redirect(
        url_for("admin_users")
    )


# ============================================================
# STATUS API
# ============================================================

@app.route(
    "/api/status"
)
@login_required
def api_status():

    connected = (
        ensure_mqtt_connection()
    )

    with mqtt_lock:

        return jsonify(
            {
                "mqtt_connected": connected,

                "mqtt_error": (
                    mqtt_last_error
                ),

                "devices": dict(
                    device_state
                ),

                "sensors": dict(
                    sensor_state
                ),
            }
        )


# ============================================================
# DEVICE CONTROL API
# ============================================================

@app.route(
    "/api/device/<device>/<action>",
    methods=[
        "POST",
    ],
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

    success = (
        publish_device_command(
            device,
            action,
        )
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
    # Keep dashboard state synchronized with command.
    # ESP32's next home/status message will confirm
    # the physical state.
    # --------------------------------------------------------

    if success:

        with mqtt_lock:

            device_state[
                device
            ] = action

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


# ============================================================
# APPLICATION STARTUP
# ============================================================

init_db()

try:

    start_mqtt()

except Exception as exc:

    print(
        "MQTT INITIALIZATION ERROR:",
        exc,
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
