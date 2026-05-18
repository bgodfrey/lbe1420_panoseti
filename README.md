# LBE-1420 Configuration Utility

A command-line tool for the [Leo Bodnar LBE-1420](https://www.leobodnar.com/shop/index.php?main_page=product_info&products_id=393)
GPS-locked clock source. It locates the device, reports its firmware version,
sets the OUT1 frequency, and reports GNSS conditions.

## Requirements

- Python 3.12 or newer
- Dependencies in `requirements.txt`:

  ```sh
  pip install -r requirements.txt
  ```

  Only `pyserial` and `pyusb` are needed — HID access uses the Python
  standard library (`fcntl`/`ioctl`), so no `hidapi` package is required.

## Usage

```sh
# Identify the device and report its firmware version
python3 lbe-1420-conf.py

# Set the OUT1 frequency (Hz); range 1 .. 1,600,000,000
python3 lbe-1420-conf.py --f1 1420000000

# Report GNSS conditions: fix, position, satellites, C/N0, lock state
python3 lbe-1420-conf.py --status
```

## How it works

The LBE-1420 exposes two interfaces over USB:

- A **CDC serial port** (`/dev/ttyACM*`) that streams NMEA sentences. `--status`
  parses this stream for fix quality, calculated position, satellites in view,
  and per-satellite C/N0. This port is accessible to the `dialout` group.
- A **HID interface** (`/dev/hidraw*`) used for configuration. `--f1` sends the
  desired frequency directly in Hz as a HID feature report; the device firmware
  performs the internal PLL synthesis.

The device is identified by its USB descriptor (`product` string and the
`1dd2:2443` vendor/product ID), and the firmware version is read from the
`bcdDevice` descriptor field.

## HID access (one-time setup)

`/dev/hidraw*` is root-only by default, so `--f1` needs a udev rule:

```sh
echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1dd2", ATTRS{idProduct}=="2443", MODE="0660", GROUP="plugdev"' \
  | sudo tee /etc/udev/rules.d/99-lbe-1420.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then replug the device. Your user must be in the `plugdev` group. Running
`--f1` without this rule prints the same instructions.

## Credits

The HID feature-report protocol for setting the frequency (opcodes, report
layout, status fields) is based on the reverse-engineering work in
[bvernoux/lbe-142x](https://github.com/bvernoux/lbe-142x), a cross-platform
LBE-142x configuration tool. The
[hamarituc/lbgpsdo](https://github.com/hamarituc/lbgpsdo) project was also a
useful reference for the older Leo Bodnar GPSDO protocol.

## Other Info
- The command line interface that can be run from the command line is [simontheu/lbe-1420](https://github.com/simontheu/lbe-1420).
- Firmware updates still have to be done using the Windows utility from the product page for the LBE-1420 [here](https://www.leobodnar.com/shop/index.php?main_page=product_info&products_id=393). The current firmware version as of May 2026 is 1.08.
