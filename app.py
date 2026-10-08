import os
import json
import re
import threading
import time
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

import firebase_admin
from firebase_admin import credentials, firestore

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


# ============================================================
# FIREBASE / FIRESTORE
# ============================================================

FIREBASE_SERVICE_ACCOUNT = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT",
    "",
).strip()

if not FIREBASE_SERVICE_ACCOUNT:
    raise RuntimeError(
        "FIREBASE_SERVICE_ACCOUNT environment variable is missing."
    )

try:
    service_account_info = json.loads(
        FIREBASE_SERVICE_ACCOUNT
    )

except json.JSONDecodeError as exc:

    raise RuntimeError(
        "FIREBASE_SERVICE_ACCOUNT is not valid JSON."
    ) from exc


if not firebase_admin._apps:

    firebase_admin.initialize_app(
        credentials.Certificate(
            service_account_info
        )
    )


firestore_db = firestore.client()


USERS_COLLECTION = "users"
HISTORY_COLLECTION = "history"
SYSTEM_COLLECTION = "system"
STATUS_DOCUMENT = "status"


# ============================================================
# TIME / HELPERS
# ============================================================

IST = timezone(
    timedelta(
        hours=5,
        minutes=30,
    )
)


ESP32_OFFLINE_AFTER_SECONDS = int(
    os.environ.get(
        "ESP32_OFFLINE_AFTER_SECONDS",
        "20",
    )
)


def utc_now():

    return datetime.now(
        timezone.utc
    )


def as_utc_datetime(value):

    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):

        if value.tzinfo is None:

            return value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone(
            timezone.utc
        )

    if isinstance(
        value,
        str,
    ):

        text = value.strip()

        try:

            parsed = datetime.fromisoformat(
                text.replace(
                    "Z",
                    "+00:00",
                )
            )

            if parsed.tzinfo is None:

                parsed = parsed.replace(
                    tzinfo=timezone.utc
                )

            return parsed.astimezone(
                timezone.utc
            )

        except ValueError:

            return None

    return None


def format_ist(value):

    dt = as_utc_datetime(
        value
    )

    if not dt:

        return "—"

    return dt.astimezone(
        IST
    ).strftime(
        "%d-%m-%Y %I:%M:%S %p"
    )


def normalize_login_identifier(
    value,
):

    value = (
        value or ""
    ).strip()

    if "@" in value:

        return value.lower()

    value = re.sub(
        r"[\s().-]",
        "",
        value,
    )

    if value.startswith(
        "00"
    ):

        value = (
            "+"
            + value[2:]
        )

    if value.startswith(
        "+"
    ):

        return (
            "+"
            + re.sub(
                r"\D",
                "",
                value[1:],
            )
        )

    return re.sub(
        r"\D",
        "",
        value,
    )


def is_email(value):

    return bool(
        re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+",
            value or "",
        )
    )


def is_phone(value):

    digits = re.sub(
        r"\D",
        "",
        value or "",
    )

    return (
        10
        <= len(digits)
        <= 15
    )


def valid_login_identifier(
    value,
):

    return (
        is_email(value)
        or is_phone(value)
    )


def user_display_value(
    user,
):

    if not user:

        return "Unknown"

    return user.get(
        "login_id",
        "Unknown",
    )


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
# LOCAL RUNTIME CACHE
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


mqtt_client = None

mqtt_connected = False

mqtt_last_error = ""

mqtt_lock = threading.Lock()


# ============================================================
# FIRESTORE HELPERS
# ============================================================

def status_ref():

    return (
        firestore_db
        .collection(
            SYSTEM_COLLECTION
        )
        .document(
            STATUS_DOCUMENT
        )
    )


def user_doc_to_dict(
    doc,
):

    data = (
        doc.to_dict()
        or {}
    )

    data["id"] = doc.id

    data[
        "created_at_display"
    ] = format_ist(
        data.get(
            "created_at"
        )
    )

    return data


def history_doc_to_dict(
    doc,
):

    data = (
        doc.to_dict()
        or {}
    )

    data["id"] = doc.id

    data[
        "created_at_display"
    ] = format_ist(
        data.get(
            "created_at"
        )
    )

    return data


def find_user_by_login(
    login_id,
):

    normalized = (
        normalize_login_identifier(
            login_id
        )
    )

    if not normalized:

        return None

    query = (
        firestore_db
        .collection(
            USERS_COLLECTION
        )
        .where(
            "login_id",
            "==",
            normalized,
        )
        .limit(1)
        .stream()
    )

    for doc in query:

        return user_doc_to_dict(
            doc
        )

    return None


def get_user_by_id(
    user_id,
):

    if not user_id:

        return None

    doc = (
        firestore_db
        .collection(
            USERS_COLLECTION
        )
        .document(
            str(user_id)
        )
        .get()
    )

    if not doc.exists:

        return None

    return user_doc_to_dict(
        doc
    )


def init_db():

    status_document = (
        status_ref().get()
    )

    if not status_document.exists:

        status_ref().set(
            {

                "light": "OFF",

                "fan": "OFF",

                "geyser": "OFF",

                "temperature": "--",

                "humidity": "--",

                "gas": "--",

                "gas_raw": 0,

                "updated_at": None,
            }
        )

    admin_login = (
        normalize_login_identifier(
            ADMIN_USERNAME
        )
    )

    if not valid_login_identifier(
        admin_login
    ):

        print(
            "WARNING: ADMIN_USERNAME is not "
            "an email or phone number. "
            "Update it in Render before "
            "relying on email/phone login.",
            flush=True,
        )

    existing_admin = (
        find_user_by_login(
            admin_login
        )
    )

    if not existing_admin:

        (
            firestore_db
            .collection(
                USERS_COLLECTION
            )
            .add(
                {

                    "login_id": admin_login,

                    "password_hash":
                        generate_password_hash(
                            ADMIN_PASSWORD
                        ),

                    "role": "ADMIN",

                    "status": "ACTIVE",

                    "created_at":
                        firestore.SERVER_TIMESTAMP,
                }
            )
        )

        print(
            "Firebase admin account created.",
            flush=True,
        )


def read_latest_status():

    doc = (
        status_ref().get()
    )

    data = (
        doc.to_dict()
        if doc.exists
        else {}
    )

    devices = {

        "light":
            str(
                data.get(
                    "light",
                    "OFF",
                )
            ).upper(),

        "fan":
            str(
                data.get(
                    "fan",
                    "OFF",
                )
            ).upper(),

        "geyser":
            str(
                data.get(
                    "geyser",
                    "OFF",
                )
            ).upper(),
    }

    sensors = {

        "temperature":
            data.get(
                "temperature",
                "--",
            ),

        "humidity":
            data.get(
                "humidity",
                "--",
            ),

        "gas":
            data.get(
                "gas",
                "--",
            ),

        "gas_raw":
            data.get(
                "gas_raw",
                0,
            ),
    }

    updated_at = data.get(
        "updated_at"
    )

    return (
        devices,
        sensors,
        updated_at,
    )


def write_latest_status(
    devices,
    sensors,
):

    status_ref().set(
        {

            "light":
                str(
                    devices.get(
                        "light",
                        "OFF",
                    )
                ).upper(),

            "fan":
                str(
                    devices.get(
                        "fan",
                        "OFF",
                    )
                ).upper(),

            "geyser":
                str(
                    devices.get(
                        "geyser",
                        "OFF",
                    )
                ).upper(),

            "temperature":
                sensors.get(
                    "temperature",
                    "--",
                ),

            "humidity":
                sensors.get(
                    "humidity",
                    "--",
                ),

            "gas":
                sensors.get(
                    "gas",
                    "--",
                ),

            "gas_raw":
                int(
                    sensors.get(
                        "gas_raw",
                        0,
                    )
                    or 0
                ),

            "updated_at":
                firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )


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

    status_ref().set(
        {
            device:
                action.upper()
        },
        merge=True,
    )


def esp32_status_info(
    updated_at,
):

    last_seen_dt = (
        as_utc_datetime(
            updated_at
        )
    )

    if not last_seen_dt:

        return {

            "online": False,

            "last_seen": None,

            "last_seen_display": "Never",

            "age_seconds": None,
        }

    age = max(
        0,
        int(
            (
                utc_now()
                - last_seen_dt
            ).total_seconds()
        ),
    )

    return {

        "online":
            age
            <= ESP32_OFFLINE_AFTER_SECONDS,

        "last_seen":
            last_seen_dt.isoformat(),

        "last_seen_display":
            format_ist(
                last_seen_dt
            ),

        "age_seconds":
            age,
    }


# ============================================================
# CSRF
# ============================================================

def get_csrf_token():

    if (
        "csrf_token"
        not in session
    ):

        session[
            "csrf_token"
        ] = secrets.token_urlsafe(
            32
        )

    return session[
        "csrf_token"
    ]


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

                "error":
                    "Invalid security token. "
                    "Please refresh the page.",
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

    (
        firestore_db
        .collection(
            HISTORY_COLLECTION
        )
        .add(
            {

                "created_at":
                    firestore.SERVER_TIMESTAMP,

                "username":
                    username,

                "device":
                    device,

                "action":
                    action,

                "result":
                    result,

                "event_type":
                    event_type,
            }
        )
    )


def get_history(
    filters=None,
    limit=500,
):

    filters = (
        filters
        or {}
    )

    docs = (
        firestore_db
        .collection(
            HISTORY_COLLECTION
        )
        .order_by(
            "created_at",
            direction=
                firestore.Query.DESCENDING,
        )
        .limit(
            limit
        )
        .stream()
    )

    rows = [

        history_doc_to_dict(
            doc
        )

        for doc in docs
    ]

    def matches(row):

        for key in (
            "username",
            "device",
            "action",
            "event_type",
        ):

            wanted = (
                filters.get(
                    key
                )
                or ""
            ).strip()

            if (
                wanted
                and str(
                    row.get(
                        key,
                        "",
                    )
                ).lower()
                != wanted.lower()
            ):

                return False

        return True

    return [
        row
        for row in rows
        if matches(row)
    ]


# ============================================================
# AUTHENTICATION
# ============================================================

def current_user():

    return get_user_by_id(
        session.get(
            "user_id"
        )
    )


def login_required(
    view
):

    @wraps(view)
    def wrapped(
        *args,
        **kwargs
    ):

        user = current_user()

        if not user:

            return redirect(
                url_for(
                    "login"
                )
            )

        if (
            user.get(
                "status"
            )
            != "ACTIVE"
        ):

            session.clear()

            flash(
                "Your account is not approved yet.",
                "error",
            )

            return redirect(
                url_for(
                    "login"
                )
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


def admin_required(
    view
):

    @wraps(view)
    def wrapped(
        *args,
        **kwargs
    ):

        user = current_user()

        if not user:

            return redirect(
                url_for(
                    "login"
                )
            )

        if (
            user.get(
                "status"
            )
            != "ACTIVE"
            or user.get(
                "role"
            )
            != "ADMIN"
        ):

            flash(
                "Administrator access required.",
                "error",
            )

            return redirect(
                url_for(
                    "dashboard"
                )
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


@app.context_processor
def inject_template_values():

    return {

        "csrf_token":
            get_csrf_token(),

        "current_user":
            current_user(),
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

                result, _ = (
                    client.subscribe(
                        topic
                    )
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

        payload = (
            msg.payload.decode(
                "utf-8"
            )
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

        if (
            msg.topic
            != TOPIC_STATUS
        ):

            return

        data = json.loads(
            payload
        )

        devices = data.get(
            "devices",
            {}
        )

        sensors = data.get(
            "sensors",
            {}
        )

        (
            current_devices,
            current_sensors,
            _,
        ) = read_latest_status()

        if "light" in devices:

            current_devices[
                "light"
            ] = str(
                devices[
                    "light"
                ]
            ).upper()

        if "fan" in devices:

            current_devices[
                "fan"
            ] = str(
                devices[
                    "fan"
                ]
            ).upper()

        if "geyser" in devices:

            current_devices[
                "geyser"
            ] = str(
                devices[
                    "geyser"
                ]
            ).upper()

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

        connected = (
            client.is_connected()
        )

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

        client = (
            create_mqtt_client()
        )

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

        deadline = (
            time.time()
            + 5
        )

        while (
            time.time()
            < deadline
        ):

            if client.is_connected():

                mqtt_connected = True

                mqtt_last_error = ""

                print(
                    "MQTT CONNECTED SUCCESSFULLY.",
                    flush=True,
                )

                return

            time.sleep(
                0.1
            )

        mqtt_connected = (
            client.is_connected()
        )

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

        "light":
            TOPIC_LIGHT,

        "fan":
            TOPIC_FAN,

        "geyser":
            TOPIC_GEYSER,
    }

    topic = topics.get(
        device
    )

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

        command_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        command_client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD,
        )

        command_client.tls_set()

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

        command_client.loop_start()

        deadline = (
            time.time()
            + 5
        )

        while (
            time.time()
            < deadline
        ):

            if command_client.is_connected():

                break

            time.sleep(
                0.05
            )

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

        result = (
            command_client.publish(
                topic,
                payload=payload,
                qos=0,
                retain=True,
            )
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

        if (
            result.rc
            != mqtt.MQTT_ERR_SUCCESS
        ):

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

        time.sleep(
            0.5
        )

        try:

            published = (
                result.is_published()
            )

        except Exception:

            published = False

        print(
            "MQTT COMMAND IS PUBLISHED:",
            published,
            flush=True,
        )

        command_client.loop_stop()

        print(
            "MQTT COMMAND: network loop stopped.",
            flush=True,
        )

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
            url_for(
                "dashboard"
            )
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

        login_id = normalize_login_identifier(
            request.form.get(
                "login_id",
                request.form.get(
                    "username",
                    "",
                ),
            )
        )

        password = request.form.get(
            "password",
            "",
        )

        confirm_password = request.form.get(
            "confirm_password",
            "",
        )

        if (
            not login_id
            or not password
        ):

            flash(
                "Email/phone number and password are required.",
                "error",
            )

            return redirect(
                url_for(
                    "signup"
                )
            )

        if not valid_login_identifier(
            login_id
        ):

            flash(
                "Enter a valid email address or phone number.",
                "error",
            )

            return redirect(
                url_for(
                    "signup"
                )
            )

        if len(password) < 8:

            flash(
                "Password must contain at least 8 characters.",
                "error",
            )

            return redirect(
                url_for(
                    "signup"
                )
            )

        if (
            password
            != confirm_password
        ):

            flash(
                "Passwords do not match.",
                "error",
            )

            return redirect(
                url_for(
                    "signup"
                )
            )

        if find_user_by_login(
            login_id
        ):

            flash(
                "That email/phone number is already registered.",
                "error",
            )

            return redirect(
                url_for(
                    "signup"
                )
            )

        (
            firestore_db
            .collection(
                USERS_COLLECTION
            )
            .add(
                {

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
                }
            )
        )

        return render_template(
            "signup_success.html",
            username=login_id,
        )

    return render_template(
        "signup.html"
    )


# ============================================================
# SIGN IN
# ============================================================

def signin_handler():

    if request.method == "POST":

        login_id = normalize_login_identifier(
            request.form.get(
                "login_id",
                request.form.get(
                    "username",
                    "",
                ),
            )
        )

        password = request.form.get(
            "password",
            "",
        )

        user = find_user_by_login(
            login_id
        )

        if (
            not user
            or not check_password_hash(
                user.get(
                    "password_hash",
                    "",
                ),
                password,
            )
        ):

            flash(
                "Invalid email/phone number or password.",
                "error",
            )

            return redirect(
                url_for(
                    "login"
                )
            )

        if (
            user.get(
                "status"
            )
            != "ACTIVE"
        ):

            flash(
                "Your account is waiting for administrator approval.",
                "error",
            )

            return redirect(
                url_for(
                    "login"
                )
            )

        session.clear()

        session[
            "user_id"
        ] = user[
            "id"
        ]

        session[
            "login_id"
        ] = user[
            "login_id"
        ]

        session[
            "role"
        ] = user[
            "role"
        ]

        get_csrf_token()

        return redirect(
            url_for(
                "dashboard"
            )
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
        "POST"
    ],
)
def logout():

    session.clear()

    return redirect(
        url_for(
            "home"
        )
    )


# ============================================================
# DASHBOARD - VIEW ONLY
# ============================================================

@app.route(
    "/dashboard"
)
@login_required
def dashboard():

    ensure_mqtt_connection()

    devices, sensors, updated_at = (
        read_latest_status()
    )

    esp32 = esp32_status_info(
        updated_at
    )

    return render_template(
        "index.html",
        device_state=devices,
        sensor_state=sensors,
        mqtt_connected=mqtt_connected,
        esp32=esp32,
    )


# ============================================================
# DEVICES - CONTROL PAGE
# ============================================================

@app.route(
    "/devices"
)
@login_required
def devices():

    ensure_mqtt_connection()

    device_values, sensors, updated_at = (
        read_latest_status()
    )

    esp32 = esp32_status_info(
        updated_at
    )

    return render_template(
        "devices.html",
        device_state=device_values,
        sensor_state=sensors,
        mqtt_connected=mqtt_connected,
        esp32=esp32,
    )


# ============================================================
# HISTORY
# ============================================================

@app.route(
    "/history"
)
@login_required
def history():

    filters = {

        "username":
            request.args.get(
                "user",
                "",
            ),

        "device":
            request.args.get(
                "device",
                "",
            ),

        "action":
            request.args.get(
                "action",
                "",
            ),

        "event_type":
            request.args.get(
                "event",
                "",
            ),
    }

    rows = get_history(
        filters=filters,
        limit=500,
    )

    all_rows = get_history(
        filters={},
        limit=500,
    )

    users = sorted(
        {
            row.get(
                "username",
                "",
            )

            for row in all_rows

            if row.get(
                "username"
            )
        }
    )

    devices_filter = sorted(
        {
            row.get(
                "device",
                "",
            )

            for row in all_rows

            if row.get(
                "device"
            )
        }
    )

    actions = sorted(
        {
            row.get(
                "action",
                "",
            )

            for row in all_rows

            if row.get(
                "action"
            )
        }
    )

    events = sorted(
        {
            row.get(
                "event_type",
                "",
            )

            for row in all_rows

            if row.get(
                "event_type"
            )
        }
    )

    return render_template(
        "history.html",
        history=rows,
        history_filters=filters,
        history_users=users,
        history_devices=devices_filter,
        history_actions=actions,
        history_events=events,
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

    docs = (
        firestore_db
        .collection(
            USERS_COLLECTION
        )
        .order_by(
            "created_at",
            direction=
                firestore.Query.DESCENDING,
        )
        .stream()
    )

    users = [

        user_doc_to_dict(
            doc
        )

        for doc in docs
    ]

    return render_template(
        "admin_users.html",
        users=users,
    )


@app.route(
    "/admin/user/<user_id>/<action>",
    methods=[
        "POST"
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
            url_for(
                "admin_users"
            )
        )

    ref = (
        firestore_db
        .collection(
            USERS_COLLECTION
        )
        .document(
            str(user_id)
        )
    )

    doc = ref.get()

    if not doc.exists:

        flash(
            "User not found.",
            "error",
        )

        return redirect(
            url_for(
                "admin_users"
            )
        )

    user = (
        doc.to_dict()
        or {}
    )

    if (
        user.get(
            "role"
        )
        == "ADMIN"
    ):

        flash(
            "Administrator accounts cannot be changed here.",
            "error",
        )

        return redirect(
            url_for(
                "admin_users"
            )
        )

    new_status = {

        "approve":
            "ACTIVE",

        "reject":
            "REJECTED",

        "disable":
            "DISABLED",

    }[action]

    ref.update(
        {
            "status":
                new_status
        }
    )

    add_history(
        user_display_value(
            current_user()
        ),
        "USER",
        action.upper(),
        "Success",
        "User Management",
    )

    flash(
        (
            f"User "
            f"{user.get('login_id', 'Unknown')} "
            f"is now {new_status}."
        ),
        "success",
    )

    return redirect(
        url_for(
            "admin_users"
        )
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

    devices, sensors, updated_at = (
        read_latest_status()
    )

    esp32 = esp32_status_info(
        updated_at
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

            "mqtt_connected":
                connected,

            "mqtt_error":
                mqtt_last_error,

            "devices":
                devices,

            "sensors":
                sensors,

            "esp32":
                esp32,
        }
    )


# ============================================================
# DEVICE CONTROL API
# ============================================================

@app.route(
    "/api/device/<device>/<action>",
    methods=[
        "POST"
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

                "success":
                    False,

                "error":
                    "Invalid device.",
            }
        ), 400

    if action not in allowed_actions:

        return jsonify(
            {

                "success":
                    False,

                "error":
                    "Invalid action.",
            }
        ), 400

    success = (
        publish_device_command(
            device,
            action,
        )
    )

    username = session.get(
        "login_id",
        "Unknown",
    )

    add_history(
        username,
        device.upper(),
        action,
        (
            "Sent"
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

            "success":
                success,

            "device":
                device,

            "action":
                action,

            "mqtt_connected":
                ensure_mqtt_connection(),

            "mqtt_error":
                mqtt_last_error,
        }
    )


# ============================================================
# MQTT DEBUG API
# ============================================================

@app.route(
    "/api/mqtt"
)
@login_required
def api_mqtt():

    connected = (
        ensure_mqtt_connection()
    )

    return jsonify(
        {

            "connected":
                connected,

            "mqtt_connected":
                connected,

            "error":
                mqtt_last_error,

            "broker":
                MQTT_BROKER,

            "port":
                MQTT_PORT,

            "topics": {

                "light":
                    TOPIC_LIGHT,

                "fan":
                    TOPIC_FAN,

                "geyser":
                    TOPIC_GEYSER,

                "status":
                    TOPIC_STATUS,
            },
        }
    )


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health"
)
def health():

    connected = (
        ensure_mqtt_connection()
    )

    return jsonify(
        {

            "status":
                "ok",

            "mqtt_connected":
                connected,
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
