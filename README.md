# Lasershow Monitor â Installatiegids

Dagelijkse mediascan voor gemeentelijke, zakelijke en NGO-plannen voor
oudejaarsvieringen met licht- en/of lasershows.

---

## Wat doet het?

- Doorzoekt dagelijks **Google News** op 14 zoekopdrachten (NL) Ã©n **16 directe RSS-feeds**
  van lokale omroepen en gemeentenieuws-sites
- Beoordeelt elk nieuw bericht met Claude (1â5 significantie)
- Stuurt **direct een e-mail-alert** bij score â¥ 4 (concreet plan, budget, besluit)
- Stuurt **elke maandag een weekoverzicht** met alles wat er die week voorbijkwam
- Slaat alles op in een lokale SQLite-database (`monitor.db`)

---

## Installatie

### 1. Python-afhankelijkheden

```bash
pip install -r requirements.txt
```

### 2. Configuratie

```bash
cp config.example.json config.json
```

Open `config.json` en vul in:
- `anthropic_api_key` â jouw Anthropic API-sleutel (van console.anthropic.com)
- `email.smtp_host`, `email.username`, `email.password` â zie hieronder
- `email.to` â e-mailadres(sen) die alerts ontvangen

#### Gmail-app-wachtwoord instellen
1. Ga naar myaccount.google.com â Beveiliging â 2-stapsverificatie (aan)
2. Zoek "App-wachtwoorden" â maak een nieuw wachtwoord
3. Gebruik dat wachtwoord als `password` in config.json

### 3. Database initialiseren (eerste keer)

```bash
python monitor.py --init
```

---

## Gebruik

### Eenmalig handmatig draaien
```bash
python monitor.py
```

### Wekelijks digest nu forceren
```bash
python monitor.py --digest
```

### Testen (geen opslag, geen e-mail)
```bash
python monitor.py --test
```

---

## Automatisch draaien â cron (Linux/Mac)

```bash
crontab -e
```

Voeg toe (elke dag om 07:00):
```
0 7 * * * cd /pad/naar/lasershow_monitor && /usr/bin/python3 monitor.py >> monitor.log 2>&1
```

Vervang `/pad/naar/lasershow_monitor` door het absolute pad naar de map.

---

## Automatisch draaien â Windows Taakplanner

1. Open **Taakplanner** (Task Scheduler)
2. Maak een nieuwe basistaak aan:
   - **Trigger**: Dagelijks om 07:00
   - **Actie**: Programma starten
   - **Programma**: `C:\Python312\python.exe`
   - **Argumenten**: `monitor.py`
   - **Starten in**: `C:\pad\naar\lasershow_monitor`

---

## Automatisch draaien â GitHub Actions (cloud, gratis)

Maak een bestand aan in je repo: `.github/workflows/monitor.yml`

```yaml
name: Lasershow Monitor

on:
  schedule:
    - cron: '0 6 * * *'   # Elke dag om 06:00 UTC (= 07:00 NL zomertijd)
  workflow_dispatch:        # Handmatig starten via GitHub UI

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Restore database
        uses: actions/cache@v4
        with:
          path: monitor.db
          key: monitor-db-${{ github.run_id }}
          restore-keys: monitor-db-

      - name: Run monitor
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          # Maak config.json vanuit secrets
          echo '{
            "anthropic_api_key": "'$ANTHROPIC_API_KEY'",
            "email": {
              "smtp_host": "${{ secrets.SMTP_HOST }}",
              "smtp_port": 587,
              "use_tls": true,
              "username": "${{ secrets.SMTP_USER }}",
              "password": "${{ secrets.SMTP_PASS }}",
              "from": "Lasershow Monitor <${{ secrets.SMTP_USER }}>",
              "to": ["${{ secrets.ALERT_EMAIL }}"]
            }
          }' > config.json
          python monitor.py

      - name: Save database
        uses: actions/cache/save@v4
        if: always()
        with:
          path: monitor.db
          key: monitor-db-${{ github.run_id }}
```

Voeg de volgende **Secrets** toe in je GitHub repo (Settings â Secrets):
- `ANTHROPIC_API_KEY`
- `SMTP_HOST` (bijv. `smtp.gmail.com`)
- `SMTP_USER`
- `SMTP_PASS`
- `ALERT_EMAIL`

---

## Significantieniveaus

| Score | Symbool | Betekenis | Actie |
|-------|---------|-----------|-------|
| 5 | ð¨ | Zeer significant (groot budget, VNG, nationaal) | Direct alert |
| 4 | ð´ | Significant (concreet plan, besluit, nieuwe gemeente) | Direct alert |
| 3 | ð  | Matig relevant (show genoemd maar vaag) | Weekdigest |
| 2 | ð¡ | Zwak relevant (algemene vuurwerkdiscussie) | Weekdigest |
| 1 | âª | Niet relevant | Weekdigest (ter info) |

---

## Database raadplegen

```bash
sqlite3 monitor.db

# Alle significante berichten
SELECT found_at, significance, title, source FROM articles
WHERE significance >= 4 ORDER BY found_at DESC;

# Weekoverzicht exporteren naar CSV
.mode csv
.output weekoverzicht.csv
SELECT found_at, significance, title, source, url FROM articles
WHERE digested = 0 ORDER BY significance DESC;
.quit
```

---

## Geschatte kosten (Anthropic API)

- Per artikel: ~300 tokens in + ~60 tokens uit â â¬0,0005
- Per dag: ~50â150 nieuwe unieke artikelen Ã â¬0,0005 â **â¬0,03ââ¬0,08 per dag**
- Per maand: **< â¬2**
