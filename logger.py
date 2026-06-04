"""
Collect data from a tinyPFA (precision phase/frequency analyzer) over a serial
connection and write it to disk. The resulting log can be analyzed later to
derive clocking metrics and to evaluate (GNSS receiver) options.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import serial


# ----------------------------
# Configuration
# ----------------------------
# Serial port settings for the tinyPFA's USB-CDC interface.
PORT = "/dev/ttyACM4"
BAUD = 115200
TIMEOUT = 1.0

# Default rollover threshold in megabytes (overridable via --max-size).
DEFAULT_MAX_FILE_SIZE_MB = 10

# In non-verbose mode, print a status line at most this often (seconds).
STATUS_INTERVAL_SEC = 10.0


def default_savefile() -> str:
    """
    Build the default save-file name using the current local time, formatted
    as log_YYYY_MM_DD_HH_MM_SS.txt.
    """
    timestamp = time.strftime("%Y_%m_%d_%H_%M_%S")
    return f"log_{timestamp}.txt"


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments. Currently only the output save-file path
    is configurable.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Collect tinyPFA serial data into a log file for later analysis "
            "of clocking metrics and GNSS receiver options."
        )
    )
    parser.add_argument(
        "--savefile",
        type=Path,
        default=Path(default_savefile()),
        help=(
            "Path to the log file to write. Defaults to "
            "log_{YYYY_MM_DD_HH_MM_SS}.txt in the current directory."
        ),
    )
    parser.add_argument(
        "--max-size",
        type=float,
        default=DEFAULT_MAX_FILE_SIZE_MB,
        help=(
            "Maximum log-file size in megabytes (MB) before rolling over "
            f"to a new file. Defaults to {DEFAULT_MAX_FILE_SIZE_MB} MB."
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help=(
            "Echo every measurement to the terminal as it is logged. "
            "Without this flag, a status line (total data collected) is "
            f"printed every {int(STATUS_INTERVAL_SEC)} seconds instead."
        ),
    )
    return parser.parse_args()


def roll_path(base: Path, index: int) -> Path:
    """
    Build a rolled-over file name by appending an index before the suffix,
    e.g. log_....txt -> log_...._1.txt for the first rollover.
    """
    return base.with_name(f"{base.stem}_{index}{base.suffix}")


def open_log_file(path: Path):
    """
    Ensure the parent directory exists and open the file in line-buffered
    append mode. Returns the open file handle.
    """
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a", buffering=1)  # line-buffered so partial logs are visible
    print(f"Opened log file: {path}")
    return fh


def should_roll_file(fh, max_size_bytes: int) -> bool:
    """
    Check whether the current file has reached the rollover threshold.
    """
    fh.flush()
    return fh.tell() >= max_size_bytes


def format_bytes(num_bytes: int) -> str:
    """
    Render a byte count as a human-readable string (B / KB / MB / GB).
    """
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.2f} {unit}"
        size /= 1024.0


def main():
    args = parse_args()

    # Convert the MB-valued CLI argument into bytes for comparison against
    # the file handle's byte offset.
    max_file_size_bytes = int(args.max_size * 1024 * 1024)

    # Track the active log path/handle, plus a rollover counter so each new
    # file in a single run gets a unique name.
    base_path: Path = args.savefile
    current_path: Path = base_path
    roll_index = 0

    # Counters/timers used for the periodic status line in non-verbose mode.
    total_bytes_collected = 0
    start_time = time.monotonic()
    last_status_time = start_time

    log_fh = None
    ser = None

    try:
        # Open the initial log file before touching the serial port so that
        # if file creation fails we never leave the port open.
        log_fh = open_log_file(current_path)

        print(f"Opening serial port {PORT} at {BAUD} baud...")
        ser = serial.Serial(PORT, BAUD, timeout=TIMEOUT)

        # Flush any stale data sitting in the OS buffers from a previous
        # session so we start logging from a clean boundary.
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print("Logging started. Press Ctrl+C to stop.\n")

        while True:
            # Block until a full line arrives or the read times out.
            raw = ser.readline()

            # Timeout with no data — loop and try again.
            if not raw:
                continue

            # Decode as ASCII; replace any garbage bytes rather than crashing
            # so a transient line-noise glitch doesn't kill the logger.
            line = raw.decode("ascii", errors="replace")

            # Persist the line to disk; in verbose mode also mirror it to the
            # terminal so the operator can watch the stream live.
            log_fh.write(line)
            total_bytes_collected += len(line)

            if args.verbose:
                print(line, end="")
            else:
                # Quiet mode: emit a periodic status line so the operator
                # knows the logger is still alive and how much has been
                # captured so far.
                now = time.monotonic()
                if now - last_status_time >= STATUS_INTERVAL_SEC:
                    elapsed = now - start_time
                    print(
                        f"[status] time elapsed = {elapsed:7.1f}s  "
                        f"data collected={format_bytes(total_bytes_collected)}  "
                        f"file = {current_path}"
                    )
                    last_status_time = now

            # When the file gets large, close it and start a new one with an
            # incrementing suffix to keep individual files manageable.
            if should_roll_file(log_fh, max_file_size_bytes):
                log_fh.close()
                print("\nMax file size reached, rolling to a new file.")
                roll_index += 1
                current_path = roll_path(base_path, roll_index)
                log_fh = open_log_file(current_path)

    except KeyboardInterrupt:
        # Normal way to stop the logger: user hits Ctrl+C.
        print("\nCtrl+C received. Shutting down cleanly...")

    except serial.SerialException as e:
        # Cable unplugged, port disappeared, permission denied, etc.
        print(f"\nSerial error: {e}", file=sys.stderr)
        sys.exit(1)

    finally:
        # Best-effort cleanup: close the serial port and flush/close the log
        # file. Swallow secondary exceptions so we always return cleanly.
        if ser is not None:
            try:
                if ser.is_open:
                    ser.close()
                    print("Serial port closed.")
            except Exception:
                pass

        if log_fh is not None:
            try:
                log_fh.flush()
                os.fsync(log_fh.fileno())
                log_fh.close()
                print(f"Log file closed: {current_path}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
