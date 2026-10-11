
"use strict";

/*
 * ============================================================
 * HomeIoT Frontend
 * ============================================================
 *
 * Dashboard:
 *   - View only
 *   - Shows appliance states
 *   - Shows sensor readings
 *   - Shows MQTT status
 *   - Shows actual ESP32 online/offline status
 *
 * Devices:
 *   - Only page where appliance state can be changed
 *   - One dynamic button per appliance
 *
 * MQTT:
 *   - Backend handles MQTT communication
 *   - Frontend only communicates with Flask API
 * ============================================================
 */

const HomeIoT = {
    state: {
        devices: {
            light: "OFF",
            fan: "OFF",
            geyser: "OFF"
        },

        sensors: {
            temperature: null,
            humidity: null,
            gas: "--",
            gas_raw: null
        },

        mqtt: {
            connected: false
        },

        esp32: {
            online: false,
            last_seen: null
        }
    },

    /*
     * --------------------------------------------------------
     * Utility
     * --------------------------------------------------------
     */

    escapeHtml(value) {
        if (value === null || value === undefined) {
            return "";
        }

        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    },

    getElement(id) {
        return document.getElementById(id);
    },

    setText(id, value) {
        const element = this.getElement(id);

        if (element) {
            element.textContent = value;
        }
    },

    /*
     * --------------------------------------------------------
     * Sidebar
     * --------------------------------------------------------
     */

    toggleSidebar() {
        const sidebar = this.getElement("sidebar");

        if (!sidebar) {
            return;
        }

        sidebar.classList.toggle("open");
    },

    closeSidebarOnNavigation() {
        const sidebar = this.getElement("sidebar");

        if (!sidebar) {
            return;
        }

        if (window.innerWidth <= 900) {
            sidebar.classList.remove("open");
        }
    },

    /*
     * --------------------------------------------------------
     * CSRF
     * --------------------------------------------------------
     */

    getCsrfToken() {
        const element = document.querySelector(
            'input[name="csrf_token"]'
        );

        if (element) {
            return element.value;
        }

        const meta = document.querySelector(
            'meta[name="csrf-token"]'
        );

        if (meta) {
            return meta.getAttribute("content");
        }

        return "";
    },

    /*
     * --------------------------------------------------------
     * API
     * --------------------------------------------------------
     */

    async getStatus() {
        try {
            const response = await fetch(
                "/api/status",
                {
                    method: "GET",
                    headers: {
                        "Accept": "application/json"
                    },
                    cache: "no-store"
                }
            );

            if (!response.ok) {
                throw new Error("Status request failed");
            }

            const data = await response.json();

            this.updateState(data);
            this.updateDashboard();
            this.updateDevices();
            this.updateConnectionIndicators();

        } catch (error) {
            console.error(
                "HomeIoT status error:",
                error
            );
        }
    },

    async sendDeviceCommand(
        device,
        action,
        button
    ) {
        if (!device || !action) {
            return;
        }

        if (button) {
            button.disabled = true;

            button.dataset.originalText =
                button.textContent;

            button.textContent = "Sending...";
        }

        try {
            const csrfToken = this.getCsrfToken();

            const response = await fetch(
                `/api/device/${encodeURIComponent(device)}/${encodeURIComponent(action)}`,
                {
                    method: "POST",

                    headers: {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-CSRF-Token": csrfToken
                    },

                    body: JSON.stringify({})
                }
            );

            let data = {};

            try {
                data = await response.json();
            } catch (_) {
                data = {};
            }

            if (!response.ok) {
                throw new Error(
                    data.error ||
                    data.message ||
                    "Unable to send command"
                );
            }

            /*
             * Do not permanently change the device state here.
             * The actual state is refreshed from /api/status.
             */

            this.showCommandMessage(
                data.message ||
                "Command sent successfully.",
                "success"
            );

            /*
             * Give the ESP32 time to process the command.
             */

            setTimeout(
                () => this.getStatus(),
                700
            );

            setTimeout(
                () => this.getStatus(),
                1800
            );

        } catch (error) {
            console.error(
                "Device command error:",
                error
            );

            this.showCommandMessage(
                error.message ||
                "Command failed.",
                "error"
            );

        } finally {
            if (button) {
                setTimeout(
                    () => {
                        button.disabled = false;

                        const currentState =
                            this.state.devices[device] || "OFF";

                        button.textContent =
                            currentState === "ON"
                                ? "Turn OFF"
                                : "Turn ON";
                    },
                    1200
                );
            }
        }
    },

    /*
     * --------------------------------------------------------
     * State
     * --------------------------------------------------------
     */

    updateState(data) {
        if (!data || typeof data !== "object") {
            return;
        }

        if (data.devices) {
            this.state.devices = {
                ...this.state.devices,
                ...data.devices
            };
        }

        if (data.sensors) {
            this.state.sensors = {
                ...this.state.sensors,
                ...data.sensors
            };
        }

        /*
         * FIX: The Flask API returns data.mqtt.connected,
         * not data.mqtt_connected.
         */

        this.state.mqtt.connected =
            data.mqtt?.connected === true;

        if (data.esp32) {
            this.state.esp32 = {
                ...this.state.esp32,
                ...data.esp32
            };
        }
    },

    /*
     * --------------------------------------------------------
     * Dashboard
     * --------------------------------------------------------
     */

    updateDashboard() {
        this.updateDeviceStatus(
            "light",
            this.state.devices.light
        );

        this.updateDeviceStatus(
            "fan",
            this.state.devices.fan
        );

        this.updateDeviceStatus(
            "geyser",
            this.state.devices.geyser
        );

        const sensors = this.state.sensors;

        /*
         * Temperature
         */

        if (
            sensors.temperature !== null &&
            sensors.temperature !== undefined
        ) {
            this.setText(
                "temperature",
                `${sensors.temperature} °C`
            );
        } else {
            this.setText("temperature", "--");
        }

        /*
         * Humidity
         */

        if (
            sensors.humidity !== null &&
            sensors.humidity !== undefined
        ) {
            this.setText(
                "humidity",
                `${sensors.humidity} %`
            );
        } else {
            this.setText("humidity", "--");
        }

        /*
         * Gas
         */

        this.setText(
            "gas",
            sensors.gas || "--"
        );

        /*
         * Raw gas value, if displayed.
         */

        if (
            sensors.gas_raw !== null &&
            sensors.gas_raw !== undefined
        ) {
            this.setText(
                "gasRaw",
                sensors.gas_raw
            );
        }
    },

    /*
     * --------------------------------------------------------
     * Device state display
     * --------------------------------------------------------
     */

    updateDeviceStatus(device, status) {
        const normalized =
            String(status || "OFF").toUpperCase();

        const elements = document.querySelectorAll(
            `[data-device-status="${device}"]`
        );

        elements.forEach(element => {
            element.textContent = normalized;

            element.classList.remove(
                "on",
                "off"
            );

            if (normalized === "ON") {
                element.classList.add("on");
            } else {
                element.classList.add("off");
            }
        });
    },

    /*
     * --------------------------------------------------------
     * Devices page
     * --------------------------------------------------------
     */

    updateDevices() {
        const devices = [
            "light",
            "fan",
            "geyser"
        ];

        devices.forEach(device => {
            const state =
                String(
                    this.state.devices[device] || "OFF"
                ).toUpperCase();

            /*
             * Device status labels
             */

            this.updateDeviceStatus(
                device,
                state
            );

            /*
             * Exactly one button per device.
             * OFF -> Turn ON
             * ON  -> Turn OFF
             */

            const button = this.getElement(
                `${device}Button`
            );

            if (!button) {
                return;
            }

            button.textContent =
                state === "ON"
                    ? "Turn OFF"
                    : "Turn ON";

            button.classList.remove(
                "is-on",
                "is-off"
            );

            if (state === "ON") {
                button.classList.add("is-on");
            } else {
                button.classList.add("is-off");
            }

            button.dataset.state = state;
        });
    },

    /*
     * --------------------------------------------------------
     * Dynamic device toggle
     * --------------------------------------------------------
     */

    toggleDevice(device) {
        if (
            ![
                "light",
                "fan",
                "geyser"
            ].includes(device)
        ) {
            console.error(
                "Invalid device:",
                device
            );

            return;
        }

        const currentState =
            String(
                this.state.devices[device] || "OFF"
            ).toUpperCase();

        const action =
            currentState === "ON"
                ? "OFF"
                : "ON";

        const button = this.getElement(
            `${device}Button`
        );

        this.sendDeviceCommand(
            device,
            action,
            button
        );
    },

    /*
     * --------------------------------------------------------
     * MQTT / ESP32 indicators
     * --------------------------------------------------------
     */

    updateConnectionIndicators() {
        const mqttConnected =
            this.state.mqtt.connected === true;

        /*
         * MQTT status
         *
         * Include #mqttBadge because the Devices page may
         * use that ID instead of data-mqtt-status.
         */

        const mqttElements = document.querySelectorAll(
            "[data-mqtt-status], #mqttBadge"
        );

        mqttElements.forEach(element => {
            element.textContent =
                mqttConnected
                    ? "Connected"
                    : "Disconnected";

            element.classList.remove(
                "connected",
                "disconnected",
                "online",
                "offline"
            );

            element.classList.add(
                mqttConnected
                    ? "connected"
                    : "disconnected"
            );

            element.classList.add(
                mqttConnected
                    ? "online"
                    : "offline"
            );
        });

        /*
         * ESP32 status
         */

        const esp32Online =
            this.state.esp32.online === true;

        const espElements = document.querySelectorAll(
            "[data-esp32-status]"
        );

        espElements.forEach(element => {
            element.textContent =
                esp32Online
                    ? "Online"
                    : "Offline";

            element.classList.remove(
                "connected",
                "disconnected",
                "online",
                "offline"
            );

            element.classList.add(
                esp32Online
                    ? "connected"
                    : "disconnected"
            );

            element.classList.add(
                esp32Online
                    ? "online"
                    : "offline"
            );
        });

        /*
         * Last Seen
         */

        const lastSeen =
            this.state.esp32.last_seen;

        const lastSeenElements = document.querySelectorAll(
            "[data-esp32-last-seen]"
        );

        lastSeenElements.forEach(element => {
            element.textContent =
                lastSeen || "Never";
        });
    },

    /*
     * --------------------------------------------------------
     * Command message
     * --------------------------------------------------------
     */

    showCommandMessage(
        message,
        type = "success"
    ) {
        let container = this.getElement(
            "commandMessage"
        );

        if (!container) {
            container = document.createElement("div");

            container.id = "commandMessage";
            container.className = "command-message";

            document.body.appendChild(container);
        }

        container.textContent = message;

        container.classList.remove(
            "success",
            "error"
        );

        container.classList.add(type);
        container.classList.add("visible");

        clearTimeout(this.commandMessageTimer);

        this.commandMessageTimer = setTimeout(
            () => {
                container.classList.remove("visible");
            },
            3500
        );
    },

    /*
     * --------------------------------------------------------
     * Automatic status refresh
     * --------------------------------------------------------
     */

    startPolling() {
        /*
         * Initial request.
         */

        this.getStatus();

        /*
         * Refresh every 5 seconds.
         * Appliance automation is NOT performed here.
         */

        this.pollingTimer = setInterval(
            () => this.getStatus(),
            5000
        );
    },

    /*
     * --------------------------------------------------------
     * Initialization
     * --------------------------------------------------------
     */

    init() {
        /*
         * Make sidebar available globally through the
         * existing onclick="toggleSidebar()" calls.
         */

        window.toggleSidebar =
            () => this.toggleSidebar();

        /*
         * Make device toggle available to the Devices page.
         */

        window.toggleDevice =
            device => this.toggleDevice(device);

        /*
         * Close mobile sidebar after navigation.
         */

        document
            .querySelectorAll(".sidebar a")
            .forEach(link => {
                link.addEventListener(
                    "click",
                    () => this.closeSidebarOnNavigation()
                );
            });

        /*
         * Start status updates.
         */

        this.startPolling();
    }
};

/*
 * ============================================================
 * START APPLICATION
 * ============================================================
 */

document.addEventListener(
    "DOMContentLoaded",
    () => HomeIoT.init()
);
