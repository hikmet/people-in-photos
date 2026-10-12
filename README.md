# People in Photographs

A self-hosted web app that enables a street photographer to share the photos with people they photographed without exchange of contact info. The photographer could also ask for consent to publish if they choose to. 

You hand a card with a QR code to the person you photographed after you also photograph the QR code with your camera. Later when you have the photos uploaded to this app, it will split the sequence by the QR code frames detected. 

The person opens the card, downloads their photos, and if you asked they also respond to a consent to publish request as well. No email, no accounts, and nothing typed on the street. 

One photographer behind one password; English and Turkish.

## Why this exists

A street photographer friend told me he’d mostly stopped shooting. People he photographed often asked for a copy, and there was no easy way to get it to them. Getting their permission to publish, on social media or in print, was even harder. I built this to solve both problems without asking strangers for an email address on the sidewalk.

## Run it locally

You need Docker with Compose.

```
cp .env.example .env
docker compose up -d --build
```

Open http://localhost:8010/login and sign in with the password from `.env` (`people-local`). The
**How to use** page in the dashboard walks through the workflow.

## Run it on a server with a domain

You need a machine with Docker and ports 80 and 443 reachable from the internet.

1. Point the domain's DNS at the server.
2. Clone the repository, copy `.env.example` to `.env` and set `APP_ENV=production`,
   `PUBLIC_BASE_URL=https://<domain>`, `SITE_ADDRESS=<domain>`, `HTTP_BIND=80`, `HTTPS_BIND=443`, a
   random `SECRET_KEY` (`openssl rand -hex 32`) and a `PHOTOGRAPHER_PASSWORD` of at least 12
   characters.
3. Run `docker compose up -d --build`. Caddy obtains and renews the HTTPS certificate by itself.

Set `PUBLIC_BASE_URL` before printing cards, because it is printed into every QR code. To update,
run `git pull` and `docker compose up -d --build`.

## Storage

Photos are stored on local disk by default, in the `data` Docker volume next to the SQLite database.
To keep them in a Cloudflare R2 bucket instead, set `STORAGE_BACKEND=r2` and the four `R2_` values
in `.env`. The app makes no backups, so copy the volume (and the bucket) somewhere else yourself.

The default permission text has not been reviewed by a lawyer; adapt it to your country on the
Profile & release page. [DESIGN.md](DESIGN.md) describes how everything works.

## How it was built

I designed the app and built it entirely with Claude Code, using an agent-first workflow from spec to working product.

It started as a hosted, multi-photographer service. Because I didn’t want to charge for it, and photo storage gets expensive at scale without payments, I removed multi-account support. It is now a single-owner app that installs with one Docker command; all you need is a server with a domain. DESIGN.md covers the architecture.

## License

[MIT](LICENSE).
