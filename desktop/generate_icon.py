"""Draw the monochrome CapsWriter voice mark and Windows icon."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1] / "assets"
root.mkdir(parents=True, exist_ok=True)
scale = 4
size = 256
canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
draw = ImageDraw.Draw(canvas)
box = (8 * scale, 8 * scale, 248 * scale, 248 * scale)
draw.rounded_rectangle(box, radius=62 * scale, fill="#18191B")
for x, height in ((76, 42), (102, 78), (128, 112), (154, 78), (180, 42)):
    width = 11 * scale
    left = (x - 5.5) * scale
    top = (128 - height / 2) * scale
    draw.rounded_rectangle((left, top, left + width, top + height * scale),
                           radius=5.5 * scale, fill="#F8F8F6")
icon = canvas.resize((size, size), Image.Resampling.LANCZOS)
icon.save(root / "icon.png")
icon.save(root / "icon.ico", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">',
       '<rect x="8" y="8" width="240" height="240" rx="62" fill="#18191B"/>']
for x, height in ((76,42),(102,78),(128,112),(154,78),(180,42)):
    svg.append(f'<rect x="{x-5.5}" y="{128-height/2}" width="11" height="{height}" rx="5.5" fill="#F8F8F6"/>')
svg.append("</svg>")
(root / "icon.svg").write_text("\n".join(svg) + "\n", encoding="utf-8")
print(root / "icon.ico")
