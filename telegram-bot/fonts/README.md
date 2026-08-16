# Fonts

Pillow needs real `.ttf` font files with Arabic glyph coverage to draw the
footer text — it can't use system font names.

Download an Arabic-capable font family and place the files here as:

- `Cairo-Regular.ttf`
- `Cairo-Bold.ttf`

Good options (all free/open-license, available on Google Fonts):
`Cairo`, `Noto Naskh Arabic`, `Noto Kufi Arabic`, `Amiri`, `Tajawal`.

If you use a different font or filenames, update `FONT_REGULAR_PATH` /
`FONT_BOLD_PATH` in `.env` to match.
