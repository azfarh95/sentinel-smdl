"""Minimal WebM (Matroska) muxer that writes a VP9 video track WITH a real
alpha channel — the AlphaMode=1 + per-frame BlockAdditional structure that
ffmpeg's libvpx wrapper fails to emit in this toolchain (it sets the alpha
flag but writes zero alpha data — verified via mkvinfo).

Pipeline (driven by sticker_processor):
    colour frames → VP9 (vpxenc, IVF)
    alpha  frames → VP9 (vpxenc, IVF)   # grayscale: luma = alpha, white=opaque
    mux: colour frame i = the Block, alpha frame i = its BlockAdditional.

This is the only path to transparent *animated* Telegram stickers here.
Each frame is wrapped in a BlockGroup so it can carry BlockAdditions; the
colour and alpha VP9 streams are decoded as two parallel VP9 instances by a
compliant player (Telegram/Chrome), paired frame-for-frame.

vpxenc MUST be invoked with --auto-alt-ref=0 --lag-in-frames=0 so there are
no hidden/alt-ref frames — otherwise the IVF frame count wouldn't match the
displayed frames and colour/alpha would desync.
"""
from __future__ import annotations

import struct
from pathlib import Path


# ── IVF (vpxenc output) ─────────────────────────────────────────────────────
def parse_ivf(path: Path) -> dict:
    """Parse an IVF file into {width,height,fps_num,fps_den,frames:[bytes,…]}."""
    data = path.read_bytes()
    if data[:4] != b"DKIF":
        raise ValueError("not an IVF file (missing DKIF signature)")
    hdr_len = struct.unpack_from("<H", data, 6)[0]
    width   = struct.unpack_from("<H", data, 12)[0]
    height  = struct.unpack_from("<H", data, 14)[0]
    fps_num = struct.unpack_from("<I", data, 16)[0]
    fps_den = struct.unpack_from("<I", data, 20)[0]
    frames: list[bytes] = []
    off = hdr_len
    while off + 12 <= len(data):
        sz = struct.unpack_from("<I", data, off)[0]
        off += 12
        frames.append(data[off:off + sz])
        off += sz
    return {"width": width, "height": height,
            "fps_num": fps_num or 30, "fps_den": fps_den or 1, "frames": frames}


def vp9_is_keyframe(frame: bytes) -> bool:
    """Read the VP9 uncompressed-header frame_type bit (assumes profile 0, which
    vpxenc --profile=0 guarantees). frame_marker(2)=10, profile_low, profile_high,
    show_existing_frame, frame_type(0=key)."""
    if not frame:
        return False
    b = frame[0]
    if (b >> 3) & 1:      # show_existing_frame → not a real coded frame
        return False
    return ((b >> 2) & 1) == 0   # frame_type 0 = KEY_FRAME


def vp8_is_keyframe(frame: bytes) -> bool:
    """VP8 frame tag: the low bit of the first byte is key_frame (0 = key)."""
    if not frame:
        return False
    return (frame[0] & 1) == 0


# ── EBML primitives ─────────────────────────────────────────────────────────
def _vint(n: int) -> bytes:
    """EBML data-size descriptor (with leading length-marker bit)."""
    for L in range(1, 9):
        if n < (1 << (7 * L)) - 1:
            return (n | (1 << (7 * L))).to_bytes(L, "big")
    raise ValueError("vint too large")


def _uint(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def _sint(n: int) -> bytes:
    L = 1
    while not (-(1 << (8 * L - 1)) <= n < (1 << (8 * L - 1))):
        L += 1
    return n.to_bytes(L, "big", signed=True)


def _el(eid: bytes, data: bytes) -> bytes:
    return eid + _vint(len(data)) + data


# Matroska/WebM element IDs (written as their full byte sequences).
_EBML            = b"\x1A\x45\xDF\xA3"
_Segment         = b"\x18\x53\x80\x67"
_Info            = b"\x15\x49\xA9\x66"
_TimestampScale  = b"\x2A\xD7\xB1"
_Duration        = b"\x44\x89"
_MuxingApp       = b"\x4D\x80"
_WritingApp      = b"\x57\x41"
_Tracks          = b"\x16\x54\xAE\x6B"
_TrackEntry      = b"\xAE"
_TrackNumber     = b"\xD7"
_TrackUID        = b"\x73\xC5"
_TrackType       = b"\x83"
_FlagLacing      = b"\x9C"
_CodecID         = b"\x86"
_Video           = b"\xE0"
_PixelWidth      = b"\xB0"
_PixelHeight     = b"\xBA"
_AlphaMode       = b"\x53\xC0"
_Cluster         = b"\x1F\x43\xB6\x75"
_Timestamp       = b"\xE7"
_BlockGroup      = b"\xA0"
_Block           = b"\xA1"
_BlockDuration   = b"\x9B"
_ReferenceBlock  = b"\xFB"
_BlockAdditions  = b"\x75\xA1"
_BlockMore       = b"\xA6"
_BlockAddID      = b"\xEE"
_BlockAdditional = b"\xA5"


def _ebml_head() -> bytes:
    body = b"".join([
        _el(b"\x42\x86", _uint(1)),   # EBMLVersion
        _el(b"\x42\xF7", _uint(1)),   # EBMLReadVersion
        _el(b"\x42\xF2", _uint(4)),   # EBMLMaxIDLength
        _el(b"\x42\xF3", _uint(8)),   # EBMLMaxSizeLength
        _el(b"\x42\x82", b"webm"),    # DocType
        _el(b"\x42\x87", _uint(2)),   # DocTypeVersion
        _el(b"\x42\x85", _uint(2)),   # DocTypeReadVersion
    ])
    return _el(_EBML, body)


def mux_alpha_webm(color_ivf: Path, alpha_ivf: Path, out: Path,
                   codec: str = "vp9") -> tuple[bool, str | None]:
    """Mux paired colour+alpha IVF streams into a transparent WebM.

    codec: 'vp9' (V_VP9, Telegram's required codec) or 'vp8' (V_VP8 — useful
    for validating the muxer since ffmpeg can decode VP8 alpha).

    Returns (ok, error). Pairs frames 1:1 (truncating to the shorter stream);
    frame 0 is the keyframe (no ReferenceBlock), later frames reference the
    previous frame's timecode."""
    codec_id = b"V_VP8" if codec == "vp8" else b"V_VP9"
    is_key = vp8_is_keyframe if codec == "vp8" else vp9_is_keyframe
    try:
        col = parse_ivf(color_ivf)
        alp = parse_ivf(alpha_ivf)
    except Exception as e:
        return False, f"ivf parse failed: {e}"
    cframes, aframes = col["frames"], alp["frames"]
    n = min(len(cframes), len(aframes))
    if n == 0:
        return False, "no frames to mux"
    w, h = col["width"], col["height"]
    fps_num, fps_den = col["fps_num"], col["fps_den"]

    def tc(i: int) -> int:                       # ms timecode of frame i
        return round(i * 1000 * fps_den / fps_num)
    frame_ms = max(1, round(1000 * fps_den / fps_num))
    total_ms = tc(n)

    info = _el(_Info, b"".join([
        _el(_TimestampScale, _uint(1_000_000)),  # 1 ms
        _el(_MuxingApp, b"sentinel-webm-alpha"),
        _el(_WritingApp, b"sentinel-webm-alpha"),
        _el(_Duration, struct.pack(">d", float(total_ms))),
    ]))
    video = _el(_Video, b"".join([
        _el(_PixelWidth, _uint(w)),
        _el(_PixelHeight, _uint(h)),
        _el(_AlphaMode, _uint(1)),
    ]))
    tracks = _el(_Tracks, _el(_TrackEntry, b"".join([
        _el(_TrackNumber, _uint(1)),
        _el(_TrackUID, _uint(1)),
        _el(_TrackType, _uint(1)),       # video
        _el(_FlagLacing, _uint(0)),
        _el(_CodecID, codec_id),
        video,
    ])))

    groups = []
    prev_tc = 0
    for i in range(n):
        rel = tc(i)
        block = _el(_Block, _vint(1) + struct.pack(">h", rel) + b"\x00" + cframes[i])
        parts = [block, _el(_BlockDuration, _uint(frame_ms))]
        if not is_key(cframes[i]):
            parts.append(_el(_ReferenceBlock, _sint(prev_tc - rel)))
        parts.append(_el(_BlockAdditions, _el(_BlockMore,
            _el(_BlockAddID, _uint(1)) + _el(_BlockAdditional, aframes[i]))))
        groups.append(_el(_BlockGroup, b"".join(parts)))
        prev_tc = rel

    cluster = _el(_Cluster, _el(_Timestamp, _uint(0)) + b"".join(groups))
    blob = _ebml_head() + _el(_Segment, info + tracks + cluster)
    try:
        out.write_bytes(blob)
    except Exception as e:
        return False, f"write failed: {e}"
    return True, None
