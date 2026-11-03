# Door opening alarms

Frigate's native CPU state classification distinguishes `closed` and `open` on
a private camera crop. Configure a one-second interval and motion triggers.
Collect real examples of both states in Classification → States and train the
model before treating the alarm as operational. Include people passing a closed
door, partial occlusion, and different lighting in the validation set. A zone or
person detection alone does not establish that the door opened.

`python -m ocr.frigate_state_notify --directory <private directory>` runs on the
Windows notification host. Install `websocket-client` there. Its private
`runtime.local.json` contains `camera`, `model`, `label`, verified HTTPS
`frigate_url`, `api_user`, `api_password`, `python`, the existing Codex bot
`helper` path and `owner_chat`. The worker reads only the exact
`<camera>/classification/<model>` topic from Frigate's authenticated WSS stream.
MQTT does not need to be enabled. Keep this file accessible
only to the worker account and administrators. Never put credentials or camera
images in Git. The worker does not contact a vault during startup.

Run a single persistent scheduled task as the account owning the existing bot
runtime, with an at-logon trigger and restart on failure. The recorder gets no
Telegram token. Each fresh closed-to-open transition creates a separate
`door_open` Frigate event with recording, ten seconds before and thirty seconds
after its trigger, then sends the owner a clip link using `tg_bot.py` with
identity and delivery readback. Recording retention follows the recorder's
policy. No alarm cooldown drops successive door openings. Initial open states,
duplicate states and reconnects do not invent openings. A known door state is
retained across worker restarts; its first-ever observation must establish a
closed baseline before an opening alarm can fire.

`--test` creates a clearly labelled `door_open_test` recording and sends a test
message. This verifies the recording and delivery path, not visual recognition.
Inspect the exact event, clip and verified Telegram message. A single camera
cannot establish openings hidden from view or occurring entirely between
sampled frames. After installing/training, validate a real open-close cycle.

`state.json` journals transitions before network writes. Ambiguous event or
Telegram POSTs are kept as `started`/`unknown` and never blindly repeated. Check
these markers and `stream_connected`/`heartbeat` when diagnosing delivery. Worker
restarts resume unsent queued incidents but do not replay attempted sends.
