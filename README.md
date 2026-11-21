# BILLEE Air8 Charger Tool

Control an **ISDT Air8** battery charger from a laptop browser, without its front
buttons or the ISDLink phone app. It sets up and starts charge jobs with every
option the charger has, and shows live pack voltage, current, cell balance,
capacity and energy.

```
Browser ──HTTP/WS──▶ Node front end (:3000) ──proxy──▶ Python backend (:8765) ──Bluetooth LE──▶ Air8
                     UI + saved presets                 protocol, safety limits
```

> **Why Bluetooth and not USB?** The Air8's USB port is only known to carry
> firmware updates. Nobody has published charge-control commands for it. The
> Bluetooth protocol has been reverse-engineered by the community (see
> [Credits](#credits)), so this tool uses Bluetooth. If your laptop has no
> Bluetooth, any cheap USB Bluetooth 4.0+ dongle works.

## ⚠️ Read before charging

- **No remote stop.** No Bluetooth "stop" command for the Air8 is known. To stop a
  job, **unplug the battery** (main lead first, then the balance lead) or switch
  off the charger's power supply.
- **Job start is experimental on the Air8.** The 0xEA start-job command is
  documented for the Air8, but the community projects only confirmed it on other
  ISDT models. For your first test, use a small LiPo at a low current (≈1 A) and
  stay next to it. Check that the charger's own screen shows the settings you sent.
- The backend refuses unsafe settings (over-voltage cut-offs, too many cells, over
  20 A, storage mode on NiMH/Pb, and so on). The charger also enforces its own limits.
- Never charge unattended, and charge LiPos in a fire-safe bag or box.
- If the charger is connected to the ISDLink app on a phone, close the app. The
  charger accepts only one Bluetooth connection. Don't pair it in your OS Bluetooth
  settings either. The tool connects to it directly.

## Requirements

- Python 3.10+ with [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js 18+
- Bluetooth LE on the laptop (built-in or USB dongle). Runs on Windows, macOS and Linux.
  - Linux: BlueZ must be running (`systemctl status bluetooth`).
  - macOS: allow Bluetooth access for your terminal app when prompted.

## Quick start

```bash
cd BILLEE-air8-charger-tool/frontend
npm install
```

```bash
npm start          # real charger
```

Open <http://localhost:3000>, click **Scan**, then **Connect**.

To try the UI without hardware:

```bash
npm run sim
```

Run the parts separately (for example the backend on another machine):

```bash
cd backend && uv run air8-backend                # --sim, --port 8765, --host 127.0.0.1
```

```bash
cd frontend && BACKEND_URL=http://127.0.0.1:8765 npm run frontend
```

Both servers listen on `127.0.0.1` only, because anyone who can reach the API can
start a charge. Use `--host 0.0.0.0` (backend) or `HOST=0.0.0.0` (front end) only
on a network you trust.

## What you can set

| Setting | Options |
|---|---|
| Mode | Charge, Storage, Discharge, Destroy (full drain for disposal; needs confirmation) |
| Chemistry | LiPo, LiHV, ULiHV, LiIon, LiFe (1–8S) · Pb (1–12S) · NiMH/NiCd (1–16S) |
| Cells | Auto-detect or a fixed count |
| Current | 0.1–20 A. Enter amps directly, or pack mAh × C-rate |
| Cut-off | Per-cell target voltage, range-checked per chemistry. NiMH charge uses delta-peak in mV (3–12) |
| Capacity limit | Stop after N mAh (0 = off) |

Default per-cell voltages: LiPo 4.20 · LiHV 4.35 · ULiHV 4.45 · LiIon 4.10 · LiFe 3.60 · Pb 2.40.
Storage: LiPo/LiHV 3.85 · ULiHV 3.90 · LiIon 3.75 · LiFe 3.30. Limits live in
`backend/src/air8_backend/limits.py`.

**Presets** save a battery's settings (for example "Rover 6S 22Ah") to
`frontend/data/presets.json`, so the whole team uses the same profile.

## Layout

```
backend/                     Python (FastAPI + bleak)
  src/air8_backend/
    protocol.py              packet builders/parsers (pure, unit-tested)
    limits.py                per-chemistry limits + job validation
    device.py                BLE connection, polling, simulator
    server.py                REST + WebSocket API, CLI entry point
  tests/                     pytest suite (protocol, limits, API via simulator)
frontend/                    Node (Express)
  server.js                  static UI, presets API, proxy to backend
  start.js                   launches backend + front end together
  public/                    UI (vanilla JS, no build step)
```

Run the tests with `cd backend && uv run pytest`.

### API (backend)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/scan?timeout=5` | find nearby ISDT chargers |
| POST | `/api/connect` `{address}` | connect, bind, start telemetry |
| POST | `/api/disconnect` | |
| GET | `/api/state` | connection + latest readings |
| GET | `/api/limits` | chemistry limits for the UI |
| POST | `/api/job/validate` | check a job and preview its packet |
| POST | `/api/job/start` | validate and send a job |
| POST | `/api/alarm` `{enabled}` | charger beeper on/off |
| POST | `/api/raw` `{characteristic, hex}` | send raw bytes (only with `--allow-raw`) |
| WS | `/ws` | live `electric`, `work_state`, `status`, `log`, `rx`/`tx` events |

## Troubleshooting

- **Scan finds nothing:** close ISDLink on every phone nearby, power-cycle the
  charger and scan again. On Linux, check that `bluetoothctl show` lists a powered controller.
- **"Bound: no reply":** normal on some firmware. Telemetry still works.
- **Cell voltages missing:** connect the balance lead.
- **Telemetry stops:** the backend logs "Charger stopped answering". Disconnect and reconnect.
- **The job was accepted but the charger does something different:** stop it by
  unplugging, then file an issue with the **Show raw packets** log. That's how the Air8
  protocol gets verified.

## Helping finish the protocol (stop command, state names)

1. On an Android phone, turn on **Developer options → Enable Bluetooth HCI snoop log**.
2. In ISDLink, start a job, then **stop** it from the app.
3. Pull the log (`adb bugreport`) and open `btsnoop_hci.log` in Wireshark with the
   filter `btatt`.
4. Look for writes to handle AF01. The stop command is the write that isn't
   `12 E4`, `13 E6` or `13 EA`.
5. Test candidate bytes with `uv run air8-backend --allow-raw` and the raw-send box
   in the Log panel, then add the command to `protocol.py`.

## Credits

The protocol is based on these MIT-licensed reverse-engineering projects:

- [mtheli/isdt_air_ble](https://github.com/mtheli/isdt_air_ble): Air8 discovery,
  telemetry layout, Air8 state and battery-type tables
- [DittelHome/ISDT-Charge-Utility](https://github.com/DittelHome/ISDT-Charge-Utility):
  the 0xEA work-task (job start) command

Not affiliated with or endorsed by ISDT.
