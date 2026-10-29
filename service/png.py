"""将 MaaFramework 截图（BGR numpy 数组）编码为 PNG，不依赖第三方图像库。"""

import struct
import zlib

import numpy


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(bgr: numpy.ndarray) -> bytes:
    if bgr.ndim != 3 or bgr.shape[2] != 3:
        raise ValueError(f"需要 HxWx3 的 BGR 图像，实际为 {bgr.shape}")
    height, width = bgr.shape[:2]
    rgb = numpy.ascontiguousarray(bgr[:, :, ::-1], dtype=numpy.uint8)
    # 每行前加一个字节的过滤类型 0
    raw = numpy.hstack([numpy.zeros((height, 1), dtype=numpy.uint8), rgb.reshape(height, width * 3)])
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)),
            _chunk(b"IEND", b""),
        ]
    )
