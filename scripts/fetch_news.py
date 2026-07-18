#!/usr/bin/env python3
"""
France Territoriale - Collecteur d'actualites quotidien
Variables d'environnement requises (GitHub Secrets) :
  WIKI_USERNAME      - identifiant admin du wiki
  WIKI_PASSWORD      - mot de passe
  ANTHROPIC_API_KEY  - cle API Anthropic
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests
import yaml

WIKI_BASE               = "https://france-territoriale.yeswiki.pro"
PAGE_TAG                = "ActualitEs"
PARIS_TZ                = ZoneInfo("Europe/Paris")
MAX_ARTICLES_PER_SOURCE = 5
MAX_AGE_HOURS           = 48
MAX_KW_IN_PROMPT        = 20
MAX_RELATED_RESOURCES   = 3
MIN_SCORE               = 0.05

THEMATIQUES_VALIDES = [
    "Transitions",
    "Numerique",
    "Finances",
    "Urbanisme / Amenagement",
    "Action Sociale",
    "Animer / Decider / Cooperer",
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


def camel_to_label(tag):
    tag = re.sub(r'([a-z])([A-Z])$', lambda m: m.group(1) + m.group(2).lower(), tag)
    return re.sub(r'(?<=[a-z])([A-Z])', r' \1', tag).strip()


def fetch_keyword_labels(session):
    try:
        resp = session.get(WIKI_BASE + "/?api/forms/9/entries&fields=id_fiche,bf_titre", timeout=15)
        return {
            e["id_fiche"]: e["bf_titre"]
            for e in resp.json()
            if e.get("id_fiche") and e.get("bf_titre")
        }
    except Exception as e:
        print("  Impossible de charger les mots-cles : " + str(e), file=sys.stderr)
        return {}


def fetch_all_wiki_resources(session, kw_labels):
    try:
        resp    = session.get(WIKI_BASE + "/?api/forms/8/entries", timeout=20)
        entries = resp.json()
        resources = []
        for entry in entries:
            if not entry.get("bf_titre") or not entry.get("id_fiche"):
                continue
            kw_ids = []
            for field in KEYWORD_FIELDS:
                val = entry.get(field, "")
                if val:
                    kw_ids.extend(val.split(","))
            keywords = [kw_labels.get(k.strip(), camel_to_label(k.strip())) for k in kw_ids if k.strip()]
            resources.append({
                "title":      entry["bf_titre"],
                "wiki_url":   WIKI_BASE + "/?" + entry["id_fiche"],
                "thematique": entry.get("bf_thematique", ""),
                "keywords":   keywords,
            })
        print("  OK " + str(len(resources)) + " ressources chargees depuis la gare centrale")
        return resources
    except Exception as e:
        print("  Erreur chargement ressources : " + str(e), file=sys.stderr)
        return []


def find_related_resources(article, all_resources, shown_counts):
    article_kws   = set(article.get("mots_cles_wiki", []))
    article_theme = article.get("thematique", "")

    if not article_kws:
        return []

    scored = []
    for r in all_resources:
        resource_kws = set(r["keywords"])
        if not resource_kws:
            continue
        shared = article_kws & resource_kws
        if not shared:
            continue
        union            = article_kws | resource_kws
        jaccard          = len(shared) / len(union)
        theme_bonus      = 0.2 if r["thematique"] == article_theme else 0.0
        repetitions      = shown_counts.get(r["wiki_url"], 0)
        diversity_factor = 1.0 / (1.0 + repetitions)
        score = (jaccard + theme_bonus) * diversity_factor
        if score >= MIN_SCORE:
            scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    result = [r for _, r in scored[:MAX_RELATED_RESOURCES]]
    for r in result:
        shown_counts[r["wiki_url"]] = shown_counts.get(r["wiki_url"], 0) + 1
    return result


def fetch_resource_keywords(session, wiki_id, kw_labels):
    try:
        resp    = session.get(WIKI_BASE + "/?api/forms/8/entries&id_fiche=" + wiki_id, timeout=10)
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
        print("  Erreur mots-cles pour " + wiki_id + " : " + str(e), file=sys.stderr)
        return []


def fetch_rss(url):
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


def summarize_article(client, article, thematiques_source, wiki_keywords):
    kw_str = ", ".join(wiki_keywords[:MAX_KW_IN_PROMPT]) or "aucun disponible"
    prompt = (
        "Tu aides des agents territoriaux francais a se tenir informes.\n\n"
        "Article :\n"
        "Titre : " + article["title"] + "\n"
        "Contenu : " + article["summary"][:800] + "\n\n"
        "Mots-cles de cette source dans la gare centrale : " + kw_str + "\n\n"
        "Reponds uniquement en JSON valide (sans bloc markdown) :\n"
        '1. "resume" : 2-3 phrases factuelles utiles pour un agent territorial.\n'
        '2. "thematique" : la plus pertinente parmi : ' + " | ".join(thematiques_source) + "\n"
        '3. "mots_cles_wiki" : 2-4 mots-cles CHOISIS PARMI la liste ci-dessus (liste JSON de strings exactes).\n\n'
        '{"resume": "...", "thematique": "...", "mots_cles_wiki": ["...", "..."]}'
    )
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


# ---------------------------------------------------------------------------
# COULEURS PAR THEMATIQUE
# ---------------------------------------------------------------------------

THEME_COLORS = {
    "Transitions":                 ("e1f5ee", "0f6e56", "085041"),
    "Numerique":                   ("eeedfe", "534ab7", "3c3489"),
    "Finances":                    ("faeeda", "ba7517", "854f0b"),
    "Urbanisme / Amenagement":     ("e6f1fb", "185fa5", "0c447c"),
    "Action Sociale":              ("fbeaf0", "993556", "72243e"),
    "Animer / Decider / Cooperer": ("eaf3de", "3b6d11", "27500a"),
    "Autre":                       ("f1efe8", "5f5e5a", "444441"),
}

def theme_colors(theme):
    return THEME_COLORS.get(theme, THEME_COLORS["Autre"])


# ---------------------------------------------------------------------------
# CONSTRUCTION DE LA PAGE KANBAN
# ---------------------------------------------------------------------------

def build_page(articles, all_resources, date_str):
    grouped    = defaultdict(list)
    for a in articles:
        grouped[a.get("thematique", "Autre")].append(a)

    thematiques = sorted(grouped.keys())
    all_kws     = sorted({kw for a in articles for kw in a.get("mots_cles_wiki", [])})
    nb_sources  = len({a["source"] for a in articles})
    nb_src_lbl  = str(nb_sources) + (" sources" if nb_sources > 1 else " source")

    # ---- CSS ---------------------------------------------------------------
    css = (
        "<style>\n"
        ".ft-wrap{font-family:sans-serif;max-width:100%}\n"
        ".ft-header{background:#f0f4f8;border-left:4px solid #2d6a9f;padding:14px 18px;"
        "border-radius:4px;margin-bottom:16px;font-size:14px}\n"
        ".ft-header strong{font-size:16px;display:block;margin-bottom:4px}\n"
        ".ft-section-label{font-size:11px;font-weight:600;text-transform:uppercase;"
        "letter-spacing:.06em;color:#888;margin:0 0 8px}\n"
        ".ft-filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}\n"
        ".ft-btn{padding:5px 14px;border:1.5px solid #6b7280;border-radius:20px;"
        "background:white;color:#6b7280;cursor:pointer;font-size:12px;transition:all .15s}\n"
        ".ft-btn:hover,.ft-btn.active{background:#6b7280;color:white}\n"
        ".ft-empty-msg{display:none;font-size:13px;color:#888;padding:10px 0}\n"
        ".ft-kanban{display:flex;gap:14px;align-items:flex-start;"
        "overflow-x:auto;padding-bottom:12px}\n"
        ".ft-col{min-width:260px;flex:1 1 260px;border-radius:8px;"
        "border:1px solid #dde3ea;overflow:hidden;background:#f9fafb}\n"
        ".ft-col.ft-col-hidden{display:none}\n"
        ".ft-col-header{display:flex;justify-content:space-between;align-items:center;"
        "padding:10px 14px;border-bottom:1px solid #dde3ea}\n"
        ".ft-col-title{font-size:13px;font-weight:600;margin:0}\n"
        ".ft-col-badge{font-size:11px;font-weight:600;padding:2px 8px;"
        "border-radius:10px;min-width:20px;text-align:center}\n"
        ".ft-col-cards{padding:10px;display:flex;flex-direction:column;gap:10px}\n"
        ".ft-card{background:white;border:1px solid #e5e7eb;border-radius:6px;"
        "padding:12px 14px;font-size:13px}\n"
        ".ft-card.ft-hidden{display:none}\n"
        ".ft-card-title{font-size:14px;font-weight:600;margin:0 0 4px;line-height:1.4}\n"
        ".ft-card-title a{color:#1a3c5e;text-decoration:none}\n"
        ".ft-card-title a:hover{text-decoration:underline}\n"
        ".ft-card-meta{font-size:11px;color:#888;margin-bottom:8px}\n"
        ".ft-card-resume{color:#444;line-height:1.55;margin-bottom:8px}\n"
        ".ft-kws{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}\n"
        ".ft-kw{font-size:11px;background:#f3f4f6;padding:2px 7px;"
        "border-radius:10px;color:#555;border:1px solid #e5e7eb}\n"
        ".ft-related{border-top:1px solid #f0f0f0;padding-top:8px;margin-top:4px}\n"
        ".ft-related-label{font-size:10px;font-weight:600;text-transform:uppercase;"
        "letter-spacing:.05em;color:#aaa;display:block;margin-bottom:4px}\n"
        ".ft-res-link{display:inline-block;font-size:11px;color:#2d6a9f;"
        "background:#f0f4fb;border:1px solid #c7d8f0;border-radius:4px;"
        "padding:2px 7px;margin:2px 3px 2px 0;text-decoration:none}\n"
        ".ft-res-link:hover{background:#2d6a9f;color:white}\n"
        ".ft-no-results{display:none;text-align:center;padding:16px;"
        "font-size:13px;color:#888;font-style:italic}\n"
        "</style>\n"
    )

    # ---- Filtre mots-cles --------------------------------------------------
    kw_btns = '<button class="ft-btn active" data-kw="all">Tous mots-cles</button>\n'
    for kw in all_kws:
        kw_btns += '    <button class="ft-btn" data-kw="' + kw + '">' + kw + '</button>\n'

    # ---- Colonnes ----------------------------------------------------------
    shown_counts  = {}
    columns_html  = ""
    for theme in thematiques:
        bg, txt, txt2 = theme_colors(theme)
        cards_html = ""
        for a in grouped[theme]:
            kws_list = a.get("mots_cles_wiki", [])
            kws_data = json.dumps(kws_list, ensure_ascii=False)
            kws_html = "".join('<span class="ft-kw">' + kw + '</span>' for kw in kws_list)
            pub      = a["published"].astimezone(PARIS_TZ).strftime("%d/%m/%Y") if a.get("published") else ""
            meta     = a["source"] + (" &bull; " + pub if pub else "")

            related  = find_related_resources(a, all_resources, shown_counts)
            res_html = ""
            if related:
                res_links = "".join(
                    '<a class="ft-res-link" href="' + r["wiki_url"]
                    + '" target="_blank" rel="noopener">' + r["title"] + '</a>'
                    for r in related
                )
                res_html = (
                    '<div class="ft-related">'
                    '<span class="ft-related-label">Ressources liees</span>'
                    + res_links
                    + '</div>'
                )

            cards_html += (
                '    <div class="ft-card" data-kws=\'' + kws_data + '\'>\n'
                '      <div class="ft-card-title"><a href="' + a["link"]
                + '" target="_blank" rel="noopener">' + a["title"] + '</a></div>\n'
                '      <div class="ft-card-meta">' + meta + '</div>\n'
                '      <div class="ft-card-resume">' + a["resume"] + '</div>\n'
                '      <div class="ft-kws">' + kws_html + '</div>\n'
                '      ' + res_html + '\n'
                '    </div>\n'
            )

        nb = str(len(grouped[theme]))
        columns_html += (
            '  <div class="ft-col" data-theme="' + theme + '">\n'
            '    <div class="ft-col-header" style="background:#' + bg + '">\n'
            '      <span class="ft-col-title" style="color:#' + txt2 + '">' + theme + '</span>\n'
            '      <span class="ft-col-badge" style="background:#' + txt + ';color:white">'
            + nb + '</span>\n'
            '    </div>\n'
            '    <div class="ft-col-cards">\n'
            + cards_html
            + '    </div>\n'
            '  </div>\n'
        )

    # ---- JS ----------------------------------------------------------------
    js = (
        "<script>\n"
        "(function(){\n"
        "  var activeKw='all';\n"
        "  function apply(){\n"
        "    document.querySelectorAll('.ft-card').forEach(function(c){\n"
        "      var kws=JSON.parse(c.dataset.kws||'[]');\n"
        "      var ok=activeKw==='all'||kws.indexOf(activeKw)!==-1;\n"
        "      c.classList.toggle('ft-hidden',!ok);\n"
        "    });\n"
        "    var anyVisible=false;\n"
        "    document.querySelectorAll('.ft-col').forEach(function(col){\n"
        "      var cards=col.querySelectorAll('.ft-card');\n"
        "      var vis=0;\n"
        "      cards.forEach(function(c){if(!c.classList.contains('ft-hidden'))vis++;});\n"
        "      col.classList.toggle('ft-col-hidden',vis===0);\n"
        "      var badge=col.querySelector('.ft-col-badge');\n"
        "      if(badge)badge.textContent=vis;\n"
        "      if(vis>0)anyVisible=true;\n"
        "    });\n"
        "    var msg=document.getElementById('ft-no-results');\n"
        "    if(msg)msg.style.display=anyVisible?'none':'block';\n"
        "  }\n"
        "  document.querySelectorAll('.ft-btn').forEach(function(b){\n"
        "    b.addEventListener('click',function(){\n"
        "      document.querySelectorAll('.ft-btn').forEach(function(x){"
        "x.classList.remove('active');});\n"
        "      b.classList.add('active');\n"
        "      activeKw=b.dataset.kw;\n"
        "      apply();\n"
        "    });\n"
        "  });\n"
        "})();\n"
        "</script>\n"
    )

    # ---- Assembly ----------------------------------------------------------
    html = (
        css
        + '<div class="ft-wrap">\n'
        + '  <div class="ft-header">\n'
        + '    <strong>Actualites de la gare centrale</strong>\n'
        + '    Mise a jour : ' + date_str + ' &bull; '
        + str(len(articles)) + ' articles &bull; ' + nb_src_lbl + '\n'
        + '  </div>\n'
        + '  <div class="ft-section-label">Filtrer par mot-cle</div>\n'
        + '  <div class="ft-filters">\n'
        + '    ' + kw_btns
        + '  </div>\n'
        + '  <div id="ft-no-results" class="ft-no-results">'
        + 'Aucun article pour ce mot-cle.</div>\n'
        + '  <div class="ft-kanban">\n'
        + columns_html
        + '  </div>\n'
        + '</div>\n'
        + js
    )

    return chr(34) + chr(34) + chr(10) + html + chr(10) + chr(34) + chr(34)


def get_csrf_token(session):
    try:
        resp  = session.get(WIKI_BASE + "/?PagePrincipale", timeout=10)
        match = re.search(r'name=["\']?antispam["\']?\s+[^>]*value=["\']([^"\']+)["\']', resp.text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def publish_page(session, page_tag, content):
    url     = WIKI_BASE + "/?api/pages/" + page_tag
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
    print("  ERREUR publication : HTTP " + str(resp.status_code) + " - " + resp.text[:200], file=sys.stderr)
    return False


def main():
    config_path = Path(__file__).parent.parent / "config" / "sources.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    session = requests.Session()
    session.headers.update({"User-Agent": "FranceTerritoriale-Bot/1.0"})

    print("Authentification au wiki...")
    base_url = WIKI_BASE + "/?PagePrincipale="
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
    print("  OK Session etablie")

    print("\nChargement des mots-cles du wiki...")
    kw_labels = fetch_keyword_labels(session)
    print("  OK " + str(len(kw_labels)) + " mots-cles charges")

    print("\nChargement des ressources de la gare centrale...")
    all_resources = fetch_all_wiki_resources(session, kw_labels)

    all_articles = []
    for source in config.get("sources", []):
        name        = source["name"]
        wiki_id     = source.get("wiki_id", "")
        thematiques = source.get("thematiques", ["Autre"])
        print("\nSource : " + name)

        wiki_keywords = []
        if wiki_id:
            wiki_keywords = fetch_resource_keywords(session, wiki_id, kw_labels)
            preview = ", ".join(wiki_keywords[:8]) + ("..." if len(wiki_keywords) > 8 else "")
            print("  Mots-cles : " + (preview or "aucun"))

        raw = []
        for url in source.get("rss_urls", []):
            try:
                items = fetch_rss(url)
                print("  " + url.split("/")[2] + " -> " + str(len(items)) + " articles")
                raw.extend(items)
            except Exception as e:
                print("  Erreur " + url + " : " + str(e), file=sys.stderr)

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
                print("  OK " + article["title"][:55] + " [" + (", ".join(kws) or "-") + "]")
            except Exception as e:
                print("  Erreur Haiku : " + article["title"][:40] + " : " + str(e), file=sys.stderr)

    if not all_articles:
        print("\nAucun article trouve, page non mise a jour.")
        sys.exit(0)

    all_articles.sort(
        key=lambda a: a.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    date_str = datetime.now(PARIS_TZ).strftime("%d/%m/%Y a %Hh%M")
    content  = build_page(all_articles, all_resources, date_str)

    print("\nPublication de " + str(len(all_articles)) + " articles sur " + PAGE_TAG + "...")
    if publish_page(session, PAGE_TAG, content):
        print("  OK " + WIKI_BASE + "/?" + PAGE_TAG)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
