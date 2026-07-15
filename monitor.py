#!/usr/bin/env python3
"""
Lasershow Monitor â dagelijkse mediascan voor licht- en lasershow plannen
bij oudejaarsvieringen door gemeenten, bedrijven en non-gouvernementele organisaties.

Gebruik:
    python monitor.py           â dagelijkse run (ook wekelijks digest op maandag)
    python monitor.py --digest  â forceer wekelijks overzicht nu
    python monitor.py --test    â test alle feeds + email, sla niets op
    python monitor.py --init    â initialiseer database (eerste keer)
"""

import argparse
import json
import logging
import os
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
import anthropic

# ââ Configuratie ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
DB_FILE = Path(os.environ.get("MONITOR_DB", BASE_DIR / "monitor.db"))
LOG_FILE = BASE_DIR / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Schrijf alleen naar logfile als die schrijfbaar is (lokaal gebruik)
        *(
            [logging.FileHandler(LOG_FILE)]
            if os.access(BASE_DIR, os.W_OK)
            else []
        ),
    ],
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    """
    Laad configuratie â eerst uit omgevingsvariabelen (GitHub Actions / CI),
    daarna als fallback vanuit config.json (lokaal gebruik).

    Omgevingsvariabelen:
        ANTHROPIC_API_KEY   â Anthropic API-sleutel
        SMTP_HOST           â bijv. smtp.gmail.com
        SMTP_PORT           â standaard 587
        SMTP_USER           â gebruikersnaam / e-mailadres
        SMTP_PASS           â wachtwoord of app-wachtwoord
        ALERT_EMAIL         â ontvanger(s), komma-gescheiden
    """
    # Probeer omgevingsvariabelen
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    smtp_host = os.environ.get("SMTP_HOST")

    if api_key and smtp_host:
        log.info("Configuratie geladen vanuit omgevingsvariabelen.")
        to_raw = os.environ.get("ALERT_EMAIL", os.environ.get("SMTP_USER", ""))
        return {
            "anthropic_api_key": api_key,
            "email": {
                "smtp_host": smtp_host,
                "smtp_port": int(os.environ.get("SMTP_PORT", 587)),
                "use_tls": os.environ.get("SMTP_TLS", "true").lower() != "false",
                "username": os.environ.get("SMTP_USER", ""),
                "password": os.environ.get("SMTP_PASS", ""),
                "from": os.environ.get("SMTP_FROM", f"Lasershow Monitor <{os.environ.get('SMTP_USER','')}>"),
                "to": [a.strip() for a in to_raw.split(",") if a.strip()],
            },
        }

    # Fallback: config.json
    if CONFIG_FILE.exists():
        log.info("Configuratie geladen vanuit config.json.")
        with open(CONFIG_FILE) as f:
            return json.load(f)

    log.error(
        "Geen configuratie gevonden. Stel omgevingsvariabelen in "
        "(ANTHROPIC_API_KEY, SMTP_HOST, â¦) of kopieer config.example.json â config.json."
    )
    sys.exit(1)


# ââ Zoekqueries âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
GOOGLE_NEWS_BASE = "https://news.google.com/rss/search?hl=nl&gl=NL&ceid=NL:nl&q="

# Combinaties van kernbegrippen â alle queries worden dagelijks gedraaid
SEARCH_QUERIES = [
    # Primair: laser/lichtshow + jaarwisseling/aftelmoment
    'lasershow jaarwisseling gemeente',
    'lichtshow aftelmoment gemeente',
    '"licht- en lasershow" oudjaarsavond',
    '"laser- en lichtshow" oudejaarsavond',
    'lasershow "oud en nieuw" gemeente',
    # Beleidsmatig: verbod + alternatief
    'vuurwerkverbod lasershow gemeente 2025 OR 2026',
    'vuurwerkvrij aftelmoment lichtshow aanbesteding',
    # Marktverkenning / tender / raadsbesluit
    'marktverkenning jaarwisseling lasershow',
    'aanbesteding lasershow oudjaarsavond',
    'raadsbesluit jaarwisseling lichtshow',
    # Non-gouvernementeel / zakelijk
    'VNG lasershow jaarwisseling',
    'evenementenbureau lasershow aftelmoment gemeente',
    # Uitbreiding naar plannen/subsidies
    '"aftelmoment" lasershow subsidie',
    'jaarwisseling 2026 lasershow gemeente plan',
]

# Extra directe RSS-bronnen (lokale omroepen, gemeentenieuws)
DIRECT_RSS_FEEDS = [
    "https://www.omroepwest.nl/rss/nieuws",
    "https://www.omroepgelderland.nl/rss",
    "https://oogtv.nl/feed/",
    "https://www.rtvnoord.nl/feeds/nieuws.xml",
    "https://www.rtvfocuszwolle.nl/feed/",
    "https://www.gld.nl/rss/nieuws",
    "https://www.duic.nl/feed/",
    "https://zogouds.nl/rss",
    "https://rijswijksdagblad.nl/rss",
    "https://groningen.nieuws.nl/feed/",
    "https://arnhem.nieuws.nl/feed/",
    "https://lichtfestivals.nl/feed",
    "https://gemeente.nu/feed/",
]

# Trefwoorden voor directe feed-filtering (minimaal 1 vereist)
FEED_KEYWORDS = [
    "lasershow", "lichtshow", "laser", "aftelmoment",
    "jaarwisseling", "oudjaarsavond", "vuurwerkvrij",
    "licht- en laser", "laser- en licht",
]


# ââ Database ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT UNIQUE NOT NULL,
            title       TEXT,
            source      TEXT,
            published   TEXT,
            summary     TEXT,
            significance INTEGER DEFAULT 0,
            alerted     INTEGER DEFAULT 0,
            digested    INTEGER DEFAULT 0,
            found_at    TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at      TEXT DEFAULT (datetime('now')),
            new_articles INTEGER,
            alerts_sent  INTEGER,
            mode        TEXT
        );
    """)
    conn.commit()
    log.info("Database geÃ¯nitialiseerd.")


def is_known(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,))
    return cur.fetchone() is not None


def save_article(conn: sqlite3.Connection, url, title, source, published, summary, significance):
    try:
        conn.execute(
            """INSERT OR IGNORE INTO articles
               (url, title, source, published, summary, significance)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (url, title, source, published, summary, significance),
        )
        conn.commit()
    except sqlite3.Error as e:
        log.warning(f"DB-fout bij opslaan {url}: {e}")


def get_pending_alert(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM articles WHERE significance >= ? AND alerted = 0 ORDER BY found_at DESC",
        (4,),
    )
    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]


def get_pending_digest(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM articles WHERE digested = 0 ORDER BY significance DESC, found_at DESC"
    )
    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]


def mark_alerted(conn: sqlite3.Connection, article_ids: list[int]):
    conn.executemany(
        "UPDATE articles SET alerted = 1, digested = 1 WHERE id = ?",
        [(i,) for i in article_ids],
    )
    conn.commit()


def mark_digested(conn: sqlite3.Connection, article_ids: list[int]):
    conn.executemany(
        "UPDATE articles SET digested = 1 WHERE id = ?",
        [(i,) for i in article_ids],
    )
    conn.commit()


# ââ Feed ophalen ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def fetch_google_news(query: str, max_items: int = 15) -> list[dict]:
    url = GOOGLE_NEWS_BASE + quote_plus(query)
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            published = entry.get("published", "")
            items.append({
                "url": entry.get("link", ""),
                "title": entry.get("title", ""),
                "source": entry.get("source", {}).get("title", "Google News"),
                "published": published,
                "summary": entry.get("summary", "")[:800],
            })
        return items
    except Exception as e:
        log.warning(f"Google News fout voor '{query}': {e}")
        return []


def fetch_direct_feed(feed_url: str, max_items: int = 20) -> list[dict]:
    try:
        feed = feedparser.parse(feed_url, request_headers={"User-Agent": "Mozilla/5.0"})
        items = []
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))[:800]
            combined = (title + " " + summary).lower()
            # Alleen doorgaan als minimaal 1 trefwoord aanwezig is
            if not any(kw in combined for kw in FEED_KEYWORDS):
                continue
            items.append({
                "url": entry.get("link", ""),
                "title": title,
                "source": feed.feed.get("title", feed_url),
                "published": entry.get("published", ""),
                "summary": summary,
            })
        return items
    except Exception as e:
        log.warning(f"Feed fout {feed_url}: {e}")
        return []


# ââ AI-significantiebeoordeling âââââââââââââââââââââââââââââââââââââââââââââââ
SIGNIFICANCE_PROMPT = """Je bent een onderzoeksassistent die mediaberichten beoordeelt voor relevantie.

Onderwerp: plannen, marktverkenningen of besluiten van gemeenten, bedrijven of NGO's in Nederland 
voor oudejaarsvieringen waarbij een licht- en/of lasershow wordt ingezet als alternatief voor vuurwerk.

Beoordeel het volgende bericht op een schaal van 1 tot 5:
1 = Totaal niet relevant (geen relatie met lasershow/lichtshow bij jaarwisseling)
2 = Zwak relevant (generieke vuurwerkdiscussie, geen concrete show/plan)
3 = Matig relevant (lasershow/lichtshow genoemd maar weinig concreet)
4 = Significant (concreet plan, budget, besluit, aanbesteding, of nieuwe gemeente/organisatie)
5 = Zeer significant (groot besluit, hoog budget, VNG/nationale context, meerdere gemeenten)

Geef ALLEEN een JSON-object terug in dit formaat:
{"score": <1-5>, "reden": "<max 100 woorden>", "gemeente": "<naam of null>", "budget_eur": <getal of null>}

Bericht:
Titel: {title}
Bron: {source}
Samenvatting: {summary}
"""


def score_article(client: anthropic.Anthropic, article: dict) -> tuple[int, str, str | None, int | None]:
    prompt = SIGNIFICANCE_PROMPT.format(
        title=article["title"],
        source=article["source"],
        summary=article["summary"],
    )
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Strip eventuele markdown code fences
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return (
            int(data.get("score", 1)),
            data.get("reden", ""),
            data.get("gemeente"),
            data.get("budget_eur"),
        )
    except Exception as e:
        log.warning(f"Score fout voor '{article['title']}': {e}")
        return 1, "Beoordeling mislukt", None, None


# ââ Email âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def send_email(cfg: dict, subject: str, html_body: str):
    ec = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = ec["from"]
    msg["To"] = ", ".join(ec["to"]) if isinstance(ec["to"], list) else ec["to"]
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(ec["smtp_host"], ec["smtp_port"]) as server:
            if ec.get("use_tls", True):
                server.starttls()
            if ec.get("username"):
                server.login(ec["username"], ec["password"])
            server.send_message(msg)
        log.info(f"E-mail verstuurd: {subject}")
    except Exception as e:
        log.error(f"E-mail mislukt: {e}")


SIGNIFICANCE_STARS = {1: "âª", 2: "ð¡", 3: "ð ", 4: "ð´", 5: "ð¨"}


def build_alert_html(articles: list[dict]) -> str:
    items_html = ""
    for a in articles:
        stars = SIGNIFICANCE_STARS.get(a["significance"], "")
        items_html += f"""
        <div style="border-left:4px solid #e53e3e;padding:12px 16px;margin:16px 0;background:#fff5f5;border-radius:0 4px 4px 0">
          <p style="margin:0 0 4px;font-size:13px;color:#888">{stars} Significantie {a['significance']}/5 Â· {a['source']} Â· {a['published'][:10]}</p>
          <p style="margin:0 0 8px;font-weight:700;font-size:16px"><a href="{a['url']}" style="color:#c53030;text-decoration:none">{a['title']}</a></p>
          <p style="margin:0;color:#555;font-size:14px">{a['summary'][:300]}â¦</p>
        </div>"""
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto">
      <div style="background:#c53030;padding:20px;border-radius:8px 8px 0 0">
        <h1 style="color:white;margin:0;font-size:20px">ð¨ Lasershow Monitor â Significante melding</h1>
        <p style="color:#fecaca;margin:4px 0 0;font-size:13px">{datetime.now().strftime('%d %B %Y, %H:%M')}</p>
      </div>
      <div style="padding:20px;background:#fafafa;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px">
        <p style="color:#555">Er zijn <strong>{len(articles)} significante berichten</strong> gevonden die directe aandacht vragen:</p>
        {items_html}
        <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">
        <p style="font-size:12px;color:#aaa">Lasershow Monitor Â· Automatisch gegenereerd Â· Zie monitor.db voor volledig archief</p>
      </div>
    </div>"""


def build_digest_html(articles: list[dict], week_nr: int) -> str:
    if not articles:
        return f"""
        <div style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;padding:24px">
          <h2>ð Weekoverzicht #{week_nr} â Lasershow Monitor</h2>
          <p>Geen nieuwe relevante berichten deze week.</p>
        </div>"""

    by_score = {5: [], 4: [], 3: [], 2: [], 1: []}
    for a in articles:
        by_score[a["significance"]].append(a)

    sections = ""
    labels = {
        5: ("ð¨", "Zeer significant"),
        4: ("ð´", "Significant"),
        3: ("ð ", "Matig relevant"),
        2: ("ð¡", "Zwak relevant"),
        1: ("âª", "Niet relevant (ter info)"),
    }
    for score in [5, 4, 3, 2]:
        group = by_score[score]
        if not group:
            continue
        icon, label = labels[score]
        items = ""
        for a in group:
            items += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:14px">
                <a href="{a['url']}" style="color:#2b6cb0;font-weight:600">{a['title']}</a><br>
                <span style="color:#888;font-size:12px">{a['source']} Â· {a['published'][:10]}</span><br>
                <span style="color:#555;font-size:13px">{a['summary'][:200]}â¦</span>
              </td>
            </tr>"""
        sections += f"""
        <h3 style="font-size:15px;color:#2d3748;margin:24px 0 8px">{icon} {label} ({len(group)})</h3>
        <table style="width:100%;border-collapse:collapse;background:white;border:1px solid #e2e8f0;border-radius:4px">{items}</table>"""

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto">
      <div style="background:#2b6cb0;padding:20px;border-radius:8px 8px 0 0">
        <h1 style="color:white;margin:0;font-size:20px">ð Weekoverzicht #{week_nr} â Lasershow Monitor</h1>
        <p style="color:#bee3f8;margin:4px 0 0;font-size:13px">{datetime.now().strftime('%d %B %Y')} Â· {len(articles)} berichten verwerkt</p>
      </div>
      <div style="padding:20px;background:#fafafa;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px">
        {sections}
        <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">
        <p style="font-size:12px;color:#aaa">Lasershow Monitor Â· Automatisch gegenereerd Â· Zie monitor.db voor volledig archief</p>
      </div>
    </div>"""


# ââ Hoofdlogica âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def run(cfg: dict, force_digest: bool = False, test_mode: bool = False):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])

    log.info(f"=== Run gestart {'(TEST)' if test_mode else ''} ===")

    # 1. Haal alle berichten op
    all_items: list[dict] = []

    log.info(f"Google News queries: {len(SEARCH_QUERIES)}")
    for query in SEARCH_QUERIES:
        items = fetch_google_news(query)
        log.info(f"  '{query[:50]}': {len(items)} berichten")
        all_items.extend(items)
        time.sleep(0.5)  # beleefd wachten

    log.info(f"Directe feeds: {len(DIRECT_RSS_FEEDS)}")
    for feed_url in DIRECT_RSS_FEEDS:
        items = fetch_direct_feed(feed_url)
        if items:
            log.info(f"  {feed_url}: {len(items)} relevante berichten")
        all_items.extend(items)
        time.sleep(0.3)

    # 2. Dedupliceer op URL
    seen_urls = set()
    unique_items = []
    for item in all_items:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_items.append(item)

    log.info(f"Unieke berichten: {len(unique_items)}")

    # 3. Filter al bekende berichten
    new_items = [i for i in unique_items if not is_known(conn, i["url"])]
    log.info(f"Nieuw (nog niet gezien): {len(new_items)}")

    # 4. Score en sla op
    new_count = 0
    for item in new_items:
        score, reden, gemeente, budget = score_article(client, item)
        log.info(f"  [{score}/5] {item['title'][:60]} ({gemeente or '-'}, â¬{budget or '-'})")

        if not test_mode:
            save_article(
                conn,
                url=item["url"],
                title=item["title"],
                source=item["source"],
                published=item["published"],
                summary=item["summary"],
                significance=score,
            )
            new_count += 1
        time.sleep(0.2)

    # 5. Stuur alerts voor significante berichten (score >= 4)
    alerts_sent = 0
    if not test_mode:
        pending_alerts = get_pending_alert(conn)
        if pending_alerts:
            log.info(f"Significante berichten voor alert: {len(pending_alerts)}")
            html = build_alert_html(pending_alerts)
            subject = f"ð¨ Lasershow Alert â {len(pending_alerts)} significante melding(en)"
            send_email(cfg, subject, html)
            mark_alerted(conn, [a["id"] for a in pending_alerts])
            alerts_sent = len(pending_alerts)

    # 6. Wekelijks digest (elke maandag, of geforceerd)
    is_monday = datetime.now().weekday() == 0
    if (is_monday or force_digest) and not test_mode:
        pending_digest = get_pending_digest(conn)
        week_nr = datetime.now().isocalendar()[1]
        log.info(f"Wekelijks digest: {len(pending_digest)} berichten")
        html = build_digest_html(pending_digest, week_nr)
        subject = f"ð Weekoverzicht #{week_nr} â Lasershow Monitor ({len(pending_digest)} berichten)"
        send_email(cfg, subject, html)
        mark_digested(conn, [a["id"] for a in pending_digest])

    # 7. Logboek
    if not test_mode:
        conn.execute(
            "INSERT INTO runs (new_articles, alerts_sent, mode) VALUES (?, ?, ?)",
            (new_count, alerts_sent, "digest" if force_digest else "daily"),
        )
        conn.commit()

    conn.close()
    log.info(f"=== Run klaar: {new_count} nieuw, {alerts_sent} alerts ===")


# ââ CLI âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def main():
    parser = argparse.ArgumentParser(description="Lasershow Monitor")
    parser.add_argument("--digest", action="store_true", help="Forceer wekelijks digest nu")
    parser.add_argument("--test", action="store_true", help="Test-modus: geen opslag, geen e-mail")
    parser.add_argument("--init", action="store_true", help="Initialiseer database en stop")
    args = parser.parse_args()

    if args.init:
        conn = sqlite3.connect(DB_FILE)
        init_db(conn)
        conn.close()
        return

    cfg = load_config()
    run(cfg, force_digest=args.digest, test_mode=args.test)


if __name__ == "__main__":
    main()
