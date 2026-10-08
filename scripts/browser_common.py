"""What the browser scripts share: the loopback port forward into the Compose network, and the
sample portraits and QR marker frames they upload."""

import asyncio
import io
import threading
from datetime import datetime, timezone

import segno
from PIL import Image, ImageDraw

run = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")  # uploads are deduplicated by content


def forward(listen_port, target_host, target_port):
    """Expose the Compose stack on the loopback port the public URL names."""

    async def pump(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def handle(client_reader, client_writer):
        upstream_reader, upstream_writer = await asyncio.open_connection(target_host, target_port)
        await asyncio.gather(pump(client_reader, upstream_writer), pump(upstream_reader, client_writer))

    async def serve():
        server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
        async with server:
            await server.serve_forever()

    threading.Thread(target=lambda: asyncio.run(serve()), daemon=True).start()


def sample(index, number=None):
    """A labelled stand-in for a portrait; `number` sets its place in the camera sequence."""
    image = Image.new("RGB", (1000, 1250), ["#a7b8a0", "#d1b19a", "#a5b7bf"][index])
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 850, 1000, 1250), fill=["#50654b", "#795a46", "#4c6774"][index])
    draw.ellipse((180, 140, 820, 780), fill=["#e6e3c6", "#ead9bf", "#e1decf"][index])
    draw.polygon(
        [(0, 1100), (460, 420), (760, 1250), (0, 1250)], fill=["#708669", "#a77657", "#718b93"][index]
    )
    draw.text((45, 45), f"PEOPLE IN PHOTOGRAPHS / LOCAL SAMPLE {index + 1}", fill="#24392d", font_size=30)
    draw.text((45, 90), run, fill="#24392d", font_size=24)
    draw.text((45, 1180), "Test artwork - upload your own photographs", fill="#ffffff", font_size=28)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    number = index + 1 if number is None else number
    return {"name": f"local-sample-{number}.jpg", "mimeType": "image/jpeg", "buffer": buffer.getvalue()}


def marker(recipient_url, number=9):
    """A frame of the printed QR card, as the photographer would shoot it after the portraits."""
    png = io.BytesIO()
    segno.make(recipient_url, micro=False).save(png, kind="png", scale=12, border=4)
    png.seek(0)
    with Image.open(png) as qr:
        image = Image.new("RGB", (1000, 1000), "white")
        code_image = qr.convert("RGB")
        image.paste(code_image, ((image.width - code_image.width) // 2, (image.height - code_image.height) // 2))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95)
    return {"name": f"local-sample-{number}-qr-marker.jpg", "mimeType": "image/jpeg", "buffer": buffer.getvalue()}
