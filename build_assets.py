# -*- coding: utf-8 -*-
"""用纯标准库生成 PWA 图标，并可回读校验 PNG 结构。

Python >= 3.10，无第三方依赖。运行：python build_assets.py
生成物：ui/icon-192.png、ui/icon-512.png

校验只覆盖「是不是合法 PNG、尺寸/通道/CRC 对不对」，不声称验证视觉效果；
视觉需人工打开图标文件确认。verify_png() 同时供产品验收测试调用。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import console  # noqa: E402,F401 - 导入即把控制台切成 UTF-8

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"

BG = (0x3F, 0x3A, 0x8F, 255)      # 靛蓝底，与登录按钮同色
GOLD = (0xE8, 0xC2, 0x5A, 255)    # 金色阶梯
GOLD_HI = (0xFF, 0xE1, 0x8C, 255)  # 最高一级提亮


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def make_png(size: int) -> bytes:
    """RGBA、每行 filter=0；靛蓝圆角底 + 三级金色阶梯。"""
    radius = size * 0.18
    # 三级柱：x 起点 / 宽度 / 高度比例（相对画布）
    bars = [(0.20, 0.16, 0.28), (0.42, 0.16, 0.44), (0.64, 0.16, 0.62)]
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None)
        for x in range(size):
            # 圆角：四个角的外侧透明
            cx = min(x, size - 1 - x)
            cy = min(y, size - 1 - y)
            alpha = 255
            if cx < radius and cy < radius:
                dx, dy = radius - cx, radius - cy
                if dx * dx + dy * dy > radius * radius:
                    alpha = 0
            px = BG
            if alpha:
                for x0, w, h in bars:
                    left, right = int(size * x0), int(size * (x0 + w))
                    top, bottom = int(size * (0.82 - h)), int(size * 0.82)
                    if left <= x < right and top <= y < bottom:
                        px = GOLD_HI if h == bars[-1][2] else GOLD
                        break
            rows.extend((px[0], px[1], px[2], alpha if px is BG else 255))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _chunk(b"IEND", b""))


def verify_png(data: bytes, expected_size: int) -> dict:
    """回读 PNG：签名、chunk CRC、IHDR、IDAT 解压长度。不通过就抛异常。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG 签名错误")
    pos, chunks = 8, {}
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("PNG chunk 头被截断")
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        if len(payload) != length or crc != (zlib.crc32(kind + payload) & 0xFFFFFFFF):
            raise ValueError(f"PNG chunk {kind!r} 长度或 CRC 错误")
        chunks.setdefault(kind, b"")
        chunks[kind] += payload
        pos += 12 + length
    if b"IHDR" not in chunks or b"IDAT" not in chunks or b"IEND" not in chunks:
        raise ValueError("PNG 缺少必要 chunk")
    w, h, depth, ctype, comp, filt, inter = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    if (w, h, depth, ctype, comp, filt, inter) != (
            expected_size, expected_size, 8, 6, 0, 0, 0):
        raise ValueError(f"PNG 头不符合 8bit RGBA {expected_size}px: "
                         f"{(w, h, depth, ctype, comp, filt, inter)}")
    raw = zlib.decompress(chunks[b"IDAT"])
    if len(raw) != (expected_size * 4 + 1) * expected_size:
        raise ValueError("PNG 像素数据长度错误")
    return {"width": w, "height": h, "channels": 4, "bytes": len(data),
            "chunks": sorted(k.decode() for k in chunks)}


def main() -> None:
    UI_DIR.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        data = make_png(size)
        info = verify_png(data, size)
        path = UI_DIR / f"icon-{size}.png"
        path.write_bytes(data)
        print(f"{path.name}: {info}")


if __name__ == "__main__":
    main()
