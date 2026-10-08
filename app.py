import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta

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
import firebase_admin
from firebase_admin import credentials, firestore


# ============================================================
# FLASK CONFIGURATION
# ============================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key"
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()


# ============================================================
# FIREBASE CONFIGURATION
# ============================================================

FIREBASE_SERVICE_ACCOUNT = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT",
    ""
).strip()


if not firebase_admin._apps:
    if not FIREBASE_SERVICE_ACCOUNT:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT environment variable is not configured."
        )

    try:
        service_account_info = json.loads(FIREBASE_SERVICE_ACCOUNT)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT does not contain valid JSON."
        ) from exc

    firebase_credential = credentials.Certificate(service_account_info)

    firebase_admin.initialize_app(firebase_credential)


db = firestore.client()


# ============================================================
# MQTT CONFIGURATION
# ============================================================

MQTT_BROKER = os.environ.get(
    "MQTT_BROKER",
    "bravetawny-af88996b.a02.usw2.aws.hivemq.cloud"
).strip()

MQTT_PORT = int(
    os.environ.get("MQTT_PORT", "8883")
)

MQTT_USERNAME = os.environ.get(
    "MQTT_USERNAME",
    ""
).strip()

MQTT_PASSWORD = os.environ.get(
    "MQTT_PASSWORD",
    ""
).strip()


MQTT_STATUS_TOPIC = "home/status"

DEVICE_TOPICS = {
    "light": "home/light",
    "fan": "home/fan",
    "geyser": "home/geyser",
}


# ============================================================
# GLOBAL MQTT STATE
# ============================================================

mqtt_client = None
mqtt_connected = False
mqtt_lock = threading.Lock()


# ============================================================
# TIME HELPERS
# ============================================================

IST = timezone(timedelta(hours=5, minutes=30))


def utc_now():
    """
    Return current UTC time as a timezone-aware datetime.
    """
    return datetime.now(timezone.utc)


def to_ist_string(value):
    """
    Convert Firestore datetime / Python datetime / string
    into an IST display string.
    """

    if value is None:
        return "-"

    try:
        if hasattr(value, "to_datetime"):
            value = value.to_datetime()

        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            value = value.astimezone(IST)

            return value.strftime(
                "%d-%m-%Y %I:%M:%S %p"
            )

        if isinstance(value, str):
            return value

    except Exception:
        pass

    return str(value)


# ============================================================
# FIRESTORE COLLECTION NAMES
# ============================================================

USERS_COLLECTION = "users"
HISTORY_COLLECTION = "history"
LATEST_STATUS_COLLECTION = "latest_status"


# ============================================================
# FIRESTORE USER HELPERS
# ============================================================

def find_user_by_login_id(login_id):
    """
    Find a user using email or phone number.
    """

    normalized = (login_id or "").strip().lower()

    if not normalized:
        return None

    query = (
        db.collection(USERS_COLLECTION)
        .where("login_id", "==", normalized)
        .limit(1)
        .stream()
    )

    for document in query:
        data = document.to_dict() or {}
        data["id"] = document.id
        return data

    return None


def get_user_by_id(user_id):
    """
    Retrieve a user by Firestore document ID.
    """

    if not user_id:
        return None

    document = (
        db.collection(USERS_COLLECTION)
        .document(user_id)
        .get()
    )

    if not document.exists:
        return None

    data = document.to_dict() or {}
    data["id"] = document.id

    return data


def create_admin_if_needed():
    """
    Create the administrator account from Render environment
    variables if it does not already exist.
    """

    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return

    normalized_login = ADMIN_USERNAME.strip().lower()

    existing_admin = find_user_by_login_id(normalized_login)

    if existing_admin:
        return

    now = utc_now()

    db.collection(USERS_COLLECTION).add(
        {
            "login_id": normalized_login,
            "password_hash": generate_password_hash(
                ADMIN_PASSWORD
            ),
            "role": "ADMIN",
            "status": "ACTIVE",
            "created_at": now,
            "updated_at": now,
        }
    )


# ============================================================
# FIRESTORE HISTORY
# ============================================================

def save_history(
    user_login_id,
    device,
    action,
    event,
    result
):
    """
    Store meaningful application/device events in Firestore.

    History is intentionally NOT used for every ESP32 sensor
    update. It records meaningful events such as device commands,
    login attempts, and account-management actions.
    """

    try:
        db.collection(HISTORY_COLLECTION).add(
            {
                "created_at": utc_now(),
                "user": user_login_id or "SYSTEM",
                "device": device or "-",
                "action": action or "-",
                "event": event or "-",
                "result": result or "-",
            }
        )
    except Exception as exc:
        print(
            "HISTORY SAVE ERROR:",
            repr(exc),
            flush=True
        )


def get_history():
    """
    Retrieve history records from Firestore.

    Results are converted into the format expected by
    templates/history.html.
    """

    records = []

    try:
        documents = (
            db.collection(HISTORY_COLLECTION)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(500)
            .stream()
        )

        for document in documents:
            data = document.to_dict() or {}

            created_at = data.get("created_at")

            records.append(
                {
                    "created_at": to_ist_string(created_at),
                    "user": data.get("user", "-"),
                    "device": data.get("device", "-"),
                    "action": data.get("action", "-"),
                    "event": data.get("event", "-"),
                    "result": data.get("result", "-"),
                }
            )

    except Exception as exc:
        print(
            "HISTORY READ ERROR:",
            repr(exc),
            flush=True
        )

    return records


# ============================================================
# FIRESTORE ESP32 LATEST STATUS
# ============================================================

def save_latest_status(status):
    """
    Save the most recent ESP32 status to Firestore.
    """

    try:
        db.collection(LATEST_STATUS_COLLECTION).document(
            "esp32"
        ).set(
            {
                "devices": status.get(
                    "devices",
                    {}
                ),
                "sensors": status.get(
                    "sensors",
                    {}
                ),
                "updated_at": utc_now(),
            }
        )

    except Exception as exc:
        print(
            "LATEST STATUS SAVE ERROR:",
            repr(exc),
            flush=True
        )


def load_latest_status():
    """
    Load the latest ESP32 status from Firestore.
    """

    default_status = {
        "devices": {
            "light": "OFF",
            "fan": "OFF",
            "geyser": "OFF",
        },
        "sensors": {
            "temperature": None,
            "humidity": None,
            "gas": "UNKNOWN",
            "gas_raw": None,
        },
        "updated_at": None,
    }

    try:
        document = (
            db.collection(LATEST_STATUS_COLLECTION)
            .document("esp32")
            .get()
        )

        if not document.exists:
            return default_status

        data = document.to_dict() or {}

        devices = data.get("devices") or {}
        sensors = data.get("sensors") or {}

        return {
            "devices": {
                "light": str(
                    devices.get("light", "OFF")
                ).upper(),
                "fan": str(
                    devices.get("fan", "OFF")
                ).upper(),
                "geyser": str(
                    devices.get("geyser", "OFF")
                ).upper(),
            },
            "sensors": {
                "temperature": sensors.get(
                    "temperature"
                ),
                "humidity": sensors.get(
                    "humidity"
                ),
                "gas": sensors.get(
                    "gas",
                    "UNKNOWN"
                ),
                "gas_raw": sensors.get(
                    "gas_raw"
                ),
            },
            "updated_at": data.get(
                "updated_at"
            ),
        }

    except Exception as exc:
        print(
            "LATEST STATUS READ ERROR:",
            repr(exc),
            flush=True
        )

        return default_status


# ============================================================
# ESP32 ONLINE/OFFLINE STATUS
# ============================================================

ESP32_TIMEOUT_SECONDS = 20


def get_esp32_status():
    """
    ESP32 status is based on the most recent home/status
    message, not merely on the MQTT broker connection.

    The ESP32 is considered online when its latest status
    message was received within ESP32_TIMEOUT_SECONDS.
    """

    latest_status = load_latest_status()

    updated_at = latest_status.get(
        "updated_at"
    )

    online = False

    if updated_at is not None:

        try:
            if hasattr(updated_at, "to_datetime"):
                updated_at = updated_at.to_datetime()

            if isinstance(updated_at, datetime):

                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(
                        tzinfo=timezone.utc
                    )

                age = (
                    utc_now() - updated_at
                ).total_seconds()

                online = (
                    age <= ESP32_TIMEOUT_SECONDS
                )

        except Exception as exc:
            print(
                "ESP32 STATUS CHECK ERROR:",
                repr(exc),
                flush=True
            )

    return {
        "online": online,
        "last_seen": to_ist_string(updated_at),
    }


# ============================================================
# MQTT CALLBACKS
# ============================================================

def on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties=None
):
    """
    Called when the persistent MQTT listener connects.
    """

    global mqtt_connected

    try:
        mqtt_connected = (
            int(reason_code) == 0
        )
    except Exception:
        mqtt_connected = False

    print(
        "MQTT CONNECT:",
        reason_code,
        "CONNECTED:",
        mqtt_connected,
        flush=True
    )

    if mqtt_connected:
        try:
            client.subscribe(
                MQTT_STATUS_TOPIC,
                qos=0
            )

            print(
                "MQTT SUBSCRIBED:",
                MQTT_STATUS_TOPIC,
                flush=True
            )

        except Exception as exc:
            print(
                "MQTT SUBSCRIBE ERROR:",
                repr(exc),
                flush=True
            )


def on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None
):
    """
    Called when the persistent MQTT listener disconnects.
    """

    global mqtt_connected

    mqtt_connected = False

    print(
        "MQTT DISCONNECTED:",
        reason_code,
        flush=True
    )


def on_message(
    client,
    userdata,
    message
):
    """
    Process ESP32 status messages.
    """

    if message.topic != MQTT_STATUS_TOPIC:
        return

    try:
        payload = message.payload.decode(
            "utf-8"
        )

        print(
            "MQTT STATUS RECEIVED:",
            payload,
            flush=True
        )

        data = json.loads(payload)

        devices = data.get(
            "devices",
            {}
        )

        sensors = data.get(
            "sensors",
            {}
        )

        normalized_status = {
            "devices": {
                "light": str(
                    devices.get(
                        "light",
                        "OFF"
                    )
                ).upper(),

                "fan": str(
                    devices.get(
                        "fan",
                        "OFF"
                    )
                ).upper(),

                "geyser": str(
                    devices.get(
                        "geyser",
                        "OFF"
                    )
                ).upper(),
            },

            "sensors": {
                "temperature": sensors.get(
                    "temperature"
                ),

                "humidity": sensors.get(
                    "humidity"
                ),

                "gas": sensors.get(
                    "gas",
                    "UNKNOWN"
                ),

                "gas_raw": sensors.get(
                    "gas_raw"
                ),
            },
        }

        save_latest_status(
            normalized_status
        )

    except Exception as exc:
        print(
            "MQTT STATUS PROCESSING ERROR:",
            repr(exc),
            flush=True
        )


# ============================================================
# MQTT PERSISTENT LISTENER
# ============================================================

def create_mqtt_listener():
    """
    Create the long-running MQTT client used only for
    receiving ESP32 status messages.
    """

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=(
            "home-iot-flask-listener-"
            + str(int(time.time()))
        ),
    )

    client.username_pw_set(
        MQTT_USERNAME,
        MQTT_PASSWORD
    )

    client.tls_set()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    return client


def mqtt_listener_loop():
    """
    Maintain the persistent MQTT listener connection.
    """

    global mqtt_client
    global mqtt_connected

    while True:

        try:

            client = create_mqtt_listener()

            with mqtt_lock:
                mqtt_client = client

            print(
                "MQTT LISTENER CONNECTING...",
                flush=True
            )

            client.connect(
                MQTT_BROKER,
                MQTT_PORT,
                60
            )

            client.loop_forever()

        except Exception as exc:

            mqtt_connected = False

            print(
                "MQTT LISTENER ERROR:",
                repr(exc),
                flush=True
            )

            time.sleep(5)


def start_mqtt():
    """
    Start the persistent MQTT listener in a background thread.
    """

    thread = threading.Thread(
        target=mqtt_listener_loop,
        daemon=True
    )

    thread.start()


# ============================================================
# MQTT DEVICE COMMAND
# ============================================================

def publish_device_command(
    device,
    action
):
    """
    Publish an appliance command using a dedicated,
    short-lived MQTT client.

    Important:
    - QoS remains 0.
    - retain remains True.
    - The command client is separate from the persistent
      status-listener client.
    """

    if device not in DEVICE_TOPICS:
        return False

    action = str(action).upper()

    if action not in (
        "ON",
        "OFF"
    ):
        return False

    topic = DEVICE_TOPICS[device]

    client_id = (
        "home-iot-command-"
        + device
        + "-"
        + str(int(time.time() * 1000))
    )

    command_client = None

    try:

        print(
            "MQTT COMMAND START",
            flush=True
        )

        print(
            "Device:",
            device,
            flush=True
        )

        print(
            "Topic:",
            topic,
            flush=True
        )

        print(
            "Payload:",
            action,
            flush=True
        )

        print(
            "QoS:",
            0,
            flush=True
        )

        print(
            "Retain:",
            True,
            flush=True
        )

        command_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        command_client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD
        )

        command_client.tls_set()

        command_client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60
        )

        command_client.loop_start()

        result = command_client.publish(
            topic,
            payload=action,
            qos=0,
            retain=True,
        )

        print(
            "MQTT PUBLISH RETURN CODE:",
            result.rc,
            flush=True
        )

        print(
            "MQTT PUBLISH MESSAGE ID:",
            result.mid,
            flush=True
        )

        result.wait_for_publish(
            timeout=5
        )

        time.sleep(0.5)

        published = result.is_published()

        print(
            "MQTT IS PUBLISHED:",
            published,
            flush=True
        )

        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            return False

        return published

    except Exception as exc:

        print(
            "MQTT COMMAND ERROR:",
            repr(exc),
            flush=True
        )

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


# ============================================================
# REQUEST / AUTH HELPERS
# ============================================================

@app.context_processor
def inject_template_values():
    """
    Make the current user available to all templates.
    """

    return {
        "current_user": session.get(
            "user"
        )
    }


def login_required(function):
    """
    Protect authenticated routes.
    """

    from functools import wraps

    @wraps(function)
    def decorated(*args, **kwargs):

        if "user" not in session:
            return redirect(
                url_for("signin")
            )

        return function(
            *args,
            **kwargs
        )

    return decorated


def admin_required(function):
    """
    Protect administrator-only routes.
    """

    from functools import wraps

    @wraps(function)
    def decorated(*args, **kwargs):

        current_user = session.get(
            "user"
        )

        if not current_user:
            return redirect(
                url_for("signin")
            )

        if current_user.get("role") != "ADMIN":
            flash(
                "Administrator access required.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )

        return function(
            *args,
            **kwargs
        )

    return decorated


# ============================================================
# CSRF PROTECTION
# ============================================================

def csrf_token():
    """
    Create a CSRF token for the current session.
    """

    import secrets

    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(
            32
        )

    return session["csrf_token"]


@app.context_processor
def inject_csrf():
    return {
        "csrf_token": csrf_token()
    }


@app.before_request
def csrf_protection():

    if request.method not in (
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ):
        return

    endpoint = request.endpoint

    if endpoint in (
        "signin",
        "signup",
    ):
        return

    expected_token = session.get(
        "csrf_token"
    )

    supplied_token = (
        request.headers.get(
            "X-CSRF-Token"
        )
        or request.form.get(
            "csrf_token"
        )
    )

    if not expected_token:
        return jsonify(
            {
                "success": False,
                "message": "CSRF token missing."
            }
        ), 403

    if not supplied_token:
        return jsonify(
            {
                "success": False,
                "message": "CSRF token missing."
            }
        ), 403

    if supplied_token != expected_token:
        return jsonify(
            {
                "success": False,
                "message": "Invalid CSRF token."
            }
        ), 403


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    if "user" in session:
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
    methods=["GET", "POST"]
)
def signup():

    if request.method == "GET":
        return render_template(
            "signup.html"
        )

    login_id = (
        request.form.get(
            "login_id",
            ""
        )
        .strip()
        .lower()
    )

    password = request.form.get(
        "password",
        ""
    )

    confirm_password = request.form.get(
        "confirm_password",
        ""
    )

    if not login_id:
        flash(
            "Email or phone number is required.",
            "error"
        )

        return render_template(
            "signup.html"
        )

    if len(password) < 8:
        flash(
            "Password must contain at least 8 characters.",
            "error"
        )

        return render_template(
            "signup.html"
        )

    if password != confirm_password:
        flash(
            "Passwords do not match.",
            "error"
        )

        return render_template(
            "signup.html"
        )

    if find_user_by_login_id(
        login_id
    ):
        flash(
            "An account with this email or phone number already exists.",
            "error"
        )

        return render_template(
            "signup.html"
        )

    now = utc_now()

    db.collection(
        USERS_COLLECTION
    ).add(
        {
            "login_id": login_id,
            "password_hash": generate_password_hash(
                password
            ),
            "role": "USER",
            "status": "PENDING",
            "created_at": now,
            "updated_at": now,
        }
    )

    save_history(
        login_id,
        "-",
        "SIGNUP",
        "Account Registration",
        "PENDING APPROVAL",
    )

    return render_template(
        "signup_success.html",
        login_id=login_id
    )


# ============================================================
# SIGN IN
# ============================================================

@app.route(
    "/signin",
    methods=["GET", "POST"]
)
@app.route(
    "/login",
    methods=["GET", "POST"]
)
def signin():

    if request.method == "GET":

        if "user" in session:
            return redirect(
                url_for("dashboard")
            )

        return render_template(
            "login.html"
        )

    login_id = (
        request.form.get(
            "login_id",
            ""
        )
        .strip()
        .lower()
    )

    password = request.form.get(
        "password",
        ""
    )

    user = find_user_by_login_id(
        login_id
    )

    if not user:

        save_history(
            login_id,
            "-",
            "LOGIN",
            "Sign In",
            "FAILED",
        )

        flash(
            "Invalid email/phone or password.",
            "error"
        )

        return render_template(
            "login.html"
        )

    password_hash = user.get(
        "password_hash",
        ""
    )

    if not check_password_hash(
        password_hash,
        password
    ):

        save_history(
            login_id,
            "-",
            "LOGIN",
            "Sign In",
            "FAILED",
        )

        flash(
            "Invalid email/phone or password.",
            "error"
        )

        return render_template(
            "login.html"
        )

    status = str(
        user.get(
            "status",
            "PENDING"
        )
    ).upper()

    if status != "ACTIVE":

        save_history(
            login_id,
            "-",
            "LOGIN",
            "Sign In",
            status,
        )

        if status == "PENDING":
            flash(
                "Your account is waiting for administrator approval.",
                "error"
            )

        elif status == "REJECTED":
            flash(
                "Your account has been rejected.",
                "error"
            )

        else:
            flash(
                "Your account is not active.",
                "error"
            )

        return render_template(
            "login.html"
        )

    session.clear()

    session["user"] = {
        "id": user.get("id"),
        "login_id": user.get("login_id"),
        "role": user.get("role", "USER"),
        "status": user.get("status", "ACTIVE"),
    }

    csrf_token()

    save_history(
        login_id,
        "-",
        "LOGIN",
        "Sign In",
        "SUCCESS",
    )

    return redirect(
        url_for("dashboard")
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/logout",
    methods=["POST"]
)
@login_required
def logout():

    current_user = session.get(
        "user"
    )

    login_id = (
        current_user.get(
            "login_id"
        )
        if current_user
        else "SYSTEM"
    )

    save_history(
        login_id,
        "-",
        "LOGOUT",
        "Sign Out",
        "SUCCESS",
    )

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

    latest_status = load_latest_status()
    esp32 = get_esp32_status()

    return render_template(
        "index.html",
        status=latest_status,
        mqtt_connected=mqtt_connected,
        esp32=esp32,
    )


# ============================================================
# DEVICES
# ============================================================

@app.route("/devices")
@login_required
def devices():

    latest_status = load_latest_status()
    esp32 = get_esp32_status()

    return render_template(
        "devices.html",
        status=latest_status,
        mqtt_connected=mqtt_connected,
        esp32=esp32,
    )


# ============================================================
# HISTORY
# ============================================================

@app.route("/history")
@login_required
def history():

    records = get_history()

    return render_template(
        "history.html",
        history=records,
    )


# ============================================================
# SETTINGS
# ============================================================

@app.route("/settings")
@login_required
def settings():

    current_user = session.get(
        "user"
    )

    return render_template(
        "settings.html",
        user=current_user,
    )


# ============================================================
# API: CURRENT STATUS
# ============================================================

@app.route(
    "/api/status",
    methods=["GET"]
)
@login_required
def api_status():

    latest_status = load_latest_status()
    esp32 = get_esp32_status()

    return jsonify(
        {
            "success": True,

            "devices": latest_status.get(
                "devices",
                {}
            ),

            "sensors": latest_status.get(
                "sensors",
                {}
            ),

            "mqtt": {
                "connected": mqtt_connected
            },

            "esp32": {
                "online": esp32.get(
                    "online",
                    False
                ),

                "last_seen": esp32.get(
                    "last_seen",
                    "-"
                ),
            },
        }
    )


# ============================================================
# API: DEVICE CONTROL
# ============================================================

@app.route(
    "/api/device/<device>/<action>",
    methods=["POST"]
)
@login_required
def api_device_control(
    device,
    action
):

    device = str(device).lower()
    action = str(action).upper()

    if device not in DEVICE_TOPICS:

        return jsonify(
            {
                "success": False,
                "message": "Invalid device."
            }
        ), 400

    if action not in (
        "ON",
        "OFF"
    ):

        return jsonify(
            {
                "success": False,
                "message": "Invalid action."
            }
        ), 400

    current_user = session.get(
        "user"
    )

    login_id = (
        current_user.get(
            "login_id"
        )
        if current_user
        else "SYSTEM"
    )

    print(
        "MQTT COMMAND CONNECTION CHECK:",
        mqtt_connected,
        flush=True
    )

    published = publish_device_command(
        device,
        action
    )

    if published:

        save_history(
            login_id,
            device,
            action,
            "Device Command",
            "SENT",
        )

        return jsonify(
            {
                "success": True,
                "message": (
                    device.capitalize()
                    + " command sent: "
                    + action
                ),
                "device": device,
                "action": action,
            }
        )

    save_history(
        login_id,
        device,
        action,
        "Device Command",
        "FAILED",
    )

    return jsonify(
        {
            "success": False,
            "message": (
                "Failed to publish "
                + device
                + " command."
            ),
        }
    ), 500


# ============================================================
# ADMIN USER MANAGEMENT
# ============================================================

@app.route("/admin/users")
@admin_required
def admin_users():

    users = []

    try:

        documents = (
            db.collection(
                USERS_COLLECTION
            )
            .order_by(
                "created_at",
                direction=firestore.Query.DESCENDING
            )
            .stream()
        )

        for document in documents:

            data = document.to_dict() or {}

            data["id"] = document.id

            users.append(data)

    except Exception as exc:

        print(
            "ADMIN USERS READ ERROR:",
            repr(exc),
            flush=True
        )

    return render_template(
        "admin_users.html",
        users=users,
    )


@app.route(
    "/admin/users/<user_id>/<action>",
    methods=["POST"]
)
@admin_required
def admin_user_action(
    user_id,
    action
):

    target_user = get_user_by_id(
        user_id
    )

    if not target_user:

        return jsonify(
            {
                "success": False,
                "message": "User not found."
            }
        ), 404

    if target_user.get(
        "role"
    ) == "ADMIN":

        return jsonify(
            {
                "success": False,
                "message": "Administrator accounts cannot be modified here."
            }
        ), 403

    action = str(
        action
    ).lower()

    status_map = {
        "approve": "ACTIVE",
        "reject": "REJECTED",
        "disable": "REJECTED",
    }

    if action not in status_map:

        return jsonify(
            {
                "success": False,
                "message": "Invalid action."
            }
        ), 400

    new_status = status_map[action]

    try:

        db.collection(
            USERS_COLLECTION
        ).document(
            user_id
        ).update(
            {
                "status": new_status,
                "updated_at": utc_now(),
            }
        )

        current_admin = session.get(
            "user"
        )

        admin_login_id = (
            current_admin.get(
                "login_id"
            )
            if current_admin
            else "ADMIN"
        )

        save_history(
            admin_login_id,
            "-",
            action.upper(),
            "User Management",
            target_user.get(
                "login_id",
                "-"
            )
            + " → "
            + new_status,
        )

        return redirect(
            url_for("admin_users")
        )

    except Exception as exc:

        print(
            "ADMIN USER UPDATE ERROR:",
            repr(exc),
            flush=True
        )

        flash(
            "Unable to update user.",
            "error"
        )

        return redirect(
            url_for("admin_users")
        )


# ============================================================
# STARTUP
# ============================================================

create_admin_if_needed()

start_mqtt()


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "5000"
            )
        ),
        debug=False,
    )
