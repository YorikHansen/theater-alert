'''Render seatmaps as images for Discord messages.'''

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from theater_alert.models import Event, Seatmap

STAMPS = {
    'sold_out': ('[Ausgebucht!]', '#E74C3C'),
    'past': ('[Vergangen!]', '#808080'),
}
STAMP_TILT = -12
BLANK_SIZE = (640, 360)


def render_seatmap_image(
    event: Event,
    seatmap: Seatmap,
    checked_at: datetime,
    status_at: datetime | None = None,
) -> bytes:
    '''Return the seatmap image with a status stamp for sold out or past events.'''
    image = _load_image(seatmap)
    status = event.notification_status(seatmap, status_at or checked_at)
    if status in STAMPS:
        text, color = STAMPS[status]
        image = _stamp(_grayscale(image), text, color)
    return _encode(image)


def _load_image(seatmap: Seatmap) -> Image.Image:
    if seatmap.screenshot:
        try:
            image = Image.open(io.BytesIO(seatmap.screenshot))
            image.load()
            return image.convert('RGB')
        except (UnidentifiedImageError, OSError, ValueError):
            pass
    return Image.new('RGB', BLANK_SIZE, '#151a20')


def _grayscale(image: Image.Image) -> Image.Image:
    return ImageOps.grayscale(image).convert('RGB')


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _stamp(image: Image.Image, text: str, color: str) -> Image.Image:
    font_size = max(16, min(image.height // 7, image.width // 7))
    font = _font(font_size)
    box = font.getbbox(text)
    text_width = box[2] - box[0]
    if text_width > image.width * 0.85:
        font_size = max(8, int(font_size * image.width * 0.85 / text_width))
        font = _font(font_size)
        box = font.getbbox(text)

    padding = max(8, font_size // 3)
    layer = Image.new(
        'RGBA',
        (box[2] - box[0] + padding * 2, box[3] - box[1] + padding * 2),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(layer)
    stroke = max(1, font_size // 16)
    draw.text(
        (padding - box[0], padding - box[1]),
        text,
        font=font,
        fill=color,
        stroke_width=stroke,
        stroke_fill='#ffffff',
    )
    draw.rectangle(
        (
            padding // 2,
            padding // 2,
            layer.width - padding // 2,
            layer.height - padding // 2,
        ),
        outline=color,
        width=stroke,
    )

    layer = layer.rotate(STAMP_TILT, expand=True, resample=Image.Resampling.BICUBIC)
    result = image.convert('RGBA')
    result.alpha_composite(
        layer,
        ((image.width - layer.width) // 2, (image.height - layer.height) // 2),
    )
    return result.convert('RGB')


def _encode(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue()
