#!/usr/bin/env python3
"""
Scrapuje strony Confluence bez API: otwiera link, zapisuje HTML treści,
przechodzi do kolejnych stron (drzewo / linki potomne) i dokleja do jednego pliku.

Użycie:
  python scrape_confluence.py "https://twoja-firma.atlassian.net/wiki/spaces/XXX/pages/123/Tytul"
  python scrape_confluence.py URL --max-pages 50 --output wynik.html
  python scrape_confluence.py URL --headed   # logowanie ręczne w przeglądarce
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# Selektory treści — Confluence Cloud / Server / DC
CONTENT_SELECTORS = [
    "#main-content",
    "[data-testid='page-content']",
    ".wiki-content",
    "#content .page-content",
    "article#content",
    "[id='main-content']",
]

TITLE_SELECTORS = [
    "[data-testid='title-text']",
    "h1#title-text",
    "#title-text",
    "h1[data-test-id='title']",
    "h1",
]

# Linki do innych stron w przestrzeni (sidebar, drzewo, treść)
LINK_SELECTORS = [
    "a[href*='/wiki/spaces/'][href*='/pages/']",
    "a[href*='/pages/']",
    "[data-testid='page-tree'] a[href*='/pages/']",
    "[data-testid='space-navigation'] a[href*='/pages/']",
    ".plugin_pagetree a[href*='/pages/']",
    "#sidebar a[href*='/pages/']",
    "nav a[href*='/wiki/spaces/'][href*='/pages/']",
    "#main-content a[href*='/pages/']",
    "[data-testid='page-content'] a[href*='/pages/']",
]

# Tylko linki z tresci strony (tryb --only-under)
CONTENT_LINK_SELECTORS = [
    "#main-content a[href*='/pages/']",
    "[data-testid='page-content'] a[href*='/pages/']",
    ".wiki-content a[href*='/pages/']",
    "article a[href*='/pages/']",
]


def normalize_page_url(url: str) -> str:
    """Ujednolica URL strony (bez query/fragment, bez końcowego /)."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # /wiki/spaces/X/pages/123/Nazwa -> zostaw; /pages/viewpage.action?pageId=123 osobno
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def page_id_from_url(url: str) -> str | None:
    m = re.search(r"/pages/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]pageId=(\d+)", url)
    return m.group(1) if m else None


def same_space(url: str, start_url: str) -> bool:
    """Ogranicza skan do tej samej przestrzeni / hosta."""
    a, b = urlparse(url), urlparse(start_url)
    if a.netloc != b.netloc:
        return False
    ma = re.search(r"/spaces/([^/]+)", a.path)
    mb = re.search(r"/spaces/([^/]+)", b.path)
    if ma and mb:
        return ma.group(1) == mb.group(1)
    return True


def wait_for_login(page, timeout_sec: int = 300) -> None:
    """Czeka az po zalogowaniu pojawi sie tresc strony."""
    print(
        f"Zaloguj sie w oknie przegladarki (max {timeout_sec}s)...",
        flush=True,
    )
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for sel in CONTENT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el and el.inner_text(timeout=500).strip():
                    print("Wykryto tresc strony - kontynuuje.", flush=True)
                    return
            except Exception:
                pass
        # ekran logowania Atlassian / SSO
        if page.query_selector("input#username, input[name='username'], #login-form"):
            time.sleep(2)
            continue
        time.sleep(1.5)
    raise TimeoutError("Timeout logowania - nie znaleziono tresci strony.")


def extract_title(page) -> str:
    for sel in TITLE_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if text:
                    return text
        except Exception:
            continue
    return page.title() or "Bez tytułu"


def extract_content_html(page) -> str:
    for sel in CONTENT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                html = el.inner_html()
                if html and html.strip():
                    return html
        except Exception:
            continue
    # fallback: body
    return page.inner_html("body")


def page_key(url: str) -> str:
    return page_id_from_url(url) or normalize_page_url(url)


def urls_match(a: str, b: str) -> bool:
    """Porownuje strony po pageId albo znormalizowanym URL."""
    ka, kb = page_id_from_url(a), page_id_from_url(b)
    if ka and kb:
        return ka == kb
    return normalize_page_url(a) == normalize_page_url(b)


def collect_page_links(
    page,
    base_url: str,
    start_url: str,
    *,
    only_under: bool = False,
) -> list[str]:
    """Zbiera unikalne URL-e stron Confluence z aktualnego DOM."""
    found: list[str] = []
    seen: set[str] = set()
    selectors = CONTENT_LINK_SELECTORS if only_under else LINK_SELECTORS

    for sel in selectors:
        try:
            for a in page.query_selector_all(sel):
                href = a.get_attribute("href")
                if not href:
                    continue
                full = normalize_page_url(urljoin(base_url, href))
                if not page_id_from_url(full):
                    continue
                if not same_space(full, start_url):
                    continue
                key = page_key(full)
                if key in seen:
                    continue
                seen.add(key)
                found.append(full)
        except Exception:
            continue

    return found


def load_urls_file(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # dopuszcza "12. https://..." z --list-only
        m = re.search(r"https?://\S+", line)
        urls.append(normalize_page_url(m.group(0) if m else line))
    return urls


def clean_fragment(html: str) -> str:
    """Usuwa skrypty i zbędne elementy z fragmentu treści."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for sel in (
        "[data-testid='comments-section']",
        "[data-test-id='comments-section-fabric-new']",
        ".comment-container",
    ):
        for el in soup.select(sel):
            el.decompose()
    return str(soup)


def build_document(sections: list[tuple[str, str, str]], start_url: str) -> str:
    """Składa jeden HTML z sekcji (tytuł, url, treść)."""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="pl">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>Confluence export — {len(sections)} stron</title>",
        "<style>",
        "body{font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:960px;",
        "margin:2rem auto;padding:0 1rem;line-height:1.5;color:#172b4d;}",
        "h1.export-title{border-bottom:2px solid #0052cc;padding-bottom:.4rem;}",
        ".page-meta{color:#6b778c;font-size:.9rem;margin-bottom:1rem;}",
        ".page-section{margin:3rem 0;padding-top:1rem;border-top:1px solid #dfe1e6;}",
        ".toc{background:#f4f5f7;padding:1rem 1.25rem;margin:1.5rem 0;}",
        ".toc a{color:#0052cc;}",
        "img{max-width:100%;height:auto;}",
        "table{border-collapse:collapse;width:100%;}",
        "td,th{border:1px solid #dfe1e6;padding:.4rem .6rem;}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1 class='export-title'>Eksport Confluence ({len(sections)} stron)</h1>",
        f"<p class='page-meta'>Start: <a href='{start_url}'>{start_url}</a><br>",
        f"Wygenerowano: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>",
        "<div class='toc'><strong>Spis treści</strong><ol>",
    ]
    for i, (title, url, _) in enumerate(sections, 1):
        parts.append(f"<li><a href='#page-{i}'>{title}</a></li>")
    parts.append("</ol></div>")

    for i, (title, url, content) in enumerate(sections, 1):
        parts.append(f"<section class='page-section' id='page-{i}'>")
        parts.append(f"<h1>{title}</h1>")
        parts.append(f"<p class='page-meta'><a href='{url}'>{url}</a></p>")
        parts.append(f"<div class='page-body'>{content}</div>")
        parts.append("</section>")

    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)


def scrape(
    start_url: str,
    output: Path,
    max_pages: int,
    headed: bool,
    login_wait: int,
    delay: float,
    expand_tree: bool,
    from_url: str | None = None,
    to_url: str | None = None,
    from_index: int | None = None,
    to_index: int | None = None,
    only_under: bool = False,
    list_only: bool = False,
    fixed_urls: list[str] | None = None,
    on_progress=None,
) -> list[tuple[int, str, str]]:
    """
    Scrapuje strony. Zwraca liste (index, title, url).
    on_progress: opcjonalny callback(str) do GUI / logow.
    """

    def log(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    sections: list[tuple[str, str, str]] = []
    visited: set[str] = set()
    listed: list[tuple[int, str, str]] = []  # index, title, url

    # 0 = bez limitu
    limit = sys.maxsize if max_pages == 0 else max_pages

    if fixed_urls:
        queue: list[str] = list(fixed_urls)
        crawl_links = False
    else:
        seed = normalize_page_url(from_url or start_url)
        queue = [seed]
        crawl_links = True

    # Czy juz jestesmy w zakresie do zapisu?
    saving = from_index is None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(60_000)

        open_url = normalize_page_url(from_url or start_url)
        if fixed_urls:
            open_url = fixed_urls[0]

        log(f"Otwieram: {open_url}")
        page.goto(
            open_url if not fixed_urls else start_url,
            wait_until="domcontentloaded",
        )

        needs_login = bool(
            page.query_selector(
                "input#username, input[name='username'], #login-form, "
                "[data-testid='login-page']"
            )
        )
        if needs_login or headed:
            if not headed:
                log(
                    "Wykryto ekran logowania. Uruchom ponownie z --headed "
                    "albo zaloguj sie w juz otwartym oknie."
                )
            wait_for_login(page, timeout_sec=login_wait)
            page.goto(start_url, wait_until="networkidle")
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except PlaywrightTimeout:
                pass

        _dismiss_cookies(page)

        if expand_tree and not fixed_urls:
            _expand_sidebar_tree(page, on_progress=on_progress)

        try:
            page.wait_for_selector("a[href*='/pages/']", timeout=15_000)
        except PlaywrightTimeout:
            log("Uwaga: nie znaleziono linkow /pages/ na starcie.")

        if from_url:
            log(f"Zakres od: {from_url}")
        if to_url:
            log(f"Zakres do: {to_url}")
        if from_index or to_index:
            log(f"Indeksy: od {from_index or 1} do {to_index or 'konca'}")
        if max_pages == 0:
            log("Limit stron: bez limitu (calosc)")
        if only_under:
            log("Tryb --only-under: tylko linki z tresci strony")

        order_index = 0
        space_ref = start_url

        while queue and len(sections) < limit:
            url = queue.pop(0)
            pid = page_key(url)
            if pid in visited:
                continue
            visited.add(pid)
            order_index += 1

            limit_label = "all" if max_pages == 0 else str(max_pages)
            log(f"[{order_index}/{limit_label}] {url}")
            try:
                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(int(delay * 1000))

                title = extract_title(page)

                if from_index is not None and order_index >= from_index:
                    saving = True
                if from_url and urls_match(url, from_url):
                    saving = True

                in_range = saving
                if from_index is not None and order_index < from_index:
                    in_range = False
                if to_index is not None and order_index > to_index:
                    break

                listed.append((order_index, title, page.url))

                if list_only:
                    log(f"  {order_index}. {title}")
                elif in_range:
                    raw = extract_content_html(page)
                    content = clean_fragment(raw)
                    sections.append((title, page.url, content))
                    log(f"  OK: {title}")
                else:
                    log(f"  skip (poza zakresem): {title}")

                if crawl_links:
                    use_only = only_under
                    new_links = 0
                    for link in collect_page_links(
                        page, page.url, space_ref, only_under=use_only
                    ):
                        lid = page_key(link)
                        if lid not in visited and link not in queue:
                            queue.append(link)
                            new_links += 1
                    if not list_only or new_links:
                        log(f"  (+{new_links} linkow, kolejka={len(queue)})")

                if to_url and urls_match(url, to_url):
                    log("Osiagnieto --to-url, stop.")
                    break
                if to_index is not None and order_index >= to_index:
                    log("Osiagnieto --to-index, stop.")
                    break

            except Exception as exc:
                log(f"  ! blad: {exc}")
                continue

        browser.close()

    if list_only:
        out_list = output.with_suffix(".txt") if output.suffix == ".html" else output
        lines = [f"{i}. {title}\n   {url}" for i, title, url in listed]
        out_list.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"\nLista {len(listed)} stron -> {out_list.resolve()}")
        return listed

    if not sections:
        raise RuntimeError("Nie zebrano zadnej strony.")

    doc = build_document(sections, start_url)
    output.write_text(doc, encoding="utf-8")
    log(f"\nZapisano {len(sections)} stron -> {output.resolve()}")
    return listed


def discover_pages(
    start_url: str,
    *,
    headed: bool = False,
    login_wait: int = 300,
    expand_tree: bool = True,
    deep: bool = False,
    max_pages: int = 0,
    delay: float = 0.5,
    only_under: bool = False,
    on_progress=None,
) -> list[tuple[str, str]]:
    """
    Zwraca liste (title, url).
    deep=False: szybkie zebranie z sidebara (jedna wizyta).
    deep=True: BFS po stronach (wolniejsze, pelniejsze).
    """

    def log(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    if deep:
        tmp = Path("_confluence_list_tmp.txt")
        listed = scrape(
            start_url=start_url,
            output=tmp,
            max_pages=max_pages,
            headed=headed,
            login_wait=login_wait,
            delay=delay,
            expand_tree=expand_tree,
            only_under=only_under,
            list_only=True,
            on_progress=on_progress,
        )
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return [(title, url) for _, title, url in listed]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(60_000)

        log(f"Otwieram (lista): {start_url}")
        page.goto(start_url, wait_until="domcontentloaded")

        needs_login = bool(
            page.query_selector(
                "input#username, input[name='username'], #login-form, "
                "[data-testid='login-page']"
            )
        )
        if needs_login or headed:
            wait_for_login(page, timeout_sec=login_wait)
            page.goto(start_url, wait_until="networkidle")
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except PlaywrightTimeout:
                pass

        _dismiss_cookies(page)
        if expand_tree:
            _expand_sidebar_tree(page, max_clicks=80, on_progress=on_progress)

        try:
            page.wait_for_selector("a[href*='/pages/']", timeout=15_000)
        except PlaywrightTimeout:
            pass

        results: list[tuple[str, str]] = []
        seen: set[str] = set()

        # Zacznij od biezacej strony (overview tez)
        try:
            cur_title = extract_title(page)
            cur_url = normalize_page_url(page.url)
            results.append((cur_title, cur_url))
            seen.add(page_key(cur_url))
        except Exception:
            pass

        for a in page.query_selector_all("a[href*='/pages/']"):
            try:
                href = a.get_attribute("href")
                if not href:
                    continue
                full = normalize_page_url(urljoin(page.url, href))
                if not page_id_from_url(full):
                    continue
                if not same_space(full, start_url):
                    continue
                key = page_key(full)
                if key in seen:
                    continue
                title = (a.inner_text() or "").strip() or key
                title = re.sub(r"\s+", " ", title)
                seen.add(key)
                results.append((title, full))
            except Exception:
                continue

        browser.close()

    log(f"Znaleziono {len(results)} stron (szybka lista).")
    return results


def _dismiss_cookies(page) -> None:
    for cookie_sel in (
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Close')",
    ):
        try:
            btn = page.query_selector(cookie_sel)
            if btn and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass


def _expand_sidebar_tree(page, max_clicks: int = 40, on_progress=None) -> None:
    """Rozwija wezly drzewa stron w sidebarze, zeby zebrac wiecej linkow."""
    selectors = [
        "[data-testid='page-tree'] button[aria-expanded='false']",
        ".plugin_pagetree button[aria-expanded='false']",
        "[data-testid='page-tree'] [aria-label*='Expand']",
    ]
    clicks = 0
    for _ in range(max_clicks):
        clicked = False
        for sel in selectors:
            btns = page.query_selector_all(sel)
            if not btns:
                continue
            try:
                btns[0].click(timeout=2000)
                page.wait_for_timeout(400)
                clicks += 1
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            break
    if clicks:
        msg = f"Rozwinieto {clicks} wezlow drzewa stron."
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrapuj Confluence (bez API) i zloz jeden HTML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Przyklady:
  # Cala przestrzen (bez limitu)
  python scrape_confluence.py URL --max-pages 0 -o all.html

  # Najpierw lista stron z numerami
  python scrape_confluence.py URL --list-only -o lista.txt

  # Zakres po numerach z listy (np. od 5 do 20)
  python scrape_confluence.py URL --from-index 5 --to-index 20 -o czesc.html

  # Od konkretnej strony do konkretnej
  python scrape_confluence.py URL --from-url "https://.../pages/111/A" --to-url "https://.../pages/222/B"

  # Tylko podgalaz (start = korzen, linki z tresci)
  python scrape_confluence.py "https://.../pages/123/Podrecznik" --only-under --max-pages 0

  # Dokladna lista URL-i
  python scrape_confluence.py URL --urls-file lista.txt -o wybor.html
""",
    )
    parser.add_argument("url", help="URL startowej strony / przestrzeni Confluence")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("confluence_export.html"),
        help="Plik wynikowy (domyslnie confluence_export.html)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="Max stron; 0 = bez limitu / calosc (domyslnie 100)",
    )
    parser.add_argument(
        "--from-url",
        help="Zacznij crawlowanie od tej strony (skad)",
    )
    parser.add_argument(
        "--to-url",
        help="Zatrzymaj po tej stronie (dokad)",
    )
    parser.add_argument(
        "--from-index",
        type=int,
        help="Zapisuj od N-tej strony w kolejnosci odkrywania (1-based)",
    )
    parser.add_argument(
        "--to-index",
        type=int,
        help="Zatrzymaj po N-tej stronie w kolejnosci odkrywania (1-based)",
    )
    parser.add_argument(
        "--only-under",
        action="store_true",
        help="Tylko podgalaz: linki z tresci strony, nie z calego sidebara",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Tylko wypisz / zapisz liste stron (bez HTML tresci)",
    )
    parser.add_argument(
        "--urls-file",
        type=Path,
        help="Pobierz dokladnie URL-e z pliku (jeden na linie)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Pokaz przegladarke (logowanie SSO / Atlassian)",
    )
    parser.add_argument(
        "--login-wait",
        type=int,
        default=300,
        help="Czas na reczne logowanie w sekundach (domyslnie 300)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Opoznienie po zaladowaniu strony w sekundach (domyslnie 1)",
    )
    parser.add_argument(
        "--expand-tree",
        action="store_true",
        default=True,
        help="Rozwijaj drzewo stron w sidebarze (domyslnie wlaczne)",
    )
    parser.add_argument(
        "--no-expand-tree",
        action="store_false",
        dest="expand_tree",
        help="Nie rozwijaj drzewa stron",
    )
    args = parser.parse_args()

    fixed = load_urls_file(args.urls_file) if args.urls_file else None
    if fixed is not None and not fixed:
        print("Plik --urls-file jest pusty.", file=sys.stderr)
        sys.exit(1)

    try:
        scrape(
            start_url=args.url,
            output=args.output,
            max_pages=args.max_pages,
            headed=args.headed,
            login_wait=args.login_wait,
            delay=args.delay,
            expand_tree=args.expand_tree,
            from_url=args.from_url,
            to_url=args.to_url,
            from_index=args.from_index,
            to_index=args.to_index,
            only_under=args.only_under,
            list_only=args.list_only,
            fixed_urls=fixed,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
