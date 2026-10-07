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
        (
            "Success"
            if success
            else "Failed"
        ),
        "Device Control",
    )

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
        "MQTT initialization error:",
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
