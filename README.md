# Steuer Hub

Self-hosted helper for the German tax return (Einkommensteuererklärung). It runs on a Raspberry Pi with the
receipts on your NAS, entirely on your home network.

- **Upload** receipts in the browser (phone-friendly). Each goes into the NAS folder for its category.
- **Read automatically**: text layer or Tesseract OCR extracts date, amount and vendor. Low confidence goes to review.
- **People**: everyone on the return, with Steuerklasse (per year), optional Faktor and one-way commute km.
- **Work days**: office / home / away per person, with one-tap "today" buttons and a range filler.
- **Sections**: totals per ELSTER section, per person for Anlage N, with the Pauschbetrag comparison.
- **Review gate**: approve items, then sign off. Any later change shows "figures changed since sign-off".
- **Export** CSV (summary + audit list of all documents). Submission stays manual in Mein ELSTER.

## Quick start (Raspberry Pi, Docker)

```bash
cp .env.example .env          # set NAS_PATH, PUID/PGID, optional STEUER_USER/PASSWORD
mkdir -p data
docker compose up -d --build
# open http://<pi-address>:8080
```

First steps in the UI: **People** (names, Steuerklasse, commute km), then upload a receipt.

Without Docker (development): `pip install -r requirements-dev.txt`, install `tesseract-ocr` and `poppler-utils`,
then `uvicorn app.main:app` and, in a second terminal, `python -m worker.run`. Data goes to `./data` by default.

## NAS mount

Mount the share on the Pi first and point `NAS_PATH` at it, for example in `/etc/fstab`:

```
//nas.local/steuer  /mnt/nas/steuer  cifs  credentials=/etc/smb-steuer.cred,uid=1000,gid=1000,file_mode=0640,dir_mode=0750,_netdev,nofail,x-systemd.automount  0 0
# or NFS:
nas.local:/volume1/steuer  /mnt/nas/steuer  nfs  defaults,_netdev,nofail  0 0
```

**Marker file.** An unmounted share often shows up as an empty folder on the Pi's own disk, and Docker will happily
bind-mount that. The app therefore writes `.steuer-hub-marker` into the NAS root on first run and refuses uploads and
OCR work while it is missing (red banner, `/healthz` returns 503). If you ever restore onto a fresh NAS, `touch`
that file once.

```
<NAS>/
  2026/
    arbeitsmittel/  fortbildung/  versicherung/  schulgeld/  spenden/  steuererklaerung/  handwerker/  sonstiges/
    _inbox/    drop scans here (e.g. from a scanner); they appear as "Unsorted" until you pick a category
    _trash/    "Remove" moves the original here, nothing is deleted
  _backups/    daily copy of the SQLite DB
```

Files are stored as `<date>-<hash8>-<name>`. The originals are never edited. Moving a file happens only when you
change its category or sort an inbox file.

## Configuration

- `config/categories.yml`: folders, which section each belongs to, share that counts (`factor`) and caps.
- `config/tax_rules.yml`: home-office rate and day cap, commute rates, Pauschbetrag, per year. A year that is missing
  falls back to the closest earlier year and the app warns you. **Check these figures every year.**

Values were checked in November 2026: 6 EUR/day home office (max 210 days), 0.38 EUR/km commuting from the first
kilometre in 2026, Arbeitnehmer-Pauschbetrag 1,230 EUR.

## Hosting notes

- **SQLite lives on local disk** (`./data`), never on the NAS share (file locking over SMB/NFS corrupts databases).
  Use an SSD rather than an SD card. The worker copies the DB to `<NAS>/_backups` daily and keeps 14 copies.
- **Polling, not inotify**: change events are unreliable on network mounts, so the worker scans every 30 s.
- **OCR speed**: a Pi needs a few seconds per page. Uploads return immediately; the dashboard shows "being read".
- **Security**: LAN only. Set `STEUER_USER`/`STEUER_PASSWORD` if anything else can reach the Pi, and add HTTPS with
  `Caddyfile.example`. For access away from home use WireGuard or Tailscale, never port forwarding. Cross-site form
  posts are rejected. This is financial data: keep the NAS share and backups encrypted or access-controlled.
- **Backups**: the DB backup does not replace backing up the NAS itself. Keep a second, offsite copy of the receipts.

## What it does not do

- It does **not** submit to ELSTER. The export gives you the numbers to enter in Mein ELSTER yourself.
- OCR is a heuristic and EUR only. Every amount is a suggestion until you approve it.
- Not modelled (the tax office or ELSTER applies them, or you adjust the amount): Höchstbeträge for Vorsorgeaufwendungen,
  the donation income limit, the 5,000 EUR cap per child for Schulgeld, GWG/depreciation of equipment, the 4,500 EUR
  commute cap without a car, and whether you had another workplace on a home-office day. Public holidays are not
  filled in automatically.
- It is a bookkeeping aid, not tax advice.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
