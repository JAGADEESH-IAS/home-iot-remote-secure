# HomeIoT — Internet-Based Remote Control of Home Appliances (Flask)

This package is the **redesigned interface (v2, "Paper & Ember")** of the existing
project. The backend is untouched: `app.py`, `static/app.js`, `requirements.txt`,
`runtime.txt` and `.gitignore` are byte-for-byte identical to the files you supplied.
Routes, API contracts, MQTT, ESP32 protocol, Firebase, authentication and CSRF
protection are unchanged.

```
Browser → Flask (Render) → HiveMQ Cloud (MQTT/TLS 8883) → ESP32 → relays (Light / Fan / Geyser)
                         ↘ Firebase Firestore (users, history, latest status)
```

## Setup (unchanged)

```bash
python -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt
python app.py                                          # http://localhost:5000
gunicorn app:app                                       # production, as on Render
```

Environment variable names are the ones the app already reads: `SECRET_KEY`,
`FIREBASE_SERVICE_ACCOUNT`, `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`,
`MQTT_PASSWORD`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, plus your existing SMTP/OTP
variables. **No secrets, `.env` files or keys are in this ZIP.**

## Design direction

The first redesign was a dark navy/teal dashboard. It was replaced because it read as a
generic admin template. v2 is a different product identity:

* **Warm paper, ink and one ember accent.** Light, calm surfaces (`#F2EEE6`), near-black
  ink, and a single orange "ember" colour reserved for things that are *lit*: ON states,
  the active page, the lamp in the logo. Status colours (green/red/amber) are muted and
  always paired with a text label.
* **Type.** Serif display type ("HomeIoT Serif", a renamed Lora subset) for headings and
  big sensor readouts; Inter for interface text. Both are self-hosted.
* **Structure instead of boxes.** Sections are divided by hairlines and whitespace; only
  forms, tables and the device list sit on raised surfaces. Sensor readings are
  typographic readouts, not repeated cards.
* **Top navigation on desktop**, slide-in drawer on tablet/phone (Escape, scrim tap and
  the menu button all close it).
* **Devices.** Each appliance is a row with a large icon, state label and a real switch
  button. A lit appliance gets a warm wash and ember edge. The switch is drawn in CSS and
  follows the `is-on` / `is-off` classes `app.js` already maintains; the loading state
  (spinner + "Sending…") is CSS-only.
* **Stale-data notice, CSS only.** When `app.js` marks the ESP32 chip `.offline`, a
  warning appears ("states below are the last ones it reported"). It uses live data and
  needs no extra JavaScript.
* **Auth pages** are a split screen: ink brand panel with an original line illustration,
  and a quiet form. On phones the panel becomes a header band.
* **Landing page** is editorial: large serif headline, an original house-section
  illustration (labelled "Illustration, not live data"), numbered architecture timeline.
  It shows **no fake readings**.
* **History/Admin tables** turn into labelled stacked rows on phones (no sideways scroll).
* Original logo + favicon; one consistent 1.5 px line-icon set (`templates/_icons.html`).
  No third-party brand logos, no stock photos, no CDN.

### Honest status labels (preserved from your debugging)
* "**MQTT status feed**" is driven by `/api/status` → `mqtt.connected`, which your API
  derives from recent ESP32 status messages. It is **not** presented as proof of the
  broker connection. The ESP32 chip and "Last seen" are separate items.
* Nothing is hard-coded "Connected". First paint uses the same template variables as
  before and `app.js` takes over.
* The small dot beside the signed-in user is neutral (it only means "signed in").

### Deliberately not added
* No charts/trends: the backend stores only the latest values.
* No gas thresholds/colours: the gas strings the ESP32 sends are not defined in the code.

## Files

| Path | Status | Notes |
|---|---|---|
| `app.py`, `static/app.js`, `requirements.txt`, `runtime.txt`, `.gitignore` | unchanged | identical to your upload |
| `static/style.css` | rewritten | new design system, tokens at top; all JS hooks kept (`on/off`, `is-on/is-off`, `connected/disconnected`, `online/offline`, `.sidebar.open`, `.command-message.visible`) |
| `templates/base_app.html`, `base_public.html` | new | shared layouts |
| `templates/_sidebar.html`, `_brand.html`, `_icons.html`, `_components.html`, `_flash.html`, `_auth_aside.html`, `_house_art.html` | new | shared partials (`_auth_card.html` from v1 was removed) |
| `templates/index.html` (Dashboard), `devices.html`, `history.html`, `settings.html`, `admin_users.html`, `home.html`, `login.html`, `signup.html`, `signup_success.html`, `forgot_password.html`, `reset_password.html`, `verify_otp.html` | redesigned | same routes, form actions/methods, field names, CSRF inputs, IDs, data attributes and inline handlers |
| `static/img/favicon.svg`, `logo.svg` | new | original brand mark |
| `static/fonts/*` | new | Inter + HomeIoT Serif (Lora subset), licences `LICENSE-Inter.txt`, `LICENSE-Lora.txt` |
| `docs/screenshots/*` | new | previews rendered with **stub data**, not real readings |

### JavaScript / template changes
* `static/app.js` — **not modified**. The History filter script and the signup password
  check were moved verbatim into their templates.
* `base_app.html` contains a small inline script: it defines `toggleSidebar()` only if
  missing (History/Settings/Admin never loaded `app.js`, so the menu button did nothing
  there) and closes the drawer on Escape. `app.js` overrides `toggleSidebar` where loaded.
* Devices page: the ON/OFF label and the ESP32 status / last-seen elements now also carry
  the `data-device-status`, `data-esp32-status`, `data-esp32-last-seen` attributes that
  `app.js` already updates, so they refresh with the 5-second poll instead of staying stale.
* History/Admin table cells gained `data-label` attributes (used only by CSS on phones).

## Tests actually performed
Harness (not shipped): the **real `app.py`** with Firebase and `paho-mqtt` mocked and
only data-access functions stubbed, driven by headless Chromium — real routes, real CSRF
checks, real `app.js`.

* All 12 templates render without Jinja errors.
* Markup comparison original vs redesign on 9 pages: **no missing** element IDs, `data-*`
  hooks, `onclick` handlers, links, form actions/methods or input names (additions only).
* **37 behavioural checks passed**: command POST reaches `/api/device/<d>/<ON|OFF>` with
  CSRF; "Sending…"/disabled state; label, button and switch update from the next poll;
  success and error toasts; ESP32 and status-feed indicators flip independently and
  recover; stale notice appears/disappears from live data; last-seen, temperature,
  humidity, gas and raw values update; History filter/clear/empty state; Admin summary and
  Approve/Reject/Disable/Protected actions with CSRF; signup mismatch alert; mobile drawer
  opens/closes (menu, scrim, Escape) on Dashboard, Devices, History, Settings; no Admin
  link for non-admins; no uncaught JS errors.
* All pages at 1440 / 820 / 390 px: no horizontal overflow, fonts load, no JS errors.
  (A `/favicon.ico` 404 can appear in the console; the icon is linked via `<link>`.)

## NOT tested (needs your environment)
Live HiveMQ, real ESP32/relays, real Firestore reads/writes, email OTP delivery, Render,
Safari/Firefox, real touch devices, screen readers. The stale notice uses CSS `:has()`
(Chrome/Edge 105+, Safari 15.4+, Firefox 121+); older browsers simply don't show it.

### Manual checklist after deploying
1. Sign in; Dashboard shows real temperature/humidity/gas and appliance states.
2. Devices → toggle Light, Fan, Geyser: relay actuates, label + switch flip within ~2 s, History gets a row.
3. Power the ESP32 off → ESP32 chip and "MQTT status feed" go Offline/Disconnected and the amber notice appears; power on → they recover.
4. Break the broker/credentials → a command shows the red toast and a FAILED history row.
5. History filters; Settings; Admin approve/reject/disable (admin account).
6. Sign-up → OTP → pending; forgot/reset password.
7. Phone: drawer, switch buttons, tables stacked, no sideways scroll.

## Rollback
Only `templates/` and `static/` (plus `README.md` and `docs/`) differ from your original.
Restore those from your original ZIP / previous git commit (`git revert <redesign-commit>`
or redeploy the previous Render deploy). No data or configuration migration is involved.
