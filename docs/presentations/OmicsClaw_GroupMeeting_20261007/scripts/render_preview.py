"""Render the finalized SVGs to a PDF preview with a local CJK font."""
from pathlib import Path
import io
import cairosvg
import fitz
from PIL import Image, ImageDraw, ImageFont

project = Path(__file__).resolve().parents[1]
exports = project / 'exports'
output = exports / 'OmicsClaw_GroupMeeting_20261007_preview.pdf'
combined = fitz.open()
for svg in sorted((project / 'svg_final').glob('*.svg')):
    data = svg.read_text().replace('Microsoft YaHei, Arial', 'WenQuanYi Zen Hei').replace('Microsoft YaHei, Consolas', 'WenQuanYi Zen Hei Mono')
    page = fitz.open(stream=cairosvg.svg2pdf(bytestring=data.encode()), filetype='pdf')
    combined.insert_pdf(page)
    page.close()
combined.set_metadata({'title': 'OmicsClaw 组会汇报：架构与设计', 'author': 'OmicsClaw', 'subject': 'SVG source preview; PowerPoint rendering may differ', 'creator': 'ppt-master SVG + CairoSVG'})
combined.save(output, garbage=4, deflate=True)
selected = [0, 3, 7, 14, 16, 24]
canvas = Image.new('RGB', (1320, 1195), '#dbe4ea')
draw = ImageDraw.Draw(canvas)
for index, page_number in enumerate(selected):
    pix = combined[page_number].get_pixmap(matrix=fitz.Matrix(2/3, 2/3), alpha=False)
    frame = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
    x, y = 15+(index%2)*655, 15+(index//2)*393
    canvas.paste(frame, (x,y))
    draw.text((x+4,y+363), f'{page_number+1:02d} / 36', fill='#17324D')
canvas.save(exports/'preview.jpg', quality=92)
assert len(combined)==36
print(f'PDF preview: {output}; {len(combined)} pages')
combined.close()
