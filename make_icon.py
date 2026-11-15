"""Генерация иконки приложения: белая стрелка загрузки на синем квадрате."""

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]
BASE = 256


def render() -> Image.Image:
    img = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = 12  # отступ
    # скруглённый квадрат
    d.rounded_rectangle([m, m, BASE - m, BASE - m], radius=48, fill=(37, 99, 235, 255))
    # стрелка вниз
    cx = BASE // 2
    top, bot = 56, 172
    shaft_w = 44
    d.rectangle([cx - shaft_w // 2, top, cx + shaft_w // 2, 130], fill=(255, 255, 255, 255))
    d.polygon([(cx - 62, 118), (cx + 62, 118), (cx, bot)], fill=(255, 255, 255, 255))
    # полка-«приёмник»
    d.rounded_rectangle([56, 190, BASE - 56, 212], radius=10, fill=(255, 255, 255, 255))
    return img


def main():
    out = Path(__file__).resolve().parent / "icon.ico"
    base = render()
    base.save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print("icon:", out)


if __name__ == "__main__":
    main()
