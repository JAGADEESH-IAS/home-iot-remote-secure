import os
import json
import sqlite3
import threading
import time
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

app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

DATABASE = "home_iot.db"

MQTT_BROKER = os.environ.get(
    "MQTT_BROKER",
    "bravetawny-af88996b.a02.usw2.aws.hivemq.cloud",
)

MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")


# ============================================================
# MQTT TOPICS
# ============================================================

TOPIC_LIGHT = "home/light"
TOPIC_FAN = "home/fan"
TOPIC_GEYSER = "home/geyser"
TOPIC_STATUS = "home/status"


# ============================================================
# CURRENT DEVICE / SENSOR STATUS
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

mqtt_connected = False
mqtt_client = None
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
            result TEXT NOT NULL
        )
        """
    )

    existing_admin = cur.execute(
        "SELECT id FROM users WHERE username = ?",
        (ADMIN_USERNAME,),
    ).fetchone()

    if not existing_admin:
        cur.execute(
            """
            INSERT INTO users
            (username, password_hash, role, status, created_at)
            VALUES (?, ?, 'ADMIN', 'ACTIVE', ?)
            """,
            (
                ADMIN_USERNAME,
                generate_password_hash(ADMIN_PASSWORD),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    conn.commit()
    conn.close()


# ============================================================
# HISTORY
# ============================================================

def add_history(username, device, action, result):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO history
        (created_at, username, device, action, result)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            username,
            device,
            action,
            result,
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# AUTHENTICATION HELPERS
# ============================================================

def current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()

    conn.close()

    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()

        if not user:
            return redirect(url_for("signin"))

        if user["status"] != "ACTIVE":
            session.clear()
            flash("Your account is not approved yet.", "error")
            return redirect(url_for("signin"))

        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()

        if not user:
            return redirect(url_for("signin"))

        if user["status"] != "ACTIVE" or user["role"] != "ADMIN":
            flash("Administrator access required.", "error")
            return redirect(url_for("dashboard"))

        return view(*args, **kwargs)

    return wrapped


# ============================================================
# MQTT
# ============================================================

def mqtt_on_connect(client, userdata, flags, reason_code, properties=None):
    global mqtt_connected

    if reason_code == 0:
        mqtt_connected = True

        client.subscribe(TOPIC_LIGHT)
        client.subscribe(TOPIC_FAN)
        client.subscribe(TOPIC_GEYSER)
        client.subscribe(TOPIC_STATUS)

        print("MQTT connected successfully")
    else:
        mqtt_connected = False
        print("MQTT connection failed:", reason_code)


def mqtt_on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    global mqtt_connected
    mqtt_connected = False
    print("MQTT disconnected:", reason_code)


def mqtt_on_message(client, userdata, msg):
    global device_state, sensor_state

    try:
        payload = msg.payload.decode("utf-8")

        if msg.topic == TOPIC_STATUS:
            data = json.loads(payload)

            with mqtt_lock:
                if "light" in data:
                    device_state["light"] = str(data["light"]).upper()

                if "fan" in data:
                    device_state["fan"] = str(data["fan"]).upper()

                if "geyser" in data:
                    device_state["geyser"] = str(data["geyser"]).upper()

                if "temperature" in data:
                    sensor_state["temperature"] = data["temperature"]

                if "humidity" in data:
                    sensor_state["humidity"] = data["humidity"]

                if "gas" in data:
                    sensor_state["gas"] = data["gas"]

    except Exception as exc:
        print("MQTT message error:", exc)


def start_mqtt():
    global mqtt_client

    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"home-iot-flask-{int(time.time())}",
        )

        client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD,
        )

        client.tls_set()

        client.on_connect = mqtt_on_connect
        client.on_disconnect = mqtt_on_disconnect
        client.on_message = mqtt_on_message

        mqtt_client = client

        client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60,
        )

        client.loop_start()

        print("MQTT background client started")

    except Exception as exc:
        print("MQTT startup error:", exc)


def publish_device_command(device, action):
    global mqtt_client

    topics = {
        "light": TOPIC_LIGHT,
        "fan": TOPIC_FAN,
        "geyser": TOPIC_GEYSER,
    }

    topic = topics.get(device)

    if not topic:
        return False

    if not mqtt_client or not mqtt_connected:
        return False

    try:
        result = mqtt_client.publish(
            topic,
            action.upper(),
            qos=0,
        )

        return result.rc == mqtt.MQTT_ERR_SUCCESS

    except Exception as exc:
        print("MQTT publish error:", exc)
        return False


# ============================================================
# PUBLIC / HOME
# ============================================================

@app.route("/")
def home():
    if current_user():
        return redirect(url_for("dashboard"))

    return render_template("home.html")


# ============================================================
# SIGN UP
# ============================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return redirect(url_for("signup"))

        conn = get_db()

        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if existing:
            conn.close()
            flash("Username already exists.", "error")
            return redirect(url_for("signup"))

        conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, status, created_at)
            VALUES (?, ?, 'USER', 'PENDING', ?)
            """,
            (
                username,
                generate_password_hash(password),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

        conn.commit()
        conn.close()

        flash(
            "Registration successful. Wait for administrator approval.",
            "success",
        )

        return redirect(url_for("signin"))

    return render_template("signup.html")


# ============================================================
# SIGN IN
# ============================================================

@app.route("/signin", methods=["GET", "POST"])
def signin():
    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()

        user = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        conn.close()

        if not user:
            flash("Invalid username or password.", "error")
            return redirect(url_for("signin"))

        if not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "error")
            return redirect(url_for("signin"))

        if user["status"] != "ACTIVE":
            flash(
                "Your account is waiting for administrator approval.",
                "error",
            )
            return redirect(url_for("signin"))

        session.clear()

        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]

        return redirect(url_for("dashboard"))

    return render_template("signin.html")


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template(
        "index.html",
        user=current_user(),
        device_state=device_state,
        sensor_state=sensor_state,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# DEVICES PAGE
# ============================================================

@app.route("/devices")
@login_required
def devices():
    return render_template(
        "devices.html",
        user=current_user(),
        device_state=device_state,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# MONITORING PAGE
# ============================================================

@app.route("/monitoring")
@login_required
def monitoring():
    return render_template(
        "monitoring.html",
        user=current_user(),
        sensor_state=sensor_state,
        device_state=device_state,
        mqtt_connected=mqtt_connected,
    )


# ============================================================
# HISTORY PAGE
# ============================================================

@app.route("/history")
@login_required
def history():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM history
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()

    conn.close()

    return render_template(
        "history.html",
        user=current_user(),
        history=rows,
    )


# ============================================================
# SETTINGS
# ============================================================

@app.route("/settings")
@login_required
def settings():
    return render_template(
        "settings.html",
        user=current_user(),
    )


# ============================================================
# ADMIN USER MANAGEMENT
# ============================================================

@app.route("/admin")
@admin_required
def admin():
    conn = get_db()

    users = conn.execute(
        """
        SELECT id, username, role, status, created_at
        FROM users
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return render_template(
        "admin.html",
        user=current_user(),
        users=users,
    )


@app.route("/admin/approve/<int:user_id>", methods=["POST"])
@admin_required
def approve_user(user_id):
    conn = get_db()

    conn.execute(
        """
        UPDATE users
        SET status = 'ACTIVE'
        WHERE id = ?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    flash("User approved successfully.", "success")

    return redirect(url_for("admin"))


@app.route("/admin/reject/<int:user_id>", methods=["POST"])
@admin_required
def reject_user(user_id):
    conn = get_db()

    conn.execute(
        """
        UPDATE users
        SET status = 'REJECTED'
        WHERE id = ?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    flash("User rejected.", "success")

    return redirect(url_for("admin"))


# ============================================================
# STATUS API
# ============================================================

@app.route("/api/status")
@login_required
def api_status():

    with mqtt_lock:
        return jsonify(
            {
                "mqtt_connected": mqtt_connected,
                "devices": dict(device_state),
                "sensors": dict(sensor_state),
            }
        )


# ============================================================
# DEVICE CONTROL API
# ============================================================

@app.route("/api/device/<device>/<action>", methods=["POST"])
@login_required
def api_device(device, action):

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
                "message": "Invalid device.",
            }
        ), 400

    if action not in allowed_actions:
        return jsonify(
            {
                "success": False,
                "message": "Invalid action.",
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
    )

    if success:
        with mqtt_lock:
            device_state[device] = action

    return jsonify(
        {
            "success": success,
            "device": device,
            "action": action,
            "mqtt_connected": mqtt_connected,
        }
    )


# ============================================================
# STARTUP
# ============================================================

init_db()

try:
    start_mqtt()
except Exception as exc:
    print("MQTT initialization error:", exc)


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
