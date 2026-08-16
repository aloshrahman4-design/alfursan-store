# Fonts

`Cairo-Bold.ttf` is bundled here (downloaded from Google Fonts, SIL Open
Font License -- free to redistribute) so there's nothing to set up.

`image_processor.py` only ever draws digits and commas (the new price
numbers patched into an image) -- Arabic labels live in the Telegram
caption text, which Telegram renders natively, not in the image itself.
So Pillow just needs one bold `.ttf` with numeral glyphs; it doesn't need
to be Arabic-capable.

To use a different font, replace this file (keeping the name
`Cairo-Bold.ttf`) or point `FONT_BOLD_PATH` in `.env` at wherever you put
it instead.
