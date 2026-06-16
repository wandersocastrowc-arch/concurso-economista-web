#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agregador do Monitor de Concursos para Economista do Wanderson.

A cada execucao (segunda 9h via GitHub Actions):
  1. Carrega events.json e sources.json
  2. Recalcula status por prazo (encerra sozinho quando a data passa)
  3. Coleta candidatos de:
       - Feeds RSS de portais de concursos (PCI, JC, Gran, Estrategia)  [sem chave]
       - Google Custom Search (busca ampla na web)         [opcional: GOOGLE_API_KEY + GOOGLE_CSE_ID]
  4. Curadoria: por IA se ANTHROPIC_API_KEY existir; senao, filtro por palavras-chave
  5. Merge/dedupe por id e link, com teto de novos itens (entram como categoria "aceita", a confirmar)
  6. Salva events.json e last_update.json
  7. Envia e-mail digest (se GMAIL_USER/GMAIL_APP_PASSWORD/MAIL_TO existirem)

Regra: so entra concurso que mencione Economia/economista. Cargos "qualquer area" genericos
sao descartados na curadoria por IA; no fallback por palavra-chave, exigimos termo economico.
"""

import json, os, re, datetime, unicodedata, smtplib, ssl, socket, urllib.request, urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

socket.setdefaulttimeout(15)

ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(ROOT, "events.json")
SOURCES = os.path.join(ROOT, "sources.json")
LAST = os.path.join(ROOT, "last_update.json")
TODAY = datetime.date.today()
UA = "Mozilla/5.0 (compatible; MonitorConcursos/1.0; +https://github.com)"
MAX_NEW = 25

# Precisa conter algum termo economico forte
KEYWORDS = [
    "economista", "ciencias economicas", "ciências econômicas", "economia",
    "economico", "econômico", "corecon", "planejamento e orcamento",
    "orcamento", "orçamento", "financas publicas", "finanças públicas",
    "controle externo", "auditor fiscal", "politicas publicas", "regulacao",
]
# Termos que indicam concurso fora do perfil mesmo citando "economia" de passagem
BLOCK = [
    "professor", "magisterio", "magistério", "saude", "saúde", "enfermag",
    "medic", "tecnico de enfermagem", "guarda municipal", "militar", "soldado",
    "agente de saude", "merenda", "motorista", "operario", "vigilante",
]


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:60] or "item"


def norm(text):
    return unicodedata.normalize("NFKD", (text or "").lower()).encode("ascii", "ignore").decode()


def norm_link(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"[#?].*$", "", u)
    return u.rstrip("/")


def parse_date(iso):
    try:
        return datetime.date.fromisoformat(iso)
    except Exception:
        return None


def relevant(text):
    t = norm(text)
    if any(norm(b) in t for b in BLOCK):
        return False
    return any(norm(k) in t for k in KEYWORDS)


MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7,
         "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


def extract_deadline(text):
    if not text:
        return None
    t = text.lower()
    m = re.search(r"(\d{1,2})[/\.](\d{1,2})[/\.](\d{4})", t)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except Exception:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    m = re.search(r"(\d{1,2})\s+de\s+([a-z]{3,9})\s+de\s+(\d{4})", t)
    if m and m.group(2)[:3] in MESES:
        try:
            return datetime.date(int(m.group(3)), MESES[m.group(2)[:3]], int(m.group(1)))
        except Exception:
            pass
    return None


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def http_json(url, timeout=25):
    return json.loads(http_get(url, timeout))


# ----------------------------------------------------------------- fetchers
def fetch_rss(feeds):
    out = []
    try:
        import feedparser
    except Exception:
        print("[rss] feedparser indisponivel.")
        return out
    for f in feeds:
        url = f.get("url")
        try:
            d = feedparser.parse(url, agent=UA)
            if not d.entries:
                print("[rss] sem entradas: " + url)
                continue
        except Exception as e:
            print("[rss] falha " + url + ": " + str(e))
            continue
        for e in d.entries[:40]:
            title = (e.get("title") or "").strip()
            summary = (e.get("summary") or e.get("description") or "")[:600]
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue
            out.append({"nome": title, "summary": summary, "link": link,
                        "inst": d.feed.get("title", "Portal de concursos"),
                        "areas": list(f.get("area", [])), "idioma": "PT", "source": "RSS"})
        print("[rss] " + url + ": " + str(len(d.entries)) + " entradas lidas")
    return out


def fetch_google(queries):
    key = os.environ.get("GOOGLE_API_KEY")
    cx = os.environ.get("GOOGLE_CSE_ID")
    out = []
    if not (key and cx):
        print("[google] sem GOOGLE_API_KEY/GOOGLE_CSE_ID - pulando busca web ampla.")
        return out
    for q in queries:
        url = ("https://www.googleapis.com/customsearch/v1?key=" + key + "&cx=" + cx
               + "&num=8&q=" + urllib.parse.quote(q))
        try:
            data = http_json(url)
        except Exception as e:
            print("[google] falha: " + str(e))
            continue
        for it in data.get("items", []):
            out.append({"nome": it.get("title", "").strip(),
                        "summary": it.get("snippet", ""), "link": it.get("link", ""),
                        "inst": "Via busca web", "areas": [], "source": "Google"})
    print("[google] " + str(len(out)) + " resultados")
    return out


# ----------------------------------------------------------------- curadoria
def ai_curate(cands):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not cands:
        return None
    compact = [{"i": n, "t": c["nome"][:160], "s": (c.get("summary") or "")[:240],
                "src": c["source"]} for n, c in enumerate(cands)]
    prompt = (
        "Voce e curador do Wanderson, economista em Sao Paulo com registro ativo no CORECON, "
        "que busca CONCURSOS PUBLICOS para economista. Interessa: (1) cargos de Economista "
        "(privativos, exigem Ciencias Economicas) e (2) cargos cujo edital aceita economista "
        "JUNTO a outras formacoes especificas (ex.: economia, administracao, contabilidade, "
        "direito), ou carreiras de area economica (Banco Central, IPEA, controle externo, "
        "orcamento/financas publicas, regulacao).\n"
        "NAO interessa: concursos abertos a QUALQUER nivel superior sem relacao com economia; "
        "cargos de saude, professor, militar, nivel medio/fundamental operacional.\n"
        "Para CADA item decida. Responda APENAS um JSON: lista de objetos {\"i\":indice,"
        "\"keep\":true/false,\"score\":0-100,\"categoria\":\"economista\"|\"aceita\","
        "\"areas\":[ate 3 tags em pt],\"resumo\":\"1 frase em pt\"}. Sem texto fora do JSON.\n\nITENS:\n"
        + json.dumps(compact, ensure_ascii=False))
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        verdicts = json.loads(text)
        by_i = {v["i"]: v for v in verdicts if isinstance(v, dict) and "i" in v}
        kept = []
        for n, c in enumerate(cands):
            v = by_i.get(n)
            if not v or not v.get("keep") or v.get("score", 0) < 55:
                continue
            if v.get("areas"):
                c["areas"] = v["areas"][:3]
            if v.get("resumo"):
                c["obs"] = (v["resumo"] + " " + c.get("obs", "")).strip()
            c["categoria"] = "economista" if v.get("categoria") == "economista" else "aceita"
            c["score"] = v.get("score", 0)
            kept.append(c)
        kept.sort(key=lambda x: x.get("score", 0), reverse=True)
        print("[ia] curadoria: " + str(len(kept)) + " de " + str(len(cands)) + " aprovados")
        return kept
    except Exception as e:
        print("[ia] falha na curadoria: " + str(e))
        return None


# ----------------------------------------------------------------- nucleo
def recompute_status(items):
    changed = []
    for it in items:
        if it.get("status") in ("monitor", "soon"):
            continue
        d = parse_date(it.get("prazoSort", ""))
        if not d or d.year >= 2099:
            continue
        new = "closed" if d < TODAY else "open"
        if new != it.get("status"):
            if new == "closed":
                changed.append(it)
            it["status"] = new
    return changed


def to_item(c):
    deadline = extract_deadline((c.get("nome", "") + " " + c.get("summary", "")))
    if deadline:
        if deadline < TODAY:
            return None
        prazo, prazoSort, status = "Inscricao ate " + deadline.isoformat(), deadline.isoformat(), "open"
    else:
        prazo, prazoSort, status = "Verificar cronograma", "2026-12-01", "monitor"
    obs = c.get("obs", "")
    if c["source"] in ("Google", "RSS"):
        obs = (obs + " Descoberto via " + c["source"] + " - confira o edital oficial e as formacoes aceitas.").strip()
    return {
        "id": c["source"][:4].lower() + "-" + slug(c["nome"]),
        "nome": c["nome"], "inst": c.get("inst", ""),
        "categoria": c.get("categoria", "aceita"),
        "areas": c.get("areas") or ["a confirmar"],
        "ambito": "federal", "local": "A confirmar", "municipio": "A confirmar",
        "banca": "a definir", "vagas": "", "salario": "",
        "prazo": prazo, "prazoSort": prazoSort, "status": status,
        "novo": True, "addedOn": TODAY.isoformat(),
        "fonte": c["source"], "obs": obs, "link": c["link"],
    }


def collect(cfg, existing_links, existing_ids):
    cands = []
    cands += fetch_rss(cfg.get("rss_feeds", []))
    cands += fetch_google(cfg.get("google_cse_queries", []))

    seen = set(existing_links)
    uniq = []
    for c in cands:
        nl = norm_link(c.get("link"))
        if not nl or nl in seen:
            continue
        seen.add(nl)
        uniq.append(c)
    print("[collect] " + str(len(cands)) + " candidatos, " + str(len(uniq)) + " novos (pos-dedupe)")

    curated = ai_curate(uniq)
    if curated is None:
        curated = [c for c in uniq if relevant(c["nome"] + " " + c.get("summary", ""))]
        print("[curadoria] sem IA - filtro por palavras-chave: " + str(len(curated)))

    new_items = []
    for c in curated[:MAX_NEW]:
        it = to_item(c)
        if it and it["id"] not in existing_ids:
            existing_ids.add(it["id"])
            new_items.append(it)
    return new_items


# ----------------------------------------------------------------- e-mail
def li(i, extra=""):
    return ('<li style="margin:6px 0;"><a href="' + i.get("link", "#")
            + '" style="color:#14532d;text-decoration:none;"><b>' + i.get("nome", "")
            + '</b></a><br><span style="color:#666;font-size:13px;">' + i.get("inst", "")
            + ' &middot; ' + i.get("prazo", "") + (" &middot; " + i.get("fonte", "") if i.get("fonte") else "")
            + extra + '</span></li>')


def build_email_html(items, new_items, closed_now):
    abertos = sorted([i for i in items if i["status"] in ("open", "soon")], key=lambda x: x.get("prazoSort", ""))
    p = ['<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;color:#16181d;">'
         '<h2 style="color:#14532d;border-bottom:3px solid #16a34a;padding-bottom:6px;">'
         'Monitor de Concursos para Economista &mdash; ' + TODAY.strftime("%d/%m/%Y") + '</h2>'
         '<p style="color:#444;font-size:14px;">Ola, Wanderson! Resumo desta rodada.</p>']
    if new_items:
        p.append('<h3 style="color:#92400e;">Novidades encontradas (' + str(len(new_items)) + ')</h3><ul>')
        p.append("".join(li(i) for i in new_items)); p.append("</ul>")
    p.append('<h3 style="color:#14532d;">Abertos / iminentes (' + str(len(abertos)) + ')</h3><ul>')
    p.append("".join(li(i) for i in abertos)); p.append("</ul>")
    if closed_now:
        p.append('<h3 style="color:#991b1b;">Encerraram desde a ultima atualizacao</h3><ul>')
        p.append("".join('<li style="color:#888;">' + i.get("nome", "") + '</li>' for i in closed_now)); p.append("</ul>")
    p.append('<p style="font-size:12px;color:#999;margin-top:18px;">Painel completo (com filtros, favoritar e arquivar) no GitHub Pages. '
             'Atualizacao automatica &middot; segundas as 9h.</p></div>')
    return "".join(p)


def send_email(html):
    user, pwd, to = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD"), os.environ.get("MAIL_TO")
    if not (user and pwd and to):
        print("[email] credenciais ausentes - pulando envio.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Monitor de Concursos (Economista) - " + TODAY.strftime("%d/%m/%Y")
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText("Abra em HTML.", "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print("[email] enviado para " + to)


def main():
    data = json.load(open(EVENTS, encoding="utf-8"))
    cfg = json.load(open(SOURCES, encoding="utf-8"))
    items = data.get("items", [])
    existing_ids = set(i["id"] for i in items)
    existing_links = set(norm_link(i.get("link", "")) for i in items)

    closed_now = recompute_status(items)
    try:
        new_items = collect(cfg, existing_links, existing_ids)
    except Exception as e:
        print("[collect] erro geral: " + str(e)); new_items = []
    items.extend(new_items)
    data["items"] = items

    with open(EVENTS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    counts, cats = {}, {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
        cats[i.get("categoria", "aceita")] = cats.get(i.get("categoria", "aceita"), 0) + 1
    with open(LAST, "w", encoding="utf-8") as f:
        json.dump({"timestamp": datetime.datetime.now().isoformat(timespec="minutes"),
                   "total": len(items), "novos": len(new_items),
                   "encerrados_neste_run": len(closed_now),
                   "por_status": counts, "por_categoria": cats}, f, ensure_ascii=False, indent=2)
    print("[ok] total=" + str(len(items)) + " novos=" + str(len(new_items)) + " status=" + str(counts))
    try:
        send_email(build_email_html(items, new_items, closed_now))
    except Exception as e:
        print("[email] erro: " + str(e))


if __name__ == "__main__":
    main()
