# LBE-1420 Configuration Utility

A command-line tool for the [Leo Bodnar LBE-1420](https://www.leobodnar.com/shop/index.php?main_page=product_info&products_id=393)
GPS-locked clock source. It locates the device, reports its firmware version,
sets the OUT1 frequency, selects which GNSS constellations the receiver uses,
and reports GNSS conditions. It runs on both Linux and Windows.

## Requirements

- Python 3.12 or newer
- Dependencies in `requirements.txt`:

  ```sh
  pip install -r requirements.txt
  ```

  `pyserial` is used on both platforms for CDC serial-port discovery and
  reading the NMEA stream. The HID backend is platform-specific, and the
  markers in `requirements.txt` install only what each OS needs:

  - **Linux** — `pyusb` for USB descriptor reads; HID feature reports use
    the Python standard library (`fcntl`/`ioctl`), so no `hidapi` is needed.
  - **Windows** — `hidapi` for HID feature reports and the firmware version.

## Usage

```sh
# Identify the device and report its firmware version
python3 lbe-1420-conf.py

# Set the OUT1 frequency (Hz); range 1 .. 1,600,000,000
python3 lbe-1420-conf.py --f1 1420000000

# Select which GNSS constellations the receiver uses
python3 lbe-1420-conf.py --gnss gps,galileo,beidou
python3 lbe-1420-conf.py --gnss recommended  # GPS + SBAS + Galileo + BeiDou
python3 lbe-1420-conf.py --gnss default      # GPS + SBAS (factory default)
python3 lbe-1420-conf.py --gnss all

# Report GNSS conditions: fix, position, satellites, C/N0, lock state
python3 lbe-1420-conf.py --status
```

`--gnss` accepts a comma-separated list of `gps`, `sbas`, `galileo`,
`beidou`, or one of these keywords:

- `recommended` — GPS + SBAS + Galileo + BeiDou, the set used for this
  application.
- `default` — GPS + SBAS, the factory default.
- `all` — every selectable constellation.

Changing the set makes the receiver re-acquire satellites, so GPS lock
drops briefly before recovering.

**GLONASS is intentionally not selectable.** On the LBE-1420 firmware
tested here, BeiDou and GLONASS cannot be enabled at the same time —
selecting one blocks the other. Since this application uses BeiDou and
does not need GLONASS, GLONASS is left out rather than offered as a choice
that would silently conflict.

## How it works

The LBE-1420 exposes two interfaces over USB:

- A **CDC serial port** (`/dev/ttyACM*`) that streams NMEA sentences. `--status`
  parses this stream for fix quality, calculated position, satellites in view,
  and per-satellite C/N0. This port is accessible to the `dialout` group.
- A **HID interface** used for configuration — `/dev/hidraw*` on Linux,
  reached through HIDAPI on Windows. `--f1` sends the desired frequency
  directly in Hz as a HID feature report; the device firmware performs the
  internal PLL synthesis. `--gnss` sends a constellation-enable bitmask the
  same way (opcode `0x07`); the firmware then reconfigures its internal GNSS
  receiver. That opcode and bit layout were recovered by USB-capturing the
  Windows configuration tool, since they are not documented.

The device is identified by its USB descriptor (`product` string and the
`1dd2:2443` vendor/product ID), and the firmware version is read from the
`bcdDevice` descriptor field.

## HID access

### Linux (one-time setup)

`/dev/hidraw*` is root-only by default, so `--f1` and `--gnss` need a udev
rule:

```sh
echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1dd2", ATTRS{idProduct}=="2443", MODE="0660", GROUP="plugdev"' \
  | sudo tee /etc/udev/rules.d/99-lbe-1420.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then replug the device. Your user must be in the `plugdev` group. Running
`--f1` without this rule prints the same instructions.

### Windows

No setup is required. However, only one program can hold the HID interface
at a time — close the Leo Bodnar configuration tool before running `--f1`
or `--gnss`, or the write fails with a permission error.

## Credits

The HID feature-report protocol for setting the frequency (opcodes, report
layout, status fields) is based on the reverse-engineering work in
[bvernoux/lbe-142x](https://github.com/bvernoux/lbe-142x), a cross-platform
LBE-142x configuration tool. The
[hamarituc/lbgpsdo](https://github.com/hamarituc/lbgpsdo) project was also a
useful reference for the older Leo Bodnar GPSDO protocol.

## Other Info
- The command line interface tool that is offered by Leo Bodnar is [simontheu/lbe-1420](https://github.com/simontheu/lbe-1420).
- Firmware updates still have to be done using the Windows utility from the product page for the LBE-1420 [here](https://www.leobodnar.com/shop/index.php?main_page=product_info&products_id=393). The current firmware version as of May 2026 is 1.08.
