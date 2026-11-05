"""Minimal scanline OpenEXR ZIP reader for QRISP motion vectors.

Only the subset used by the inspected QRISP files is accepted: version 2,
non-subsampled channels, increasing-Y scanlines, and 16-scanline ZIP chunks.
No external OpenEXR package is required.
"""
import json
from pathlib import Path
import struct
import zlib

import numpy as np


def _cstring(payload, position):
    end = payload.index(0, position)
    return payload[position:end].decode("ascii"), end + 1


def _parse_header(payload):
    if len(payload) < 9:
        raise ValueError("truncated EXR")
    magic, version = struct.unpack_from("<II", payload, 0)
    if magic != 20000630 or version != 2:
        raise ValueError(f"unsupported EXR magic/version: {magic:#x}/{version:#x}")
    position = 8
    attributes = {}
    while True:
        if position >= len(payload):
            raise ValueError("unterminated EXR header")
        if payload[position] == 0:
            position += 1
            break
        name, position = _cstring(payload, position)
        kind, position = _cstring(payload, position)
        size = struct.unpack_from("<I", payload, position)[0]
        position += 4
        if position + size > len(payload):
            raise ValueError(f"truncated EXR attribute: {name}")
        attributes[name] = (kind, payload[position : position + size])
        position += size
    return attributes, position


def decode_exr(payload):
    """Return (float32 NHWC array, channel names, metadata) for ZIP scanline EXR."""
    attributes, position = _parse_header(payload)
    if attributes["compression"][1][0] != 3:
        raise ValueError("only OpenEXR ZIP compression (3) is supported")
    if attributes["lineOrder"][1][0] != 0:
        raise ValueError("only increasing-Y scanlines are supported")
    x0, y0, x1, y1 = struct.unpack("<4i", attributes["dataWindow"][1])
    if x0 != 0 or y0 != 0:
        raise ValueError("nonzero data-window origin is unsupported")
    width, height = x1 + 1, y1 + 1
    if width < 1 or height < 1:
        raise ValueError("invalid EXR data window")

    channel_data = attributes["channels"][1]
    cursor = 0
    channels = []
    while channel_data[cursor] != 0:
        name, cursor = _cstring(channel_data, cursor)
        pixel_type, p_linear = struct.unpack_from("<iB", channel_data, cursor)
        x_sampling, y_sampling = struct.unpack_from("<II", channel_data, cursor + 8)
        cursor += 16
        if pixel_type not in (0, 1, 2) or (x_sampling, y_sampling) != (1, 1):
            raise ValueError(f"unsupported channel layout: {name}")
        channels.append((name, pixel_type, p_linear))

    chunk_count = (height + 15) // 16
    table_bytes = chunk_count * 8
    if position + table_bytes > len(payload):
        raise ValueError("truncated EXR offset table")
    offsets = struct.unpack_from(f"<{chunk_count}Q", payload, position)
    output = np.zeros((height, width, len(channels)), dtype=np.float32)
    dtypes = {0: np.dtype("<u4"), 1: np.dtype("<f2"), 2: np.dtype("<f4")}

    for expected_y, offset in enumerate(offsets):
        del expected_y  # Chunk Y is authoritative and checked below.
        if offset + 8 > len(payload):
            raise ValueError("invalid EXR chunk offset")
        y, compressed_size = struct.unpack_from("<iI", payload, offset)
        if y < 0 or y >= height or y % 16:
            raise ValueError(f"invalid EXR scanline chunk Y: {y}")
        start = offset + 8
        end = start + compressed_size
        if end > len(payload):
            raise ValueError("truncated EXR chunk")
        rows = min(16, height - y)
        raw_size = rows * width * sum(dtypes[kind].itemsize for _, kind, _ in channels)
        compressed = payload[start:end]
        if compressed_size == raw_size:
            raw = compressed
        else:
            interleaved = bytearray(zlib.decompress(compressed))
            if len(interleaved) != raw_size:
                raise ValueError("unexpected EXR ZIP output size")
            for index in range(1, len(interleaved)):
                interleaved[index] = (
                    interleaved[index - 1] + interleaved[index] - 128
                ) & 255
            reconstructed = bytearray(raw_size)
            midpoint = (raw_size + 1) // 2
            reconstructed[0::2] = interleaved[:midpoint]
            reconstructed[1::2] = interleaved[midpoint:]
            raw = reconstructed

        cursor = 0
        for row in range(rows):
            for channel_index, (_, kind, _) in enumerate(channels):
                dtype = dtypes[kind]
                nbytes = width * dtype.itemsize
                if cursor + nbytes > len(raw):
                    raise ValueError("truncated decoded EXR scanline")
                values = np.frombuffer(raw, dtype=dtype, count=width, offset=cursor)
                output[y + row, :, channel_index] = values
                cursor += nbytes
        if cursor != len(raw):
            raise ValueError("extra bytes in decoded EXR chunk")

    names = [name for name, _, _ in channels]
    metadata = {
        "width": width,
        "height": height,
        "channels": names,
        "pixel_types": [kind for _, kind, _ in channels],
        "compression": "ZIP",
        "chunks": chunk_count,
    }
    return output, names, metadata


def read_exr(path):
    return decode_exr(Path(path).read_bytes())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args()
    array, names, metadata = read_exr(args.path)
    metadata["statistics"] = {
        name: {
            "min": float(array[:, :, index].min()),
            "max": float(array[:, :, index].max()),
            "mean": float(array[:, :, index].mean()),
            "nonzero_fraction": float(np.count_nonzero(array[:, :, index]) / array[:, :, index].size),
        }
        for index, name in enumerate(names)
    }
    print(json.dumps(metadata, indent=2))
