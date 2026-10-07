const state = {
    devices: {
        light: "OFF",
        fan: "OFF",
        geyser: "OFF"
    },

    sensors: {
        temperature: null,
        humidity: null,
        gas: "UNKNOWN",
        gas_raw: 0
    },

    mqtt: {
        connected: false
    }
};


/* =========================================================
   REFRESH STATUS
   ========================================================= */

async function refreshStatus() {

    try {

        const response = await fetch(
            "/api/status?t=" + Date.now(),
            {
                method: "GET",
                cache: "no-store",
                headers: {
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache"
                }
            }
        );


        if (!response.ok) {

            throw new Error(
                "HTTP " + response.status
            );
        }


        const data =
            await response.json();


        console.log(
            "STATUS:",
            data
        );


        /* -------------------------
           DEVICES
           ------------------------- */

        if (data.devices) {

            state.devices = {
                ...state.devices,
                ...data.devices
            };
        }


        /* -------------------------
           SENSORS
           ------------------------- */

        if (data.sensors) {

            state.sensors = {
                ...state.sensors,
                ...data.sensors
            };
        }


        /* -------------------------
           MQTT
           ------------------------- */

        if (data.mqtt) {

            state.mqtt = {
                ...state.mqtt,
                ...data.mqtt
            };
        }


        updateDashboard();


    } catch (error) {

        console.error(
            "STATUS ERROR:",
            error
        );


        state.mqtt.connected = false;

        updateMQTTStatus();
    }
}


/* =========================================================
   DASHBOARD
   ========================================================= */

function updateDashboard() {

    updateMQTTStatus();

    updateTemperature();

    updateHumidity();

    updateGas();

    updateDevice(
        "light",
        state.devices.light
    );

    updateDevice(
        "fan",
        state.devices.fan
    );

    updateDevice(
        "geyser",
        state.devices.geyser
    );
}


/* =========================================================
   MQTT STATUS
   ========================================================= */

function updateMQTTStatus() {

    const connected =
        state.mqtt.connected === true;


    const badge =
        document.getElementById(
            "mqttBadge"
        );


    const text =
        document.getElementById(
            "mqttText"
        );


    if (badge) {

        badge.textContent =
            connected
                ? "● MQTT Connected"
                : "● MQTT Offline";


        badge.classList.remove(
            "offline"
        );

        badge.classList.remove(
            "online"
        );


        badge.classList.add(
            connected
                ? "online"
                : "offline"
        );
    }


    if (text) {

        text.textContent =
            connected
                ? "Connected"
                : "Offline";
    }


    console.log(
        "MQTT:",
        connected
            ? "CONNECTED"
            : "OFFLINE"
    );
}


/* =========================================================
   TEMPERATURE
   ========================================================= */

function updateTemperature() {

    const element =
        document.getElementById(
            "temperature"
        );


    if (!element) {
        return;
    }


    const value =
        Number(
            state.sensors.temperature
        );


    if (Number.isFinite(value)) {

        element.textContent =
            value.toFixed(1) +
            " °C";
    }
}


/* =========================================================
   HUMIDITY
   ========================================================= */

function updateHumidity() {

    const element =
        document.getElementById(
            "humidity"
        );


    if (!element) {
        return;
    }


    const value =
        Number(
            state.sensors.humidity
        );


    if (Number.isFinite(value)) {

        element.textContent =
            value.toFixed(1) +
            " %";
    }
}


/* =========================================================
   GAS
   ========================================================= */

function updateGas() {

    const element =
        document.getElementById(
            "gas"
        );


    if (element) {

        element.textContent =
            state.sensors.gas ||
            "UNKNOWN";
    }
}


/* =========================================================
   DEVICE STATUS
   ========================================================= */

function updateDevice(
    device,
    status
) {

    const value =
        String(
            status || "OFF"
        ).toUpperCase();


    const element =
        document.getElementById(
            device + "State"
        );


    if (!element) {
        return;
    }


    element.textContent =
        value;


    element.classList.remove(
        "on"
    );

    element.classList.remove(
        "off"
    );


    element.classList.add(
        value === "ON"
            ? "on"
            : "off"
    );
}


/* =========================================================
   DEVICE CONTROL
   ========================================================= */

async function controlDevice(
    device,
    action
) {

    console.log(
        "COMMAND:",
        device,
        action
    );


    try {

        const response =
            await fetch(
                "/api/device/" +
                encodeURIComponent(device) +
                "/" +
                encodeURIComponent(action),
                {
                    method: "POST",
                    cache: "no-store",
                    headers: {
                        "Cache-Control":
                            "no-cache",
                        "Pragma":
                            "no-cache"
                    }
                }
            );


        const result =
            await response.json();


        console.log(
            "COMMAND RESPONSE:",
            result
        );


        if (!response.ok) {

            throw new Error(
                result.error ||
                "Command failed"
            );
        }


        /*
         * Update UI immediately.
         */

        state.devices[device] =
            action.toUpperCase();


        updateDevice(
            device,
            state.devices[device]
        );


        /*
         * Ask Flask/ESP32 for the
         * real state after 1 second.
         */

        setTimeout(
            refreshStatus,
            1000
        );


    } catch (error) {

        console.error(
            "CONTROL ERROR:",
            error
        );


        /*
         * Restore actual state.
         */

        refreshStatus();
    }
}


/* =========================================================
   START
   ========================================================= */

refreshStatus();


/* =========================================================
   REFRESH EVERY 5 SECONDS
   ========================================================= */

setInterval(
    refreshStatus,
    5000
);
