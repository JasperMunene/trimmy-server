"""Shrinking uploaded photos before they're stored, so phones on slow data load a few tens of KB
instead of a camera's multi-MB original. Pillow does the work in a thread (it's CPU-bound)."""
import asyncio
import io

from PIL import Image, ImageOps, UnidentifiedImageError

# A decompression bomb (tiny file, huge pixel count) is refused rather than decoded.
Image.MAX_IMAGE_PIXELS = 60_000_000

AVATAR_SIDE = 512  # square; shown at up to ~96px, so plenty for sharp 3x screens
PHOTO_SIDE = 1600  # longest side for portfolio photos (full-screen viewer)


class ImageError(ValueError):
    """The bytes aren't a photo Pillow can read."""


def _shrink(data: bytes, max_side: int, square: bool, quality: int) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)  # phone photos come sideways otherwise
            if img.mode not in ("RGB", "L"):
                # Transparent PNGs go onto white (JPEG has no transparency).
                rgba = img.convert("RGBA")
                img = Image.new("RGB", rgba.size, (255, 255, 255))
                img.paste(rgba, mask=rgba.split()[-1])
            elif img.mode == "L":
                img = img.convert("RGB")
            if square:
                img = ImageOps.fit(img, (max_side, max_side), Image.Resampling.LANCZOS)
            else:
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            # Progressive: a blurry version shows first on slow connections. No EXIF (location, etc.).
            img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as e:
        raise ImageError(str(e)) from e


async def shrink_image(data: bytes, max_side: int, *, square: bool = False, quality: int = 80) -> bytes:
    """A JPEG no bigger than max_side (cropped square if asked). Raises ImageError."""
    return await asyncio.to_thread(_shrink, data, max_side, square, quality)
