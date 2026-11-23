"""On-demand image reduction for the fixed-domain OpenAI completions relay.

Length checks use base64 bytes (as DSH does), not decoded file sizes. Images
are occurrences, in oldest-message/part order. No downloads, image cache or
conversation state: a relay cannot persist a DSH image/offload event.
"""
import base64
import binascii
import io
import json
import math
from PIL import Image, ImageOps, PngImagePlugin

try:
    import orjson as _native_json
except ImportError:
    _native_json = None

MIB = 1024 * 1024
GATEWAY_BODY_BYTES = 10 * MIB
# The workspace proxy reserializes JSON and may add stream/max_tokens fields.
# A small wire margin also covers JSON.stringify's escaping of control chars.
WIRE_MARGIN_BYTES = 4096
MAX_IMAGES = 50
PRESERVE_IMAGES = 20
MAX_IMAGE_BASE64_BYTES = 20 * MIB
PER_IMAGE_BYTES = MIB
PIXEL_BUDGET = 2048 * 2048
MAX_DIMENSION = 8192
MAX_SOURCE_PIXELS = 64_000_000
MIN_IMAGE_BYTES = 64 * 1024
# Codec work runs serially. Pillow releases the GIL inside libjpeg but not
# inside its own resize/encode paths, so a second worker on the relay's 2 vCPU
# box measures slower than one (median 887 ms vs 869 ms) and with visibly wider
# spread (stdev 19 ms vs 14 ms), while reserving a second decode surface.
# No codec retries either way.
Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS
PngImagePlugin.MAX_TEXT_CHUNK = 256 * 1024
PngImagePlugin.MAX_TEXT_MEMORY = MIB
# Resampling is the largest single codec cost after the decoders. The reduce path
# always finishes with JPEG q60-70, which hides LANCZOS's extra sharpness, so the
# middle kernel buys ~30% off the resize for no visible difference on the wire.
# Measured on 2048x1536 -> 683x512: LANCZOS 52 ms / BICUBIC 36 ms / BILINEAR 22 ms.
_RESAMPLE = getattr(Image, 'Resampling', Image).BICUBIC


class RequestImageLimit(ValueError):
    def __init__(self, message, limit=GATEWAY_BODY_BYTES):
        super().__init__(message)
        self.limit = limit


def _stdlib_dump(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode('utf-8', 'backslashreplace')


def dump(doc):
    if _native_json is not None:
        try:
            return _native_json.dumps(doc)
        except _native_json.JSONEncodeError:
            pass
    return _stdlib_dump(doc)


def _string_bytes(value, serializer):
    if _native_json is not None and serializer is _native_json.dumps:
        return len(serializer(value)) - 2
    quoted = json.encoder.encode_basestring(value)
    return (len(quoted) if quoted.isascii() else len(quoted.encode('utf-8', 'backslashreplace'))) - 2


def _number_padding(doc):
    """Upper bound for JSON.stringify's number spelling after the Node hop.

    Ordinary safe integers have the same spelling. Other IEEE-754 numbers can
    need up to 25 characters; Python and JavaScript choose different fixed vs
    exponent cutoffs (e.g. 1e20 expands to 21 digits in JavaScript).
    """
    extra = 0
    pending = [doc]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, float):
            # Native JSON omits exponent zero padding. Use the shorter token
            # so this also covers stdlib fallback and downstream JSON.stringify.
            length = min(len(repr(value)), len(_native_json.dumps(value))) if _native_json is not None else len(repr(value))
            extra += max(0, 25 - length)
        elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53 - 1:
            extra += max(0, 25 - len(str(value)))
    return extra


def wire_size(doc, serialized):
    """Serialized bytes plus the node-side number expansion headroom."""
    return len(serialized) + _number_padding(doc)


def _image_parts(doc):
    messages = doc.get('messages') if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        return []
    images = []
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('user', 'tool'):
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'image_url':
                continue
            value = part.get('image_url')
            url = value.get('url') if isinstance(value, dict) else value
            if isinstance(url, str):
                images.append(_ImagePart(part, len(images) + 1, url))
    return images


class _ImagePart:
    def __init__(self, part, ordinal, url):
        self.part = part
        self.ordinal = ordinal
        self.removed = False
        self.format = None
        self.size = None
        self.jpeg_scale = None
        self.url = url
        comma = url.find(',', 0, 256) if url.startswith('data:') else -1
        header = url[:comma].lower() if comma >= 0 else ''
        self.offset = comma + 1 if header.startswith('data:image/') and header.endswith(';base64') else 0
        self.encoded_bytes = len(url) - self.offset if self.offset else 0
        self.raw_bytes = self.encoded_bytes // 4 * 3 - (len(url) - len(url.rstrip('='))) if self.offset else 0
        self.json_url_bytes = 0
        self.json_bytes = 0

    def set_url(self, url):
        value = self.part.get('image_url')
        if isinstance(value, dict):
            value['url'] = url
        else:
            self.part['image_url'] = url

    def omit(self):
        self.part.clear()
        self.part.update(type='text', text=(
            '[image omitted to fit request image limits; relay image #%d. '
            'No local normalized image path is available; ask the user to attach it again if needed.]'
            % self.ordinal))
        self.removed = True
        self.url = ''
        self.encoded_bytes = self.raw_bytes = 0

    def replace(self, data, media_type):
        url = 'data:%s;base64,%s' % (media_type, base64.b64encode(data).decode('ascii'))
        self.set_url(url)  # Preserve detail and other image_url fields.
        self.url = url
        self.offset = url.index(',') + 1
        self.encoded_bytes = len(url) - self.offset
        self.raw_bytes = len(data)
        self.json_bytes += len(url) - self.json_url_bytes
        self.json_url_bytes = len(url)

    def dimensions(self):
        if not self.offset:
            return None
        # Header-only, bounded probe; never decode the whole picture on the
        # normal path. Some formats with very long metadata need the cold path.
        try:
            # Most headers fit in 3 KiB; long JPEG metadata gets one bounded
            # header extension, never a trial image encoding.
            for chars in (4096, 87380):
                prefix = self.url[self.offset:self.offset + chars]
                prefix = prefix[:len(prefix) // 4 * 4]
                header = base64.b64decode(prefix, validate=True)
                size = self._header_dimensions(header)
                if size:
                    self.size = size
                    return size
        except (ValueError, OSError):
            pass
        return None

    def _header_dimensions(self, header):
        try:
            # WebP's incremental Pillow plugin waits for the complete file;
            # its standard headers expose dimensions within 30 bytes.
            if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                self.format = 'WEBP'
                kind = header[12:16]
                if kind == b'VP8X' and len(header) >= 30:
                    return (1 + int.from_bytes(header[24:27], 'little'),
                            1 + int.from_bytes(header[27:30], 'little'))
                if kind == b'VP8L' and len(header) >= 25 and header[20] == 0x2f:
                    bits = int.from_bytes(header[21:25], 'little')
                    return (1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
                if kind == b'VP8 ' and len(header) >= 30 and header[23:26] == b'\x9d\x01\x2a':
                    return (int.from_bytes(header[26:28], 'little') & 0x3fff,
                            int.from_bytes(header[28:30], 'little') & 0x3fff)
            if header[:8] == b'\x89PNG\r\n\x1a\n' and header[12:16] == b'IHDR' and len(header) >= 24:
                self.format = 'PNG'
                return (int.from_bytes(header[16:20], 'big'), int.from_bytes(header[20:24], 'big'))
            if header[:2] == b'\xff\xd8':
                self.format = 'JPEG'
                offset = 2
                while offset + 4 <= len(header) and header[offset] == 0xff:
                    marker = header[offset + 1]
                    if marker == 0xff:
                        offset += 1
                        continue
                    length = int.from_bytes(header[offset + 2:offset + 4], 'big')
                    if length < 2:
                        break
                    if marker == 0xdb and offset + 7 <= len(header) and header[offset + 4] == 0:
                        # JPEG's standard luma DC coefficient is 16. Estimate
                        # the source quantization scale without decoding pixels.
                        self.jpeg_scale = max(1, header[offset + 5]) * 100 / 16
                    if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf) and offset + 9 <= len(header):
                        return (int.from_bytes(header[offset + 7:offset + 9], 'big'),
                                int.from_bytes(header[offset + 5:offset + 7], 'big'))
                    offset += length + 2
        except Image.DecompressionBombError:
            return (MAX_SOURCE_PIXELS + 1, 1)
        except (ValueError, OSError):
            pass
        return None


def _over_dimensions(size):
    return size is not None and (size[0] * size[1] > PIXEL_BUDGET or max(size) > MAX_DIMENSION)


def _output_plan(image, target_bytes, size, alpha=False, quantization=None):
    """Choose quality and geometry from headers/bytes; never trial-encode."""
    width, height = size
    pixels = width * height
    if getattr(image, 'preserve_resolution', False):
        scale = min(1.0, math.sqrt(PIXEL_BUDGET / max(1, pixels)), MAX_DIMENSION / max(size))
        return (max(1, int(width * scale)), max(1, int(height * scale))), 80
    fraction = target_bytes / max(1, image.raw_bytes)
    quality = 70 if fraction >= 0.55 else 60
    if image.format == 'JPEG':
        # A source quantization estimate adjusts bytes/pixel for the chosen
        # quality. Custom JPEG tables remain an estimate, never a size proof.
        source_scale = quantization or image.jpeg_scale or 60
        output_scale = 200 - quality * 2
        bpp = image.raw_bytes / max(1, pixels) * min(1.8, max(0.35, (source_scale / output_scale) ** 0.55))
    else:
        # PNG/WebP encoded length does not predict JPEG entropy. Use a
        # conservative pixel-rate estimate instead, with room for alpha.
        bpp = 0.7 if alpha else 0.55
    target_pixels = max(1, (target_bytes * 0.86 - 1024) / max(0.02, bpp))
    scale = min(1.0, math.sqrt(PIXEL_BUDGET / max(1, pixels)),
                math.sqrt(target_pixels / max(1, pixels)), MAX_DIMENSION / max(size))
    return (max(1, int(width * scale)), max(1, int(height * scale))), quality


def _encode_image(image, target_bytes, reservation):
    """Decode at most once, resize at most once, encode exactly once."""
    if reservation.capacity < 8 * MIB:
        return None
    reservation.grow(8 * MIB)
    try:
        data = base64.b64decode(image.url[image.offset:], validate=True)
        with Image.open(io.BytesIO(data)) as source:
            original_size = source.size
            pixels = original_size[0] * original_size[1]
            if pixels > MAX_SOURCE_PIXELS or min(original_size) <= 0:
                return None
            alpha = source.mode in ('RGBA', 'LA') or 'transparency' in source.info
            image.format = source.format
            quantization = None
            if source.format == 'JPEG':
                table = getattr(source, 'quantization', {}).get(0)
                quantization = table[0] * 100 / 16 if table else None
            geometry, quality = _output_plan(image, target_bytes, original_size, alpha, quantization)
            if source.format == 'JPEG':
                source.draft('RGB', geometry)
            # draft only changes the decoder plan. Admit actual decode/output
            # surfaces before any pixel load, preserving the source bomb guard.
            scratch = max(source.size[0] * source.size[1], geometry[0] * geometry[1]) * 16 + 16 * MIB
            if scratch > reservation.capacity:
                return None
            reservation.grow(scratch)
            orientation = source.getexif().get(274, 1)
            oriented = ImageOps.exif_transpose(source) if orientation in range(2, 9) else source
            try:
                mode = 'RGBA' if alpha else 'RGB'
                converted = oriented if oriented.mode == mode else oriented.convert(mode)
                try:
                    # EXIF orientations 5..8 swap the planned output axes.
                    if orientation in (5, 6, 7, 8):
                        geometry = geometry[::-1]
                    if converted.size == geometry:
                        resized = converted
                    else:
                        resized = converted.resize(geometry, _RESAMPLE, reducing_gap=2.0)
                    try:
                        with io.BytesIO() as output:
                            if alpha:
                                resized.save(output, format='WEBP', quality=quality, method=0)
                            else:
                                resized.save(output, format='JPEG', quality=quality, optimize=False)
                            encoded = output.getvalue()
                    finally:
                        if resized is not converted:
                            resized.close()
                finally:
                    if converted is not oriented:
                        converted.close()
            finally:
                if oriented is not source:
                    oriented.close()
        # The byte target stays soft, like DSH. Final exact body accounting
        # handles an estimate miss without a second encode attempt.
        return encoded, 'image/webp' if alpha else 'image/jpeg'
    except (binascii.Error, ValueError, OSError, Image.DecompressionBombError):
        return None


def _measure(doc, images):
    """One small JSON skeleton plus exact C-level string counts, no full dump."""
    try:
        for image in images:
            image.set_url('')
        serializers = [_native_json.dumps, _stdlib_dump] if _native_json is not None else [_stdlib_dump]
        for serializer in serializers:
            try:
                measured = len(serializer(doc))
                for image in images:
                    image.json_url_bytes = _string_bytes(image.url, serializer)
                    image.json_bytes = len(serializer(image.part)) + image.json_url_bytes
                    measured += image.json_url_bytes
                return measured, serializer
            except (TypeError, ValueError):
                if serializer is _stdlib_dump:
                    raise
    finally:
        for image in images:
            image.set_url(image.url)


def _select_images(images, measured, ceiling):
    """Mandatory guards first, then highest byte saving per decoded pixel.

    `remaining` is the byte gap to the ceiling. A mandatory image is one a guard
    forces through the codec regardless of the body size (unreadable header, out
    of geometry budget, or larger than PER_IMAGE_BYTES on its own); those guards
    are what keep a request admissible at all, so they apply even when the body
    is currently under the ceiling. Optional images only help close a gap.
    """
    inline = [image for image in images if not image.removed and image.offset]
    mandatory = [image for image in inline if image.size is None
                 or _over_dimensions(image.size) or image.raw_bytes > PER_IMAGE_BYTES]
    optional = [image for image in inline if image not in mandatory and image.raw_bytes > MIN_IMAGE_BYTES]
    optional.sort(key=lambda image: (-image.raw_bytes / max(1, image.size[0] * image.size[1]), image.ordinal))
    jobs = []
    remaining = max(0, measured - ceiling + 1)
    planned = mandatory + optional
    for position, image in enumerate(planned):
        required = image in mandatory
        image.preserve_resolution = remaining <= 0 and image.raw_bytes <= PER_IMAGE_BYTES
        if not required and remaining <= 0:
            break
        if remaining > 0:
            # Spread the missing bytes over the images not yet planned, so a
            # small overage costs each image a small cut instead of asking the
            # first one for a deep one. Never cut below 40% of the source.
            share = remaining / max(1, len(planned) - position)
            target = min(image.raw_bytes * 0.90,
                         max(image.raw_bytes * 0.40, image.raw_bytes - share * 1.20))
        else:
            target = image.raw_bytes
        target = max(MIN_IMAGE_BYTES, int(min(target, PER_IMAGE_BYTES)))
        predicted = min(image.raw_bytes, target * 0.90)
        remaining -= max(0, image.encoded_bytes - math.ceil(predicted / 3) * 4)
        jobs.append((image, target, required))
    return jobs


def reduce_images(doc, body, reservation, limit=GATEWAY_BODY_BYTES, mutated=False):
    """Select once, encode selected images once, serialize the final body once."""
    # Walk the document once for the node-side number headroom. It only changes
    # when the document's numbers change, and images are replaced as strings, so
    # this figure stays valid for the whole reduce pass.
    padding = _number_padding(doc)
    ceiling = min(limit, GATEWAY_BODY_BYTES) - WIRE_MARGIN_BYTES - padding
    images = _image_parts(doc)
    stats = {'images': len(images), 'removed': 0, 'compressed': 0}
    measured, serializer = _measure(doc, images)
    changed = mutated

    def omit(image):
        nonlocal changed, measured
        old_bytes = image.json_bytes
        image.omit()
        image.json_bytes = len(serializer(image.part))
        measured += image.json_bytes - old_bytes
        stats['removed'] += 1
        changed = True

    count_remove = max(0, len(images) - MAX_IMAGES)
    excess = max(0, sum(image.encoded_bytes for image in images) - MAX_IMAGE_BASE64_BYTES)
    removed_bytes = 0
    for index, image in enumerate(images):
        if index >= count_remove and removed_bytes >= excess:
            break
        removed_bytes += image.encoded_bytes
        omit(image)

    retained = len(images) - stats['removed']
    for image in images:
        if measured < ceiling or retained <= PRESERVE_IMAGES:
            break
        if not image.removed:
            omit(image)
            retained -= 1

    for image in images:
        if not image.removed and image.offset:
            size = image.dimensions()
            if size and size[0] * size[1] > MAX_SOURCE_PIXELS:
                omit(image)

    inline = [image for image in images if not image.removed and image.offset]
    # No codec can help when non-image fields alone exceed the ceiling.
    available = ceiling - 1 - (measured - sum(image.json_url_bytes for image in inline))
    jobs = _select_images(images, measured, ceiling) if available > 0 else []

    def encode(job):
        image, target, _ = job
        worker = reservation.fork()
        try:
            return _encode_image(image, target, worker)
        finally:
            worker.release()

    # Walk the preselected plan once. Savings are exact after each encode, so a
    # mandatory job is always run and an optional one only while the measured
    # body is still over the ceiling; an image is never encoded twice.
    for job in jobs:
        image, _, required = job
        if not required and measured < ceiling:
            continue
        result = encode(job)
        if result is None:
            omit(image)
            continue
        data, media_type = result
        # An image whose header could not be read is kept whatever its size: the
        # re-encode is the only way its dimensions become known, and it was
        # scheduled as mandatory. For a known image a re-encode that did not
        # shrink it wastes the client's latency and the gateway's bytes, so the
        # original wins.
        if image.size is None or len(data) < image.raw_bytes or _over_dimensions(image.size):
            old_bytes = image.json_bytes
            image.replace(data, media_type)
            measured += image.json_bytes - old_bytes
            stats['compressed'] += 1
            changed = True

    # Estimate misses only omit oldest occurrences; no further codec work.
    for image in images:
        if measured < ceiling:
            break
        if not image.removed:
            omit(image)
    if measured >= ceiling:
        raise RequestImageLimit(
            'request remains too large after image reduction; text, tool schemas or '
            'other non-removable content exceeds the gateway budget; compact the conversation', limit)
    if not changed and len(body) < ceiling:
        return body, stats
    out = serializer(doc)
    if len(out) != measured or len(out) >= ceiling:
        raise RequestImageLimit('request serialization exceeds the gateway budget; compact the conversation', limit)
    return out, stats
