let state = {
    devices: {
        light: "OFF",
        fan: "OFF",
        geyser: "OFF"
    },

    sensors: {
        temperature: null,
        humidity: null,
        gas: "UNKNOWN"
    },

    mqtt: {
        connected: false
    }
};


function toggleSidebar() {

    const sidebar =
        document.getElementById("sidebar");

    if (sidebar) {
        sidebar.classList.toggle("open");
    }
}


function updateMQTT() {

    const connected =
        state.mqtt.connected === true;

    const badge =
        document.getElementById("mqttBadge");

    const text =
        document.getElementById("mqttText");

    if (badge) {

        badge.textContent = connected
            ? "● MQTT Connected"
            : "● MQTT Offline";

        badge.classList.remove(
            "offline",
            "online"
        );

        badge.classList.add(
            connected
                ? "online"
                : "offline"
        );
    }

    if (text) {

        text.textContent = connected
            ? "Connected"
            : "Offline";
    }
}


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

    } else {

        element.textContent =
            "-- °C";
    }
}


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

    } else {

        element.textContent =
            "-- %";
    }
}


function updateGas() {

    const element =
        document.getElementById("gas");

    if (!element) {
        return;
    }

    element.textContent =
        state.sensors.gas ||
        "UNKNOWN";
}


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
        "on",
        "off"
    );

    element.classList.add(
        value === "ON"
            ? "on"
            : "off"
    );
}


async function refreshStatus() {

    try {

        const response =
            await fetch(
                "/api/status?t=" +
                Date.now(),
                {
                    cache: "no-store",

                    headers: {
                        "Cache-Control":
                            "no-cache",

                        "Pragma":
                            "no-cache"
                    }
                }
            );

        if (
            response.status === 401 ||
            response.redirected
        ) {

            window.location.href =
                "/login";

            return;
        }

        if (!response.ok) {

            throw new Error(
                "Status request failed"
            );
        }

        const data =
            await response.json();

        state = {

            devices:
                data.devices || {},

            sensors:
                data.sensors || {},

            mqtt: {
                connected:
                    data.mqtt_connected === true
            }
        };

        updateMQTT();

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

    } catch (error) {

        console.error(
            "STATUS ERROR:",
            error
        );
    }
}


async function controlDevice(
    device,
    action
) {

    const message =
        document.getElementById(
            "message"
        );

    if (message) {

        message.textContent =
            "Sending command...";

        message.className =
            "message sending";
    }

    try {

        const response =
            await fetch(
                "/api/device/" +
                encodeURIComponent(
                    device
                ) +
                "/" +
                encodeURIComponent(
                    action
                ),
                {
                    method: "POST",

                    cache: "no-store",

                    headers: {

                        "Cache-Control":
                            "no-cache",

                        "Pragma":
                            "no-cache",

                        "X-CSRF-Token":
                            window.CSRF_TOKEN ||
                            ""
                    }
                }
            );

        if (
            response.status === 401 ||
            response.status === 302
        ) {

            window.location.href =
                "/login";

            return;
        }

        const result =
            await response.json();

        if (!response.ok) {

            throw new Error(
                result.error ||
                "Command failed"
            );
        }

        if (result.success !== true) {

            throw new Error(
                result.error ||
                "MQTT command could not be sent."
            );
        }

        state.devices[device] =
            action.toUpperCase();

        updateDevice(
            device,
            state.devices[device]
        );

        if (message) {

            message.textContent =
                device +
                " turned " +
                action.toUpperCase() +
                " successfully.";

            message.className =
                "message success";
        }

        setTimeout(
            refreshStatus,
            1000
        );

    } catch (error) {

        console.error(
            "CONTROL ERROR:",
            error
        );

        if (message) {

            message.textContent =
                error.message ||
                "Command failed.";

            message.className =
                "message error";
        }

        refreshStatus();
    }
}


/*
 * Compatibility for the old Dashboard anchors.
 *
 * Older pages contain:
 *     href="#devices"
 *     href="#monitoring"
 *
 * They now open the dedicated pages.
 */

document.addEventListener(
    "click",
    function (event) {

        const link =
            event.target.closest(
                ".sidebar a"
            );

        if (!link) {
            return;
        }

        const href =
            link.getAttribute(
                "href"
            );

        if (href === "#devices") {

            event.preventDefault();

            window.location.href =
                "/devices";

            return;
        }

        if (
            href === "#monitoring"
        ) {

            event.preventDefault();

            window.location.href =
                "/monitoring";

            return;
        }

        if (
            window.innerWidth <= 720
        ) {

            const sidebar =
                document.getElementById(
                    "sidebar"
                );

            if (sidebar) {

                sidebar.classList.remove(
                    "open"
                );
            }
        }
    }
);


refreshStatus();


setInterval(
    refreshStatus,
    5000
);
