# Al Jazari Kebbi Backend — Education Specialist v2.0

This backend keeps the existing Gemini Live bootstrap, WebRTC signaling and remote-movement protocol, but restructures Kebbi's managed knowledge for an **educational robotics specialist** role.

## What changed

- Kebbi's detailed scope is now limited to:
  - NAO
  - JetArm
  - JetAuto
  - UGOT
  - uKit Explore
  - Kebbi Air S
  - Unitree G1 EDU (on request)
  - Booster K1 Education (on request)
- Sales phone is a separate dashboard setting. Default: `07738903874`.
- Price questions follow a fixed flow: refer to Sales, ask whether the visitor wants the phone number, and only give the number after they ask/agree.
- Other Al Jazari robot families are brief-only:
  - PUDU delivery/cleaning -> refer to **Pepper** for details.
  - Humanoid/service robots -> refer to **Winno** for details.
- Dashboard is split into sections instead of one giant knowledge textarea.
- Each educational robot has independent fields for availability, overview, technical specs, educational capabilities, differentiator, customization and notes.
- Each educational robot can be disabled without deleting its stored text.
- A knowledge-preview page shows exactly what the structured knowledge compiler sends to Gemini.

## Existing Render data migration

This is schema version 2. On the first request after deploy, an existing v1 `/var/data/aljazari_content.json` is migrated automatically:

- company name, robot name, Gemini voice and greetings are preserved;
- the old generic knowledge textarea is saved into `legacy_knowledge_backup` for reference only;
- the new education-focused system behavior and structured robot database are installed;
- the old giant knowledge block is no longer injected into Gemini.

This prevents the old perfume/general-company knowledge from overriding the new educational role.

## Render

No new dependency is required. Keep the same commands:

```text
Build Command: pip install -r requirements.txt
Start Command: gunicorn -k eventlet -w 1 kebbicall:app
```

Keep the existing environment variables:

```text
GEMINI_API_KEY=...
GEMINI_LIVE_MODEL=...
ADMIN_USERNAME=...
ADMIN_PASSWORD=...
FLASK_SECRET_KEY=...
ROBOT_API_KEY=...
DATA_DIR=/var/data
COOKIE_SECURE=1
```

## Technical-spec research basis

The default robot data was prepared from manufacturer documentation/product pages available in October 2026. The editable dashboard remains the source of truth for what Kebbi says in production.

- NAO: Aldebaran/NAO6 datasheet and NAOqi documentation.
- JetArm / JetAuto: Hiwonder product pages and manuals.
- UGOT / uKit Explore: UBTECH product pages and education brochures.
- Kebbi Air S: NUWA hardware support and coding/SDK documentation.
- Unitree G1 EDU: Unitree official G1 parameter page.
- Booster K1 Education: Booster Robotics official K1 page/manual.
