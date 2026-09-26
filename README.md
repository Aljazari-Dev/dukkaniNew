# Al Jazari Kebbi backend

This replaces the perfume/OpenAI chat layer with:

- Gemini Live ephemeral-token bootstrap for the Kebbi Android client.
- Small password-protected Al Jazari content dashboard.
- Existing Socket.IO call signaling / WebRTC routing.
- Existing remote movement protocol.

## Render environment variables

Required:

- `GEMINI_API_KEY` — Google Gemini API key (server only).
- `ADMIN_USERNAME` — dashboard username.
- `ADMIN_PASSWORD` — dashboard password.
- `FLASK_SECRET_KEY` — long random value.
- `ROBOT_API_KEY` — must match `Config.ROBOT_API_KEY` in the Android app.
- `DATA_DIR=/var/data` — recommended when using a Render persistent disk.

Optional:

- `GEMINI_LIVE_MODEL=gemini-3.8-live`
- `COOKIE_SECURE=1`

Start command:

`gunicorn -k eventlet -w 1 kebbicall:app`

Dashboard: `/dashboard`
Health: `/ping`
Robot bootstrap: `/api/robot/bootstrap`

The photo activity remains on its existing `photo-enh.onrender.com/send-image` endpoint and is intentionally not changed here.
