#!/usr/bin/env python3
"""
France Territoriale – Collecteur d'actualités quotidien
--------------------------------------------------------
Variables d'environnement requises (GitHub Secrets) :
  WIKI_USERNAME      – identifiant admin du wiki
  WIKI_PASSWORD      – mot de passe
  ANTHROPIC_API_KEY  – clé API Anthropic
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests
import yaml

# ── Configuration ─────────────────────────────────────────────────────────────

WIKI_BASE               = "https://france-territoriale.yeswiki.pro"
PAGE_TAG                = "ActualitEs"
PARIS_TZ                = ZoneInfo("Europe/Paris")
MAX_ARTICLES_PER_SOURCE = 5
MAX_AGE_HOURS           = 48
MAX_KW_IN_PROMPT        = 20

THEMATIQUES_VALIDES = [
    "Transitions",
    "Numérique",
    "Finances",
    "Urbanisme / Aménagement",
    "Action Sociale",
    "Animer / Décider / Coopérer",
    "Autre",
]

KEYWORD_FIELDS = [
    "bf_mots_cle_transitions",
    "bf_mots_cle_numerique",
    "bf_mots_cle_finances",
    "bf_mots_cle_urbanisme",
    "bf_mots_cle_action_sociale",
    "bf_mots_cle_animer",
]

# ── Données wiki ──────────────────────────────────────────────────────────────

def camel_to_label(tag: str) -> str:
    tag = re.sub(r'([a-z])([A-Z])$', lambda m: m.group(1) + m.group(2).lower(), tag)
    return re.sub(r'(?<=[a-z])([A-Z])', r' \1', tag).strip()


def fetch_keyword_labels(session: requests.Session) -> dict:
    try:
        resp = session.get(
            f"{WIKI_BASE}/?api/forms/9/entries&fields=id_fiche,bf_titre",
            timeout=15,
        )
        return {
            e["id_fiche"]: e["bf_titre"]
            for e in resp.json()
            if e.get("id_fiche") and e.get("bf_titre")
        }
    except Exception as e:
        print(f"  ⚠ Impossible de charger les libellés de mots-clés : {e}", file=sys.stderr)
        return {}


def fetch_resource_keywords(session: requests.Session, wiki_id: str, kw_labels: dict) -> list:
    try:
        resp = session.get(
            f"{WIKI_BASE}/?api/forms/8/entries&id_fiche={wiki_id}",
            timeout=10,
        )
        entries = resp.json()
        if not entries:
            return []
        entry  = entries[0]
        kw_ids = []
        for field in KEYWORD_FIELDS:
            val = entry.get(field, "")
            if val:
                kw_ids.extend(val.split(","))
        return [kw_labels.get(k.strip(), camel_to_label(k.strip())) for k in kw_ids if k.strip()]
    except Exception as e:
        print(f"  ⚠ Erreur mots-clés pour {wiki_id} : {e}", file=sys.stderr)
        return []

  # ── Collecte RSS ──────────────────────────────────────────────────────────────

def fetch_rss(url: str) -> list:
    feed   = feedparser.parse(url, request_headers={"User-Agent": "FranceTerritoriale-Bot/1.0"})
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    articles = []
    for entry in feed.entries:
        published = None
        for attr in ("published_parsed", "updated_parsed"):
            val = getattr(entry, attr, None)
            if val:
                published = datetime(*val[:6], tzinfo=timezone.utc)
                break
        if published and published < cutoff:
            continue
        raw   = entry.get("summary", entry.get("description", ""))
        clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
        articles.append({
            "title":     entry.get("title", "").strip(),
            "link":      entry.get("link", ""),
            "summary":   clean[:1500],
            "published": published,
        })
    return articles[:MAX_ARTICLES_PER_SOURCE]


# ── Enrichissement Claude Haiku ───────────────────────────────────────────────

def summarize_article(client, article: dict, thematiques_source: list, wiki_keywords: list) -> dict:
    kw_str = ", ".join(wiki_keywords[:MAX_KW_IN_PROMPT]) or "aucun disponible"
    prompt = f"""Tu aides des agents territoriaux français à se tenir informés.

Article :
Titre : {article['title']}
Contenu : {article['summary'][:800]}

Mots-clés de cette source dans la gare centrale : {kw_str}

Réponds uniquement en JSON valide (sans bloc markdown) :
1. "resume" : 2-3 phrases factuelles utiles pour un agent territorial.
2. "thematique" : la plus pertinente parmi : {' | '.join(thematiques_source)}
3. "mots_cles_wiki" : 2-4 mots-clés CHOISIS PARMI la liste ci-dessus (liste JSON de strings exactes).

{{"resume": "...", "thematique": "...", "mots_cles_wiki": ["...", "..."]}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text  = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        data = json.loads(match.group())
        if data.get("thematique") not in THEMATIQUES_VALIDES:
            data["thematique"] = thematiques_source[0]
        valid = set(wiki_keywords)
        data["mots_cles_wiki"] = [k for k in data.get("mots_cles_wiki", []) if k in valid]
        return data
    return {"resume": article["summary"][:250], "thematique": thematiques_source[0], "mots_cles_wiki": []}


# ── Construction page HTML ────────────────────────────────────────────────────

def build_page(articles: list, date_str: str) -> str:
    thematiques = sorted({a["thematique"] for a in articles if a.get("thematique")})
    all_kws     = sorted({kw for a in articles for kw in a.get("mots_cles_wiki", [])})
    nb_sources  = len({a["source"] for a in articles})

    theme_btns = '<button class="ft-btn ft-theme-btn active" data-theme="all">Toutes thématiques</button>\n'
    for t in thematiques:
        theme_btns += f'    <button class="ft-btn ft-theme-btn" data-theme="{t}">{t}</button>\n'

    kw_btns   = ""
    kw_section = ""
    if all_kws:
        kw_btns = '<button class="ft-btn ft-kw-btn active" data-kw="all">Tous mots-clés</button>\n'
        for kw in all_kws:
            kw_btns += f'    <button class="ft-btn ft-kw-btn ft-kw-pill" data-kw="{kw}">{kw}</button>\n'
        kw_section = f"""
  <div class="ft-section-label">Filtrer par mot-clé</div>
  <div class="ft-filters ft-kw-filters">
    {kw_btns}  </div>"""

    cards_html = ""
    for a in articles:
        theme    = a.get("thematique", "Autre")
        kws_list = a.get("mots_cles_wiki", [])
        kws_data = json.dumps(kws_list)
        kws_html = "".join(f'<span class="ft-kw">{kw}</span>' for kw in kws_list)
        pub      = a["published"].astimezone(PARIS_TZ).strftime("%d/%m/%Y") if a.get("published") else ""
        cards_html += f"""  <div class="ft-card" data-theme="{theme}" data-kws='{kws_data}'>
    <span class="ft-tag">{theme}</span>
    <div class="ft-title"><a href="{a['link']}" target="_blank" rel="noopener">{a['title']}</a></div>
    <div class="ft-meta">{a['source']}{' — ' + pub if pub else ''}</div>
    <div class="ft-resume">{a['resume']}</div>
    <div class="ft-kws">{kws_html}</div>
  </div>
"""

    return f"""\"\"
<style>
    .ft-wrap{{font-family:sans-serif;max-width:900px}}
    .ft-header{{background:#f0f4f8;border-left:4px solid #2d6a9f;padding:14px 18px;border-radius:4px;margin-bottom:18px;font-size:14px}}   
    .ft-header strong{{font-size:16px;display:block;margin-bottom:4px}}
    .ft-section-label{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#888;margin:14px 0 6px}}
    .ft-filters{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}}
    .ft-btn{{padding:5px 14px;border:1.5px solid #2d6a9f;border-radius:20px;background:white;color:#2d6a9f;cursor:pointer;font-size:13px;transition:all .15s}}
    .ft-btn:hover,.ft-btn.active{{background:#2d6a9f;color:white}}
    .ft-kw-pill{{border-color:#6b7280;color:#6b7280;font-size:12px}}
    .ft-kw-pill:hover,.ft-kw-pill.active{{background:#6b7280;color:white}}
    .ft-grid{{display:grid;gap:14px;margin-top:18px}}
    .ft-card{{border:1px solid #dde3ea;border-radius:6px;padding:14px 16px;background:white}}
    .ft-card.ft-hidden{{display:none}}
    .ft-tag{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;background:#e8f0fe;color:#2d6a9f;margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em}}
    .ft-title{{font-size:15px;font-weight:bold;margin-bottom:5px;line-height:1.4}}
    .ft-title a{{color:#1a3c5e;text-decoration:none}}
    .ft-title a:hover{{text-decoration:underline}}
    .ft-meta{{font-size:12px;color:#888;margin-bottom:8px}}
    .ft-resume{{font-size:13px;color:#444;line-height:1.55}}
    .ft-kws{{margin-top:10px;display:flex;flex-wrap:wrap;gap:5px}}
    .ft-kw{{font-size:11px;background:#f3f4f6;padding:2px 8px;border-radius:10px;color:#555;border:1px solid #e5e7eb}}
    </style>

    <div class="ft-wrap">
      <div class="ft-header">
        <strong>Actualités de la gare centrale</strong>
        Mise à jour : {date_str} · {len(articles)} articles · {nb_sources} source{'s' if nb_sources > 1 else ''}
      </div>
      <div class="ft-section-label">Filtrer par thématique</div>
      <div class="ft-filters">
        {theme_btns}  </div>{kw_section}
      <div class="ft-grid">
    {cards_html}  </div>
    </div>

    <script>
    (function(){{
      var activeTheme='all', activeKw='all';
      function apply(){{
        document.querySelectorAll('.ft-card').forEach(function(c){{
          var tOk=activeTheme==='all'||c.dataset.theme===activeTheme;
          var kOk=activeKw==='all'||JSON.parse(c.dataset.kws||'[]').indexOf(activeKw)!==-1;
          c.classList.toggle('ft-hidden',!(tOk&&kOk));
        }});
      }}
      document.querySelectorAll('.ft-theme-btn').forEach(function(b){{
        b.addEventListener('click',function(){{
          document.querySelectorAll('.ft-theme-btn').forEach(function(x){{x.classList.remove('active');}});
          b.classList.add('active'); activeTheme=b.dataset.theme; apply();
    }});
  }});
  document.querySelectorAll('.ft-kw-btn').forEach(function(b){{
    b.addEventListener('click',function(){{
      document.querySelectorAll('.ft-kw-btn').forEach(function(x){{x.classList.remove('active');}});
          b.classList.add('active'); activeKw=b.dataset.kw; apply();
        }});
      }});
    }})();
    </script>
\"\"\"\"\"\"

# ── Publication wiki ──────────────────────────────────────────────────────────

def get_csrf_token(session: requests.Session):
    try:
        resp  = session.get(f"{WIKI_BASE}/?PagePrincipale", timeout=10)
        match = re.search(r'name=["\']?antispam["\']?\s+[^>]*value=["\']([^"\']+)["\']', resp.text)
        if match:
            return match.group(1)
        match = re.search(r'"csrf[_-]?token"\s*:\s*"([^"]+)"', resp.text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def publish_page(session: requests.Session, page_tag: str, content: str) -> bool:
    url     = f"{WIKI_BASE}/?api/pages/{page_tag}"
    payload = {"body": content}
    resp    = session.post(url, data=payload, timeout=15)
    if resp.status_code in (200, 201):
        return True
    csrf = get_csrf_token(session)
    if csrf:
        payload["antispam"] = csrf
        resp = session.post(url, data=payload, timeout=15)
        if resp.status_code in (200, 201):
            return True
    print(f"  ERREUR publication : HTTP {resp.status_code} – {resp.text[:200]}", file=sys.stderr)
    return False


# ── Point d'entrée ────────────────────────────────────────────────────────────

def main():
    config_path = Path(__file__).parent.parent / "config" / "sources.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    session = requests.Session()
    session.headers.update({"User-Agent": "FranceTerritoriale-Bot/1.0"})

    print("Authentification au wiki…")
    base_url = f"{WIKI_BASE}/?PagePrincipale="
    session.post(
        base_url,
        data={
            "name":        os.environ["WIKI_USERNAME"],
            "password":    os.environ["WIKI_PASSWORD"],
            "action":      "login",
            "context":     "PageRapideHaut",
            "incomingurl": base_url,
            "userpage":    base_url,
        },
        timeout=10,
        allow_redirects=True,
    )
    print("  ✓ Session établie")

    print("\nChargement des mots-clés du wiki…")
    kw_labels = fetch_keyword_labels(session)
    print(f"  ✓ {len(kw_labels)} mots-clés chargés")

    all_articles = []
    for source in config.get("sources", []):
        name        = source["name"]
        wiki_id     = source.get("wiki_id", "")
        thematiques = source.get("thematiques", ["Autre"])
        print(f"\nSource : {name}")

        wiki_keywords = []
        if wiki_id:
            wiki_keywords = fetch_resource_keywords(session, wiki_id, kw_labels)
            print(f"  Mots-clés : {', '.join(wiki_keywords[:8])}{'…' if len(wiki_keywords) > 8 else ''}")

        raw = []
        for url in source.get("rss_urls", []):
            try:
                items = fetch_rss(url)
                print(f"  {url.split('/')[2]} → {len(items)} articles")
                raw.extend(items)
            except Exception as e:
                print(f"  ⚠ {url} : {e}", file=sys.stderr)

        seen, unique = set(), []
        for a in raw:
            if a["link"] and a["link"] not in seen:
                seen.add(a["link"])
                unique.append(a)

        for article in unique[:MAX_ARTICLES_PER_SOURCE]:
            if not article["title"]:
                continue
            try:
                enriched = summarize_article(client, article, thematiques, wiki_keywords)
                enriched.update({
                    "title":     article["title"],
                    "link":      article["link"],
                    "published": article["published"],
                    "source":    name,
                })
                all_articles.append(enriched)
                kws = enriched.get("mots_cles_wiki", [])
                print(f"  ✓ {article['title'][:55]} [{', '.join(kws) or '–'}]")
            except Exception as e:
                print(f"  ⚠ {article['title'][:40]} : {e}", file=sys.stderr)

    if not all_articles:
        print("\nAucun article trouvé, page non mise à jour.")
        sys.exit(0)

    all_articles.sort(
        key=lambda a: a.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    date_str = datetime.now(PARIS_TZ).strftime("%d/%m/%Y à %Hh%M")
    content  = build_page(all_articles, date_str)

    print(f"\nPublication de {len(all_articles)} articles sur {PAGE_TAG}…")
    if publish_page(session, PAGE_TAG, content):
        print(f"  ✓ {WIKI_BASE}/?{PAGE_TAG}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
