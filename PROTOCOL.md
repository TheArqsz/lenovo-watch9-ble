# Lenovo hybrid watch BLE protocol

These are my notes on the Bluetooth Low Energy protocol that Lenovo hybrid
(analog-hand) watches speak. I pulled it apart by decompiling the vendor Android
app and then checking the frames against a real watch. It documents the frame
format, the command set, the payload encodings, and the security model, with
byte-level examples from a Watch 9.

## 1. Source material

| | |
|---|---|
| Application | Lenovo Watch |
| Package | `com.pmpd.dmap.lenovo` |
| Version | 3.1.7 (versionCode 3170) |
| Min SDK | 18 |
| Download | https://www.apkmirror.com/apk/lenovo-group/lenovo-watch/lenovo-watch-3-1-7-release/lenovo-watch-3-1-7-android-apk-download/ |
| File | `com.pmpd.dmap.lenovo_3.1.7-3170_minAPI18(arm64-v8a,armeabi-v7a)(nodpi)_apkmirror.com.apk` |
| SHA-256 | `d67306541ba6a8cbe73e619c093d1b510369fc387b363a2172c7e777ce57ed5e` |
| SHA-1 | `f36d7b6499bce4020301ea05d1c6aab373add8cf` |
| MD5 | `c46d6bd37b499311071f558fdc5a66fb` |

Everything below I read out of the decompiled bytecode. The byte-level examples
in section 12 are live captures from my own Watch 9 (advertised model `P1015`,
firmware 0.4.0). The rest comes from the code and may change with firmware.

## 2. Where the protocol lives in the app

The watch protocol is compiled into `classes5.dex`, under the Java package
`com.pmpd.protocol.ble`. The relevant files:

| Path (under the decompiled `src/`) | Role |
|---|---|
| `com/pmpd/protocol/ble/BleChannel.java` | GATT service and characteristic UUID constants |
| `com/pmpd/protocol/ble/DeviceCompat.java` | model-code table and variant selection |
| `com/pmpd/protocol/ble/ProtocolProxy.java` | dispatch to the C001 or C007 implementation |
| `com/pmpd/protocol/ble/c007/ProtocolC007.java` | C007 command factory (one method per command) |
| `com/pmpd/protocol/ble/c007/api/*.java` | one class per C007 command: `make()` builds the frame, `callback()`/`onSuccess()` parse the reply |
| `com/pmpd/protocol/ble/c007/api/BaseModelApiService.java` | C007 checksum, length, flow helpers |
| `com/pmpd/protocol/ble/c001/api/*.java` | the same, for the older C001 variant |
| `com/pmpd/protocol/ble/util/BCDHelper.java` | BCD packing used by time and alarm payloads |
| `com/pmpd/util/bluetooth/BleHelper.java` | scan, connect, GATT read/write, notification setup |
| `com/pmpd/util/bluetooth/DeviceMessage.java` | stores the advertised model string and the MAC |

The engine is deterministic: `ProtocolC007.getInstance()` increments a global
counter `sTag` on every call and stamps it into the frame as the sequence byte.
Each command class is a small state object whose `make()` returns the outgoing
byte array and whose `callback()` decides whether an incoming frame answers it.

## 3. Device identification and variant selection

The watch advertises BLE manufacturer data that contains an ASCII model code of
the form `P<nnnn>` (for example `P1015`). `BleHelper$3.onLeScan` accepts a
device when the manufacturer hex has `50` ('P') right after the `FF`
manufacturer-data marker (at offset +2, or +6 on the newer advertisement
layout). The whole advertisement is cached as the "device message"
(`DeviceMessage.putDeviceMessage`).

`DeviceCompat.isNewProtocol()` selects the command variant by testing whether
the device message string contains any known model token. Two variants exist:

- C007, the newer protocol. Writes and notifications on characteristic `a801`.
- C001, the older protocol. Writes and notifications on characteristic `a802`.

Tokens that select C007 include `5037` (ASCII "P7"), the `P101x` family
(`5031303131` through `5031303139`), the whole `H001` family (`5042xxxx`
through `504Axxxx`), plus `5047303030`, `5048303030`, `504E303030`,
`5033303135`, and the `F001`/`D000` model families. A device whose model
matches none of these uses C001. Watch 9 (`P1015`, token `5031303135`) is C007.

`DeviceCompat.getDeviceNumber()` extracts the printable model string from the
advertisement by locating `FF` and reading the ASCII that follows.

## 4. Transport

All traffic uses a single GATT service (`BleChannel.java`):

| UUID | Use |
|---|---|
| `0000a800-0000-1000-8000-00805f9b34fb` | service |
| `0000a801-0000-1000-8000-00805f9b34fb` | C007 command and notify channel |
| `0000a802-0000-1000-8000-00805f9b34fb` | C001 command and notify channel |
| `0000a803-0000-1000-8000-00805f9b34fb` | secondary write channel |
| `0000a804-0000-1000-8000-00805f9b34fb` | firmware update (OTA) data channel |
| `00002902-0000-1000-8000-00805f9b34fb` | Client Characteristic Configuration descriptor |

A command and its reply use the same characteristic: `BaseModelApiService`
returns the same UUID from both `sendChannel()` and `callbackChannel()`. Before
sending commands the client enables notifications by writing `01 00` to the
`2902` descriptor of that characteristic. Writes are write-with-response
(`BleHelper.bleWriteCharacteristic`, which calls `setValue` then
`writeCharacteristic`).

## 5. C007 frame format

Requests are sent on characteristic `a801`.

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | start byte `0x23` ('#') |
| 1 | 1 | constant `0x01` |
| 2 | 1 | length = total frame length minus 6 |
| 3 | 1 | `0x31` ('1') in a request |
| 4 | 1 | sequence tag, echoed by the reply at offset 4 |
| 5 | 1 | operation: `0x01` set, `0x02` get, `0x04` action, `0x08` or `0x80` bulk |
| 6 | 1 | command group |
| 7 | 1 | command id |
| 8..n | var | payload |
| last | 1 | checksum |

Checksum (`BaseModelApiService.makeProtocolCheck`): add `byte[i] XOR i` for
every byte before the checksum, then keep the low 8 bits.

```
chk = 0
for i in range(len(frame) - 1):
    chk = (chk + ((frame[i] ^ i) & 0xff)) & 0xff
```

Length (`makeProtocolLength`): `frame.length - 6`.

## 6. C007 reply format

Replies arrive as notifications on the same characteristic.

- Byte 3 is `0x13` in a reply, where a request has `0x31`.
- Byte 4 echoes the request sequence for most commands. Some data replies
  (for example the serial number, operation `0x80`) do not echo it, so a reader
  should match replies on the command group and id at bytes 6 and 7 rather than
  on the sequence.
- For a get (request operation `0x02`) the reply operation byte at offset 5 is
  `0x08`; bytes 6 and 7 echo the group and id; the payload starts at offset 8.
- For an action (request operation `0x04`) the reply is a short acknowledgement
  of length 7 whose offset-5 byte is a status code (`0x00` means success).

## 7. C001 frame format

Requests are sent on characteristic `a802`.

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | start byte `0x24` ('$') |
| 1 | 1 | length = total frame length minus 3 |
| 2 | 1 | type / operation |
| 3 | 1 | command |
| 4 | 1 | sub-command |
| 5..n | var | payload |
| last | 1 | checksum |

Checksum (`c001 BaseModelApiService.makeProtocolCheck`): the 8-bit sum of
byte 3 through the byte before the checksum. In a reply the command byte at
offset 3 comes back with `0x10` added (request `0x0C` becomes reply `0x1C`).
A bare acknowledgement is 6 bytes ending in `AA AB`.

## 8. Authorization and pairing

There is no key exchange, nonce or account binding. Authorization is a fixed
frame. On a first pairing the watch shows no code on the phone; the user
confirms the connection physically by shaking the watch or pressing a button
(application string `shake_or_click_watch_confirm_connect`). The app allows
60 seconds for that on the C007 authorize.

- C007 authorize (`AuthorizationApi`, op `0x04` grp `0x01` cmd `0x05`),
  non-constraint form sent on bind: `23 01 04 31 <seq> 04 01 05 00 <chk>`.
- C007 pair (`PairApi`, op `0x80` grp `0x00` cmd `0x07`):
  `23 01 03 31 <seq> 80 00 07 00 <chk>`.
- C001 authorize (`AuthorizationApi`): the constant `24 04 02 0A 01 03 0E`.

Once the watch is paired the app re-sends the authorize on each reconnect with
an 8-second timeout and proceeds whether or not it replies; in practice reads
and writes work on a reconnect without the shake.

## 9. Connection and sync sequence

The app's flow (`BleHelper`, `NotificationService`):

1. Scan, filter by model code, connect, discover services.
2. Enable notifications on the command characteristic (write `01 00` to its
   `2902` descriptor).
3. Send authorize (section 8).
4. On success, set the watch time, then request battery, firmware and serial,
   then read or push feature state (step goal, notification switches, alarms,
   weather, and so on).

## 10. C007 command catalogue

Operation byte: `01` set, `02` get, `04` action, `08`/`80` bulk. Group and id
are the two bytes after the operation. Each row is one class in
`com/pmpd/protocol/ble/c007/api/`.

| Command | op | grp | id |
|---|---|---|---|
| Authorize | 04 | 01 | 05 |
| Pair | 80 | 00 | 07 |
| Firmware version | 02 | 01 | 02 |
| Serial number (SN) | 02 | 01 | 06 |
| Battery | 02 | 01 | 14 |
| Set time | 01 | 01 | 08 |
| Get time | 02 | 01 | 08 |
| Personal info (age/height/weight/sex) | 01 | 01 | 0e |
| Weather set / push | 08 / 80 | 01 | 10 |
| Alarm set / get | 01 / 02 | 01 | 0a |
| Alarm2 set / get | 01 / 02 | 01 | 0b |
| Rename BLE name | 01 | 00 | 03 |
| Disconnect reminder set / get | 01 / 02 | 00 | 11 |
| Step goal set / get | 01 / 02 | 10 | 02 |
| Steps today | 02 | 10 | 03 |
| Sport / climb / run / swim scene | 04 | 10 / 11 / 12 / 13 | 01 |
| Run data update / time / history | 04 / 02 | 12 | 02 / 04 |
| Sleep history number / content / delete | 02 / 04 | 14 | 03 / 04 / 14 / 05 |
| Heart rate current / history | 02 | 15 | 03 / 02 |
| Notify switch set / get | 01 / 02 | 03 | 02 |
| Incoming notify | 04 | 03 | 01 |
| Cancel notify | 04 | 03 | 04 |
| Message detail / detail2 | 80 | 03 | 06 / 16 |
| Vibration pattern (incoming/msg/alarm/longsit/aims) | 01 | 03 | 71 |
| Do-not-disturb switch / window / get | 01 / 02 | 03 | 61 / 62 |
| Sedentary (long-sit) switch / window / get | 01 / 02 | 03 | 51 / 52 |
| System setting 1 / 2 / 3 set and get | 01 / 02 | 03 | 91 / 92 / 93 |
| Green / power-save mode | 04 | 03 | 81 |
| Special event | 04 | 03 | a1 |
| Camera mode enter / exit | 04 | 03 | 0d |
| Take-photo success | 08 | 03 | 22 |
| Hand correction mode / position / small hand | 04 | 03 | 31 / 33 / 32 |
| Local coordinates | 08 | 03 | 94 |
| UV level | 04 | 05 | 02 |
| Blood-pressure calibration / scene / measure | 04 / 02 | 05 | 0b..0f |
| Infrared / environment temperature | 01 / 02 / 04 | 03 / 05 | b1 / b2 / 04 |
| Directory number / content / package / delete (history) | 02 / 04 | f0 / f2 | 10 / 11 / 12 / 32 |
| Enter DFU, start OTA, file transfer | various | | OTA, uses `a804` |

## 11. Encodings

### Battery (`ElectricityApi`, reply bytes 8 and 9)

The reply carries a type byte (offset 8) and a value byte (offset 9). The app
(`DeviceViewModel`, `HomeViewModel`) reads them as:

- type `0` (coin-cell watches, including Watch 9): the value is a state flag,
  not a percent. Value `1` means the battery is sufficient; any other value
  means it should be replaced.
- type non-zero (rechargeable watches): the value is a percentage from 0 to 100.

### Firmware version (`VersionApi` / `FirmwareVersionApi`, reply bytes 8-10)

Three bytes, printed as `major.minor.patch`.

### Serial number (`SNCodeApi`)

The reply arrives as two frames with operation `0x80`, group `01`, id `06`.
Offset 8 flags which half (`0` = low, non-zero = high). Each frame carries 10
ASCII characters at offsets 9 through 18. The serial is the low half followed
by the high half, 20 characters total.

### Time (`TimeSetApi` and `McuTimeApi`, payload at offset 8)

| Payload offset | Field | Encoding |
|---|---|---|
| 0 | year minus 2000 | BCD, so 2026 is `0x26` |
| 1 | month 1-12 | BCD |
| 2 | day | BCD |
| 3 | hour, 24h | BCD |
| 4 | minute | BCD |
| 5 | second | BCD |
| 6 | timezone, whole hours | signed byte, minutes east of UTC divided by 60 |
| 7 | timezone fraction | `round((abs(tz_minutes) % 60) / 60 * 100)` |
| 8 | weekday | `Calendar.DAY_OF_WEEK - 1`, so 0 is Sunday |

"BCD" here packs the two decimal digits into one byte: `((n / 10) << 4) | (n % 10)`.
The C001 set-time command uses plain binary for the same fields and carries no
timezone.

### Notification source bitmask (`Notify2Api`, notify switch)

A 4-byte big-endian mask names the source of a notification:

| Value | Source | Value | Source |
|---|---|---|---|
| `0x0000001` | QQ | `0x0000400` | Call |
| `0x0000002` | WeChat | `0x0000800` | SMS / message |
| `0x0000008` | Skype | `0x0001000` | Missed call |
| `0x0000010` | Weibo | `0x0002000` | Calendar |
| `0x0000020` | Facebook | `0x0004000` | Email |
| `0x0000040` | Twitter | `0x0008000` | Contact |
| `0x0000080` | WhatsApp | (higher bits) | Tim, Taobao, JD, DingDing, Instagram |
| `0x0000100` | Line | | |

## 12. Captured frames (Watch 9, model P1015, firmware 0.4.0)

Request bytes are marked `tx`, notifications `rx`.

```
Authorize
  tx 23 01 04 31 01 04 01 05 00 72
  rx 23 01 01 13 01 00 40          reply op-area byte 5 = 00 -> accepted

Battery
  tx 23 01 03 31 01 02 01 14 7c
  rx 23 01 05 13 01 08 01 14 00 01 76
                              ^type 00, ^value 01 -> "sufficient" (fresh cell)

Set time (2026-10-25 13:55:33, UTC+01:00)
  tx 23 01 0c 31 02 01 01 08 26 10 25 13 55 33 01 00 00 d6
                              year month day  h  m  s tz frac week
  rx 23 01 01 13 02 00 41          status 00 -> success
```

Values decoded from the same session (reply frames read with notifications):
firmware `0.4.0`; serial (20 ASCII characters, redacted here); steps today
`18`; step goal `10000`; watch clock matched the host after the set-time
command.

Request frames for the remaining reads (sequence byte shown as `01`):

```
Firmware version   23 01 03 31 01 02 01 02 6e
Serial number      23 01 03 31 01 02 01 06 6a
Steps today        23 01 03 31 01 02 10 03 7c
Step goal (get)    23 01 03 31 01 02 10 02 7d
Step goal = 8000   23 01 05 31 01 01 10 02 1f 40 e0
Rename to "Watch"  23 01 08 31 01 01 00 03 57 61 74 63 68 83
Vibrate (SMS bit)  23 01 07 31 01 04 03 01 00 00 08 00 89
```

## 13. Security model

The link-layer protocol has no application-layer authentication or encryption.
Authorization is a constant frame, and the only barrier to control is the
one-time physical confirmation (shake or button press) performed on the watch
at first pairing, plus classic-Bluetooth bonding where the platform enforces
it. A BLE peer in range that has not been bonded out can connect to the watch
and issue the full command set above, including set-time, rename, notification
injection and, through the OTA path on `a804`, firmware entry. This is a
property of the watch firmware and the protocol design, not of any particular
client.

## 14. Method

I unpacked and decompiled the APK and read the UUIDs, frame layouts and command
ids out of the `com.pmpd.protocol.ble` package (section 2). The byte examples in
section 12 I captured with a BLE client against my own Watch 9. The frame format
and the command ids are stable in the code. For commands I have not yet run
against hardware, the payload layout comes straight from the matching `make()`
method, so confirm those per firmware before you rely on them.
