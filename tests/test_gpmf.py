"""GPMF reading on hand-built bytes: KLV types, a minimal GoPro-like mp4 (udta/GPMF and a
gpmd track with stco or co64), a plain mp4 without GPMF, and damaged files.

The tag values below are made up for the test; they are not HERO13 measurements.
"""

import struct

import pytest

from gopro_charuco_calibrator.gpmf import decode_value, parse_klv, read_clip_metadata


def klv(key: str, type_char: str, size: int, values, repeat: int | None = None) -> bytes:
    """Encode one KLV item; ``values`` is raw bytes or a list packed with ``type_char``."""
    if isinstance(values, bytes):
        body = values
    else:
        fmt = {
            "b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I",
            "f": "f", "d": "d", "j": "q", "J": "Q", "q": "i", "Q": "q",
        }[type_char]
        body = struct.pack(f">{len(values)}{fmt}", *values)
    if repeat is None:
        repeat = len(body) // size if size else 0
    type_byte = 0 if type_char == "" else ord(type_char)
    head = key.encode() + bytes([type_byte, size]) + struct.pack(">H", repeat)
    return head + body + b"\0" * ((-len(body)) % 4)


def nested(key: str, *children: bytes) -> bytes:
    body = b"".join(children)
    return key.encode() + bytes([0, 1]) + struct.pack(">H", len(body)) + body


def text(key: str, value: str) -> bytes:
    return klv(key, "c", 1, value.encode())


# --- KLV --------------------------------------------------------------------


def test_klv_types_decode():
    data = b"".join(
        [
            text("MINF", "HERO-TEST"),
            klv("VRES", "L", 8, [4000, 3000]),
            klv("NEGL", "l", 4, [-5]),
            klv("ZFOV", "f", 4, [150.5]),
            klv("TYPE", "F", 4, b"ACCL"),
            klv("BYTE", "B", 1, [7, 8, 9]),
            klv("SBYT", "b", 1, [-1]),
            klv("USHR", "S", 2, [65000]),
            klv("SSHR", "s", 6, [1, -2, 3, 4, -5, 6]),
        ]
    )
    items = {item.key: item.value for item in parse_klv(data)}
    assert items["MINF"] == "HERO-TEST"
    assert items["VRES"] == [4000, 3000]
    assert items["NEGL"] == -5
    assert items["ZFOV"] == pytest.approx(150.5)
    assert items["TYPE"] == "ACCL"
    assert items["BYTE"] == [7, 8, 9]
    assert items["SBYT"] == -1
    assert items["USHR"] == 65000
    # a 3 x int16 structure repeated twice: one row per sample
    assert items["SSHR"] == [[1, -2, 3], [4, -5, 6]]


def test_klv_nested_and_padding():
    data = nested("DEVC", text("DVNM", "Cam"), nested("STRM", klv("SCAL", "s", 2, [10])))
    (devc,) = parse_klv(data)
    assert devc.type == "" and devc.value is None
    assert [child.key for child in devc.children] == ["DVNM", "STRM"]
    assert devc.children[0].value == "Cam"  # 3 bytes, padded to 4
    assert devc.children[1].children[0].value == 10


def test_klv_repeated_strings_and_fixed_point():
    assert decode_value("c", 3, 2, b"ab\0cde") == ["ab", "cde"]
    assert decode_value("q", 4, 1, struct.pack(">i", 3 * 65536 + 32768)) == 3.5
    assert decode_value("?", 4, 1, b"abcd") == b"abcd"


def test_klv_truncated_stops_quietly():
    good = text("MINF", "X")
    items = parse_klv(good + b"VRES" + bytes([ord("L"), 8]) + struct.pack(">H", 5) + b"\0" * 4)
    assert [item.key for item in items] == ["MINF"]


# --- mp4 --------------------------------------------------------------------


def box(box_type: bytes, *payload: bytes, large: bool = False) -> bytes:
    body = b"".join(payload)
    if large:
        return struct.pack(">I4sQ", 1, box_type, len(body) + 16) + body
    return struct.pack(">I4s", len(body) + 8, box_type) + body


def full(box_type: bytes, body: bytes) -> bytes:
    return box(box_type, b"\0\0\0\0" + body)


GLOBAL = nested(
    "DEVC",
    text("DVNM", "Camera"),
    text("MINF", "HERO13 Black"),
    text("CASN", "C3501234567890"),
    text("FIRM", "H24.01.02.00.00"),
    nested(
        "STRM",
        text("VFOV", "W"),
        klv("ZFOV", "f", 4, [155.25]),
        text("EISE", "N"),
        text("EISA", "N/A"),
        klv("VRES", "L", 8, [4000, 3000]),
        klv("VFPS", "L", 8, [60000, 1001]),
    ),
)

TELEMETRY = nested(
    "DEVC",
    nested("STRM", klv("SCAL", "s", 2, [418]), klv("ACCL", "s", 6, [1, 2, 3, 4, 5, 6])),
    nested("STRM", klv("SHUT", "f", 4, [1 / 480, 1 / 480, 1 / 470])),
    nested("STRM", klv("ISOE", "S", 2, [100, 110, 120])),
)


def gopro_mp4(tmp_path, *, co64: bool = False, per_sample_sizes: bool = True, name="clip.mp4"):
    """ftyp, a 'video' mdat, then the gpmd sample, then moov (like a camera writes it)."""
    ftyp = box(b"ftyp", b"mp41\0\0\0\0mp41")
    junk = b"\x11" * 4096  # stands in for video
    mdat_header = 8
    sample_offset = len(ftyp) + mdat_header + len(junk)
    mdat = box(b"mdat", junk + TELEMETRY)
    hdlr = full(b"hdlr", b"\0\0\0\0meta" + b"\0" * 12 + b"GoPro MET\0")
    alias = full(b"hdlr", b"dhlralis" + b"\0" * 12 + b"GoPro Alias\0")
    stsd = full(b"stsd", struct.pack(">I", 1) + box(b"gpmd", b"\0" * 8))
    if per_sample_sizes:
        stsz = full(b"stsz", struct.pack(">III", 0, 1, len(TELEMETRY)))
    else:
        stsz = full(b"stsz", struct.pack(">II", len(TELEMETRY), 1))
    if co64:
        offsets = full(b"co64", struct.pack(">IQ", 1, sample_offset))
    else:
        offsets = full(b"stco", struct.pack(">II", 1, sample_offset))
    video_trak = box(
        b"trak", box(b"mdia", full(b"hdlr", b"\0\0\0\0vide" + b"\0" * 12 + b"\0"))
    )
    meta_trak = box(
        b"trak",
        box(b"tkhd", b"\0" * 84),
        # minf/hdlr is QuickTime's data handler ("alis"); only mdia/hdlr names the track
        box(b"mdia", hdlr, box(b"minf", alias, box(b"stbl", stsd, stsz, offsets))),
    )
    udta = box(b"udta", box(b"FIRM", b"H24"), box(b"GPMF", GLOBAL))
    moov = box(b"moov", box(b"mvhd", b"\0" * 100), video_trak, meta_trak, udta)
    path = tmp_path / name
    path.write_bytes(ftyp + mdat + moov)
    return path


@pytest.mark.parametrize("co64", [False, True])
def test_reads_global_and_telemetry_tags(tmp_path, co64):
    meta = read_clip_metadata(gopro_mp4(tmp_path, co64=co64))
    assert meta["gpmf_found"] is True and meta["error"] is None
    assert meta["model"] == "HERO13 Black"
    assert meta["device_name"] == "Camera"
    assert meta["serial"] == "C3501234567890"
    assert meta["firmware"] == "H24.01.02.00.00"
    assert meta["vfov"] == "W"
    assert meta["zfov"] == pytest.approx(155.25)
    assert (meta["eise"], meta["eisa"]) == ("N", "N/A")
    assert meta["vres"] == [4000, 3000]
    assert meta["vfps"] == pytest.approx(59.94, abs=0.01)
    assert meta["has_imu"] is True
    assert meta["shutter_s"] == pytest.approx(1 / 480)
    assert meta["iso"] == 110
    assert {"ACCL", "SHUT", "CASN"} <= set(meta["tags"])


def test_constant_sample_size_and_large_boxes(tmp_path):
    path = gopro_mp4(tmp_path, per_sample_sizes=False)
    meta = read_clip_metadata(path)
    assert meta["has_imu"] is True
    # A 64-bit box header (size == 1) in front of everything must be walked past.
    data = path.read_bytes()
    wrapped = tmp_path / "large.mp4"
    free = box(b"free", b"\0" * 32, large=True)
    wrapped.write_bytes(free + data)
    # The gpmd chunk offset now points 48 bytes early (offsets are absolute), so the
    # telemetry read is junk and is skipped; the udta tags are still found.
    meta_shifted = read_clip_metadata(wrapped)
    assert meta_shifted["model"] == "HERO13 Black" and meta_shifted["error"] is None


def test_plain_mp4_without_gpmf(tmp_path):
    ftyp = box(b"ftyp", b"isom\0\0\0\0isom")
    moov = box(b"moov", box(b"mvhd", b"\0" * 100), box(b"trak", box(b"tkhd", b"\0" * 84)))
    path = tmp_path / "plain.mp4"
    path.write_bytes(ftyp + box(b"mdat", b"\0" * 1000) + moov)
    meta = read_clip_metadata(path)
    assert meta["gpmf_found"] is False and meta["error"] is None
    assert meta["model"] is None and meta["serial"] is None and meta["has_imu"] is False


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\0\0\0\x08",
        b"garbage that is not an mp4 at all",
        struct.pack(">I4s", 0xFFFFFF, b"moov"),
    ],
)
def test_odd_files_never_raise(tmp_path, payload):
    path = tmp_path / "odd.mp4"
    path.write_bytes(payload)
    meta = read_clip_metadata(path)
    assert meta["gpmf_found"] is False and meta["error"] is None


def test_truncated_gopro_file_keeps_what_it_found(tmp_path):
    path = gopro_mp4(tmp_path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])  # cut into moov/udta
    meta = read_clip_metadata(path)
    assert meta["error"] is None  # truncated boxes are skipped, not fatal


def test_missing_file_reports_error(tmp_path):
    meta = read_clip_metadata(tmp_path / "nope.mp4")
    assert meta["error"] and meta["gpmf_found"] is False
