#!/usr/bin/env python3
"""
QXR Wiegand Swipe Simulator - Raspberry Pi controller

Talks to the Uno (running wiegand-agent firmware) over USB, runs tests, keeps the
clock and the schedule, writes CSV logs and serves a web dashboard.

Run:  python3 app.py            (normally started by the systemd service)
Data: ./data  (config.json, tests.json, jobs.json, logs/)
"""
import collections
import csv
import datetime as dt
import glob
import hmac
import json
import os
import queue
import random
import re
import socket
import threading
import time

import serial
from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("WIEGAND_DATA", os.path.join(BASE, "data"))
LOGDIR = os.path.join(DATA, "logs")
CONFIG_FILE = os.path.join(DATA, "config.json")
TESTS_FILE = os.path.join(DATA, "tests.json")
JOBS_FILE = os.path.join(DATA, "jobs.json")
os.makedirs(LOGDIR, exist_ok=True)

DEFAULT_CONFIG = {
    "serial_port": "auto",          # or e.g. /dev/ttyACM0
    "baud": 115200,
    "http_port": 8080,
    "password": "",                 # set to require a password (user name: admin)
    "pulse_us": 40,
    "interval_us": 2000,
    "reader_labels": ["Door 1 In", "Door 1 Out", "Door 2 In", "Door 2 Out"],
    "feedback": {                   # automatic pass/fail from the panel's lock relays
        "enabled": False,
        "reader_inputs": [1, 1, 2, 2],   # which Uno input (A0=1 .. A3=4) shows each reader's door unlocking
        "window_ms": 3000,
    },
}


# ---------------------------------------------------------------- storage helpers

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(default))


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def merge(default, saved):
    out = json.loads(json.dumps(default))
    for k, v in (saved or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


CFG = merge(DEFAULT_CONFIG, load_json(CONFIG_FILE, {}))
if not os.path.exists(CONFIG_FILE):
    save_json(CONFIG_FILE, CFG)


def now():
    return dt.datetime.now()


def iso(t=None):
    return (t or now()).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- event hub (live log)

class Hub:
    def __init__(self):
        self.recent = collections.deque(maxlen=500)
        self.subs = set()
        self.lock = threading.Lock()

    def emit(self, kind, text, **fields):
        rec = {"t": iso(), "kind": kind, "text": text, **fields}
        with self.lock:
            self.recent.append(rec)
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(rec)
            except queue.Full:
                pass
        print(f"[{rec['t']}] {kind}: {text}", flush=True)
        return rec

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


HUB = Hub()


# ---------------------------------------------------------------- Uno serial link

class UnoError(Exception):
    pass


class Uno:
    def __init__(self, hub):
        self.hub = hub
        self.ser = None
        self.port = None
        self.fw = None
        self.status = "starting"
        self.inputs = [0, 0, 0, 0]
        self.cmd_lock = threading.Lock()
        self.replies = queue.Queue()
        self.input_listeners = []
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def connected(self):
        return self.ser is not None and self.fw is not None

    def _find_port(self):
        if CFG["serial_port"] != "auto":
            return CFG["serial_port"] if os.path.exists(CFG["serial_port"]) else None
        for pattern in ("/dev/serial/by-id/*Arduino*", "/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*"):
            found = sorted(glob.glob(pattern))
            if found:
                return found[0]
        return None

    def _readline(self, ser):
        raw = ser.readline()
        return raw.decode("ascii", "replace").strip() if raw else None

    def _handshake(self, ser):
        time.sleep(2.2)                      # opening the port resets the Uno
        ser.reset_input_buffer()
        for _ in range(3):
            ser.write(b"PING\n")
            end = time.time() + 2
            while time.time() < end:
                line = self._readline(ser)
                if line and line.startswith("OK PONG"):
                    return line.split()[-1]
        return None

    def _apply_cfg(self, ser):
        ser.write(f"CFG {CFG['pulse_us']} {CFG['interval_us']}\n".encode())
        end = time.time() + 2
        while time.time() < end:
            line = self._readline(ser)
            if line and (line.startswith("OK CFG") or line.startswith("ERR")):
                return line

    def _run(self):
        last_msg = None
        while True:
            port = self._find_port()
            if not port:
                self.status = "No Uno found on USB"
                if last_msg != self.status:
                    self.hub.emit("sys", self.status)
                    last_msg = self.status
                time.sleep(3)
                continue
            ser = None
            try:
                ser = serial.Serial(port, CFG["baud"], timeout=0.2)
                self.status = "connecting"
                fw = self._handshake(ser)
                if not fw:
                    raise UnoError("no reply to PING - is the wiegand-agent firmware loaded?")
                cfg_reply = self._apply_cfg(ser)
                self.ser, self.port, self.fw = ser, port, fw
                self.status = "connected"
                last_msg = None
                self.hub.emit("sys", f"Uno connected on {port}, firmware {fw} ({cfg_reply})")
                while True:
                    line = self._readline(ser)
                    if line is None:
                        continue
                    if line.startswith("OK") or line.startswith("ERR"):
                        self.replies.put(line)
                    elif line.startswith("EVT IN"):
                        _, _, n, v, ms = line.split()
                        n, v = int(n), int(v)
                        self.inputs[n - 1] = v
                        self.hub.emit("input", f"Input {n} {'closed' if v else 'open'}", input=n, state=v)
                        for cb in list(self.input_listeners):
                            cb(n, v, time.time())
                    elif line.startswith("EVT READY"):
                        self.hub.emit("sys", "Uno restarted")
                        self._apply_cfg_async()
            except Exception as e:  # noqa: BLE001
                msg = f"Uno link problem: {e}"
                if last_msg != msg:
                    self.hub.emit("error", msg)
                    last_msg = msg
                self.status = str(e)
            finally:
                try:
                    if ser:
                        ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ser, self.fw = None, None
            time.sleep(3)

    def _apply_cfg_async(self):
        def go():
            try:
                self.command(f"CFG {CFG['pulse_us']} {CFG['interval_us']}")
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=go, daemon=True).start()

    def command(self, cmd, timeout=5.0):
        with self.cmd_lock:
            if not self.connected:
                raise UnoError("Uno not connected")
            while not self.replies.empty():
                self.replies.get_nowait()
            self.ser.write((cmd + "\n").encode())
            try:
                reply = self.replies.get(timeout=timeout)
            except queue.Empty:
                raise UnoError(f"no reply to {cmd.split()[0]}")
            if reply.startswith("ERR"):
                raise UnoError(reply[4:])
            return reply


UNO = Uno(HUB)


# ---------------------------------------------------------------- Wiegand helpers

def frame_bits(fmt, fc, card):
    """Return the full Wiegand frame as a 0/1 string (with parity)."""
    fbits = 8 if fmt == 26 else 16
    data = format(fc, f"0{fbits}b") + format(card, "016b")
    half = len(data) // 2
    even = data[:half].count("1") % 2
    odd = 1 if data[half:].count("1") % 2 == 0 else 0
    return f"{even}{data}{odd}"


def frame_ms(nbits):
    return nbits * (CFG["pulse_us"] + CFG["interval_us"]) / 1000.0


def reader_label(r):
    labels = CFG.get("reader_labels") or []
    return labels[r - 1] if 0 < r <= len(labels) and labels[r - 1] else f"R{r}"


DAY_SETS = {"all": {0, 1, 2, 3, 4, 5, 6}, "wd": {0, 1, 2, 3, 4}, "we": {5, 6}}   # Python weekday(): Mon=0
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def day_matches(rule, d):
    rule = str(rule)
    if rule in DAY_SETS:
        return d in DAY_SETS[rule]
    if rule in DAY_NAMES:
        return DAY_NAMES.index(rule) == d
    return False


# ---------------------------------------------------------------- test runner

class StopTest(Exception):
    pass


LOG_FIELDS = ["time", "test", "step", "reader", "reader_label", "format", "fc", "card", "pin",
              "expect", "note", "result", "verdict", "uno_ms"]


class Runner:
    def __init__(self, uno, hub):
        self.uno, self.hub = uno, hub
        self.thread = None
        self.stop_ev = threading.Event()
        self.state = None
        self.csv_file = None
        self.csv = None
        self.lock = threading.Lock()
        self.input_events = collections.deque(maxlen=200)
        uno.input_listeners.append(lambda n, v, t: self.input_events.append((n, v, t)))

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def status(self):
        if not self.running or not self.state:
            return {"running": False}
        s = dict(self.state)
        s["running"] = True
        return s

    def start(self, name, test, duration_min=None, source="dashboard"):
        with self.lock:
            if self.running:
                raise RuntimeError(f"'{self.state['test']}' is already running")
            self.stop_ev.clear()
            until = now() + dt.timedelta(minutes=float(duration_min)) if duration_min else None
            self.state = {"test": name, "type": test.get("type"), "started": iso(),
                          "until": iso(until) if until else None, "swipes": 0,
                          "passed": 0, "failed": 0, "source": source}
            self.until = until
            self.thread = threading.Thread(target=self._run, args=(name, test), daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_ev.set()

    # -- plumbing

    def _open_csv(self, name):
        slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40] or "test"
        path = os.path.join(LOGDIR, f"{now():%Y%m%d-%H%M%S}_{slug}.csv")
        self.csv_file = open(path, "w", newline="")
        self.csv = csv.DictWriter(self.csv_file, fieldnames=LOG_FIELDS)
        self.csv.writeheader()
        return os.path.basename(path)

    def _close_csv(self):
        if self.csv_file:
            self.csv_file.close()
        self.csv_file = self.csv = None

    def write_row(self, row):
        if self.csv:
            self.csv.writerow({k: row.get(k, "") for k in LOG_FIELDS})
            self.csv_file.flush()

    def sleep(self, seconds):
        end = time.time() + max(0.0, seconds)
        while True:
            if self.stop_ev.is_set():
                raise StopTest("stopped")
            if self.until and now() >= self.until:
                raise StopTest("time limit reached")
            left = end - time.time()
            if left <= 0:
                return
            self.stop_ev.wait(min(left, 0.25))

    def check(self):
        self.sleep(0)

    # -- one swipe (card and/or PIN), logged

    def swipe(self, reader, fc=None, card=None, pin="", fmt=26, pin_fmt=4, key_gap_ms=300,
              card_pin_gap_ms=1000, end_hash=False, step="", expect="", note="", test_name=None):
        uno_ms = ""
        in_runner = threading.current_thread() is self.thread
        pause = self.sleep if in_runner else time.sleep
        t_send = time.time()
        if card is not None and card != "":
            reply = self.uno.command(f"W {reader} {fmt} {int(fc)} {int(card)}", timeout=3 + frame_ms(fmt) / 1000)
            uno_ms = reply.split()[-1]
        keys = (pin or "") + ("#" if pin and end_hash else "")
        if keys:
            if card is not None and card != "":
                pause(card_pin_gap_ms / 1000.0)
            t = 3 + len(keys) * (key_gap_ms + frame_ms(pin_fmt)) / 1000
            reply = self.uno.command(f"K {reader} {pin_fmt} {keys} {int(key_gap_ms)}", timeout=t)
            uno_ms = reply.split()[-1]
        t_done = time.time()
        if in_runner and self.state is not None:
            self.state["swipes"] += 1
        row = {"time": iso(), "test": test_name or (self.state or {}).get("test", "manual"), "step": step,
               "reader": reader, "reader_label": reader_label(reader), "format": fmt,
               "fc": "" if card in (None, "") else fc, "card": "" if card in (None, "") else card,
               "pin": pin or "", "expect": expect, "note": note, "uno_ms": uno_ms}
        return row, t_send, t_done

    def judge(self, row, reader, t_from):
        """Fill result/verdict from the feedback inputs, if enabled."""
        fb = CFG.get("feedback", {})
        exp = row.get("expect")
        if not fb.get("enabled") or exp not in ("GRANT", "DENY"):
            return
        inputs = fb.get("reader_inputs") or [1, 1, 2, 2]
        n = inputs[reader - 1] if reader - 1 < len(inputs) else 1
        deadline = t_from + fb.get("window_ms", 3000) / 1000.0
        granted = False
        while time.time() < deadline and not granted:
            granted = any(e[0] == n and e[1] == 1 and e[2] >= t_from for e in list(self.input_events))
            if not granted:
                self.sleep(0.05)
        row["result"] = "GRANT" if granted else "DENY"
        row["verdict"] = "PASS" if row["result"] == exp else "FAIL"
        if self.state is not None:
            self.state["passed" if row["verdict"] == "PASS" else "failed"] += 1

    def log_swipe(self, row):
        parts = [f"R{row.get('reader')} ({row.get('reader_label', '')})"]
        if row.get("card", "") != "":
            parts.append(f"{row.get('fc')}-{row.get('card')}")
        if row.get("pin"):
            parts.append(f"PIN {row['pin']}")
        if row.get("expect"):
            parts.append(f"expect {row['expect']}")
        if row.get("result"):
            parts.append(f"got {row['result']} → {row['verdict']}")
        if row.get("note"):
            parts.append(f"— {row['note']}")
        prefix = f"[{row['step']}] " if row.get("step") != "" else ""
        kind = "fail" if row.get("verdict") == "FAIL" else "pass" if row.get("verdict") == "PASS" else "swipe"
        self.hub.emit(kind, prefix + "  ".join(parts), row=row)
        self.write_row(row)

    # -- main thread

    def _run(self, name, test):
        fname = self._open_csv(name)
        self.hub.emit("start", f"Started '{name}' ({test.get('type')})"
                      + (f", until {self.until:%H:%M}" if self.until else "") + f" → log {fname}")
        reason = "finished"
        try:
            kind = test.get("type")
            fn = {"load": self.run_load, "sequence": self.run_sequence,
                  "schedule_edges": self.run_schedule_edges, "bad_frames": self.run_bad_frames}.get(kind)
            if not fn:
                raise ValueError(f"unknown test type '{kind}'")
            fn(name, test)
        except StopTest as e:
            reason = str(e)
        except Exception as e:  # noqa: BLE001
            reason = f"error: {e}"
            self.hub.emit("error", f"'{name}' stopped: {e}")
        finally:
            self._close_csv()
            s = self.state or {}
            extra = f", {s.get('passed', 0)} pass / {s.get('failed', 0)} fail" if (s.get("passed") or s.get("failed")) else ""
            self.hub.emit("end", f"'{name}' ended ({reason}): {s.get('swipes', 0)} swipes{extra}")

    # -- test types

    def run_load(self, name, t):
        rng = random.Random(t.get("seed")) if t.get("seed") not in (None, "", 0) else random.Random()
        readers = [int(r) for r in t.get("readers", [1, 2, 3, 4])] or [1]
        fmt = int(t.get("format", 26))
        cmin, cmax = int(t.get("card_min", 1000)), int(t.get("card_max", 1999))
        seq_card = cmin
        rr = 0
        count = 0
        max_swipes = int(t.get("max_swipes", 0))
        dmin, dmax = int(t.get("delay_min_ms", 500)), int(t.get("delay_max_ms", 500))
        while True:
            self.check()
            if max_swipes and count >= max_swipes:
                return
            mode = t.get("reader_mode", "random")
            if mode == "random":
                targets = [rng.choice(readers)]
            elif mode == "rr":
                targets = [readers[rr % len(readers)]]
                rr += 1
            else:
                targets = readers
            cm = t.get("card_mode", "random")
            if cm == "random":
                card = rng.randint(cmin, cmax)
            elif cm == "seq":
                card = seq_card
                seq_card = cmin if seq_card >= cmax else seq_card + 1
            else:
                card = int(t.get("card", cmin))
            if t.get("fc_mode", "fixed") == "random":
                fc = rng.randint(int(t.get("fc_min", 0)), int(t.get("fc_max", 255)))
            else:
                fc = int(t.get("fc", 185))
            count += 1
            for r in targets:
                row, _, _ = self.swipe(r, fc, card, fmt=fmt, step=count)
                self.log_swipe(row)
                self.sleep(rng.randint(min(dmin, dmax), max(dmin, dmax)) / 1000.0)

    def run_sequence(self, name, t):
        steps = t.get("steps", [])
        repeat = int(t.get("repeat", 1))
        fmt = int(t.get("format", 26))
        opts = dict(fmt=fmt, pin_fmt=int(t.get("pin_format", 4)), key_gap_ms=int(t.get("key_gap_ms", 300)),
                    card_pin_gap_ms=int(t.get("card_pin_gap_ms", 1000)), end_hash=bool(t.get("end_hash", False)))
        run = 0
        while repeat == 0 or run < repeat:
            run += 1
            self.hub.emit("sys", f"Run {run}" + (f" of {repeat}" if repeat else ""))
            for i, s in enumerate(steps, 1):
                self.check()
                reader = int(s.get("reader", 1))
                exp = {"G": "GRANT", "D": "DENY", "?": "DEPENDS"}.get(str(s.get("expect", "")).upper()[:1], "")
                if reader == 0 or s.get("wait_only"):
                    self.hub.emit("sys", f"[{run}.{i}] wait {s.get('wait_s', 0)} s — {s.get('note', '')}")
                    self.sleep(float(s.get("wait_s", 0)))
                    continue
                card = s.get("card")
                card = None if card in (None, "") else int(card)
                row, t_send, _ = self.swipe(reader, int(s.get("fc", 185)), card, pin=str(s.get("pin") or ""),
                                            step=f"{run}.{i}", expect=exp, note=s.get("note", ""), **opts)
                self.judge(row, reader, t_send)
                self.log_swipe(row)
                waited = time.time() - t_send
                self.sleep(max(0.0, float(s.get("wait_s", 3)) - waited))
            if repeat == 0 or run < repeat:
                self.sleep(float(t.get("run_gap_s", 10)))

    def run_schedule_edges(self, name, t):
        W = float(t.get("window_min", 2)) * 60
        every = max(1.0, float(t.get("every_s", 15)))
        gap = int(t.get("card_gap_ms", 1500)) / 1000.0
        reader = int(t.get("reader", 1))
        fmt = int(t.get("format", 26))
        cards = t.get("cards", [])
        edges = t.get("edges", [])
        done = set()
        self.hub.emit("sys", "Watching schedule times: " + ", ".join(f"{e.get('days')} {e.get('time')}" for e in edges))
        while True:
            self.check()
            n = now()
            for e in edges:
                hh, mm = [int(x) for x in str(e.get("time", "00:00")).split(":")[:2]]
                for day_off in (-1, 0, 1):
                    d = (n + dt.timedelta(days=day_off)).date()
                    if not day_matches(e.get("days", "all"), d.weekday()):
                        continue
                    edge_t = dt.datetime.combine(d, dt.time(hh, mm))
                    diff = (n - edge_t).total_seconds()
                    k = int((diff + W) // every)
                    if -W <= diff < W + every and k * every <= 2 * W:
                        key = (edge_t, k)
                        if key not in done:
                            done.add(key)
                            off = -W + k * every
                            label = f"{'+' if off >= 0 else '-'}{int(abs(off)) // 60}:{int(abs(off)) % 60:02d} from {DAY_NAMES[edge_t.weekday()]} {edge_t:%H:%M}"
                            for i, c in enumerate(cards):
                                row, _, _ = self.swipe(reader, int(c.get("fc", 185)), int(c.get("card")), fmt=fmt,
                                                       step=label, note=c.get("label", ""))
                                self.log_swipe(row)
                                if i < len(cards) - 1:
                                    self.sleep(gap)
            if len(done) > 5000:
                done = {k for k in done if k[0] > n - dt.timedelta(days=2)}
            self.sleep(0.2)

    def run_bad_frames(self, name, t):
        reader = int(t.get("reader", 1))
        fc, card = int(t.get("fc", 185)), int(t.get("card", 1020))
        wait = float(t.get("wait_s", 3))
        repeat = int(t.get("repeat", 1))
        good = frame_bits(26, fc, card)
        flip = lambda s, i: s[:i] + ("1" if s[i] == "0" else "0") + s[i + 1:]  # noqa: E731
        frames = [
            ("Good frame (control)", good, "GRANT"),
            ("Even parity wrong", flip(good, 0), "DENY"),
            ("Odd parity wrong", flip(good, 25), "DENY"),
            ("One data bit flipped", flip(good, 12), "DENY"),
            ("25 bits (last bit missing)", good[:25], "DENY"),
            ("27 bits (extra bit)", good + "0", "DENY"),
            ("All zeros, 26 bits", "0" * 26, "DENY"),
            ("All ones, 26 bits", "1" * 26, "DENY"),
            ("Single noise pulse", "1", "DENY"),
            ("4-bit noise burst", "1010", "DENY"),
            ("Good frame again (recovery)", good, "GRANT"),
        ]
        for run in range(1, repeat + 1):
            for i, (label, bits, exp) in enumerate(frames, 1):
                self.check()
                t_send = time.time()
                reply = self.uno.command(f"B {reader} {bits}", timeout=3 + frame_ms(len(bits)) / 1000)
                self.state["swipes"] += 1
                row = {"time": iso(), "test": name, "step": f"{run}.{i}", "reader": reader,
                       "reader_label": reader_label(reader), "format": len(bits), "fc": "", "card": "", "pin": "",
                       "expect": exp, "note": f"{label}: {bits}", "uno_ms": reply.split()[-1]}
                self.judge(row, reader, t_send)
                self.log_swipe(row)
                self.sleep(max(0.0, wait - (time.time() - t_send)))


RUNNER = Runner(UNO, HUB)


# ---------------------------------------------------------------- saved tests and schedule

DEFAULT_TESTS = {
    "Larry's original": {"type": "load", "description": "FC 185, random cards 1000-1999, random reader, 500 ms apart. Runs until stopped.",
                         "readers": [1, 2, 3, 4], "reader_mode": "random", "card_mode": "random", "card_min": 1000, "card_max": 1999,
                         "fc": 185, "delay_min_ms": 500, "delay_max_ms": 500, "seed": 9956, "format": 26},
    "Normal load": {"type": "load", "description": "One swipe every 1-2 s on a random reader, cards 1000-2049 (includes unknown cards).",
                    "readers": [1, 2, 3, 4], "reader_mode": "random", "card_mode": "random", "card_min": 1000, "card_max": 2049,
                    "fc": 185, "delay_min_ms": 1000, "delay_max_ms": 2000, "format": 26},
    "Stress": {"type": "load", "description": "10-50 ms between swipes (about 12 a second). Tests the panel's buffer and event upload.",
               "readers": [1, 2, 3, 4], "reader_mode": "random", "card_mode": "random", "card_min": 1000, "card_max": 2049,
               "fc": 185, "delay_min_ms": 10, "delay_max_ms": 50, "format": 26},
    "Soak (count check)": {"type": "load", "description": "Normal rate with a fixed number of swipes, so you can compare the count with Qumulex events.",
                           "readers": [1, 2, 3, 4], "reader_mode": "rr", "card_mode": "random", "card_min": 1000, "card_max": 1980,
                           "fc": 185, "delay_min_ms": 1000, "delay_max_ms": 2000, "max_swipes": 10000, "format": 26},
    "Walk every card": {"type": "load", "description": "Every card 1000-2049 once, in order, round robin, 1 s apart.",
                        "readers": [1, 2, 3, 4], "reader_mode": "rr", "card_mode": "seq", "card_min": 1000, "card_max": 2049,
                        "fc": 185, "delay_min_ms": 1000, "delay_max_ms": 1000, "max_swipes": 1050, "format": 26},
    "Schedule edges (M-F 8-5)": {"type": "schedule_edges", "description": "Swipes one card per test-DB group every 15 s from 2 min before to 2 min after 08:00 and 17:00 on weekdays. Leave running (or schedule it).",
                                 "edges": [{"days": "wd", "time": "08:00"}, {"days": "wd", "time": "17:00"}],
                                 "cards": [{"fc": 185, "card": 1020, "label": "M-F 8-5"}, {"fc": 185, "card": 1500, "label": "M-F ALT"},
                                           {"fc": 185, "card": 1751, "label": "WEEKEND"}, {"fc": 185, "card": 1960, "label": "Master"},
                                           {"fc": 185, "card": 1975, "label": "Unauthorized"}],
                                 "window_min": 2, "every_s": 15, "card_gap_ms": 1500, "reader": 1, "format": 26},
    "APB: single door": {"type": "sequence", "description": "Entry, repeat entry, exit, repeat exit, re-entry on Door 1.",
                         "format": 26, "repeat": 1, "steps": [
                             {"reader": 1, "fc": 185, "card": 1020, "wait_s": 3, "expect": "G", "note": "First entry"},
                             {"reader": 1, "fc": 185, "card": 1020, "wait_s": 3, "expect": "D", "note": "Entry again: APB"},
                             {"reader": 2, "fc": 185, "card": 1020, "wait_s": 3, "expect": "G", "note": "Exit"},
                             {"reader": 2, "fc": 185, "card": 1020, "wait_s": 3, "expect": "D", "note": "Exit again: APB"},
                             {"reader": 1, "fc": 185, "card": 1020, "wait_s": 3, "expect": "G", "note": "Re-entry"}]},
    "APB: two doors": {"type": "sequence", "description": "Enter Door 1, then try Door 2. Denied if the doors share an anti-passback area.",
                       "format": 26, "repeat": 1, "steps": [
                           {"reader": 1, "fc": 185, "card": 1021, "wait_s": 3, "expect": "G", "note": "Enter Door 1"},
                           {"reader": 3, "fc": 185, "card": 1021, "wait_s": 3, "expect": "?", "note": "Enter Door 2: D if shared area"},
                           {"reader": 4, "fc": 185, "card": 1021, "wait_s": 3, "expect": "G", "note": "Exit Door 2"},
                           {"reader": 1, "fc": 185, "card": 1021, "wait_s": 3, "expect": "?", "note": "Enter Door 1 after D2 exit"}]},
    "APB: timed": {"type": "sequence", "description": "Entry, immediate repeat (denied), wait out the timeout, repeat (granted). Set the wait to just over the Qumulex APB time.",
                   "format": 26, "repeat": 1, "steps": [
                       {"reader": 1, "fc": 185, "card": 1022, "wait_s": 3, "expect": "G", "note": "First entry"},
                       {"reader": 1, "fc": 185, "card": 1022, "wait_s": 3, "expect": "D", "note": "Entry again: within timeout"},
                       {"reader": 0, "wait_s": 65, "note": "Wait out APB timeout"},
                       {"reader": 1, "fc": 185, "card": 1022, "wait_s": 3, "expect": "G", "note": "Entry after timeout"}]},
    "Card + PIN basics": {"type": "sequence", "description": "Correct PIN, wrong PIN, card only. Give card 185-1020 PIN 1234 in Qumulex and set Door 1 to card + PIN.",
                          "format": 26, "pin_format": 4, "key_gap_ms": 300, "card_pin_gap_ms": 1000, "end_hash": False, "repeat": 1, "steps": [
                              {"reader": 1, "fc": 185, "card": 1020, "pin": "1234", "wait_s": 4, "expect": "G", "note": "Correct PIN"},
                              {"reader": 1, "fc": 185, "card": 1020, "pin": "9999", "wait_s": 4, "expect": "D", "note": "Wrong PIN"},
                              {"reader": 1, "fc": 185, "card": 1020, "pin": "", "wait_s": 20, "expect": "D", "note": "Card only: should time out"},
                              {"reader": 1, "fc": 185, "card": 1020, "pin": "1234", "wait_s": 4, "expect": "G", "note": "Correct PIN again"}]},
    "Card + PIN lockout": {"type": "sequence", "description": "Three wrong PINs, then the right one.",
                           "format": 26, "pin_format": 4, "key_gap_ms": 300, "card_pin_gap_ms": 1000, "end_hash": False, "repeat": 1, "steps": [
                               {"reader": 1, "fc": 185, "card": 1020, "pin": "1111", "wait_s": 4, "expect": "D", "note": "Wrong PIN 1"},
                               {"reader": 1, "fc": 185, "card": 1020, "pin": "2222", "wait_s": 4, "expect": "D", "note": "Wrong PIN 2"},
                               {"reader": 1, "fc": 185, "card": 1020, "pin": "3333", "wait_s": 4, "expect": "D", "note": "Wrong PIN 3"},
                               {"reader": 1, "fc": 185, "card": 1020, "pin": "1234", "wait_s": 4, "expect": "?", "note": "Right PIN: D if locked out"}]},
    "Bad frames": {"type": "bad_frames", "description": "Wrong parity, wrong bit counts, noise. The panel should reject all but the two good control frames.",
                   "reader": 1, "fc": 185, "card": 1020, "wait_s": 3, "repeat": 1},
}

TESTS = load_json(TESTS_FILE, DEFAULT_TESTS)
if not os.path.exists(TESTS_FILE):
    save_json(TESTS_FILE, TESTS)
JOBS = load_json(JOBS_FILE, [])
TEST_TYPES = ("load", "sequence", "schedule_edges", "bad_frames")


def scheduler():
    fired = set()
    while True:
        n = now()
        for job in list(JOBS):
            if not job.get("enabled", True):
                continue
            if not day_matches(job.get("days", "all"), n.weekday()):
                continue
            if job.get("start") != f"{n:%H:%M}":
                continue
            key = (job.get("id"), f"{n:%Y-%m-%d %H:%M}")
            if key in fired:
                continue
            fired.add(key)
            test = TESTS.get(job.get("test"))
            if not test:
                HUB.emit("error", f"Schedule '{job.get('name')}': test '{job.get('test')}' not found")
                continue
            try:
                RUNNER.start(job["test"], test, job.get("duration_min") or None, source=f"schedule: {job.get('name')}")
            except Exception as e:  # noqa: BLE001
                HUB.emit("error", f"Schedule '{job.get('name')}' skipped: {e}")
        if len(fired) > 1000:
            fired = set(list(fired)[-200:])
        time.sleep(5)


threading.Thread(target=scheduler, daemon=True).start()


# ---------------------------------------------------------------- web app

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")


@app.before_request
def require_password():
    pw = CFG.get("password")
    if not pw:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.password or "", pw):
        return None
    return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Wiegand Simulator"'})


def bad(msg, code=400):
    return jsonify({"error": msg}), code


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status():
    return jsonify({
        "host": socket.gethostname(),
        "time": iso(),
        "uno": {"connected": UNO.connected, "status": UNO.status, "port": UNO.port, "fw": UNO.fw, "inputs": UNO.inputs},
        "runner": RUNNER.status(),
    })


@app.get("/api/log")
def api_log():
    return jsonify(list(HUB.recent))


@app.get("/api/stream")
def api_stream():
    q = HUB.subscribe()

    def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    rec = q.get(timeout=15)
                    yield f"data: {json.dumps(rec)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            HUB.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/run")
def api_run():
    body = request.get_json(force=True)
    name = body.get("test")
    if name not in TESTS:
        return bad(f"test '{name}' not found")
    try:
        RUNNER.start(name, TESTS[name], body.get("duration_min") or None)
    except RuntimeError as e:
        return bad(str(e), 409)
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    RUNNER.stop()
    return jsonify({"ok": True})


MANUAL_LOCK = threading.Lock()


@app.post("/api/swipe")
def api_swipe():
    b = request.get_json(force=True)
    try:
        reader = int(b.get("reader", 1))
        fmt = int(b.get("format", 26))
        card = b.get("card")
        card = None if card in (None, "") else int(card)
        pin = str(b.get("pin") or "")
        if card is None and not pin:
            return bad("Enter a card number, a PIN, or both")
        if pin and not re.fullmatch(r"[0-9*#]{1,16}", pin):
            return bad("PIN can only use 0-9, * and #")
        with MANUAL_LOCK:
            row, _, _ = RUNNER.swipe(reader, int(b.get("fc", 185)), card, pin=pin, fmt=fmt,
                                     pin_fmt=int(b.get("pin_format", 4)), key_gap_ms=int(b.get("key_gap_ms", 300)),
                                     card_pin_gap_ms=int(b.get("card_pin_gap_ms", 1000)),
                                     end_hash=bool(b.get("end_hash")), step="manual", test_name="manual")
        row["note"] = "Quick swipe"
        HUB.emit("swipe", f"[manual] R{reader} ({reader_label(reader)})"
                 + (f"  {row['fc']}-{card}" if card is not None else "") + (f"  PIN {pin}" if pin else ""), row=row)
        path = os.path.join(LOGDIR, f"manual-{now():%Y%m%d}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in LOG_FIELDS})
        return jsonify({"ok": True, "row": row})
    except (UnoError, StopTest, ValueError) as e:
        return bad(str(e))


@app.get("/api/inputs")
def api_inputs():
    try:
        reply = UNO.command("IN")
        return jsonify({"inputs": [int(x) for x in reply.split()[2:6]]})
    except UnoError as e:
        return bad(str(e))


@app.get("/api/tests")
def api_tests():
    return jsonify(TESTS)


def validate_test(t):
    if not isinstance(t, dict):
        return "Test must be a JSON object"
    if t.get("type") not in TEST_TYPES:
        return f"type must be one of: {', '.join(TEST_TYPES)}"
    if t["type"] == "sequence" and not isinstance(t.get("steps"), list):
        return "A sequence test needs a steps list"
    if t["type"] == "schedule_edges":
        for e in t.get("edges", []):
            if not re.fullmatch(r"\d{1,2}:\d{2}", str(e.get("time", ""))):
                return f"Bad edge time: {e.get('time')}"
    return None


@app.put("/api/tests/<path:name>")
def api_test_put(name):
    t = request.get_json(force=True)
    err = validate_test(t)
    if err:
        return bad(err)
    TESTS[name] = t
    save_json(TESTS_FILE, TESTS)
    return jsonify({"ok": True})


@app.delete("/api/tests/<path:name>")
def api_test_delete(name):
    TESTS.pop(name, None)
    save_json(TESTS_FILE, TESTS)
    return jsonify({"ok": True})


@app.post("/api/tests/reset-defaults")
def api_tests_defaults():
    for k, v in DEFAULT_TESTS.items():
        TESTS[k] = json.loads(json.dumps(v))
    save_json(TESTS_FILE, TESTS)
    return jsonify({"ok": True})


@app.get("/api/jobs")
def api_jobs():
    return jsonify(JOBS)


@app.post("/api/jobs")
def api_jobs_save():
    jobs = request.get_json(force=True)
    if not isinstance(jobs, list):
        return bad("Expected a list")
    for i, j in enumerate(jobs):
        if not re.fullmatch(r"\d{2}:\d{2}", str(j.get("start", ""))):
            return bad(f"Job {i + 1}: start time must be HH:MM")
        if j.get("test") not in TESTS:
            return bad(f"Job {i + 1}: test '{j.get('test')}' not found")
        j.setdefault("id", f"job{int(time.time() * 1000)}{i}")
    JOBS[:] = jobs
    save_json(JOBS_FILE, JOBS)
    return jsonify({"ok": True})


@app.get("/api/config")
def api_config():
    c = dict(CFG)
    c["password"] = "set" if CFG.get("password") else ""
    return jsonify(c)


@app.post("/api/config")
def api_config_save():
    b = request.get_json(force=True)
    if "reader_labels" in b:
        CFG["reader_labels"] = [str(x)[:24] for x in b["reader_labels"]][:4]
    if "pulse_us" in b:
        CFG["pulse_us"] = max(10, min(500, int(b["pulse_us"])))
    if "interval_us" in b:
        CFG["interval_us"] = max(100, min(16000, int(b["interval_us"])))
    if "feedback" in b:
        fb = b["feedback"]
        CFG["feedback"] = {"enabled": bool(fb.get("enabled")),
                           "reader_inputs": [max(1, min(4, int(x))) for x in fb.get("reader_inputs", [1, 1, 2, 2])][:4],
                           "window_ms": max(200, min(30000, int(fb.get("window_ms", 3000))))}
    save_json(CONFIG_FILE, CFG)
    if UNO.connected:
        try:
            UNO.command(f"CFG {CFG['pulse_us']} {CFG['interval_us']}")
        except UnoError as e:
            return bad(f"Saved, but the Uno didn't take the timing: {e}")
    return jsonify({"ok": True})


@app.get("/api/logs")
def api_logs():
    files = []
    for p in sorted(glob.glob(os.path.join(LOGDIR, "*.csv")), reverse=True)[:300]:
        st = os.stat(p)
        files.append({"name": os.path.basename(p), "size": st.st_size, "modified": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    return jsonify(files)


@app.get("/logs/<path:name>")
def download_log(name):
    if "/" in name or not name.endswith(".csv"):
        abort(404)
    return send_from_directory(LOGDIR, name, as_attachment=True)


if __name__ == "__main__":
    HUB.emit("sys", f"Simulator controller starting on port {CFG['http_port']}")
    app.run(host="0.0.0.0", port=int(CFG["http_port"]), threaded=True)
