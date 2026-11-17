# Wiegand Simulator — Raspberry Pi controller

Run the Arduino Wiegand swipe simulator from anywhere. The Uno stays wired to the panel exactly as before and plugs into a Raspberry Pi by USB. The Pi runs the tests, keeps the time, runs scheduled tests, logs every swipe, and serves a web dashboard.

```
 Your PC / phone ── Tailscale ──► Raspberry Pi ── USB ──► Arduino Uno + ScrewShield ── D0/D1 ──► AC-825IP
                                  (dashboard,               (wiegand-agent firmware:
                                   schedule, logs)            puts pulses on the wires)
```

## Parts

| Item | Notes |
|---|---|
| Raspberry Pi 4 Model B (2 GB is plenty) or Pi 5 | A Pi Zero 2 W also works, but needs a micro-USB OTG adapter for the Uno cable |
| Official Pi power supply | USB-C 15 W (Pi 4) or 27 W (Pi 5) |
| microSD card, 16–32 GB | |
| USB A-to-B cable | The one you already use for the Uno |
| Network | Ethernet is most reliable; Wi-Fi is fine |

## 1. Prepare the SD card (on your PC)

1. Install **Raspberry Pi Imager** from raspberrypi.com/software.
2. Choose your Pi model, then **Raspberry Pi OS Lite (64-bit)**, then the SD card.
3. Click **Edit settings** (or Next → Edit settings) and set:
   - Hostname: `wiegand-sim`
   - User name and password (remember these)
   - Wi-Fi network and country, if not using Ethernet
   - Time zone: `America/Indiana/Indianapolis` (schedules use the Pi's local time)
   - Services tab: **Enable SSH**, use password authentication
4. Write the card, put it in the Pi, connect network and power. Give it 2 minutes on the first boot.

## 2. Connect to the Pi

From Windows Terminal or PowerShell on a PC on the same network:

```
ssh <your-user>@wiegand-sim.local
```

## 3. Install the controller

```
sudo apt-get install -y git
git clone https://github.com/qxr-repository/wiegand-swipe-simulator.git
cd wiegand-swipe-simulator/pi
bash install.sh
```

## 4. Load the firmware onto the Uno

Plug the Uno into the Pi by USB, then:

```
bash flash-uno.sh
```

This replaces Larry's sketch with the `wiegand-agent` firmware, which takes commands from the Pi. (You can also open `wiegand-agent/wiegand-agent.ino` in the Arduino IDE on a PC and upload it there.)

Open `http://wiegand-sim.local:8080` in a browser on the same network. The top bar should show **Uno connected (fw 1.0)**.

## 5. Reach it from anywhere (Tailscale)

Don't forward ports on the router: this box can make doors unlock. Use Tailscale, a free private network between your own devices.

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the link it prints and sign in. Install Tailscale on your PC and phone with the same account. Then the dashboard is at `http://wiegand-sim:8080` from anywhere. Check with IT before putting it on a company network.

## 6. Set a password (recommended)

```
bash set-password.sh
```

The user name is `admin`.

## Using it

- **Run**: pick a test, optionally a time limit, and Start. **Stop test** is always in the top bar.
- **Quick swipe**: send one card and/or PIN on any reader, for remote troubleshooting.
- **Tests**: edit the built-in tests or make your own (JSON, with a field reference on the page). Built in: Larry's original, Normal load, Stress, Soak (count check), Walk every card, Schedule edges (M-F 8-5), APB single door / two doors / timed, Card + PIN basics / lockout, Bad frames.
- **Schedule**: start any test at set times and days, for a set number of minutes. For example, Stress at 02:00 for 60 minutes every night, or Schedule edges every weekday from 07:50 for 600 minutes.
- **Logs**: one CSV per run (plus one per day for quick swipes), to compare with the Qumulex event log.
- **Settings**: reader labels, Wiegand timing, automatic pass/fail.

## Automatic pass/fail (optional)

Wire each door's lock relay (dry contact: COM and NO) between an Uno input and GND:

| Uno pin | Input |
|---|---|
| A0 | 1 |
| A1 | 2 |
| A2 | 3 |
| A3 | 4 |

Use only the relay's dry contacts, never the lock power. In **Settings**, turn on automatic pass/fail and pick which input belongs to each reader's door. Sequence and bad-frame steps that expect GRANT or DENY are then marked PASS or FAIL in the log, depending on whether the door unlocked within the wait time.

## Running on an existing Ubuntu server (e.g. a lab Qumulex server)

It works on Ubuntu 20.04, 22.04 and 24.04 (and Debian 11+). Skip steps 1–2, then follow steps 3–6.

- Load is small: one Python process using about 35 MB of RAM; under the Stress test (~12 swipes/s) it used under 1% of one CPU core in testing.
- Logs grow about 3 MB per hour under Stress and much less at normal rates. Logs older than 60 days are deleted automatically; change `log_keep_days` in `data/config.json` (0 = keep forever).
- Check that port 8080 is free first: `sudo ss -ltnp | grep 8080`. If it's taken, set `http_port` in `data/config.json` and restart with `sudo systemctl restart wiegand-sim`.
- If a firewall is on: `sudo ufw allow 8080/tcp`.
- On a Qumulex server, check with engineering before adding packages or Tailscale.
- Offline-buffer and gateway-reboot tests need the simulator on a separate machine, since rebooting the server also stops the simulator.

## Updating

```
cd ~/wiegand-swipe-simulator/pi
bash update.sh
```

Settings, tests, schedule and logs live in `pi/data/` and are kept.

## Troubleshooting

| Problem | Try |
|---|---|
| Dashboard won't load | `sudo systemctl status wiegand-sim` and `journalctl -u wiegand-sim -n 50` |
| "No Uno found on USB" | Check the cable; `ls /dev/serial/by-id/` should list an Arduino |
| "no reply to PING" | The Uno still has an old sketch: run `bash flash-uno.sh` |
| Swipes sent but panel shows nothing | Common ground, D0/D1 not swapped, reader port set to Wiegand 26-bit |

## Firmware commands (for reference)

The Pi talks to the Uno at 115200 baud. Every command gets one `OK` or `ERR` reply.

| Command | Does |
|---|---|
| `PING` | `OK PONG 1.0` |
| `CFG <pulse_us> <interval_us>` | Set Wiegand timing |
| `W <reader> <26\|34> <fc> <card>` | Send a card with correct parity |
| `K <reader> <4\|8> <keys> <gap_ms>` | Send keypad keys (0–9 * #) |
| `B <reader> <bits>` | Send a raw bit string (used for bad-frame tests) |
| `IN` | Read the feedback inputs |
| `EVT IN <n> <0\|1> <ms>` | Sent by the Uno when an input changes |
