#!/usr/bin/env python3
"""
lenovo-watch9 — an interactive BLE tool for Lenovo hybrid (analog-hand) watches.

Scans for a watch, shows its current data, and gives you a menu to set the
time, change the daily step goal, rename it or make it vibrate — all without
the vendor app.

The protocol was reverse engineered from the official Android app. It has been
confirmed working on a P1015 ("Watch 9") watch for connect / battery / set
time; other commands are built from the app's code but may vary by firmware.
Nothing here encrypts or authenticates beyond the watch's own (keyless)
handshake — see the README.

Requires Python 3.9+ and bleak (`pip install bleak`).

Usage:
    python lenovo_watch9.py                      # scan, then interactive menu
    python lenovo_watch9.py --address <MAC>      # skip the scan
    python lenovo_watch9.py --scan-time 15
    python lenovo_watch9.py --variant c007       # force a protocol variant
"""

import argparse
import asyncio
import datetime as dt

from bleak import BleakClient, BleakScanner

# ---------------------------------------------------------------------------
# GATT layout. One service; each protocol variant uses a different
# command/notify characteristic.
# ---------------------------------------------------------------------------
SERVICE = "0000a800-0000-1000-8000-00805f9b34fb"
CHAR = {"c007": "0000a801-0000-1000-8000-00805f9b34fb",
        "c001": "0000a802-0000-1000-8000-00805f9b34fb"}

# Model-code tokens (ASCII hex) the app treats as the newer C007 protocol.
# Anything whose advertisement matches none of these is the older C001.
NEW_TOKENS = [
    "5037", "5031303131", "5031303132", "5031303133", "5031303134",
    "5031303135", "5031303136", "5031303137", "5031303138", "5031303139",
    "5042303030", "5042303031", "5042303032", "5043303030", "5043303031",
    "5043303032", "5043303033", "5044303030", "5044303031", "5045303030",
    "5045303031", "5046303030", "5046303031", "5046303032", "5049303030",
    "504A303030", "5047303030", "5048303030", "504E303030", "5033303135",
    "5032303039", "5034303133", "5035303042", "5036303043", "5036303044",
    "5037303041", "5041303031", "5041303032", "5041303033", "5041303034",
    "504C303030", "504C303031", "504D303030", "504D303031", "5051303030",
    "5051303031",
]


# ---------------------------------------------------------------------------
# Advertisement / variant helpers
# ---------------------------------------------------------------------------
def manufacturer_raw(adv):
    """Rebuild the manufacturer payload bleak split into (company_id, data).

    Old watches put ASCII 'P' straight after the 0xFF AD type, so bleak reads
    the next two bytes as a little-endian company id; re-joining restores the
    original byte order the app parsed.
    """
    for company_id, data in adv.manufacturer_data.items():
        return company_id.to_bytes(2, "little") + bytes(data)
    return b""


def is_watch(raw):
    return bool(raw) and (raw[0] == 0x50 or (len(raw) > 2 and raw[2] == 0x50))


def model_code(raw):
    off = 0 if raw and raw[0] == 0x50 else 2
    chunk = raw[off:off + 5]
    try:
        return chunk.decode("ascii") if len(chunk) == 5 else ""
    except UnicodeDecodeError:
        return ""


def pick_variant(raw):
    hexstr = raw.hex().upper()
    return "c007" if any(t in hexstr for t in NEW_TOKENS) else "c001"


# ---------------------------------------------------------------------------
# Byte / frame helpers
# ---------------------------------------------------------------------------
def bcd(n):
    """Pack a decimal 0..99 as one BCD byte: 26 -> 0x26."""
    return ((n // 10) << 4) | (n % 10)


def unbcd(b):
    return ((b >> 4) & 0xF) * 10 + (b & 0xF)


def c007_frame(seq, body):
    """Build a C007 request: 0x23 01 <len> 0x31 <seq> <body...> <checksum>."""
    frame = [0x23, 0x01, 0x00, 0x31, seq & 0xFF] + list(body)
    frame[2] = len(frame) + 1 - 6
    chk = sum((b ^ i) & 0xFF for i, b in enumerate(frame)) & 0xFF
    return bytes(frame + [chk])


def c001_frame(body):
    """Build a C001 request: 0x24 <len> <body...> <checksum>."""
    frame = [0x24, 0x00] + list(body)
    frame[1] = len(frame) + 1 - 3
    chk = sum(frame[3:]) & 0xFF
    return bytes(frame + [chk])


def set_time_body(now):
    """C007 set-time payload for a timezone-aware datetime."""
    off_min = int(now.utcoffset().total_seconds() // 60) if now.utcoffset() else 0
    tz_frac = round((abs(off_min) % 60) / 60.0 * 100)
    return [0x01, 0x01, 0x08,
            bcd(now.year % 100), bcd(now.month), bcd(now.day),
            bcd(now.hour), bcd(now.minute), bcd(now.second),
            off_min // 60 & 0xFF, tz_frac & 0xFF, now.isoweekday() % 7]


# ---------------------------------------------------------------------------
# BLE client
# ---------------------------------------------------------------------------
class Watch:
    def __init__(self, address, variant):
        self.address = address
        self.variant = variant
        self.char = CHAR[variant]
        self._client = None
        self._queue = asyncio.Queue()
        self._seq = 0

    async def __aenter__(self):
        self._client = BleakClient(self.address)
        await self._client.connect()
        await self._client.start_notify(self.char, self._on_notify)
        return self

    async def __aexit__(self, *exc):
        try:
            await self._client.stop_notify(self.char)
        finally:
            await self._client.disconnect()

    def _on_notify(self, _char, data):
        self._queue.put_nowait(bytes(data))

    async def _collect(self, match, timeout, want=1):
        """Return up to `want` reply frames for which match(frame) is true."""
        frames, loop = [], asyncio.get_event_loop()
        end = loop.time() + timeout
        while len(frames) < want:
            remaining = end - loop.time()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError:
                break
            if match(frame):
                frames.append(frame)
        return frames

    async def request(self, body, timeout=6.0, want=1, quiet=False, match=None):
        """Send one C007 command and collect its reply frame(s).

        By default a reply is matched on the echoed sequence byte. Reads that
        return data (battery, serial, etc.) pass `match` to match on the
        command id instead, because some replies do not echo the sequence.
        """
        self._seq = (self._seq % 250) + 1
        seq = self._seq
        frame = c007_frame(seq, body)
        if not quiet:
            print(f"    tx {frame.hex(' ')}")
        await self._client.write_gatt_char(self.char, frame, response=True)
        pred = match or (lambda f: len(f) > 4 and f[4] == seq)
        frames = await self._collect(pred, timeout, want)
        if not quiet:
            for f in frames:
                print(f"    rx {f.hex(' ')}")
        return frames

    async def request_c001(self, body, timeout=6.0):
        frame = c001_frame(body)
        print(f"    tx {frame.hex(' ')}")
        await self._client.write_gatt_char(self.char, frame, response=True)
        # C001 has no echoed sequence byte; take the next frame that arrives.
        try:
            reply = await asyncio.wait_for(self._queue.get(), timeout)
            print(f"    rx {reply.hex(' ')}")
            return reply
        except asyncio.TimeoutError:
            return None


# ---------------------------------------------------------------------------
# High-level actions (C007). Each returns a short status string.
# ---------------------------------------------------------------------------
def on_cmd(group, cmd):
    """Predicate: a reply frame carrying this command group/id at offsets 6-7."""
    return lambda f: len(f) > 7 and f[6] == group and f[7] == cmd
async def act_authorize(w, timeout=60.0):
    if w.variant == "c001":
        reply = await w.request_c001([0x02, 0x0A, 0x01, 0x03], timeout=timeout)
        return "ok" if reply else "no reply"
    frames = await w.request([0x04, 0x01, 0x05, 0x00], timeout=timeout)
    return "accepted" if frames else "no reply (already paired, or shake the watch)"


async def read_battery(w):
    if w.variant == "c001":
        reply = await w.request_c001([0x02, 0x0C, 0x01, 0x01])
        if reply and len(reply) > 13:
            return f"{reply[13]}%"
        return "?"
    frames = await w.request([0x02, 0x01, 0x14], quiet=True, match=on_cmd(0x01, 0x14))
    for f in frames:
        if len(f) > 9:
            btype, value = f[8], f[9]
            # Coin-cell models (type 0) report a state flag, not a percent.
            if btype == 0:
                return "sufficient" if value == 1 else "LOW - replace battery"
            return f"{value}%"
    return "?"


async def read_firmware(w):
    frames = await w.request([0x02, 0x01, 0x02], quiet=True, match=on_cmd(0x01, 0x02))
    for f in frames:
        if len(f) > 10:
            return f"{f[8]}.{f[9]}.{f[10]}"
    return "?"


async def read_time(w):
    frames = await w.request([0x02, 0x01, 0x08], quiet=True, match=on_cmd(0x01, 0x08))
    for f in frames:
        if len(f) > 13:
            return (f"20{unbcd(f[8]):02d}-{unbcd(f[9]):02d}-{unbcd(f[10]):02d} "
                    f"{unbcd(f[11]):02d}:{unbcd(f[12]):02d}:{unbcd(f[13]):02d}")
    return "?"


async def read_steps(w):
    frames = await w.request([0x02, 0x10, 0x03], quiet=True, match=on_cmd(0x10, 0x03))
    for f in frames:
        if len(f) > 9:
            return str((f[8] << 8) | f[9])
    return "?"


async def read_step_goal(w):
    frames = await w.request([0x02, 0x10, 0x02], quiet=True, match=on_cmd(0x10, 0x02))
    for f in frames:
        if len(f) > 9:
            return str((f[8] << 8) | f[9])
    return "?"


async def read_sn(w):
    # The serial arrives as two frames (low half then high half); each carries
    # 10 ASCII chars at offsets 9..18, keyed by the flag at offset 8. The reply
    # uses opcode 0x80 and does not echo the sequence, so match on the command.
    frames = await w.request([0x02, 0x01, 0x06], want=2, timeout=8.0,
                             quiet=True, match=on_cmd(0x01, 0x06))
    parts = {}
    for f in frames:
        if len(f) >= 19:
            parts[0 if f[8] == 0 else 1] = f[9:19].decode("ascii", "replace")
    if parts:
        return (parts.get(0, "") + parts.get(1, "")).strip() or "?"
    return "?"


async def set_time(w):
    now = dt.datetime.now().astimezone()
    if w.variant == "c001":
        reply = await w.request_c001(
            [0x02, 0x04, 0x01, now.year % 100, now.month, now.day,
             now.hour, now.minute, now.second, 0x00])
        return "ok" if reply else "no reply"
    frames = await w.request(set_time_body(now))
    return f"set to {now:%Y-%m-%d %H:%M:%S %z}" if frames else "no reply"


async def set_step_goal(w, goal):
    frames = await w.request([0x01, 0x10, 0x02, (goal >> 8) & 0xFF, goal & 0xFF])
    ok = frames and len(frames[0]) > 5 and frames[0][5] == 0
    return "ok" if ok else ("sent" if frames else "no reply")


async def rename(w, name):
    body = [0x01, 0x00, 0x03] + [ord(c) & 0x7F for c in name]
    frames = await w.request(body)
    return "ok" if frames else "no reply"


async def vibrate(w):
    # A "message" notification with the SMS source bit set makes the watch buzz.
    frames = await w.request([0x04, 0x03, 0x01, 0x00, 0x00, 0x08, 0x00])
    return "buzzed" if frames else "no reply"


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------
async def scan(scan_time):
    found = {}

    def on_detect(device, adv):
        raw = manufacturer_raw(adv)
        if is_watch(raw) and device.address not in found:
            found[device.address] = (device, raw, model_code(raw), pick_variant(raw))

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    await asyncio.sleep(scan_time)
    await scanner.stop()
    return list(found.values())


async def choose_device(scan_time):
    print(f"Scanning {scan_time:.0f}s for a Lenovo watch ...")
    cands = await scan(scan_time)
    if not cands:
        print("No watch found. Make sure it is awake, not connected to the "
              "phone app, and Bluetooth is on.")
        return None, None
    for i, (dev, raw, model, var) in enumerate(cands):
        print(f"  [{i}] {dev.address}  model={model or '?'}  variant={var}")
    if len(cands) == 1:
        dev, _, _, var = cands[0]
        return dev.address, var
    while True:
        sel = (await ainput("Pick a watch [0]: ")).strip() or "0"
        if sel.isdigit() and int(sel) < len(cands):
            dev, _, _, var = cands[int(sel)]
            return dev.address, var


async def show_dashboard(w):
    print("\nReading watch ...")
    print(f"  Variant        : {w.variant}")
    print(f"  Battery        : {await read_battery(w)}")
    print(f"  Firmware       : {await read_firmware(w)}")
    print(f"  Serial         : {await read_sn(w)}")
    print(f"  Watch time     : {await read_time(w)}")
    print(f"  Steps today    : {await read_steps(w)}")
    print(f"  Step goal      : {await read_step_goal(w)}")


MENU = """
What would you like to do?
  1) Refresh watch info
  2) Set time to this computer
  3) Set daily step goal
  4) Rename watch
  5) Vibrate (send a test alert)
  6) Authorize / confirm pairing (shake the watch)
  0) Quit
> """


async def menu_loop(w):
    while True:
        choice = (await ainput(MENU)).strip()
        try:
            if choice == "1":
                await show_dashboard(w)
            elif choice == "2":
                print("  ->", await set_time(w))
            elif choice == "3":
                raw = (await ainput("  New step goal (e.g. 8000): ")).strip()
                if raw.isdigit():
                    print("  ->", await set_step_goal(w, min(int(raw), 0xFFFF)))
                else:
                    print("  not a number")
            elif choice == "4":
                if w.variant != "c007":
                    print("  rename is only mapped for the c007 variant")
                    continue
                name = (await ainput("  New name (max 11 chars): ")).strip()
                if name:
                    print("  ->", await rename(w, name[:11]))
            elif choice == "5":
                if w.variant != "c007":
                    print("  vibrate is only mapped for the c007 variant")
                    continue
                print("  ->", await vibrate(w))
            elif choice == "6":
                print("  ->", await act_authorize(w))
            elif choice == "0":
                print("Bye.")
                return
            else:
                print("  unknown choice")
        except Exception as exc:  # keep the menu alive on a transient BLE error
            print(f"  error: {exc}")


async def ainput(prompt):
    """Non-blocking input() for the asyncio loop."""
    return await asyncio.to_thread(input, prompt)


async def main():
    ap = argparse.ArgumentParser(description="Interactive Lenovo hybrid-watch BLE tool.")
    ap.add_argument("--address", help="watch MAC/UUID; skips the scan")
    ap.add_argument("--scan-time", type=float, default=10.0)
    ap.add_argument("--variant", choices=["c001", "c007"], help="force a variant")
    args = ap.parse_args()

    address, variant = args.address, args.variant
    if address is None or variant is None:
        a, v = await choose_device(args.scan_time)
        if a is None:
            return
        address, variant = address or a, variant or v

    print(f"\nConnecting to {address} ({variant}) ...")
    async with Watch(address, variant) as w:
        # Quick authorize on connect; a bonded watch may not reply and that is
        # fine. For a first-ever pairing use menu option 6 (waits longer).
        print("Connected. Authorizing ...")
        print("  ->", await act_authorize(w, timeout=8.0))
        await show_dashboard(w)
        await menu_loop(w)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
