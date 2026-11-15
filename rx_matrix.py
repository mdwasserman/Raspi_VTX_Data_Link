#!/usr/bin/env python3
"""Receive full-screen JSON data frames from the ESPsoup VFM USB stream."""

import argparse
import collections
from datetime import datetime, timezone
import json
import statistics
import sys
import time
import zlib

import serial


MAGIC = b"TM"
VERSION = 1
LOGICAL_ROWS = 64
CELLS = 32
FRAGMENT_DATA = 51
SYNC = (1, 0, 1, 0)
END = (0, 1)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def crc8(data):
    result = 0
    for byte in data:
        result ^= byte
        for _ in range(8):
            result = ((result << 1) ^ 0x07) & 0xFF if result & 0x80 else (result << 1) & 0xFF
    return result


class Device:
    def __init__(self, port):
        self.serial = serial.Serial()
        self.serial.port = port
        self.serial.baudrate = 2_000_000
        self.serial.timeout = 0.05
        self.serial.dtr = False
        self.serial.rts = False
        self.serial.open()
        self.buffer = bytearray()
        self.synchronise()

    def fill(self, deadline):
        block = self.serial.read(self.serial.in_waiting or 1)
        if block:
            self.buffer.extend(block)
        elif time.time() > deadline:
            raise TimeoutError("serial timeout")

    def read(self, size, timeout=5):
        deadline = time.time() + timeout
        while len(self.buffer) < size:
            self.fill(deadline)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def line(self, timeout=5):
        deadline = time.time() + timeout
        while b"\n" not in self.buffer:
            self.fill(deadline)
        end = self.buffer.index(b"\n")
        result = bytes(self.buffer[:end]).decode("utf-8", "replace").strip()
        del self.buffer[:end + 1]
        return result

    def command(self, command):
        self.serial.write((command + "\n").encode())

    def ask(self, command, timeout=5):
        self.command(command)
        return self.line(timeout)

    def synchronise(self):
        for _attempt in range(4):
            marker = f"SYNC {time.time_ns()}"
            self.serial.reset_input_buffer()
            self.buffer.clear()
            self.command("\n" + marker)
            deadline = time.time() + 2
            try:
                while True:
                    self.fill(deadline)
                    token = (marker + "\n").encode()
                    if token in self.buffer:
                        start = self.buffer.index(token) + len(token)
                        del self.buffer[:start]
                        return
            except TimeoutError:
                pass
        raise RuntimeError("could not synchronise with ESPsoup")


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def cells_from_line(pixels):
    means = []
    for cell in range(CELLS):
        start = round(cell * len(pixels) / CELLS)
        end = round((cell + 1) * len(pixels) / CELLS)
        inner = pixels[start + 1:end - 1] if end - start >= 4 else pixels[start:end]
        means.append(statistics.fmean(inner))
    low, high = percentile(means, 0.20), percentile(means, 0.80)
    if high - low < 32:
        return None
    threshold = (low + high) / 2
    return tuple(1 if sample > threshold else 0 for sample in means)


def value(bits):
    result = 0
    for bit in bits:
        result = (result << 1) | bit
    return result


def decode_row(encoded):
    if encoded is None or encoded[:4] != SYNC or encoded[-2:] != END:
        return None
    frame_sequence = value(encoded[4:8])
    row = value(encoded[8:14])
    byte = value(encoded[14:22])
    check = value(encoded[22:30])
    if row >= LOGICAL_ROWS or check != crc8(bytes((frame_sequence, row, byte))):
        return None
    return frame_sequence, row, byte


def parse_fragment(packet):
    if len(packet) != LOGICAL_ROWS or packet[:2] != MAGIC or packet[2] != VERSION:
        return None
    if zlib.crc32(packet[:60]) != int.from_bytes(packet[60:64], "little"):
        return None
    message_id = int.from_bytes(packet[3:5], "little")
    transmission_index, fragment_index, fragment_count, payload_length = packet[5:9]
    if not fragment_count or fragment_index >= fragment_count or payload_length > FRAGMENT_DATA:
        return None
    return {
        "message_id": message_id,
        "transmission_index": transmission_index,
        "fragment_index": fragment_index,
        "fragment_count": fragment_count,
        "payload": packet[9:9 + payload_length],
        "crc": int.from_bytes(packet[60:64], "little"),
    }


def validate_object(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("object_class"), str):
        raise ValueError("invalid object_class")
    location = payload.get("location")
    if not isinstance(location, dict):
        raise ValueError("invalid location")
    if not all(isinstance(location.get(key), (int, float)) for key in ("lat", "lon")):
        raise ValueError("invalid location coordinates")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--frequency", type=int, default=5865)
    parser.add_argument("--gain", type=int, default=50)
    parser.add_argument("--pixels", type=int, default=128)
    parser.add_argument("--min-row-votes", type=int, default=2)
    args = parser.parse_args()

    dev = Device(args.port)
    print("ESPsoup:", dev.ask("FWOPT?"), flush=True)
    for command in (f"FREQ {args.frequency}", f"GAIN MANUAL {args.gain}",
                    "VFMSRC 1", "PLSB AUTO"):
        print(f"> {command}: {dev.ask(command)}", flush=True)
    command = f"VFM 2 0 {args.pixels} 0 0 1 900"
    print(">", command, flush=True)
    dev.command(command)
    response = dev.line(10)
    print(response, flush=True)
    if not response.startswith("OK VFM"):
        raise RuntimeError(response)

    rows = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    last_seen = {}
    completed_fragments = {}
    messages = {}
    good_lines = total_lines = usb_crc_errors = fragment_crc_errors = 0
    last_status = time.monotonic()

    def accept_fragment(fragment):
        nonlocal messages
        fragment_key = (fragment["message_id"], fragment["transmission_index"],
                        fragment["fragment_index"], fragment["crc"])
        fragment_now = time.monotonic()
        if fragment_key in completed_fragments and fragment_now - completed_fragments[fragment_key] < 30:
            return
        completed_fragments[fragment_key] = fragment_now
        message_key = (fragment["message_id"], fragment["transmission_index"])
        assembly = messages.setdefault(message_key, {
            "count": fragment["fragment_count"], "parts": {}, "started": fragment_now,
        })
        if assembly["count"] != fragment["fragment_count"]:
            messages.pop(message_key, None)
            return
        assembly["parts"][fragment["fragment_index"]] = fragment["payload"]
        print(f"fragment: message={fragment['message_id']} "
              f"pass={fragment['transmission_index'] + 1} "
              f"part={fragment['fragment_index'] + 1}/{fragment['fragment_count']}", flush=True)
        if len(assembly["parts"]) != assembly["count"]:
            return
        raw = b"".join(assembly["parts"][index] for index in range(assembly["count"]))
        try:
            payload = validate_object(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            print(f"MESSAGE ERROR: {exc}", flush=True)
        else:
            payload["received_at"] = utc_now()
            envelope = {
                "message_id": fragment["message_id"],
                "transmission_index": fragment["transmission_index"],
                "data": payload,
            }
            print("\nRECEIVED MESSAGE", flush=True)
            print(json.dumps(envelope, indent=2, ensure_ascii=False), flush=True)
            print(flush=True)
        messages.pop(message_key, None)

    try:
        while True:
            header = dev.line(10).split()
            if not header:
                continue
            if header[0] == "END":
                print(" ".join(header), flush=True)
                break
            if header[0] != "VFL":
                continue
            line_count, pixel_count = int(header[3]), int(header[4])
            block = dev.read(line_count * (8 + pixel_count))
            if zlib.crc32(block) != int(header[6], 16):
                usb_crc_errors += 1
                continue
            now = time.monotonic()
            touched = set()
            for line_index in range(line_count):
                offset = line_index * (8 + pixel_count)
                decoded = decode_row(cells_from_line(block[offset + 8:offset + 8 + pixel_count]))
                total_lines += 1
                if decoded is None:
                    continue
                good_lines += 1
                frame_sequence, row, byte = decoded
                rows[frame_sequence][row][byte] += 1
                last_seen[frame_sequence] = now
                touched.add(frame_sequence)

            for frame_sequence in touched:
                frame_rows = rows[frame_sequence]
                if len(frame_rows) != LOGICAL_ROWS:
                    continue
                winners = [frame_rows[row].most_common(1)[0] for row in range(LOGICAL_ROWS)]
                if min(count for _byte, count in winners) < args.min_row_votes:
                    continue
                packet = bytes(byte for byte, _count in winners)
                fragment = parse_fragment(packet)
                if fragment is None:
                    fragment_crc_errors += 1
                    continue
                accept_fragment(fragment)
                rows.pop(frame_sequence, None)
                last_seen.pop(frame_sequence, None)

            for frame_sequence, seen in list(last_seen.items()):
                if now - seen > 2.0:
                    rows.pop(frame_sequence, None)
                    last_seen.pop(frame_sequence, None)
            for key, assembly in list(messages.items()):
                if now - assembly["started"] > 10:
                    messages.pop(key, None)
            for key, completed_at in list(completed_fragments.items()):
                if now - completed_at > 30:
                    completed_fragments.pop(key, None)

            if now - last_status >= 2:
                rate = 100.0 * good_lines / max(1, total_lines)
                print(f"status: row-code={rate:.1f}% ({good_lines}/{total_lines}) "
                      f"usb-crc-errors={usb_crc_errors} fragment-rejects={fragment_crc_errors}",
                      flush=True)
                last_status = now
    except KeyboardInterrupt:
        pass
    finally:
        try:
            dev.command("STOP")
        except BaseException:
            pass
        dev.serial.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
