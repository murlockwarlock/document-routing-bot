from pathlib import PurePath


IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".pjpeg", ".pjp", ".png", ".apng",
    ".webp", ".gif", ".bmp", ".dib", ".tif", ".tiff", ".heic", ".heif",
    ".hif", ".avif", ".avifs", ".svg", ".svgz", ".ico", ".cur", ".icns",
    ".jxl", ".jp2", ".j2k", ".jpf", ".jpx", ".jpm", ".mj2", ".psd",
    ".psb", ".raw", ".dng", ".cr2", ".cr3", ".nef", ".nrw", ".arw",
    ".orf", ".rw2", ".raf", ".pef", ".srw", ".tga", ".pcx", ".dds",
    ".ppm", ".pgm", ".pbm", ".pnm", ".pam", ".exr", ".hdr", ".xbm", ".xpm",
})


def is_image_attachment(file_name: str | None, mime_type: str | None = None) -> bool:
    return (
        isinstance(mime_type, str) and mime_type.strip().lower().startswith("image/")
    ) or PurePath(str(file_name or "").strip()).suffix.lower() in IMAGE_EXTENSIONS
