/* ============================================================
   HOME IOT SECURE
   Application JavaScript
   ============================================================ */


/* ============================================================
   GLOBAL CONFIGURATION
   ============================================================ */

const STATUS_REFRESH_INTERVAL = 5000;


/* ============================================================
   CSRF TOKEN
   ============================================================ */

function getCsrfToken() {
    /*
     * First use the token exposed by the page.
     * This is provided by templates/devices.html and other
     * protected pages.
     */

    if (
        typeof window.CSRF_TOKEN !== "undefined" &&
        window.CSRF_TOKEN
    ) {
        return window.CSRF_TOKEN;
    }


    /*
     * Fallback to the HTML meta tag.
     */

    const metaTag = document.querySelector(
        'meta[name="csrf-token"]'
    );

    if (
        metaTag &&
        metaTag.content
    ) {
        return metaTag.content;
    }


    /*
     * Final fallback to a hidden form field.
     */

    const hiddenInput = document.querySelector(
        'input[name="csrf_token"]'
    );

    if (
        hiddenInput &&
        hiddenInput.value
    ) {
        return hiddenInput.value;
    }


    return "";
}


/* ============================================================
   TOAST NOTIFICATIONS
   ============================================================ */

function showToast(
    message,
    type = "info"
) {
    let container = document.getElementById(
        "toastContainer"
    );


    if (!container) {

        container = document.createElement(
            "div"
        );

        container.id = "toastContainer";

        container.className =
            "toast-container";

        document.body.appendChild(
            container
        );
    }


    const toast = document.createElement(
        "div"
    );

    toast.className =
        "toast toast-" + type;


    toast.textContent = message;


    container.appendChild(
        toast
    );


    window.setTimeout(
        function () {

            toast.classList.add(
                "toast-hide"
            );


            window.setTimeout(
                function () {

                    if (
                        toast.parentNode
                    ) {
                        toast.parentNode.removeChild(
                            toast
                        );
                    }

                },
                300
            );

        },
        3000
    );
}


/* ============================================================
   DEVICE STATUS HELPERS
   ============================================================ */

function normalizeDeviceState(
    state
) {
    return String(
        state || "OFF"
    ).toUpperCase();
}


function updateDeviceStatus(
    device,
    state
) {
    const normalizedState =
        normalizeDeviceState(
            state
        );


    /*
     * Update every status element belonging
     * to the specified device.
     */

    const statusElements =
        document.querySelectorAll(
            '[data-device-status="' +
            device +
            '"]'
        );


    statusElements.forEach(
        function (element) {

            element.innerHTML = "";


            const dot =
                document.createElement(
                    "span"
                );


            dot.className =
                "status-dot " +
                (
                    normalizedState === "ON"
                        ? "online"
                        : "offline"
                );


            element.appendChild(
                dot
            );


            element.appendChild(
                document.createTextNode(
                    normalizedState
                )
            );

        }
    );


    /*
     * Update the single dynamic control button
     * on the Devices page.
     */

    const button =
        document.getElementById(
            device + "Button"
        );


    if (!button) {
        return;
    }


    if (
        normalizedState === "ON"
    ) {

        button.textContent =
            "Turn OFF";

    } else {

        button.textContent =
            "Turn ON";

    }


    button.disabled = false;

}


/* ============================================================
   SENSOR HELPERS
   ============================================================ */

function formatTemperature(
    value
) {
    if (
        value === null ||
        typeof value === "undefined" ||
        value === ""
    ) {
        return "--";
    }


    return value + " °C";
}


function formatHumidity(
    value
) {
    if (
        value === null ||
        typeof value === "undefined" ||
        value === ""
    ) {
        return "--";
    }


    return value + " %";
}


function formatSensorValue(
    value
) {
    if (
        value === null ||
        typeof value === "undefined" ||
        value === ""
    ) {
        return "--";
    }


    return value;
}


/* ============================================================
   UPDATE SENSOR DISPLAY
   ============================================================ */

function updateSensorDisplay(
    sensors
) {
    if (!sensors) {
        return;
    }


    const temperatureElement =
        document.getElementById(
            "temperature"
        );


    if (temperatureElement) {

        temperatureElement.textContent =
            formatTemperature(
                sensors.temperature
            );

    }


    const humidityElement =
        document.getElementById(
            "humidity"
        );


    if (humidityElement) {

        humidityElement.textContent =
            formatHumidity(
                sensors.humidity
            );

    }


    const gasElement =
        document.getElementById(
            "gas"
        );


    if (gasElement) {

        gasElement.textContent =
            formatSensorValue(
                sensors.gas
            );

    }


    const gasRawElement =
        document.getElementById(
            "gasRaw"
        );


    if (gasRawElement) {

        gasRawElement.textContent =
            formatSensorValue(
                sensors.gas_raw
            );

    }
}


/* ============================================================
   MQTT STATUS DISPLAY
   ============================================================ */

function updateMqttStatus(
    mqtt
) {
    /*
     * Backend response:
     *
     * "mqtt": {
     *     "connected": true
     * }
     */

    let connected = false;


    if (
        mqtt &&
        typeof mqtt.connected !== "undefined"
    ) {
        connected = Boolean(
            mqtt.connected
        );
    }


    const elements =
        document.querySelectorAll(
            "[data-mqtt-status]"
        );


    elements.forEach(
        function (element) {

            element.innerHTML = "";


            const dot =
                document.createElement(
                    "span"
                );


            dot.className =
                "status-dot " +
                (
                    connected
                        ? "online"
                        : "offline"
                );


            element.appendChild(
                dot
            );


            element.appendChild(
                document.createTextNode(
                    connected
                        ? "Connected"
                        : "Disconnected"
                )
            );

        }
    );
}


/* ============================================================
   ESP32 STATUS DISPLAY
   ============================================================ */

function updateEsp32Status(
    esp32
) {
    if (!esp32) {
        return;
    }


    const online =
        Boolean(
            esp32.online
        );


    const statusElements =
        document.querySelectorAll(
            "[data-esp32-status]"
        );


    statusElements.forEach(
        function (element) {

            element.innerHTML = "";


            const dot =
                document.createElement(
                    "span"
                );


            dot.className =
                "status-dot " +
                (
                    online
                        ? "online"
                        : "offline"
                );


            element.appendChild(
                dot
            );


            element.appendChild(
                document.createTextNode(
                    online
                        ? "Online"
                        : "Offline"
                )
            );

        }
    );


    const lastSeenElements =
        document.querySelectorAll(
            "[data-esp32-last-seen]"
        );


    lastSeenElements.forEach(
        function (element) {

            element.textContent =
                esp32.last_seen || "-";

        }
    );
}


/* ============================================================
   UPDATE COMPLETE STATUS
   ============================================================ */

function updateStatusDisplay(
    data
) {
    if (!data) {
        return;
    }


    /*
     * Device states
     */

    if (data.devices) {

        updateDeviceStatus(
            "light",
            data.devices.light
        );


        updateDeviceStatus(
            "fan",
            data.devices.fan
        );


        updateDeviceStatus(
            "geyser",
            data.devices.geyser
        );

    }


    /*
     * Sensor readings
     */

    if (data.sensors) {

        updateSensorDisplay(
            data.sensors
        );

    }


    /*
     * MQTT status
     */

    updateMqttStatus(
        data.mqtt
    );


    /*
     * ESP32 status
     */

    updateEsp32Status(
        data.esp32
    );
}


/* ============================================================
   FETCH CURRENT STATUS
   ============================================================ */

async function fetchStatus() {

    try {

        const response =
            await fetch(
                "/api/status",
                {
                    method: "GET",
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: {
                        "Accept":
                            "application/json"
                    }
                }
            );


        if (
            response.status === 401 ||
            response.redirected
        ) {
            return;
        }


        if (!response.ok) {
            throw new Error(
                "Status request failed."
            );
        }


        const data =
            await response.json();


        if (!data.success) {
            throw new Error(
                data.message ||
                "Unable to read status."
            );
        }


        updateStatusDisplay(
            data
        );

    } catch (error) {

        console.error(
            "Status refresh error:",
            error
        );

    }
}


/* ============================================================
   DEVICE CONTROL
   ============================================================ */

async function toggleDevice(
    device
) {
    const button =
        document.getElementById(
            device + "Button"
        );


    if (!button) {
        return;
    }


    /*
     * Read the current state directly from
     * the displayed status element.
     */

    const statusElement =
        document.querySelector(
            '[data-device-status="' +
            device +
            '"]'
        );


    let currentState =
        "OFF";


    if (statusElement) {

        currentState =
            statusElement.textContent
                .trim()
                .toUpperCase();

    }


    const action =
        currentState === "ON"
            ? "OFF"
            : "ON";


    /*
     * Prevent duplicate commands while
     * the current command is being sent.
     */

    if (
        button.disabled
    ) {
        return;
    }


    button.disabled = true;


    const originalText =
        button.textContent;


    button.textContent =
        "Sending...";


    try {

        const token =
            getCsrfToken();


        if (!token) {

            throw new Error(
                "Security token unavailable."
            );

        }


        const response =
            await fetch(
                "/api/device/" +
                encodeURIComponent(device) +
                "/" +
                encodeURIComponent(action),
                {
                    method: "POST",
                    credentials: "same-origin",
                    cache: "no-store",

                    headers: {
                        "Content-Type":
                            "application/json",

                        "Accept":
                            "application/json",

                        "X-CSRF-Token":
                            token
                    },

                    body: JSON.stringify(
                        {}
                    )
                }
            );


        if (
            response.status === 401 ||
            response.redirected
        ) {

            window.location.href =
                "/signin";

            return;

        }


        const data =
            await response.json();


        if (!response.ok) {

            throw new Error(
                data.message ||
                "Device command failed."
            );

        }


        if (!data.success) {

            throw new Error(
                data.message ||
                "Device command failed."
            );

        }


        showToast(
            data.message ||
            (
                device +
                " command sent."
            ),
            "success"
        );


        /*
         * Do not optimistically assume the hardware
         * changed state.
         *
         * The ESP32 status message is the source
         * of the displayed device state.
         */

        await fetchStatus();


    } catch (error) {

        console.error(
            "Device command error:",
            error
        );


        showToast(
            error.message ||
            "Unable to control device.",
            "error"
        );


        /*
         * Restore the button text from the
         * current displayed state.
         */

        updateDeviceStatus(
            device,
            currentState
        );


        button.textContent =
            originalText;

    } finally {

        button.disabled = false;


        /*
         * Refresh once more so that the button
         * reflects the actual latest ESP32 state.
         */

        fetchStatus();

    }
}


/* ============================================================
   MOBILE SIDEBAR
   ============================================================ */

function initializeMobileSidebar() {

    const sidebar =
        document.getElementById(
            "sidebar"
        );


    const menuButton =
        document.getElementById(
            "mobileMenuButton"
        );


    if (
        !sidebar ||
        !menuButton
    ) {
        return;
    }


    menuButton.addEventListener(
        "click",
        function () {

            sidebar.classList.toggle(
                "mobile-open"
            );

        }
    );


    /*
     * Close the sidebar after selecting
     * a navigation item on mobile.
     */

    const navItems =
        sidebar.querySelectorAll(
            ".nav-item"
        );


    navItems.forEach(
        function (item) {

            item.addEventListener(
                "click",
                function () {

                    sidebar.classList.remove(
                        "mobile-open"
                    );

                }
            );

        }
    );

}


/* ============================================================
   INITIALIZATION
   ============================================================ */

document.addEventListener(
    "DOMContentLoaded",
    function () {

        initializeMobileSidebar();


        /*
         * Load the latest state immediately.
         */

        fetchStatus();


        /*
         * Continue refreshing the status every
         * five seconds.
         */

        window.setInterval(
            fetchStatus,
            STATUS_REFRESH_INTERVAL
        );

    }
);


/* ============================================================
   GLOBAL DEVICE CONTROL FUNCTION
   ============================================================ */

window.toggleDevice =
    toggleDevice;
