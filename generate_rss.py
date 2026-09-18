import re
import time
import html
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup
from email.utils import format_datetime


BASE_URL = "https://emploi.chru-lille.fr"
OFFERS_URL = f"{BASE_URL}/offre"
RSS_FILE = "rss.xml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Nombre maximum de pages que le script essaiera de parcourir.
# On met une marge importante pour les futures offres.
MAX_PAGES = 50

# Petite pause entre les requêtes pour ne pas solliciter inutilement le site.
REQUEST_DELAY = 1.0


def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def clean_text(text):
    """Nettoie les espaces et caractères inutiles."""
    if not text:
        return ""

    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_page(session, url):
    """Télécharge une page."""
    response = session.get(url, timeout=30)
    response.raise_for_status()

    # Le site utilise du HTML classique.
    response.encoding = response.apparent_encoding or response.encoding

    return response.text


def is_offer_url(url):
    """Vérifie qu'une URL correspond à une offre individuelle."""
    parsed = urlparse(url)

    if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
        return False

    path = parsed.path.rstrip("/")

    # Une offre individuelle commence par /offre/
    if not path.startswith("/offre/"):
        return False

    # Évite de considérer /offre/ lui-même comme une annonce.
    if path == "/offre":
        return False

    return True


def extract_offer_links(soup):
    """Extrait les liens vers les offres présentes sur une page."""
    offers = {}

    for link in soup.find_all("a", href=True):
        href = link.get("href", "").strip()

        if not href:
            continue

        absolute_url = urljoin(BASE_URL, href)

        if not is_offer_url(absolute_url):
            continue

        # Nettoyage des éventuels paramètres inutiles.
        parsed = urlparse(absolute_url)
        clean_url = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",
                "",
                "",
            )
        )

        title = clean_text(link.get_text(" ", strip=True))

        # Certains liens "Voir l'offre" n'ont pas le titre.
        if not title or title.lower() in {
            "voir l'offre",
            "voir l’offre",
        }:
            parent = link.parent
            if parent:
                title = clean_text(parent.get_text(" ", strip=True))

        if clean_url not in offers:
            offers[clean_url] = title

    return offers


def extract_pagination_links(soup):
    """
    Récupère les liens de pagination.
    Le site utilise actuellement le paramètre 'pagesoffre'.
    """
    pagination = set()

    for link in soup.find_all("a", href=True):
        href = link.get("href", "").strip()

        if not href:
            continue

        absolute_url = urljoin(BASE_URL, href)
        parsed = urlparse(absolute_url)

        # On ne garde que les liens vers /offre.
        if parsed.path.rstrip("/") != "/offre":
            continue

        query = parse_qs(parsed.query)

        if "pagesoffre" in query:
            pagination.add(absolute_url)

    return pagination


def extract_total_offers(soup):
    """Essaie de récupérer le nombre total d'offres affiché par le site."""
    text = clean_text(soup.get_text(" ", strip=True))

    match = re.search(r"(\d+)\s+offres?\b", text, re.IGNORECASE)

    if match:
        return int(match.group(1))

    return None


def build_page_urls(session):
    """
    Parcourt la pagination du site.

    On commence par la page principale.
    Ensuite, on découvre les URLs de pagination présentes dans le HTML.
    """
    pages_to_visit = [OFFERS_URL]
    visited = set()

    while pages_to_visit and len(visited) < MAX_PAGES:
        url = pages_to_visit.pop(0)

        # Normalisation.
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        if "pagesoffre" in query:
            try:
                page_number = int(query["pagesoffre"][0])
            except (ValueError, TypeError):
                page_number = 0

            if page_number > MAX_PAGES:
                continue

        if url in visited:
            continue

        visited.add(url)

        try:
            html_content = get_page(session, url)
        except Exception as exc:
            print(f"Erreur lors du téléchargement de {url}: {exc}")
            continue

        soup = BeautifulSoup(html_content, "html.parser")

        for pagination_url in extract_pagination_links(soup):
            if pagination_url not in visited:
                pages_to_visit.append(pagination_url)

        time.sleep(REQUEST_DELAY)

    return sorted(visited)


def extract_offer_details(session, url, fallback_title=""):
    """Récupère les informations d'une offre individuelle."""
    try:
        html_content = get_page(session, url)
    except Exception as exc:
        print(f"Impossible de lire {url}: {exc}")

        return {
            "title": fallback_title or "Offre CHU de Lille",
            "url": url,
            "description": "",
            "published": datetime.now(timezone.utc),
        }

    soup = BeautifulSoup(html_content, "html.parser")

    # Titre
    title = ""

    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))

    if not title:
        title_tag = soup.find("title")
        if title_tag:
            title = clean_text(title_tag.get_text(" ", strip=True))

            # Retire éventuellement le suffixe du site.
            title = re.sub(
                r"\s*\|\s*Portail recrutement CHU de Lille.*$",
                "",
                title,
                flags=re.IGNORECASE,
            )

    if not title:
        title = fallback_title or "Offre CHU de Lille"

    # Recherche des informations générales de l'offre.
    body_text = clean_text(soup.get_text(" ", strip=True))

    # On garde volontairement une description courte.
    description = ""

    description_heading = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and "description" in clean_text(tag.get_text()).lower()
    )

    if description_heading:
        parts = []

        for element in description_heading.find_all_next():
            if element.name in ["h2", "h3"]:
                break

            text = clean_text(element.get_text(" ", strip=True))

            if text:
                parts.append(text)

            if len(" ".join(parts)) > 1500:
                break

        description = " ".join(parts)[:2000]

    if not description:
        description = body_text[:1500]

    # On essaie de trouver la date "A pourvoir le".
    published = datetime.now(timezone.utc)

    date_match = re.search(
        r"A pourvoir le\s+(\d{1,2}/\d{1,2}/\d{4})",
        body_text,
        re.IGNORECASE,
    )

    if date_match:
        try:
            date_value = datetime.strptime(
                date_match.group(1),
                "%d/%m/%Y",
            )
            published = date_value.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return {
        "title": title,
        "url": url,
        "description": description,
        "published": published,
    }


def xml_escape(value):
    """Échappe correctement le contenu XML."""
    return html.escape(str(value), quote=True)


def make_rss(offers):
    """Construit le fichier RSS 2.0."""
    now = datetime.now(timezone.utc)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        "    <title>CHU de Lille - Toutes les offres</title>",
        (
            "    <link>"
            + xml_escape(OFFERS_URL)
            + "</link>"
        ),
        (
            "    <description>"
            "Toutes les offres d'emploi du CHU de Lille"
            "</description>"
        ),
        (
            "    <language>fr-fr</language>"
        ),
        (
            "    <lastBuildDate>"
            + format_datetime(now)
            + "</lastBuildDate>"
        ),
    ]

    for offer in offers:
        title = xml_escape(offer["title"])
        url = xml_escape(offer["url"])
        description = xml_escape(offer["description"])

        published = offer["published"]

        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        lines.extend(
            [
                "    <item>",
                f"      <title>{title}</title>",
                f"      <link>{url}</link>",
                f"      <guid isPermaLink=\"true\">{url}</guid>",
                f"      <description>{description}</description>",
                (
                    "      <pubDate>"
                    + format_datetime(published)
                    + "</pubDate>"
                ),
                "    </item>",
            ]
        )

    lines.extend(
        [
            "  </channel>",
            "</rss>",
            "",
        ]
    )

    return "\n".join(lines)


def main():
    print("========================================")
    print(" CHU Lille - Génération du flux RSS")
    print("========================================")

    session = get_session()

    print("\nRecherche des pages d'offres...")

    page_urls = build_page_urls(session)

    print(f"Pages trouvées : {len(page_urls)}")

    all_offers = {}

    # Récupère les offres de chaque page.
    for page_number, page_url in enumerate(page_urls, start=1):
        print(
            f"\nLecture page {page_number}/{len(page_urls)} : "
            f"{page_url}"
        )

        try:
            html_content = get_page(session, page_url)
        except Exception as exc:
            print(f"Erreur : {exc}")
            continue

        soup = BeautifulSoup(html_content, "html.parser")

        total = extract_total_offers(soup)

        if total is not None:
            print(f"Nombre total annoncé par le site : {total}")

        offers = extract_offer_links(soup)

        print(f"Offres trouvées sur cette page : {len(offers)}")

        for url, title in offers.items():
            all_offers[url] = title

        time.sleep(REQUEST_DELAY)

    print("\n----------------------------------------")
    print(f"Offres uniques trouvées : {len(all_offers)}")
    print("----------------------------------------")

    if not all_offers:
        raise RuntimeError(
            "Aucune offre trouvée. "
            "Le site a peut-être changé sa structure."
        )

    # Récupération des détails de chaque annonce.
    detailed_offers = []

    for index, (url, title) in enumerate(
        all_offers.items(),
        start=1,
    ):
        print(
            f"[{index}/{len(all_offers)}] "
            f"Récupération : {title or url}"
        )

        offer = extract_offer_details(
            session,
            url,
            title,
        )

        detailed_offers.append(offer)

        time.sleep(REQUEST_DELAY)

    # Les plus récentes en premier.
    detailed_offers.sort(
        key=lambda item: item["published"],
        reverse=True,
    )

    rss = make_rss(detailed_offers)

    with open(
        RSS_FILE,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(rss)

    print("\n========================================")
    print(f"Flux RSS créé : {RSS_FILE}")
    print(f"Nombre d'offres : {len(detailed_offers)}")
    print("========================================")


if __name__ == "__main__":
    main()
