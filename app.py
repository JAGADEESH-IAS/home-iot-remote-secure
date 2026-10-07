import os
import json
import uuid
import time
import threading

from flask import Flask, jsonify, render_template
import paho.mqtt.client as mqtt


app = Flask(__name__)


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
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():

    ensure_status_client()

    return render_template(
        "index.html"
    )


# ============================================================
# STATUS API
# ============================================================

@app.route("/api/status")
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

    success = publish_command(
        topics[device],
        action
    )

    if not success:

        return jsonify({
            "success": False,
            "error": (
                last_error or
                "MQTT command failed"
            )
        }), 503

    return jsonify({

        "success": True,

        "device": device,

        "action": action,

        "topic": topics[device],

        "qos": 0,

        "message": "Command sent successfully"
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
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
