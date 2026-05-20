#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import json
import struct
from pathlib import Path


def read_header(path):
    with open(path, "rb") as f:
        header = f.read(32)
        count = struct.unpack("<I", header[4:8])[0]
        header_len = struct.unpack("<H", header[8:10])[0]
        record_len = struct.unpack("<H", header[10:12])[0]
        fields = []
        while True:
            field = f.read(32)
            if not field or field[0] == 0x0D:
                break
            name = field[:11].split(b"\x00", 1)[0].decode("ascii", "ignore")
            fields.append({
                "name": name,
                "type": chr(field[11]),
                "length": field[16],
                "decimal": field[17],
            })
    return count, header_len, record_len, fields


def parse_value(raw, field):
    text = raw.decode("latin1", "ignore").strip()
    if text == "":
        return None
    if field["type"] in {"N", "F"}:
        try:
            return float(text) if field["decimal"] else int(float(text))
        except ValueError:
            return text
    if field["type"] == "D" and len(text) == 8:
        try:
            return dt.datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return text
    return text


def records(path, selected=None):
    count, header_len, record_len, fields = read_header(path)
    selected_set = set(selected or [])
    offsets = []
    pos = 1
    for field in fields:
        offsets.append((field, pos, pos + field["length"]))
        pos += field["length"]
    with open(path, "rb") as f:
        f.seek(header_len)
        for _ in range(count):
            rec = f.read(record_len)
            if len(rec) < record_len or rec[:1] == b"\x1A":
                break
            if rec[:1] == b"*":
                continue
            row = {}
            for field, start, end in offsets:
                if selected_set and field["name"] not in selected_set:
                    continue
                row[field["name"]] = parse_value(rec[start:end], field)
            yield row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--schema", action="store_true")
    parser.add_argument("--head", type=int, default=0)
    parser.add_argument("--csv-out")
    parser.add_argument("--fields")
    args = parser.parse_args()

    selected = args.fields.split(",") if args.fields else None
    if args.schema:
        count, header_len, record_len, fields = read_header(args.path)
        print(json.dumps({"records": count, "header_len": header_len, "record_len": record_len, "fields": fields}, indent=2))
        return

    rows = []
    for i, row in enumerate(records(args.path, selected)):
        if args.head and i >= args.head:
            break
        rows.append(row)
    if args.csv_out:
        Path(args.csv_out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv_out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else selected)
            writer.writeheader()
            writer.writerows(rows)
    else:
        print(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
