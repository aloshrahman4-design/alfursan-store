# Fonts

`image_processor.py` only ever draws digits and commas (the new price
numbers patched into an image) -- Arabic labels live in the Telegram
caption text, which Telegram renders natively, not in the image itself.
So Pillow just needs one bold `.ttf` with numeral glyphs; it doesn't need
to be Arabic-capable.

Download a font and place it here as `Cairo-Bold.ttf` (or point
`FONT_BOLD_PATH` in `.env` at wherever you put it). `Cairo`, `Tajawal`,
or any standard bold sans-serif from Google Fonts works fine.
