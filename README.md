# HomeIoT — Internet-Based Remote Control of Home Appliances (Flask)

This is the **redesigned UI** of the existing project. The Flask backend, MQTT
logic, ESP32 protocol, Firebase integration, authentication, CSRF protection,
routes and API contracts are **unchanged**. `app.py` and `static/app.js` are
byte-for-byte identical to the version you supplied.

```
Browser → Flask (Render) → HiveMQ Cloud (MQTT/TLS 8883) → ESP32 → relays (Light / Fan / Geyser)
                         ↘ Firebase Firestore (users, history, latest status)
```

## Setup (unchanged from before)

```bash
python -m venv .venv && source .venv/bin/activate      # optional
pip install -r requirements.txt
```

Environment variables are the same ones the app already reads (nothing new,
nothing renamed): `SECRET_KEY`, `FIREBASE_SERVICE_ACCOUNT`, `MQTT_BROKER`,
`MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `ADMIN_USERNAME`,
`ADMIN_PASSWORD`, plus the SMTP/OTP variables you already use. **No secrets are
included in this ZIP.**

```bash
python app.py                     # local, http://localhost:5000
gunicorn app:app                  # production (Render start command as before)
```

Deploying: copy these files over your repository (see "Rollback" first), commit,
and let Render redeploy as usual. Nothing in Render settings needs to change.

## What changed

### Visual design
Dark "control-center" theme: deep-navy surfaces, teal accent, restrained
status colours (green online / amber warning / red offline & error), Inter
typography, a 4 px spacing scale, 12–20 px radii, subtle borders and shadows,
200 ms transitions (disabled under `prefers-reduced-motion`). Responsive at
1100 / 900 / 700 px; the sidebar becomes an off-canvas drawer with a scrim on
tablets/phones. All colours/spacing are CSS variables at the top of
`static/style.css` (section 2) — change them there.

* Original logo + favicon (house with signal arcs): `static/img/favicon.svg`, `static/img/logo.svg`.
* One consistent inline-SVG icon set (1.75 px stroke): `templates/_icons.html`.
* Self-hosted **Inter** (Latin subset, WOFF) in `static/fonts/` under the SIL
  Open Font License (`LICENSE-Inter.txt`), with a system-font fallback stack.
  No CDN or external image is used anywhere.
* No third-party brand logos are used.

### Files

| File | Status | Why |
|---|---|---|
| `static/style.css` | **rewritten** | New design system (previous 2,867-line stylesheet replaced). All JS-driven class hooks kept (`on/off`, `is-on/is-off`, `connected/disconnected`, `online/offline`, `.sidebar.open`, `.command-message.visible`). |
| `templates/base_app.html` | added | Shared authenticated layout (sidebar, top bar, footer). |
| `templates/base_public.html` | added | Shared layout for landing + auth pages. |
| `templates/_sidebar.html` | added | The sidebar that was copy-pasted in 6 templates; active item now comes from `request.endpoint`. Admin link still only for `ADMIN`. |
| `templates/_icons.html`, `_components.html`, `_flash.html`, `_auth_card.html` | added | Icon macro, status-chip macros, flash block, auth brand header. |
| `templates/index.html` (Dashboard), `devices.html`, `history.html`, `settings.html`, `admin_users.html`, `home.html`, `login.html`, `signup.html`, `signup_success.html`, `forgot_password.html`, `reset_password.html`, `verify_otp.html` | **redesigned** | Now extend the shared layouts. Same routes, form actions/methods, field names, CSRF inputs, IDs, data attributes and inline handlers. |
| `static/img/favicon.svg`, `static/img/logo.svg` | added | Logo & favicon. |
| `static/fonts/*` | added | Inter + licence. |
| `docs/screenshots/*` | added | Previews (rendered with sample stub data, not real readings). |
| `app.py`, `static/app.js`, `requirements.txt`, `runtime.txt`, `.gitignore` | **unchanged** | — |
| Deleted | none | The old `style.css` was replaced in place. |

### JavaScript
`static/app.js` was **not modified**. The history filter script (History page)
and the signup password check (Sign Up page) were moved verbatim into the new
templates. The one JS addition is a 6-line inline fallback in `base_app.html`
that defines `toggleSidebar()` only if it doesn't exist — the original
History/Settings/Admin pages called `toggleSidebar()` but never loaded
`app.js`, so the mobile menu button did nothing there. `app.js` redefines it
when loaded, so Dashboard/Devices behave exactly as before.

### Small template-level fixes (flagged because they go slightly beyond "paint")
1. **Devices page state label** (`#lightState` etc.) now also carries
   `data-device-status="…"`. `app.js` already updates elements with that
   attribute, so the ON/OFF label next to each button now refreshes with the
   5-second poll instead of staying stale until reload.
2. **Devices page ESP32 status / Last seen** now also carry `data-esp32-status`
   / `data-esp32-last-seen` for the same reason (they previously never updated).
3. Initial server-rendered buttons get `is-on` / `is-off` so styling is right
   before the first poll.
4. Landing-page preview is explicitly labelled **"sample preview"** (it is a
   static illustration, as before, not live data).

### Connectivity indicators (intentionally not "prettified")
* **MQTT status feed** — labelled that way because `/api/status` reports
  `mqtt.connected` from *recent status messages arriving* (your earlier fix).
  Its text/colour is still driven only by `app.js`.
* **ESP32** — separate indicator + "Last seen" from `esp32.online/last_seen`.
* Nothing is hard-coded "Connected". Server-rendered first paint uses the same
  template variables as before (`mqtt_connected`, `esp32`).

### Deliberately not added
* **No charts/gauges/history graphs** — the backend stores only the latest
  sensor values; drawing trends would require fabricating data or changing the
  backend.
* **No colour-coding of the gas value** — the set of strings the ESP32 sends is
  not defined in the code, so I did not guess thresholds.

## Tests actually performed
Harness: the **real `app.py`** loaded with Firebase and `paho-mqtt` mocked and
only data-access functions stubbed in memory (not shipped), driven by headless
Chromium. Real Flask routes, real CSRF checks, real `app.js`.

* All 12 templates render without Jinja errors (incl. OTP / reset / success pages).
* **Markup comparison original vs redesign on 9 pages**: no missing element
  IDs, `data-*` hooks, `onclick` handlers, links, form actions/methods or input
  names (additions only: `#main`, skip link, the data attributes above).
* 31 behavioural checks passed: command POST reaches `/api/device/<d>/<ON|OFF>`
  with CSRF; "Sending…" + disabled state; button/label/colour update after poll;
  success and error toasts; ESP32 and MQTT-feed indicators flip independently of
  hard-coded values and recover; last-seen updates; sensor values + gas + raw
  update; History filters / clear / empty state; Admin summary and
  Approve/Reject/Disable/Protected actions; signup mismatch alert; mobile menu
  opens/closes on Dashboard, Devices, History, Settings; non-admin has no Admin link.
* All pages screenshotted at 1440 / 820 / 390 px: no horizontal overflow, no JS
  errors, Inter loads. (One harmless `/favicon.ico` 404 is requested by some
  browsers; it also happened before — the icon is linked via `<link>`.)

## NOT tested (needs your environment)
Live HiveMQ connection, real ESP32/relays, real Firestore reads/writes, email
OTP delivery, Render deployment, Safari/Firefox, real touch devices.

### Manual checklist after deploying
1. Sign in; Dashboard shows real temp/humidity/gas and appliance states.
2. Devices → toggle Light, Fan, Geyser: relay actuates, label + button flip within ~2 s, History gets a row.
3. Power the ESP32 off → within the timeout both ESP32 and "MQTT status feed" show Offline/Disconnected; back on → Online/Connected.
4. Failed command (e.g. broker down) shows the red toast.
5. History filters; Settings; Admin approve/reject (admin account).
6. Sign-up → OTP → pending; forgot/reset password flow.
7. Phone: menu drawer, 44 px touch targets, no sideways scrolling.

## Rollback
Keep your previous commit. To undo: `git revert <redesign-commit>` (or redeploy
the previous commit on Render). Because only `templates/` and `static/` changed,
restoring those two folders from your original ZIP fully reverts the UI; no
data or configuration migration is involved.
