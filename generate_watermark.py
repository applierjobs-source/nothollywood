"""Regenerate watermark.png. Run once locally when the wordmark changes.

The rendered PNG is committed to the repo so Railway doesn't need Pillow at
render time (only ffmpeg). Run:

    python generate_watermark.py

"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path


def build_watermark(out: Path = Path(__file__).parent / "watermark.png") -> None:
    W, H = 900, 140
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # DejaVu Sans Bold is available on both dev sandbox and Railway's Debian base image.
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(font_path, 72)

    text = "NotHollywood.ai"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (W - tw) // 2 - bbox[0]
    y = (H - th) // 2 - bbox[1]

    # Drop shadow for readability on bright backgrounds
    d.text((x + 2, y + 3), text, font=font, fill=(0, 0, 0, 160))
    # Main wordmark
    d.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    # Trim transparent bounds and add breathing room
    bbox = img.getbbox()
    cropped = img.crop(bbox)
    padded = Image.new("RGBA", (cropped.width + 20, cropped.height + 12), (0, 0, 0, 0))
    padded.paste(cropped, (10, 6))
    padded.save(out)
    print(f"Saved {out}: {padded.size}")


if __name__ == "__main__":
    build_watermark()
