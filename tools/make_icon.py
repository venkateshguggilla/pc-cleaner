"""Draw assets/pccleaner.ico (a sparkle on a blue tile) with no image libraries."""
import os
import struct
import zlib

SIZES = (256, 48, 32, 24, 16)
SS = 4  # supersampling per axis for anti-aliasing


def _lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _in_tile(x, y, radius=0.22):
    # rounded square covering [0.04, 0.96]
    lo, hi = 0.04, 0.96
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    return lo <= x <= hi and lo <= y <= hi and (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _in_star(x, y, cx, cy, r):
    # 4-point sparkle: |dx|^0.5 + |dy|^0.5 <= r^0.5
    dx, dy = abs(x - cx), abs(y - cy)
    return dx ** 0.5 + dy ** 0.5 <= r ** 0.5


def render(size):
    top, bottom, white = (59, 130, 246), (29, 78, 216), (255, 255, 255)
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter type
        for px in range(size):
            tile = star = 0
            for sy in range(SS):
                for sx in range(SS):
                    x, y = (px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size
                    if _in_tile(x, y):
                        tile += 1
                        if _in_star(x, y, 0.46, 0.54, 0.34) or _in_star(x, y, 0.74, 0.27, 0.12):
                            star += 1
            n = SS * SS
            if not tile:
                row += bytes(4)
                continue
            base = _lerp(top, bottom, py / size)
            rgb = _lerp(base, white, star / tile)
            row += bytes(rgb) + bytes([round(255 * tile / n)])
        rows.append(bytes(row))
    return b"".join(rows)


def png(size):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


def main():
    images = [png(s) for s in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in zip(SIZES, images):
        dim = 0 if size == 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    out += b"".join(images)
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "pccleaner.ico")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(out)
    with open(path[:-4] + ".png", "wb") as f:  # preview
        f.write(images[0])
    print(path)


if __name__ == "__main__":
    main()
