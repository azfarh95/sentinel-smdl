FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    libgomp1 \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 smdl

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the small u2netp segmentation model into the image so the first cutout
# sticker doesn't pay a download. U2NET_HOME stays set for runtime too.
ENV U2NET_HOME=/app/u2net_models
RUN python -c "from rembg import new_session; new_session('u2netp')" \
    && chown -R smdl:smdl /app/u2net_models

COPY app/ ./app/
COPY data/ ./data/

RUN mkdir -p /data && chown smdl:smdl /data

USER smdl

EXPOSE 8096

# --proxy-headers + --forwarded-allow-ips='*' trust X-Forwarded-Proto / -For /
# -Host from the Cloudflare Tunnel that terminates TLS in front of us. Without
# these, request.base_url comes back as http:// and OAuth redirect_uri params
# get built with the wrong scheme — Twitch/Google then reject the round-trip
# as a redirect-URI mismatch. We can scope -allow-ips=* because the container
# is bound to 127.0.0.1 (or the tunnel-internal network) and the only thing
# reaching us speaks for the proxy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8096", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips=*"]
