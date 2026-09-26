# Al Jazari Kebbi Backend — Gemini Live v1.1

Roles:
- Admin dashboard and Al Jazari content storage.
- Fresh Gemini Live ephemeral-token provisioning for the robot.
- Existing Socket.IO/WebRTC call signaling.
- Existing remote movement signaling.

The backend no longer uses the old perfume/OpenAI chat/TTS conversation path.

## Required Render environment variables
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `FLASK_SECRET_KEY`
- `GEMINI_API_KEY`
- `GEMINI_LIVE_MODEL` (recommended current value: `gemini-3.8-live`)
- `ROBOT_API_KEY`
- `DATA_DIR=/var/data`
- `COOKIE_SECURE=1`

## Ephemeral token change in v1.1
Token provisioning intentionally uses the minimal one-use token payload (`uses`, `expireTime`, `newSessionExpireTime`). The Live model/config is sent by the robot in the WebSocket setup message. This avoids the `Unknown name "liveConnectConstraints"` provisioning failure seen on the deployed service.
