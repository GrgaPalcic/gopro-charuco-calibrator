"""Read GoPro's GPMF metadata from an mp4 without loading the file.

A HERO13 clip is about 1.4 GB for 90 s, so the mp4 boxes are walked by seeking: only
the small boxes that matter are read.

Where the metadata lives (gopro/gpmf-parser README):
- global camera metadata in the ``moov/udta/GPMF`` box;
- telemetry in a track whose sample description is ``gpmd`` (handler ``meta``); the
  first sample is found through the track's chunk offsets (``stco``/``co64``) and
  sample sizes (``stsz``).

GPMF is KLV: a 4-byte FourCC key, 1 type byte, 1 structure-size byte, a 2-byte
big-endian repeat count, then the payload padded to 4 bytes. Type 0 means the payload
is more KLV (nested). Tags read here, as the README documents them:
DVNM/MINF model name, CASN serial, FIRM firmware, VFOV lens style (L, W, S, H),
ZFOV diagonal field of view in degrees, EISE ("Y"/"N"), EISA ("N/A", "HS EIS", ...),
VRES resolution, VFPS frame-rate ratio, SHUT exposure time in seconds, ISOE sensor
ISO, ACCL/GYRO the IMU. No tag for the lens mod is documented, and which VFOV letter a
lens mod writes is not documented either.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

# Boxes whose payload is only more boxes.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts", b"dinf"}
# Never read more than this from one box or sample (GPMF payloads are kilobytes).
_MAX_READ = 8 * 1024 * 1024
# Stop walking after this many boxes: a damaged file must not loop for ever.
_MAX_BOXES = 100_000

_NUMERIC = {
    "b": "b",
    "B": "B",
    "s": "h",
    "S": "H",
    "l": "i",
    "L": "I",
    "f": "f",
    "d": "d",
    "j": "q",
    "J": "Q",
    "q": "i",  # Q15.16 fixed point
    "Q": "q",  # Q31.32 fixed point
}


@dataclass
class KLV:
    key: str
    type: str  # "" for nested
    size: int  # structure size in bytes
    repeat: int
    raw: bytes
    children: list[KLV] = field(default_factory=list)

    @property
    def value(self) -> Any:
        return decode_value(self.type, self.size, self.repeat, self.raw)


def parse_klv(data: bytes) -> list[KLV]:
    """Parse a GPMF payload into KLV items, nested items as ``children``.

    Stops quietly at the first truncated or malformed item.
    """
    items: list[KLV] = []
    pos = 0
    while pos + 8 <= len(data):
        key_bytes = data[pos : pos + 4]
        type_byte = data[pos + 4]
        size = data[pos + 5]
        repeat = struct.unpack(">H", data[pos + 6 : pos + 8])[0]
        length = size * repeat
        padded = (length + 3) & ~3
        if key_bytes == b"\0\0\0\0":
            break
        body = data[pos + 8 : pos + 8 + length]
        if len(body) < length:
            break
        key = key_bytes.decode("latin-1")
        type_char = "" if type_byte == 0 else chr(type_byte)
        item = KLV(key, type_char, size, repeat, body)
        if type_byte == 0:
            item.children = parse_klv(body)
        items.append(item)
        pos += 8 + padded
    return items


def decode_value(type_char: str, size: int, repeat: int, raw: bytes) -> Any:
    """A KLV payload as Python values.

    Strings (``c``, ``U``) come back as ``str`` (a list of them when repeated); numbers
    as a scalar for one value, a list for one structure, or a list of lists for several.
    """
    if type_char == "":
        return None
    if type_char in ("c", "U"):
        if size == 1:  # one string of `repeat` characters
            return raw.split(b"\0", 1)[0].decode("latin-1").strip()
        texts = [
            raw[i * size : (i + 1) * size].split(b"\0", 1)[0].decode("latin-1").strip()
            for i in range(repeat)
        ]
        return texts[0] if repeat == 1 else texts
    if type_char == "F":
        codes = [raw[i : i + 4].decode("latin-1") for i in range(0, len(raw) - 3, 4)]
        return codes[0] if len(codes) == 1 else codes
    fmt = _NUMERIC.get(type_char)
    if fmt is None:  # "?" complex, "G" UUID and anything unknown stay raw
        return raw
    width = struct.calcsize(fmt)
    if size == 0 or size % width:
        return raw
    per = size // width
    values: list[Any] = list(struct.unpack(f">{per * repeat}{fmt}", raw[: per * repeat * width]))
    if type_char == "q":
        values = [v / 65536.0 for v in values]
    elif type_char == "Q":
        values = [v / 4294967296.0 for v in values]
    if per == 1:
        return values[0] if repeat == 1 else values
    rows = [values[i * per : (i + 1) * per] for i in range(repeat)]
    return rows[0] if repeat == 1 else rows


def iter_klv(items: list[KLV]):
    """Every item, depth first."""
    for item in items:
        yield item
        yield from iter_klv(item.children)


def _read_box_header(f: BinaryIO, end: int) -> tuple[bytes, int, int] | None:
    """(type, payload start, box end) of the box at the current position, or None."""
    start = f.tell()
    if start + 8 > end:
        return None
    header = f.read(8)
    if len(header) < 8:
        return None
    size, box_type = struct.unpack(">I4s", header)
    payload = start + 8
    if size == 1:
        large = f.read(8)
        if len(large) < 8:
            return None
        size = struct.unpack(">Q", large)[0]
        payload += 8
    elif size == 0:
        size = end - start
    box_end = start + size
    if size < payload - start or box_end > end:
        return None
    return box_type, payload, box_end


def _walk(f: BinaryIO, start: int, end: int, path: tuple[bytes, ...], found: dict, budget: list):
    """Collect the offsets of the boxes we need, recursing into containers."""
    f.seek(start)
    position = start
    while position < end and budget[0] > 0:
        budget[0] -= 1
        f.seek(position)
        header = _read_box_header(f, end)
        if header is None:
            return
        box_type, payload, box_end = header
        here = path + (box_type,)
        if box_type == b"trak":
            found["traks"].append({})
        if box_type in _CONTAINERS:
            _walk(f, payload, box_end, here, found, budget)
        elif here[-3:] == (b"moov", b"udta", b"GPMF"):
            found["udta_gpmf"] = (payload, box_end - payload)
        elif found["traks"] and (
            here[-2:] == (b"mdia", b"hdlr")  # not minf/hdlr, QuickTime's data handler
            or box_type in (b"stsd", b"stco", b"co64", b"stsz")
        ):
            found["traks"][-1][box_type] = (payload, box_end - payload)
        position = box_end


def _read(f: BinaryIO, offset: int, length: int) -> bytes:
    f.seek(offset)
    return f.read(min(length, _MAX_READ))


def _first_gpmd_sample(f: BinaryIO, trak: dict) -> bytes | None:
    if b"stsd" not in trak or b"stsz" not in trak:
        return None
    stsd = _read(f, *trak[b"stsd"])
    # version/flags (4), entry count (4), then the first entry: size (4), format (4)
    if len(stsd) < 16 or stsd[12:16] != b"gpmd":
        return None
    stsz = _read(f, trak[b"stsz"][0], 16)
    if len(stsz) < 12:
        return None
    sample_size, count = struct.unpack(">II", stsz[4:12])
    if sample_size == 0:
        if count == 0 or len(stsz) < 16:
            return None
        sample_size = struct.unpack(">I", stsz[12:16])[0]
    if b"co64" in trak:
        table = _read(f, trak[b"co64"][0], 16)
        if len(table) < 16:
            return None
        offset = struct.unpack(">Q", table[8:16])[0]
    elif b"stco" in trak:
        table = _read(f, trak[b"stco"][0], 12)
        if len(table) < 12:
            return None
        offset = struct.unpack(">I", table[8:12])[0]
    else:
        return None
    # The first sample of the first chunk starts at the chunk offset.
    return _read(f, offset, sample_size)


def _handler(f: BinaryIO, trak: dict) -> bytes:
    if b"hdlr" not in trak:
        return b""
    hdlr = _read(f, trak[b"hdlr"][0], 12)
    return hdlr[8:12] if len(hdlr) >= 12 else b""


def _first(items: list[KLV], key: str) -> Any:
    for item in iter_klv(items):
        if item.key == key and item.type:
            return item.value
    return None


def _stream_values(items: list[KLV], key: str) -> list[float]:
    """The samples of a telemetry stream (e.g. SHUT), divided by its SCAL if any."""
    values: list[float] = []
    for strm in iter_klv(items):
        if strm.key != "STRM":
            continue
        scale = 1.0
        for item in strm.children:
            if item.key == "SCAL" and item.type:
                value = item.value
                scale = float(value[0] if isinstance(value, list) else value) or 1.0
            elif item.key == key and item.type and item.type not in ("c", "U", "F", "?"):
                value = item.value
                flat = value if isinstance(value, list) else [value]
                for entry in flat:
                    if isinstance(entry, list):
                        entry = entry[0]
                    if isinstance(entry, (int, float)):
                        values.append(float(entry) / scale)
    return values


def _clean_text(value: Any) -> str | None:
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if v)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def summarise(items: list[KLV]) -> dict[str, Any]:
    """The known tags from parsed GPMF items (missing tags are None)."""
    vres = _first(items, "VRES")
    vfps = _first(items, "VFPS")
    fps = None
    if isinstance(vfps, list) and len(vfps) >= 2 and vfps[1]:
        fps = float(vfps[0]) / float(vfps[1])
    elif isinstance(vfps, (int, float)):
        fps = float(vfps)
    zfov = _first(items, "ZFOV")
    if isinstance(zfov, list):
        zfov = zfov[0] if zfov else None
    shutter = sorted(_stream_values(items, "SHUT"))
    iso = sorted(_stream_values(items, "ISOE"))
    keys = {item.key for item in iter_klv(items)}
    return {
        "model": _clean_text(_first(items, "MINF")) or _clean_text(_first(items, "DVNM")),
        "device_name": _clean_text(_first(items, "DVNM")),
        "serial": _clean_text(_first(items, "CASN")),
        "firmware": _clean_text(_first(items, "FIRM")),
        "vfov": _clean_text(_first(items, "VFOV")),
        "zfov": None if zfov is None else float(zfov),
        "eise": _clean_text(_first(items, "EISE")),
        "eisa": _clean_text(_first(items, "EISA")),
        "vres": list(vres[:2]) if isinstance(vres, list) and len(vres) >= 2 else None,
        "vfps": fps,
        # Median over the first telemetry sample (about a second of frames).
        "shutter_s": shutter[len(shutter) // 2] if shutter else None,
        "iso": iso[len(iso) // 2] if iso else None,
        "has_imu": bool({"ACCL", "GYRO"} & keys),
        "tags": sorted(keys),
    }


def read_clip_metadata(path: str | os.PathLike[str]) -> dict[str, Any]:
    """GoPro metadata of an mp4: ``moov/udta/GPMF`` plus the first ``gpmd`` sample.

    Returns every known tag (None when absent), ``gpmf_found`` and ``tags`` (every
    FourCC seen). Never raises for a readable file, GoPro or not; an unreadable one
    gives ``error``.
    """
    result = summarise([])
    result.update(gpmf_found=False, error=None)
    try:
        with Path(path).open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            found: dict[str, Any] = {"traks": [], "udta_gpmf": None}
            _walk(f, 0, size, (), found, [_MAX_BOXES])
            items: list[KLV] = []
            if found["udta_gpmf"] is not None:
                items.extend(parse_klv(_read(f, *found["udta_gpmf"])))
            for trak in found["traks"]:
                if _handler(f, trak) not in (b"meta", b""):
                    continue
                sample = _first_gpmd_sample(f, trak)
                if sample:
                    items.extend(parse_klv(sample))
                    break
    except (OSError, struct.error, ValueError) as exc:
        result["error"] = str(exc)
        return result
    result.update(summarise(items))
    result["gpmf_found"] = bool(items)
    return result
