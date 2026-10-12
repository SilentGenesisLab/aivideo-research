"""由 web/favicon.svg 的同款图形生成位图图标：favicon.ico（16/32/48）、apple-touch-icon.png（180）、icon-512.png。
运行：python3 tools/make_favicon.py（需要 Pillow，只在改图标时运行一次）"""
from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parent.parent / "web"
S = 16  # 在 64×16 的画布上绘制再缩小，边缘更平滑
N = 64 * S


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def draw() -> Image.Image:
    bg = Image.new("RGBA", (N, N))
    c1, c2 = (0x3B, 0x82, 0xF6), (0x4F, 0x46, 0xE5)
    grad = Image.new("RGBA", (N, N))
    gp = grad.load()
    step = 8
    for y in range(0, N, step):
        for x in range(0, N, step):
            col = lerp(c1, c2, (x + y) / (2 * N)) + (255,)
            for dy in range(step):
                for dx in range(step):
                    gp[x + dx, y + dy] = col
    mask = Image.new("L", (N, N))
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius=14 * S, fill=255)
    bg.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(bg)
    white = (255, 255, 255, 255)
    cx, cy, r, w = 28 * S, 28 * S, 15 * S, 6 * S
    d.ellipse([cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2], outline=white, width=w)
    hw = 7 * S
    d.line([(39.5 * S, 39.5 * S), (51 * S, 51 * S)], fill=white, width=hw)
    for px, py in ((39.5 * S, 39.5 * S), (51 * S, 51 * S)):
        d.ellipse([px - hw / 2, py - hw / 2, px + hw / 2, py + hw / 2], fill=white)
    d.polygon([(24 * S, 20.5 * S), (35 * S, 28 * S), (24 * S, 35.5 * S)], fill=white)
    return bg


if __name__ == "__main__":
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save(WEB / "icon-512.png")
    big.resize((180, 180), Image.LANCZOS).save(WEB / "apple-touch-icon.png")
    big.resize((256, 256), Image.LANCZOS).save(WEB / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("ok")
