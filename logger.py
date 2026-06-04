import os
import sys
import time
from pathlib import Path

import serial


# ----------------------------
# Configuration
# ----------------------------
PORT = "/dev/ttyACM4"
BAUD = 115200
TIMEOUT = 1.0

OUTPUT_DIR = Path("logs")
FILE_PREFIX = "tinypfa_lb1420_splitter_nocleaner_test_10ms_unwrapped_6-02-26"
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB


def make_log_filename(output_dir: Path, prefix: str) -> Path:
    """
    Create a new log filename with a timestamp.
    """
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{prefix}_{timestamp}.txt"


def open_new_log_file(output_dir: Path, prefix: str):
    """
    Open a new log file for append and return (path, file_handle).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = make_log_filename(output_dir, prefix)
    fh = open(path, "a", buffering=1)  # line-buffered
    print(f"Opened log file: {path}")
    return path, fh


def should_roll_file(fh, max_size_bytes: int) -> bool:
    """
    Check whether the current file has reached the rollover threshold.
    """
    fh.flush()
    return fh.tell() >= max_size_bytes


def main():
    current_path = None
    log_fh = None
    ser = None

    try:
        current_path, log_fh = open_new_log_file(OUTPUT_DIR, FILE_PREFIX)

        print(f"Opening serial port {PORT} at {BAUD} baud...")
        ser = serial.Serial(PORT, BAUD, timeout=TIMEOUT)

        # Optional but helpful after reconnects
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print("Logging started. Press Ctrl+C to stop.\n")

        while True:
            raw = ser.readline()

            if not raw:
                continue

            # Keep the raw data as text, but avoid crashing on bad bytes
            line = raw.decode("ascii", errors="replace")

            # Write exactly what came in
            log_fh.write(line)

            # Optional: also echo to terminal
            print(line, end="")

            if should_roll_file(log_fh, MAX_FILE_SIZE_BYTES):
                log_fh.close()
                print("\nMax file size reached, rolling to a new file.")
                current_path, log_fh = open_new_log_file(OUTPUT_DIR, FILE_PREFIX)

    except KeyboardInterrupt:
        print("\nCtrl+C received. Shutting down cleanly...")

    except serial.SerialException as e:
        print(f"\nSerial error: {e}", file=sys.stderr)
        sys.exit(1)

    finally:
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
