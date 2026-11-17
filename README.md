# Wiegand Swipe Simulator — Config Tool

A browser tool that generates Arduino sketches for simulating Wiegand card reads into an access control panel (tested design target: Rosslare AC-825IP on Qumulex). Open `index.html` in a browser, pick a test, and copy the generated sketch into the Arduino IDE.

Based on Larry's `4ReadersAutomationStaticCard` sketch from the Rosslare support team.

## Hardware

- Arduino Uno + ScrewShield 1.0
- Up to 4 simulated readers

| Reader | D0 (green) | D1 (white) |
|---|---|---|
| R1 | Pin 2 | Pin 3 |
| R2 | Pin 4 | Pin 5 |
| R3 | Pin 6 | Pin 7 |
| R4 | Pin 8 | Pin 9 |

Tie Uno GND to the panel's reader ground. Uno pins drive 5 V / 0 V directly; check the panel's D0/D1 idle voltage before connecting, and add a buffer if it is above 5 V.

## Test modes

- **Load / stress** — random or sequential swipes at a set rate across the readers. Presets: Larry's original, Normal (1–2 s), Stress (10–50 ms), Walk every card, One card to all readers.
- **Schedule edges** — swipes a set of test cards on a fixed beat before and after each schedule start/end time, to check that access changes at the right minute. The Uno has no clock: send a clock line (`T Fri 16:55:00`) in the Serial Monitor after opening it. The tool shows the current line to copy.
- **Anti-passback** — scripted In/Out sequences (single door, two doors/area, timed APB, several cards) with the expected result printed for each step.
- **Card + PIN** — card then keypad PIN as 4-bit or 8-bit bursts (correct/wrong PIN, card only, PIN only, lockout, duress).

Shared settings: 26-bit (H10301) or 34-bit format, pulse width, bit interval, serial baud and log level.

## Notes

- Serial Monitor line ending must be **Newline** for the schedule and sequence modes.
- Opening the Serial Monitor resets the Uno.
- The Uno's timing can drift several seconds an hour; re-send the clock line before an important schedule edge.
- PINs and expiry dates are not imported by the Qumulex cardholder importer; set them in Qumulex by hand for the related tests.
- Confirm the keypad format (4-bit or 8-bit burst) Qumulex expects for the door.

## Remote control with a Raspberry Pi

The `pi/` folder turns the rig into a remote test station: the Uno plugs into a Raspberry Pi by USB, and the Pi runs tests, schedules, logs and a web dashboard you can reach from anywhere over Tailscale. See [pi/README.md](pi/README.md) for parts and setup.
