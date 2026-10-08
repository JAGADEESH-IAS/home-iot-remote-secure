import os
import json
import time
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

import paho.mqtt.client as mqtt

import firebase_admin
from firebase_admin import credentials, firestore

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)


# ============================================================
# APP CONFIGURATION
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    secrets.token_hex(32)
)


ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    ""
).strip()

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    ""
)


MQTT_BROKER = os.environ.get(
    "MQTT_BROKER",
    ""
).strip()

MQTT_PORT = int(
    os.environ.get(
        "MQTT_PORT",
        "8883"
    )
)

MQTT_USERNAME = os.environ.get(
    "MQTT_USERNAME",
    ""
)

MQTT_PASSWORD = os.environ.get(
    "MQTT_PASSWORD",
    ""
)


ESP32_OFFLINE_AFTER_SECONDS = int(
    os.environ.get(
        "ESP32_OFFLINE_AFTER_SECONDS",
        "20"
    )
)


# ============================================================
# FIREBASE
# ============================================================

FIREBASE_SERVICE_ACCOUNT = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT",
    ""
).strip()


if not FIREBASE_SERVICE_ACCOUNT:
    raise RuntimeError(
        "FIREBASE_SERVICE_ACCOUNT environment variable is required."
    )


try:

    firebase_info = json.loads(
        FIREBASE_SERVICE_ACCOUNT
    )

except json.JSONDecodeError as exc:

    raise RuntimeError(
        "FIREBASE_SERVICE_ACCOUNT is not valid JSON."
    ) from exc


if not firebase_admin._apps:

    firebase_admin.initialize_app(
        credentials.Certificate(
            firebase_info
        )
    )


firestore_db = firestore.client()


USERS_COLLECTION = "users"
HISTORY_COLLECTION = "history"
SYSTEM_COLLECTION = "system"


# ============================================================
# DEVICE CONFIGURATION
# ============================================================

DEVICE_TOPICS = {

    "light": "home/light",

    "fan": "home/fan",

    "geyser": "home/geyser",

}


DEVICE_NAMES = {

    "light": "Light",

    "fan": "Fan",

    "geyser": "Geyser",

}


# ============================================================
# MQTT STATE
# ============================================================

mqtt_client = None

mqtt_connected = False


latest_status = {

    "devices": {
        "light": "OFF",
        "fan": "OFF",
        "geyser": "OFF",
    },

    "sensors": {
        "temperature": None,
        "humidity": None,
        "gas": "--",
        "gas_raw": None,
    },

    "updated_at": None,

}


# ============================================================
# TIME HELPERS
# ============================================================

IST = timezone(
    timedelta(hours=5, minutes=30)
)


def utc_now():

    return datetime.now(
        timezone.utc
    )


def to_ist_string(value):

    if not value:
        return "—"

    try:

        if hasattr(value, "timestamp"):

            dt = value

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

        elif isinstance(value, datetime):

            dt = value

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

        else:

            return str(value)

        return dt.astimezone(
            IST
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    except Exception:

        return str(value)


# ============================================================
# PASSWORD HASHING
# ============================================================

from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)


# ============================================================
# FIRESTORE HELPERS
# ============================================================

def normalize_login_id(value):

    return (
        str(value or "")
        .strip()
        .lower()
    )


def get_user_by_login_id(login_id):

    login_id = normalize_login_id(
        login_id
    )

    if not login_id:
        return None

    query = (
        firestore_db
        .collection(USERS_COLLECTION)
        .where(
            "login_id",
            "==",
            login_id
        )
        .limit(1)
        .stream()
    )

    for document in query:

        data = document.to_dict() or {}

        data["id"] = document.id

        return data

    return None


def get_user_by_id(user_id):

    if not user_id:
        return None

    document = (
        firestore_db
        .collection(USERS_COLLECTION)
        .document(user_id)
        .get()
    )

    if not document.exists:
        return None

    data = document.to_dict() or {}

    data["id"] = document.id

    return data


def create_admin_if_needed():

    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return

    login_id = normalize_login_id(
        ADMIN_USERNAME
    )

    existing = get_user_by_login_id(
        login_id
    )

    if existing:
        return

    firestore_db.collection(
        USERS_COLLECTION
    ).add({

        "login_id": login_id,

        "password_hash":
            generate_password_hash(
                ADMIN_PASSWORD
            ),

        "role": "ADMIN",

        "status": "ACTIVE",

        "created_at":
            firestore.SERVER_TIMESTAMP,

    })


def get_all_users():

    documents = (
        firestore_db
        .collection(USERS_COLLECTION)
        .order_by(
            "created_at",
            direction=firestore.Query.DESCENDING
        )
        .stream()
    )

    users = []

    for document in documents:

        data = document.to_dict() or {}

        data["id"] = document.id

        data["created_at_display"] = (
            to_ist_string(
                data.get("created_at")
            )
        )

        users.append(data)

    return users


def add_history(
    login_id,
    device,
    action,
    result,
    event_type="Device Command"
):

    firestore_db.collection(
        HISTORY_COLLECTION
    ).add({

        "created_at":
            firestore.SERVER_TIMESTAMP,

        "login_id":
            login_id,

        "device":
            device,

        "action":
            action,

        "result":
            result,

        "event_type":
            event_type,

    })


def get_history():

    documents = (
        firestore_db
        .collection(HISTORY_COLLECTION)
        .order_by(
            "created_at",
            direction=firestore.Query.DESCENDING
        )
        .limit(500)
        .stream()
    )

    rows = []

    for document in documents:

        data = document.to_dict() or {}

        created_at =
            data.get("created_at")

        rows.append({

            "created_at":
                to_ist_string(
                    created_at
                ),

            "user":
                data.get(
                    "login_id",
                    "—"
                ),

            "device":
                data.get(
                    "device",
                    "—"
                ),

            "action":
                data.get(
                    "action",
                    "—"
                ),

            "event":
                data.get(
                    "event_type",
                    "Device Command"
                ),

            "result":
                data.get(
                    "result",
                    "SUCCESS"
                ),

        })

    return rows


# ============================================================
# LATEST STATUS
# ============================================================

def save_latest_status():

    firestore_db.collection(
        SYSTEM_COLLECTION
    ).document("status").set({

        "devices":
            latest_status["devices"],

        "sensors":
            latest_status["sensors"],

        "updated_at":
            firestore.SERVER_TIMESTAMP,

    })


def load_latest_status():

    global latest_status

    document = (
        firestore_db
        .collection(SYSTEM_COLLECTION)
        .document("status")
        .get()
    )

    if not document.exists:
        return

    data = document.to_dict() or {}

    if data.get("devices"):

        latest_status["devices"].update(
            data["devices"]
        )

    if data.get("sensors"):

        latest_status["sensors"].update(
            data["sensors"]
        )

    if data.get("updated_at"):

        latest_status["updated_at"] = (
            data["updated_at"]
        )


# ============================================================
# ESP32 STATUS
# ============================================================

def get_esp32_status():

    updated_at = (
        latest_status.get(
            "updated_at"
        )
    )

    if not updated_at:

        return {

            "online": False,

            "last_seen": None,

        }


    try:

        if hasattr(
            updated_at,
            "timestamp"
        ):

            last_timestamp = (
                updated_at.timestamp()
            )

        else:

            last_timestamp = (
                updated_at.timestamp()
            )

        age = (
            time.time()
            - last_timestamp
        )

        online = (
            age <=
            ESP32_OFFLINE_AFTER_SECONDS
        )

    except Exception:

        online = False


    return {

        "online": online,

        "last_seen":
            to_ist_string(
                updated_at
            ),

    }


# ============================================================
# MQTT CALLBACKS
# ============================================================

def mqtt_on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties=None
):

    global mqtt_connected

    mqtt_connected = (
        reason_code == 0
    )

    if mqtt_connected:

        client.subscribe(
            "home/status",
            qos=0
        )


def mqtt_on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None
):

    global mqtt_connected

    mqtt_connected = False


def mqtt_on_message(
    client,
    userdata,
    message
):

    global latest_status

    if message.topic != "home/status":
        return

    try:

        payload = json.loads(
            message.payload.decode(
                "utf-8"
            )
        )

    except Exception:

        return


    devices = payload.get(
        "devices",
        {}
    )

    sensors = payload.get(
        "sensors",
        {}
    )


    if devices:

        for device in DEVICE_TOPICS:

            if device in devices:

                latest_status[
                    "devices"
                ][device] = str(
                    devices[device]
                ).upper()


    if sensors:

        latest_status[
            "sensors"
        ].update({

            "temperature":
                sensors.get(
                    "temperature"
                ),

            "humidity":
                sensors.get(
                    "humidity"
                ),

            "gas":
                sensors.get(
                    "gas",
                    "--"
                ),

            "gas_raw":
                sensors.get(
                    "gas_raw"
                ),

        })


    latest_status[
        "updated_at"
    ] = utc_now()


    save_latest_status()


# ============================================================
# MQTT CONNECTION
# ============================================================

def start_mqtt():

    global mqtt_client
    global mqtt_connected

    if not MQTT_BROKER:
        return

    try:

        mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=(
                "home-iot-server-"
                + secrets.token_hex(5)
            )
        )

        mqtt_client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD
        )

        mqtt_client.tls_set()

        mqtt_client.on_connect = (
            mqtt_on_connect
        )

        mqtt_client.on_disconnect = (
            mqtt_on_disconnect
        )

        mqtt_client.on_message = (
            mqtt_on_message
        )

        mqtt_client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60
        )

        mqtt_client.loop_start()

    except Exception as exc:

        mqtt_connected = False

        print(
            "MQTT connection error:",
            exc
        )


# ============================================================
# DEDICATED MQTT COMMAND
# ============================================================

def publish_device_command(
    device,
    action
):

    if device not in DEVICE_TOPICS:

        return False


    action = str(
        action
    ).upper()


    if action not in (
        "ON",
        "OFF"
    ):

        return False


    command_client = None


    try:

        command_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=(
                "home-iot-command-"
                + secrets.token_hex(6)
            )
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
            DEVICE_TOPICS[device],

            payload=action,

            qos=0,

            retain=True
        )


        result.wait_for_publish(
            timeout=5
        )


        time.sleep(
            0.5
        )


        published = (
            result.rc == mqtt.MQTT_ERR_SUCCESS
            and result.is_published()
        )


        command_client.loop_stop()

        command_client.disconnect()


        return published


    except Exception as exc:

        print(
            "MQTT command error:",
            exc
        )

        try:

            if command_client:

                command_client.loop_stop()

                command_client.disconnect()

        except Exception:

            pass

        return False


# ============================================================
# AUTH DECORATORS
# ============================================================

def login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        user_id = session.get(
            "user_id"
        )

        if not user_id:

            return redirect(
                url_for("signin")
            )

        user = get_user_by_id(
            user_id
        )

        if not user:

            session.clear()

            return redirect(
                url_for("signin")
            )

        if user.get("status") != "ACTIVE":

            session.clear()

            flash(
                "Your account is not active.",
                "warning"
            )

            return redirect(
                url_for("signin")
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        user_id = session.get(
            "user_id"
        )

        if not user_id:

            return redirect(
                url_for("signin")
            )

        user = get_user_by_id(
            user_id
        )

        if not user:

            session.clear()

            return redirect(
                url_for("signin")
            )

        if user.get("role") != "ADMIN":

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

    return wrapper


# ============================================================
# CURRENT USER
# ============================================================

def get_current_user():

    user_id = session.get(
        "user_id"
    )

    if not user_id:
        return None

    return get_user_by_id(
        user_id
    )


@app.context_processor
def inject_current_user():

    return {

        "current_user":
            get_current_user(),

        "csrf_token":
            session.get(
                "csrf_token"
            ),

    }


# ============================================================
# CSRF
# ============================================================

@app.before_request
def ensure_csrf_token():

    if "csrf_token" not in session:

        session["csrf_token"] = secrets.token_urlsafe(
            32
        )


@app.before_request
def csrf_protection():

    if request.method != "POST":
        return

    if request.endpoint in (
        "signin",
        "signup"
    ):
        return

    expected = session.get(
        "csrf_token"
    )

    supplied = (
        request.form.get(
            "csrf_token"
        )
        or request.headers.get(
            "X-CSRF-Token"
        )
    )

    if not expected or supplied != expected:

        if request.path.startswith("/api/"):

            return jsonify({
                "error":
                    "Invalid CSRF token."
            }), 403

        flash(
            "Invalid security token. Please try again.",
            "error"
        )

        return redirect(
            request.referrer
            or url_for("home")
        )


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def home():

    if session.get("user_id"):

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "home.html"
    )


@app.route(
    "/signup",
    methods=["GET", "POST"]
)
def signup():

    if request.method == "POST":

        login_id = normalize_login_id(
            request.form.get(
                "login_id"
            )
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


        if get_user_by_login_id(
            login_id
        ):

            flash(
                "An account with this email or phone number already exists.",
                "error"
            )

            return render_template(
                "signup.html"
            )


        firestore_db.collection(
            USERS_COLLECTION
        ).add({

            "login_id":
                login_id,

            "password_hash":
                generate_password_hash(
                    password
                ),

            "role":
                "USER",

            "status":
                "PENDING",

            "created_at":
                firestore.SERVER_TIMESTAMP,

        })


        return render_template(
            "signup_success.html"
        )


    return render_template(
        "signup.html"
    )


@app.route(
    "/login",
    methods=["GET", "POST"]
)
@app.route(
    "/signin",
    methods=["GET", "POST"]
)
def signin():

    if request.method == "POST":

        login_id = normalize_login_id(
            request.form.get(
                "login_id"
            )
        )

        password = request.form.get(
            "password",
            ""
        )


        user = get_user_by_login_id(
            login_id
        )


        if (
            not user
            or not check_password_hash(
                user.get(
                    "password_hash",
                    ""
                ),
                password
            )
        ):

            flash(
                "Invalid email/phone number or password.",
                "error"
            )

            return render_template(
                "login.html"
            )


        if user.get(
            "status"
        ) != "ACTIVE":

            flash(
                "Your account is waiting for administrator approval.",
                "warning"
            )

            return render_template(
                "login.html"
            )


        session.clear()

        session["user_id"] = user["id"]

        session["csrf_token"] = secrets.token_urlsafe(
            32
        )


        return redirect(
            url_for("dashboard")
        )


    return render_template(
        "login.html"
    )


@app.route(
    "/logout",
    methods=["POST"]
)
@login_required
def logout():

    session.clear()

    return redirect(
        url_for("signin")
    )


@app.route("/dashboard")
@login_required
def dashboard():

    load_latest_status()

    return render_template(
        "index.html"
    )


@app.route("/devices")
@login_required
def devices():

    load_latest_status()

    return render_template(
        "devices.html"
    )


@app.route("/history")
@login_required
def history():

    rows = get_history()

    return render_template(
        "history.html",
        history=rows
    )


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

    users = get_all_users()

    return render_template(
        "admin_users.html",
        users=users
    )


@app.route(
    "/admin/user/<user_id>/<action>",
    methods=["POST"]
)
@admin_required
def admin_user_action(
    user_id,
    action
):

    if action not in (
        "approve",
        "reject"
    ):

        flash(
            "Invalid user action.",
            "error"
        )

        return redirect(
            url_for("admin_users")
        )


    user = get_user_by_id(
        user_id
    )


    if not user:

        flash(
            "User not found.",
            "error"
        )

        return redirect(
            url_for("admin_users")
        )


    if user.get(
        "role"
    ) == "ADMIN":

        flash(
            "Administrator accounts are protected.",
            "error"
        )

        return redirect(
            url_for("admin_users")
        )


    new_status = (
        "ACTIVE"
        if action == "approve"
        else "REJECTED"
    )


    firestore_db.collection(
        USERS_COLLECTION
    ).document(
        user_id
    ).update({

        "status":
            new_status

    })


    flash(
        (
            "User approved."
            if action == "approve"
            else "User disabled."
        ),
        "success"
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

    load_latest_status()

    esp32 = get_esp32_status()


    return jsonify({

        "devices":
            latest_status["devices"],

        "sensors":
            latest_status["sensors"],

        "mqtt_connected":
            mqtt_connected,

        "esp32":
            esp32,

    })


@app.route("/api/mqtt")
@login_required
def api_mqtt():

    return jsonify({

        "connected":
            mqtt_connected

    })


# ============================================================
# DEVICE CONTROL API
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

    device = str(
        device
    ).lower()

    action = str(
        action
    ).upper()


    if device not in DEVICE_TOPICS:

        return jsonify({
            "error":
                "Invalid device."
        }), 400


    if action not in (
        "ON",
        "OFF"
    ):

        return jsonify({
            "error":
                "Invalid action."
        }), 400


    user = get_current_user()


    if not user:

        return jsonify({
            "error":
                "Authentication required."
        }), 401


    success = publish_device_command(
        device,
        action
    )


    result = (
        "SUCCESS"
        if success
        else "FAILED"
    )


    add_history(

        login_id=
            user["login_id"],

        device=
            DEVICE_NAMES[device],

        action=
            action,

        result=
            result,

        event_type=
            "Device Command"

    )


    if not success:

        return jsonify({

            "error":
                "MQTT command could not be published.",

            "result":
                result,

        }), 500


    return jsonify({

        "success":
            True,

        "device":
            device,

        "action":
            action,

        "result":
            result,

        "message":
            f"{DEVICE_NAMES[device]} command sent successfully."

    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "mqtt_connected":
            mqtt_connected,

    })


# ============================================================
# STARTUP
# ============================================================

create_admin_if_needed()

load_latest_status()

start_mqtt()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "5000"
            )
        ),
        debug=False
    )
