# Scraper Confluence (bez API)

Zbiera strony Confluence z podanego linku, zapisuje tresc HTML i dokleja kolejne strony do jednego pliku. Dziala przez Playwright (bez REST API).

## Instalacja

```powershell
cd c:\Projects\confluence
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## GUI

```powershell
.\.venv\Scripts\Activate.ps1
python gui.py
```

1. Wklej URL (domyslnie Omega-PSIR)
2. **Pobierz liste** — szybka z sidebara (albo zaznacz „Gleboka lista”)
3. Ustaw **Od / Do** i kliknij **Zaznacz zakres** (albo Ctrl+klik w liscie)
4. **Pobierz zaznaczone do HTML**

## CLI

```powershell
$env:PYTHONUTF8=1

# Cala przestrzen (bez limitu)
python scrape_confluence.py "URL" --max-pages 0 -o all.html

# Najpierw lista stron z numerami (zeby wybrac zakres)
python scrape_confluence.py "URL" --list-only -o lista.txt

# Zakres po numerach
python scrape_confluence.py "URL" --from-index 5 --to-index 20 -o czesc.html

# Od URL do URL
python scrape_confluence.py "URL" --from-url "https://.../pages/111/A" --to-url "https://.../pages/222/B"

# Tylko podgalaz (korzen = startowy URL, linki z tresci)
python scrape_confluence.py "https://.../pages/123/Podrecznik" --only-under --max-pages 0

# Dokladna lista z pliku
python scrape_confluence.py "URL" --urls-file lista.txt -o wybor.html

# Logowanie SSO
python scrape_confluence.py "URL" --headed
```

## Opcje zakresu

| Flaga | Opis |
|--------|------|
| `--max-pages 0` | Calosc (az wyczerpie kolejke) |
| `--list-only` | Tylko numerowana lista stron |
| `--from-index N` / `--to-index M` | Zakres po numerach z kolejnosci odkrywania |
| `--from-url` / `--to-url` | Zakres od/do konkretnej strony |
| `--only-under` | Tylko podgalaz startowego URL |
| `--urls-file` | Scisle te URL-e z pliku |

Przy `--from-index` skrypt nadal odwiedza wczesniejsze strony (zeby odkryc linki), ale zapisuje tresc dopiero od wskazanego numeru.
