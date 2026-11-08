"""PWA 아이콘 만들기. 결과는 src/reelbox/web/static/icons/ 에 저장한다 (커밋 대상).

실행: uv run python scripts/make_icons.py
보라색 바탕 위에 흰 상자(보관함)와 재생 삼각형을 그린다. Stitch 디자인의 primary 색을 쓴다.
"""

from pathlib import Path

from PIL import Image, ImageDraw

PRIMARY = (94, 67, 216)
WHITE = (255, 255, 255)
OUT = Path(__file__).resolve().parents[1] / "src/reelbox/web/static/icons"
SCALE = 4  # 크게 그린 뒤 줄여서 가장자리를 매끄럽게 한다


def draw_icon(size: int, *, rounded: bool, glyph_ratio: float) -> Image.Image:
    big = size * SCALE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.22), fill=PRIMARY)
    else:
        d.rectangle((0, 0, big, big), fill=PRIMARY)  # 홈 화면이 알아서 모서리를 깎는다

    g = big * glyph_ratio  # 그림 영역 한 변
    x0, y0 = (big - g) / 2, (big - g) / 2
    stroke = max(int(g * 0.075), 1)
    # 상자 몸통
    d.rounded_rectangle(
        (x0, y0 + g * 0.18, x0 + g, y0 + g), radius=int(g * 0.14), outline=WHITE, width=stroke
    )
    # 상자 위 덮개 선
    d.rounded_rectangle(
        (x0 + g * 0.12, y0, x0 + g * 0.88, y0 + stroke), radius=stroke // 2, fill=WHITE
    )
    # 재생 삼각형
    cx, cy, r = x0 + g * 0.52, y0 + g * 0.59, g * 0.2
    d.polygon([(cx - r * 0.75, cy - r), (cx - r * 0.75, cy + r), (cx + r, cy)], fill=WHITE)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw_icon(192, rounded=True, glyph_ratio=0.52).save(OUT / "icon-192.png")
    draw_icon(512, rounded=True, glyph_ratio=0.52).save(OUT / "icon-512.png")
    # maskable: 가운데 80% 안에 그림이 들어가야 잘리지 않는다
    draw_icon(512, rounded=False, glyph_ratio=0.42).save(OUT / "icon-maskable-512.png")
    # 아이폰 홈 화면: 투명 배경 없이 꽉 채운다 (iOS 가 모서리를 둥글게 깎음)
    draw_icon(180, rounded=False, glyph_ratio=0.5).convert("RGB").save(OUT / "apple-touch-icon.png")


if __name__ == "__main__":
    main()
