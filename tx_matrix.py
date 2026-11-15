#!/usr/bin/env python3
"""Transmit compact JSON through a full-screen composite-video data matrix."""

import argparse
import json
import math
import secrets
import sys
import time
import tkinter as tk
from datetime import datetime, timezone
import zlib


MAGIC = b"TM"
VERSION = 1
LOGICAL_ROWS = 64
CELLS = 32
FRAGMENT_DATA = 51
SYNC = (1, 0, 1, 0)
END = (0, 1)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def crc8(data):
    result = 0
    for byte in data:
        result ^= byte
        for _ in range(8):
            result = ((result << 1) ^ 0x07) & 0xFF if result & 0x80 else (result << 1) & 0xFF
    return result


def bits(value, width):
    return tuple((value >> shift) & 1 for shift in range(width - 1, -1, -1))


def row_code(frame_sequence, row, byte):
    check = crc8(bytes((frame_sequence, row, byte)))
    encoded = SYNC + bits(frame_sequence, 4) + bits(row, 6) + bits(byte, 8) + bits(check, 8) + END
    assert len(encoded) == CELLS
    return encoded


def fragment(message, message_id, transmission_index, fragment_index, fragment_count):
    payload = message[fragment_index * FRAGMENT_DATA:(fragment_index + 1) * FRAGMENT_DATA]
    header = (MAGIC + bytes((VERSION,)) + message_id.to_bytes(2, "little") +
              bytes((transmission_index, fragment_index, fragment_count, len(payload))))
    body = header + payload.ljust(FRAGMENT_DATA, b"\0")
    assert len(body) == 60
    packet = body + zlib.crc32(body).to_bytes(4, "little")
    assert len(packet) == LOGICAL_ROWS
    return packet


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if not isinstance(payload.get("object_class"), str) or not payload["object_class"]:
        raise ValueError("object_class must be a non-empty string")
    location = payload.get("location")
    if not isinstance(location, dict):
        raise ValueError("location must be an object containing lat and lon")
    for key in ("lat", "lon"):
        if not isinstance(location.get(key), (int, float)):
            raise ValueError(f"location.{key} must be numeric")
    if not -90 <= location["lat"] <= 90:
        raise ValueError("location.lat must be between -90 and 90")
    if not -180 <= location["lon"] <= 180:
        raise ValueError("location.lon must be between -180 and 180")
    if "received_at" in payload:
        raise ValueError("received_at is added by the receiver; use observed_at for the source time")
    payload.setdefault("observed_at", utc_now())
    return payload


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--payload-json", help="complete JSON object; received_at is receiver-owned")
    source.add_argument("--payload-file", help="path to a JSON object")
    parser.add_argument("--object-class", default="test_object")
    parser.add_argument("--lat", type=float, default=51.5074)
    parser.add_argument("--lon", type=float, default=-0.1278)
    parser.add_argument("--observed-at")
    parser.add_argument("--count", type=int, default=1,
                        help="complete message transmissions; 0 loops indefinitely")
    parser.add_argument("--hold-ms", type=int, default=350,
                        help="time each 51-byte fragment remains on screen")
    args = parser.parse_args()
    if args.count < 0:
        parser.error("--count must be zero or positive")
    if args.hold_ms < 150:
        parser.error("--hold-ms below 150 is not supported by this first hardware format")
    return args


def payload_from_args(args):
    if args.payload_json:
        payload = json.loads(args.payload_json)
    elif args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = {
            "object_class": args.object_class,
            "location": {"lat": args.lat, "lon": args.lon},
            "observed_at": args.observed_at or utc_now(),
        }
    return validate_payload(payload)


def main():
    args = arguments()
    payload = payload_from_args(args)
    message = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fragment_count = math.ceil(len(message) / FRAGMENT_DATA)
    if fragment_count > 255:
        raise SystemExit("payload is too large (maximum 13005 UTF-8 bytes)")
    message_id = secrets.randbelow(65536)

    print(json.dumps({
        "message_id": message_id,
        "payload_bytes": len(message),
        "fragments": fragment_count,
        "count": args.count,
        "payload": payload,
    }, indent=2), flush=True)

    root = tk.Tk()
    root.configure(background="black")
    root.attributes("-fullscreen", True)
    root.bind("<Escape>", lambda _event: root.destroy())
    canvas = tk.Canvas(root, highlightthickness=0, borderwidth=0, background="black")
    canvas.pack(fill="both", expand=True)
    state = {"transmission": 0, "fragment": 0, "frame_sequence": 0}

    def render_packet(packet, frame_sequence):
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.delete("all")
        for row, byte in enumerate(packet):
            y0 = round(row * height / LOGICAL_ROWS)
            y1 = round((row + 1) * height / LOGICAL_ROWS)
            for cell, bit in enumerate(row_code(frame_sequence, row, byte)):
                x0 = round(cell * width / CELLS)
                x1 = round((cell + 1) * width / CELLS)
                canvas.create_rectangle(x0, y0, x1 + 1, y1 + 1,
                                        fill="white" if bit else "black", outline="")

    def advance():
        transmission = state["transmission"]
        fragment_index = state["fragment"]
        frame_sequence = state["frame_sequence"]
        packet = fragment(message, message_id, transmission & 0xFF,
                          fragment_index, fragment_count)
        render_packet(packet, frame_sequence)
        root.title(f"Telemetry message {message_id} pass {transmission + 1} "
                   f"fragment {fragment_index + 1}/{fragment_count}")
        print(f"TX message={message_id} pass={transmission + 1} "
              f"fragment={fragment_index + 1}/{fragment_count} frame={frame_sequence}", flush=True)

        state["frame_sequence"] = (frame_sequence + 1) & 0x0F
        state["fragment"] += 1
        if state["fragment"] >= fragment_count:
            state["fragment"] = 0
            state["transmission"] += 1
            if args.count and state["transmission"] >= args.count:
                root.after(args.hold_ms, root.destroy)
                return
        root.after(args.hold_ms, advance)

    root.after(300, advance)
    root.mainloop()
    print("TX complete", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
