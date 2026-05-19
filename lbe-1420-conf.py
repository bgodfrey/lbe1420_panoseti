#!/usr/bin/env python3
"""Configuration utility for the Leo Bodnar LBE-1420 GPS-locked clock source.

Supports locating the LBE-1420, reporting its firmware version, setting the
OUT1 frequency, selecting which GNSS constellations the receiver uses, and
reporting GNSS conditions. Further configuration functionality will be
added over time.

Frequency configuration is sent over the device's HID interface as a feature
report; the LBE-1420 firmware performs the internal PLL synthesis, so the
desired frequency is sent directly in Hz. The HID node (/dev/hidraw*) is
root-only by default -- see UDEV_HELP below for the one-time rule that grants
access to your user.

GNSS conditions are read from the NMEA stream the device emits on its CDC
serial port (/dev/ttyACM*), which is accessible to the 'dialout' group.

HID protocol per the reverse-engineering work in bvernoux/lbe-142x.

Made with Claude
"""

import platform
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

import argparse
if IS_LINUX:
    print('Identified Linux')
    import fcntl
    import usb.core

if IS_WINDOWS:
    print('Identified Windows')
    import hid 

import os
import struct
import sys
import time
from typing import Any

import serial
import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo

# Aggregated GNSS readout produced by read_gnss_status(); the values are
# heterogeneous (strings, floats, the satellites sub-dict), hence Any.
GnssSnapshot = dict[str, Any]

# Substring the device advertises in its USB product/description string.
LBE_1420_ID = "lbe-1420"

# USB vendor/product IDs for the LBE-1420.
LBE_1420_VID = 0x1DD2
LBE_1420_PID = 0x2443

# HID feature-report protocol. The firmware uses each command's opcode as the
# HID Report ID, and every report is a fixed LBE_REPORT_SIZE-byte payload.
LBE_REPORT_SIZE = 60
LBE_1420_SET_F1 = 0x04        # opcode: set OUT1 frequency (persisted)
LBE_1420_SET_GNSS = 0x07      # opcode: set enabled GNSS constellations
LBE_STATUS_REPORT_ID = 0x4B   # report ID for the status read
LBE_1420_MAX_FREQ = 1_600_000_000  # Hz; firmware-accepted maximum for OUT1

# Status bits in the status report's first firmware byte.
LBE_GPS_LOCK_BIT = 1 << 0
LBE_PLL_LOCK_BIT = 1 << 1
LBE_ANT_OK_BIT = 1 << 2

# GNSS constellation enable bits for the SET_GNSS feature report; report[1]
# is the OR of these. Each bit position was measured directly by USB-capturing
# the Windows config tool (v1.07) while toggling that one constellation -- the
# tool exposes exactly these five. Bits 4 and 5 were never observed.
GNSS_BITS: dict[str, int] = {
    "gps": 1 << 0,
    "sbas": 1 << 1,
    "galileo": 1 << 2,
    "beidou": 1 << 3,
    "glonass": 1 << 6,
}
# Mask the Windows tool restores as the factory default: GPS + SBAS only.
GNSS_DEFAULT_MASK = GNSS_BITS["gps"] | GNSS_BITS["sbas"]

# NMEA stream on the CDC serial port.
NMEA_BAUD = 9600
STATUS_READ_SECONDS = 3.0
# NMEA talker-ID prefixes mapped to human-readable constellation names.
_CONSTELLATION: dict[str, str] = {
    "GP": "GPS", "GL": "GLONASS", "GA": "Galileo",
    "GB": "BeiDou", "BD": "BeiDou", "GQ": "QZSS", "GN": "Combined",
}

UDEV_HELP = """\
The LBE-1420 HID node is not accessible. Install a one-time udev rule:

  echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1dd2", ATTRS{idProduct}=="2443", \
MODE="0660", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/99-lbe-1420.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger

Then replug the device (your user must be in the 'plugdev' group)."""


if IS_LINUX:
    def _ioc(direction: int, type_: int, nr: int, size: int) -> int:
        """Encode a Linux ioctl request number (asm-generic layout)."""
        return (direction << 30) | (size << 16) | (type_ << 8) | nr


    # HIDIOCSFEATURE / HIDIOCGFEATURE for a report of LBE_REPORT_SIZE bytes.
    # Both directions are READ|WRITE because the kernel needs the buffer pointer.
    _IOC_WRITE = 1
    _IOC_READ = 2
    HIDIOCSFEATURE = _ioc(_IOC_READ | _IOC_WRITE, ord("H"), 0x06, LBE_REPORT_SIZE)
    HIDIOCGFEATURE = _ioc(_IOC_READ | _IOC_WRITE, ord("H"), 0x07, LBE_REPORT_SIZE)


def find_lbe1420_port() -> ListPortInfo | None:
    """Return the serial/NMEA port for the LBE-1420 on Linux or Windows."""
    for port in serial.tools.list_ports.comports():
        if IS_LINUX and not port.device.startswith("/dev/ttyACM"):
            continue

        if port.vid == LBE_1420_VID and port.pid == LBE_1420_PID:
            return port

        fields = (port.product, port.description, port.manufacturer, port.hwid)
        if any(f and LBE_1420_ID in f.lower() for f in fields):
            return port

    return None

def get_firmware_version(serial_number: str | None = None) -> str | None:
    """Return the LBE-1420 firmware version as a string (e.g. "1.07").

    The version is taken from the USB bcdDevice descriptor, which Leo Bodnar
    uses to encode the firmware revision. If serial_number is given, the
    matching device is selected so the right unit is read when several
    LBE-1420s are connected. Returns None if the device is not found.
    """
    if IS_LINUX:
        for dev in usb.core.find(
            find_all=True,
            idVendor=LBE_1420_VID,
            idProduct=LBE_1420_PID,
        ):
            if serial_number is not None and dev.serial_number != serial_number:
                continue

            bcd: int = dev.bcdDevice
            return f"{bcd >> 8:x}.{bcd & 0xFF:02x}"

        return None

    if IS_WINDOWS:
        for dev in hid.enumerate(LBE_1420_VID, LBE_1420_PID):
            if serial_number is not None and dev.get("serial_number") != serial_number:
                continue

            bcd = dev.get("release_number")
            if bcd is None:
                return None

            return f"{bcd >> 8:x}.{bcd & 0xFF:02x}"

        return None

    return None

def find_lbe1420_hid(serial_number: str | None = None) -> str | bytes | None:
    """Return the OS-specific HID handle/path for the LBE-1420."""
    if IS_WINDOWS:
        return find_lbe1420_hid_windows(serial_number)
    if IS_LINUX:
        return find_lbe1420_hid_linux(serial_number)

    raise RuntimeError(f"Unsupported OS: {platform.system()}")


def find_lbe1420_hid_linux(serial_number: str | None = None) -> str | None:
    """Return the /dev/hidraw* path for the LBE-1420's HID interface, or None.

    If serial_number is given, only the matching unit is returned.
    """
    hidraw_root = "/sys/class/hidraw"
    if not os.path.isdir(hidraw_root):
        return None
    for name in sorted(os.listdir(hidraw_root)):
        # Each hidraw node exposes its parent HID device's uevent in sysfs.
        try:
            with open(os.path.join(hidraw_root, name, "device", "uevent")) as f:
                fields: dict[str, str] = dict(
                    line.split("=", 1)
                    for line in f.read().splitlines()
                    if "=" in line
                )
        except OSError:
            continue
        # HID_ID has the form "BUS:VVVVVVVV:PPPPPPPP" (uppercase hex).
        parts = fields.get("HID_ID", "").split(":")
        if len(parts) != 3:
            continue
        try:
            vid, pid = int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            continue
        if vid != LBE_1420_VID or pid != LBE_1420_PID:
            continue
        # HID_UNIQ carries the USB serial number for disambiguation.
        if serial_number is not None and fields.get("HID_UNIQ") != serial_number:
            continue
        return f"/dev/{name}"
    return None

def find_lbe1420_hid_windows(serial_number: str | None = None) -> bytes | None:
    """Return the HIDAPI path for the LBE-1420 on Windows."""
    for dev in hid.enumerate(LBE_1420_VID, LBE_1420_PID):
        if serial_number is not None and dev.get("serial_number") != serial_number:
            continue

        product = (dev.get("product_string") or "").lower()
        manufacturer = (dev.get("manufacturer_string") or "").lower()

        if LBE_1420_ID in product or "leo bodnar" in manufacturer:
            return dev["path"]

        # VID/PID is likely sufficient if only one matching interface appears.
        return dev["path"]

    return None

def send_feature_report(hid_path: str | bytes, report: bytearray) -> None:
    """Send one HID feature report using the OS-specific backend."""
    if IS_WINDOWS:
        dev = hid.device()
        dev.open_path(hid_path)
        try:
            dev.send_feature_report(report)
        finally:
            dev.close()
        return

    if IS_LINUX:
        fd = os.open(hid_path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, HIDIOCSFEATURE, bytes(report))
        finally:
            os.close(fd)
        return

    raise RuntimeError(f"Unsupported OS: {platform.system()}")

def get_feature_report(hid_path: str | bytes, report_id: int) -> bytes:
    """Read one HID feature report using the OS-specific backend."""
    if IS_WINDOWS:
        dev = hid.device()
        dev.open_path(hid_path)
        try:
            return bytes(dev.get_feature_report(report_id, LBE_REPORT_SIZE))
        finally:
            dev.close()

    if IS_LINUX:
        report = bytearray(LBE_REPORT_SIZE)
        report[0] = report_id

        fd = os.open(hid_path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, HIDIOCGFEATURE, report)
        finally:
            os.close(fd)

        return bytes(report)

    raise RuntimeError(f"Unsupported OS: {platform.system()}")

def set_frequency(hid_path: str | bytes, hz: int) -> None:
    """Set the LBE-1420 OUT1 frequency (Hz) via a HID feature report.

    The frequency is sent directly to the device; its firmware performs the
    PLL synthesis internally.
    """
    # report[0] is the opcode/Report ID; report[1:5] is the frequency as a
    # little-endian uint32. The rest of the 60-byte report stays zero.
    report = bytearray(LBE_REPORT_SIZE)
    report[0] = LBE_1420_SET_F1
    report[1:5] = struct.pack("<I", hz)
    send_feature_report(hid_path, report)

def set_gnss(hid_path: str | bytes, mask: int) -> None:
    """Set which GNSS constellations the receiver uses, via a HID feature
    report.

    `mask` is the OR of GNSS_BITS values. report[0] is the opcode and
    report[1] carries the bitmask; the rest of the 60-byte report stays zero.
    The firmware reconfigures its internal receiver, so GPS lock drops
    briefly before re-acquiring.
    """
    report = bytearray(LBE_REPORT_SIZE)
    report[0] = LBE_1420_SET_GNSS
    report[1] = mask
    send_feature_report(hid_path, report)


def read_status(hid_path: str | bytes) -> dict[str, int | bool]:
    """Read the LBE-1420 status feature report and return it as a dict."""
    report = get_feature_report(hid_path, LBE_STATUS_REPORT_ID)

    raw = report[1]
    return {
        "raw_status": raw,
        "frequency1": int.from_bytes(report[6:10], "little"),
        "gps_locked": bool(raw & LBE_GPS_LOCK_BIT),
        "pll_locked": bool(raw & LBE_PLL_LOCK_BIT),
        "antenna_ok": bool(raw & LBE_ANT_OK_BIT),
    }


def _nmea_checksum_ok(line: str) -> bool:
    """Return True if an NMEA sentence's *XX checksum matches its body.

    The checksum is the XOR of every character between '$' and '*'.
    """
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cksum = line[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(cksum[:2], 16)
    except ValueError:
        return False


def _nmea_coord(value: str, hemi: str) -> float | None:
    """Convert an NMEA ddmm.mmmm / dddmm.mmmm coordinate to signed degrees.

    NMEA encodes coordinates as (degrees * 100 + minutes); the minutes are
    always the two digits immediately left of the decimal point. The sign is
    negative for the southern and western hemispheres.
    """
    if not value or "." not in value:
        return None
    try:
        dot = value.index(".")
        degrees = int(value[: dot - 2])
        minutes = float(value[dot - 2:])
    except (ValueError, IndexError):
        return None
    decimal = degrees + minutes / 60.0
    return -decimal if hemi in ("S", "W") else decimal


def read_gnss_status(port_device: str, duration: float = STATUS_READ_SECONDS) -> GnssSnapshot:
    """Capture the NMEA stream for `duration` seconds and return an aggregated
    GNSS snapshot. Later sentences overwrite earlier ones, so the result
    reflects the most recent value seen for each field."""
    snap: GnssSnapshot = {
        "fix_quality": None, "fix_type": None, "valid": None,
        "sats_used": None, "lat": None, "lon": None, "altitude_m": None,
        "utc": None, "date": None,
        "pdop": None, "hdop": None, "vdop": None,
        # satellites: (constellation, prn) -> {elev, azim, cno}
        "satellites": {},
        "sentences": 0,
    }
    deadline = time.monotonic() + duration
    with serial.Serial(port_device, NMEA_BAUD, timeout=1) as ser:
        while time.monotonic() < deadline:
            line = ser.readline().decode("ascii", errors="ignore").strip()
            # Drop blank lines and anything that fails checksum validation.
            if not line or not _nmea_checksum_ok(line):
                continue
            snap["sentences"] += 1
            # Strip the trailing "*XX" checksum, then split into CSV fields.
            fields = line.split("*")[0].split(",")
            # "$GPGGA" -> talker "GP", sentence type "GGA".
            talker, stype = line[1:3], line[3:6]

            if stype == "GGA" and len(fields) >= 10:
                # GGA: time, position, fix quality, satellites used, altitude.
                snap["utc"] = fields[1] or snap["utc"]
                snap["lat"] = _nmea_coord(fields[2], fields[3]) or snap["lat"]
                snap["lon"] = _nmea_coord(fields[4], fields[5]) or snap["lon"]
                snap["fix_quality"] = fields[6] or snap["fix_quality"]
                snap["sats_used"] = fields[7] or snap["sats_used"]
                snap["altitude_m"] = fields[9] or snap["altitude_m"]
            elif stype == "RMC" and len(fields) >= 10:
                # RMC: validity flag (A/V) and the calendar date.
                snap["valid"] = fields[2] == "A"
                snap["utc"] = fields[1] or snap["utc"]
                snap["date"] = fields[9] or snap["date"]
                snap["lat"] = _nmea_coord(fields[3], fields[4]) or snap["lat"]
                snap["lon"] = _nmea_coord(fields[5], fields[6]) or snap["lon"]
            elif stype == "GSA" and len(fields) >= 18:
                # GSA: fix type (1/2/3) and dilution-of-precision values.
                snap["fix_type"] = fields[2] or snap["fix_type"]
                snap["pdop"] = fields[15] or snap["pdop"]
                snap["hdop"] = fields[16] or snap["hdop"]
                snap["vdop"] = fields[17] or snap["vdop"]
            elif stype == "GSV":
                # GSV: satellites in view, in repeating 4-field blocks of
                # (PRN, elevation, azimuth, C/N0) starting at index 4.
                constellation = _CONSTELLATION.get(talker, talker)
                for i in range(4, len(fields) - 3, 4):
                    prn = fields[i]
                    if not prn:
                        continue
                    cno = fields[i + 3]
                    snap["satellites"][(constellation, prn)] = {
                        "elev": fields[i + 1] or None,
                        "azim": fields[i + 2] or None,
                        # C/N0 is blank for satellites that are tracked but
                        # not yet contributing usable signal.
                        "cno": int(cno) if cno.isdigit() else None,
                    }
    return snap


def cmd_info() -> int:
    """Print the connected LBE-1420's identity and firmware version."""
    port = find_lbe1420_port()
    if port is None:
        print("No LBE-1420 serial/NMEA port found.")
        return 1
    firmware = get_firmware_version(port.serial_number)
    print(f"LBE-1420 found at {port.device}")
    print(f"  product:      {port.product}")
    print(f"  manufacturer: {port.manufacturer}")
    print(f"  serial:       {port.serial_number}")
    print(f"  USB VID:PID:  {port.vid:04x}:{port.pid:04x}")
    print(f"  firmware:     {firmware if firmware else 'unknown'}")
    return 0


def cmd_set_f1(hz: int) -> int:
    """Set the OUT1 frequency to hz and report the resulting device state."""
    if not 1 <= hz <= LBE_1420_MAX_FREQ:
        print(f"Frequency must be 1 .. {LBE_1420_MAX_FREQ} Hz (got {hz}).")
        return 1

    # Resolve the serial port first so the HID node can be matched by serial
    # number, picking the right unit when several are connected.
    port = find_lbe1420_port()
    serial_number = port.serial_number if port else None
    hid_path = find_lbe1420_hid(serial_number)
    if hid_path is None:
        print("No LBE-1420 HID interface found.")
        return 1

    print(f"Setting OUT1 to {hz} Hz via HID ...")
    try:
        set_frequency(hid_path, hz)
    except PermissionError:
        if IS_LINUX:
            print(UDEV_HELP)
        else:
            print("Permission denied opening the HID interface. Close the Leo Bodnar config tool and try again.")
        return 1
    except OSError as exc:
        print(f"Failed to set frequency: {exc}")
        return 1

    # Read the status report back to confirm the new setting took effect.
    try:
        status = read_status(hid_path)
    except OSError as exc:
        print(f"Frequency set, but status read-back failed: {exc}")
        return 0

    print(f"  device reports OUT1: {status['frequency1']} Hz")
    print(f"  GPS lock:            {'yes' if status['gps_locked'] else 'no'}")
    print(f"  PLL lock:            {'yes' if status['pll_locked'] else 'no'}")
    if not status["pll_locked"]:
        print("  (PLL may take a few seconds to re-lock after a change.)")
    return 0


def _parse_gnss_spec(spec: str) -> tuple[int, list[str]] | str:
    """Resolve a --gnss argument to (mask, constellation names).

    Accepts a comma-separated list of constellation names, the keyword
    'default' (GPS + SBAS) or 'all' (every named constellation). Returns an
    error string instead if the spec is empty or names something unknown.
    """
    spec = spec.strip().lower()
    if spec == "default":
        names = [n for n, b in GNSS_BITS.items() if b & GNSS_DEFAULT_MASK]
        return GNSS_DEFAULT_MASK, names
    if spec == "all":
        return sum(GNSS_BITS.values()), list(GNSS_BITS)
    # dict.fromkeys drops duplicates (e.g. "gps,gps") while keeping order.
    names = list(dict.fromkeys(n.strip() for n in spec.split(",") if n.strip()))
    if not names:
        return "No constellations given."
    unknown = [n for n in names if n not in GNSS_BITS]
    if unknown:
        return (f"Unknown constellation(s): {', '.join(unknown)}. "
                f"Valid: {', '.join(GNSS_BITS)} (or 'default', 'all').")
    mask = 0
    for n in names:
        mask |= GNSS_BITS[n]
    return mask, names


def cmd_gnss(spec: str) -> int:
    """Set which GNSS constellations the LBE-1420's receiver uses."""
    parsed = _parse_gnss_spec(spec)
    if isinstance(parsed, str):
        print(parsed)
        return 1
    mask, names = parsed
    if mask == 0:
        print("Refusing to disable every constellation -- the receiver "
              "needs at least one (e.g. --gnss gps).")
        return 1

    # Resolve the serial port first so the HID node can be matched by serial
    # number, picking the right unit when several are connected.
    port = find_lbe1420_port()
    serial_number = port.serial_number if port else None
    hid_path = find_lbe1420_hid(serial_number)
    if hid_path is None:
        print("No LBE-1420 HID interface found.")
        return 1

    print(f"Enabling GNSS constellations: {', '.join(sorted(names))} "
        f"(mask 0x{mask:02x}) via HID ...")
    try:
        set_gnss(hid_path, mask)
    except PermissionError:
        if IS_LINUX:
            print(UDEV_HELP)
        else:
            print("Permission denied opening the HID interface. Close the Leo Bodnar config tool and try again.")
        return 1
    except OSError as exc:
        print(f"Failed to set GNSS constellations: {exc}")
        return 1

    # The setting is not echoed by the status report, so there is nothing to
    # read back -- report what was sent and warn about the lock recovery.
    print("  sent. The receiver re-acquires satellites: GPS lock drops "
          "briefly, then recovers.")
    return 0


def _format_position(snap: GnssSnapshot) -> str:
    """Render the calculated position from a GNSS snapshot."""
    if snap["lat"] is None or snap["lon"] is None:
        return "n/a (no fix)"
    pos = f"{snap['lat']:.6f}, {snap['lon']:.6f}"
    if snap["altitude_m"]:
        pos += f"  (altitude {snap['altitude_m']} m)"
    return pos


def cmd_status() -> int:
    """Report GNSS conditions from the NMEA stream plus device lock state."""
    port = find_lbe1420_port()
    if port is None:
        print("No LBE-1420 serial/NMEA port found.")
        return 1

    print(f"Capturing NMEA for {STATUS_READ_SECONDS:.0f}s on {port.device} ...")
    try:
        snap = read_gnss_status(port.device)
    except (OSError, serial.SerialException) as exc:
        print(f"Failed to read NMEA stream: {exc}")
        return 1

    if snap["sentences"] == 0:
        print(f"No NMEA data received on {port.device}.")
        return 1

    # Derive a single fix description from the GSA fix type and GGA quality.
    fix_types = {"1": "no fix", "2": "2D fix", "3": "3D fix"}
    fix = fix_types.get(snap["fix_type"] or "", "unknown")
    if snap["fix_quality"] in (None, "0"):
        fix = "no fix"

    print()
    print("LBE-1420 GNSS status")
    print(f"  fix:        {fix}", end="")
    if snap["valid"] is not None:
        print(f"  ({'valid' if snap['valid'] else 'invalid'})")
    else:
        print()
    print(f"  UTC:        {snap['utc'] or 'n/a'}  date {snap['date'] or 'n/a'}")
    print(f"  position:   {_format_position(snap)}")
    dop = (snap["pdop"], snap["hdop"], snap["vdop"])
    if any(dop):
        print(f"  DOP:        PDOP {dop[0] or '-'}  HDOP {dop[1] or '-'}  "
              f"VDOP {dop[2] or '-'}")

    sats: dict[tuple[str, str], dict[str, Any]] = snap["satellites"]
    print(f"  satellites: {snap['sats_used'] or '0'} used / "
          f"{len(sats)} in view")

    # Group satellites by constellation and report per-constellation C/N0.
    by_constellation: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for (constellation, prn), info in sats.items():
        by_constellation.setdefault(constellation, []).append((prn, info))
    for constellation in sorted(by_constellation):
        # Sort by C/N0 descending; satellites with no C/N0 go last.
        entries = sorted(
            by_constellation[constellation],
            key=lambda e: (e[1]["cno"] is None, -(e[1]["cno"] or 0)),
        )
        cnos = [i["cno"] for _, i in entries if i["cno"] is not None]
        summary = f"{len(entries)} sats"
        if cnos:
            summary += (f", C/N0 best {max(cnos)} / "
                        f"avg {sum(cnos) // len(cnos)} dB-Hz")
        print(f"    {constellation:<9} {summary}")
        for prn, info in entries:
            cno = f"{info['cno']:>2} dB-Hz" if info["cno"] is not None else "--"
            print(f"      PRN {prn:>3}  elev {info['elev'] or '-':>3}  "
                  f"azim {info['azim'] or '-':>3}  C/N0 {cno}")

    # Device-level lock state from the HID status report (best effort: the
    # GNSS data above is still useful even if the HID node is inaccessible).
    hid_path = find_lbe1420_hid(port.serial_number)
    print("  device (HID):")
    if hid_path is None:
        print("    HID interface not found.")
        return 0
    try:
        status = read_status(hid_path)
    except PermissionError:
        if IS_LINUX:
            print("    not accessible -- run --f1 once for the udev-rule hint.")
        else:
            print("    not accessible -- close the Leo Bodnar config tool and try again.")
        return 0
    except OSError as exc:
        print(f"    status read failed: {exc}")
        return 0
    print(f"    GPS lock:  {'yes' if status['gps_locked'] else 'no'}")
    print(f"    PLL lock:  {'yes' if status['pll_locked'] else 'no'}")
    print(f"    antenna:   {'OK' if status['antenna_ok'] else 'fault'}")
    print(f"    OUT1:      {status['frequency1']} Hz")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configuration utility for the Leo Bodnar LBE-1420."
    )
    # --f1, --gnss and --status are distinct actions and cannot be combined.
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--f1",
        type=int,
        metavar="HZ",
        help=f"set OUT1 frequency in Hz (1 .. {LBE_1420_MAX_FREQ})",
    )
    group.add_argument(
        "--gnss",
        metavar="LIST",
        help="set enabled GNSS constellations: a comma-separated list of "
             f"{', '.join(GNSS_BITS)}, or 'default' (GPS+SBAS) or 'all'",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="report GNSS conditions (satellites, C/N0, position, lock)",
    )
    args = parser.parse_args()

    if args.f1 is not None:
        return cmd_set_f1(args.f1)
    if args.gnss is not None:
        return cmd_gnss(args.gnss)
    if args.status:
        return cmd_status()
    # No action requested: fall back to printing device identity.
    return cmd_info()


if __name__ == "__main__":
    sys.exit(main())
