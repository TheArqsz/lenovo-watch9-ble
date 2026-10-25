# lenovo-watch9-ble

Control a Lenovo hybrid (analog-hand) smartwatch over Bluetooth Low Energy from
the command line, without the vendor app.

The tool scans for the watch, prints its current state, and gives you a menu to
set the clock, change the step goal, rename it or make it vibrate. The
byte-level protocol notes are in [`PROTOCOL.md`](PROTOCOL.md).

> Unofficial project. Not affiliated with or endorsed by Lenovo. Use it on your
> own watch, at your own risk.

## Why

The last time I checked, the Lenovo Watch app (`com.pmpd.dmap.lenovo`), the
companion app for the Watch 9, was not available in Poland, so as far as I knew
my watch was basically dead. Then I found a few APKMirror entries that looked
like they belonged to this watch. So I pulled the app apart with my own Android
reverse engineering tool and some help from Claude (who stared at hex dumps
without complaining once) and worked out the protocol.

## Features

- Scan and auto-detect the watch and its protocol variant (C007 or C001).
- Dashboard: battery, firmware version, serial number, watch time, steps today,
  step goal.
- Set the watch clock to your computer's local time.
- Set the daily step goal.
- Rename the watch.
- Send a test alert (vibrate).
- Confirm a first-time pairing.

## Requirements

- Python 3.12 or newer
- [`bleak`](https://github.com/hbldh/bleak)
- A computer with Bluetooth LE (built-in is fine)

## Install

```bash
git clone https://github.com/TheArqsz/lenovo-watch9-ble.git
cd lenovo-watch9-ble
pip install -r requirements.txt
```

## Usage

```bash
python lenovo_watch9.py
```

It scans, lists the watches it finds, connects (or asks you to pick one), prints
the dashboard, then shows the menu.

| Flag | Effect |
|---|---|
| `--address AA:BB:CC:DD:EE:FF` | connect to a known address and skip scanning |
| `--scan-time 15` | scan for longer (default 10 seconds) |
| `--variant c007` | force a protocol variant instead of auto-detecting |

Example session:

```
Connecting to AA:BB:CC:DD:EE:FF (c007) ...
Connected. Authorizing ...
  -> accepted

Reading watch ...
  Variant        : c007
  Battery        : sufficient
  Firmware       : 0.4.0
  Serial         : XXXXXXXXXXXXXXXXXXXX
  Watch time     : 2026-10-25 14:11:24
  Steps today    : 18
  Step goal      : 10000
```

## Connecting to the watch

These watches have no "pairing mode" button. The watch advertises over BLE
whenever it is not already connected, so before you run the tool:

1. Make sure it has battery and is awake.
2. Disconnect or unpair it from the phone app, or close the app. A watch that is
   already connected to the phone will not appear in the scan.
3. If it is in "watch-only" or power-saving mode it turns Bluetooth off. Wake it
   first (usually a long button or crown press; check your watch's manual).

On the first connection the watch asks you to shake it or press a button to
accept. Use menu option 6, which waits up to 60 seconds. After that it stays
paired and commands work without the shake.

## Platform notes

Tested on Windows 11 with the built-in Bluetooth adapter.

## Protocol summary

- One GATT service, `0000a800-...`. Commands and notifications use
  characteristic `a801` on newer "C007" watches, or `a802` on older "C001"
  watches. The tool picks the right one from the advertised model code.
- A frame is `0x23 01 <len> 0x31 <seq> <op> <group> <cmd> <payload...>
  <checksum>`, where the checksum is `sum(byte[i] XOR i) & 0xFF` over every byte
  before it.
- There is no encryption and no secret key. The only gate is the one-time shake
  or button press at first pairing.

Full details, including the command catalogue and payload encodings, are in
[`PROTOCOL.md`](PROTOCOL.md).

## Safety and scope

- Firmware update and factory test commands are left out on purpose, since
  those can brick a watch.
- The write commands only change settings (time, step goal, name), and you can
  always set them back.
- The watch has no real authentication beyond the shake at first pairing, so
  any nearby BLE device could send it the same commands. Blame the firmware
  for that one.

## Contributing

Captures from other models are welcome. If you run the write commands and record
the reply frames, or test a watch that selects the C001 variant, open an issue
or a pull request with the raw bytes and the model code.

## License

MIT. See [`LICENSE`](LICENSE).
