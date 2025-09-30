#!/usr/bin/env python3
# main.py
"""
AI Research Bot - improved version
- Modell wird klar angewiesen, Websuche über DuckDuckGo zu verwenden
- robust fetch mit Header-Rotation und Fallbacks
- sauberes Ollama Parsing
- PDF mit Timestamp und Unicode-Support
- SSE für Live-Updates
"""

import os
import re
import json
import time
import queue
import threading
import datetime
import logging
import requests
from html import unescape
from flask import Flask, request, Response, send_file, abort
import trafilatura
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Ollama optional einbinden
try:
    import ollama
    OLLAMA_AVAILABLE = True
except Exception:
    OLLAMA_AVAILABLE = False

# -------------------
# CONFIG
# -------------------
OUTPUT_DIR = "reports"
os.makedirs(OUTPUT_DIR, exist_ok=True)

OLLAMA_MODEL = "command-r"
SEARCH_URL = "https://html.duckduckgo.com/html/"

LAST_REPORT = {"path": None}
LAST_REPORT_LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

COMMON_HEADERS = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Safari/605.1.15",
        "Accept-Language": "en-US,en;q=0.9",
    },
]

# -------------------
# Utilities
# -------------------

def safe_text(s):
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            s = str(s)
        except Exception:
            s = "<unreprable>"
    return unescape(s)

def now_ts():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def register_dejavu_font():
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/local/share/fonts/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont("DejaVuSans", p))
                logging.info("✅ Registered font DejaVuSans from %s", p)
                return "DejaVuSans"
            except Exception as e:
                logging.warning("Font error: %s", e)
    return None

DEFAULT_UNICODE_FONT = register_dejavu_font()

# -------------------
# Web Search + Scraping
# -------------------

def ddg_search(query, max_results=5):
    logging.info("Search query: %s", query)
    headers = COMMON_HEADERS[0]
    data = {"q": query}
    try:
        r = requests.post(SEARCH_URL, data=data, headers=headers, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http"):
                title = safe_text(a.get_text(strip=True) or href)
                snippet = safe_text(a.parent.get_text(" ", strip=True) if a.parent else "")
                results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= max_results:
                break
        logging.info("Search found %d results", len(results))
        return results
    except Exception as e:
        logging.warning("DDG search failed: %s", e)
        return []

def fetch_page(url, max_attempts=3):
    url = safe_text(url)
    last_exc = None
    for attempt in range(max_attempts):
        headers = COMMON_HEADERS[attempt % len(COMMON_HEADERS)]
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            raw = r.text or ""
            text = trafilatura.extract(raw, url=url)
            if text and len(text) > 200:
                return text
            soup = BeautifulSoup(raw, "html.parser")
            return soup.get_text(separator="\n", strip=True)
        except Exception as e:
            last_exc = e
            time.sleep(1)
            continue
    logging.error("❌ Fetch Fehler (%s): %s", url, last_exc)
    return ""

# -------------------
# Ollama
# -------------------

def ollama_chat(messages):
    if not OLLAMA_AVAILABLE:
        logging.warning("Ollama not available.")
        return ""
    try:
        resp = ollama.chat(model=OLLAMA_MODEL, messages=messages)
        if isinstance(resp, dict):
            if "message" in resp and isinstance(resp["message"], dict):
                return safe_text(resp["message"].get("content", ""))
            if "content" in resp:
                return safe_text(resp["content"])
        if isinstance(resp, str):
            return safe_text(resp)
        return safe_text(str(resp))
    except Exception as e:
        logging.exception("Ollama call failed: %s", e)
        return ""

def parse_json_safe(s):
    try:
        return json.loads(s)
    except Exception:
        pass
    # simple fallback
    if "search" in s.lower():
        return {"action": "search", "query": s}
    if "finish" in s.lower():
        return {"action": "finish", "summary": s}
    return {}

# -------------------
# PDF
# -------------------

def create_pdf(path, task, summary, notes):
    try:
        task = safe_text(task)
        summary = safe_text(summary)
        notes = notes or []

        doc = SimpleDocTemplate(path, pagesize=A4,
                                rightMargin=20*mm, leftMargin=20*mm,
                                topMargin=20*mm, bottomMargin=20*mm)
        styles = getSampleStyleSheet()
        if DEFAULT_UNICODE_FONT:
            body_style = ParagraphStyle("Body", parent=styles["BodyText"],
                                        fontName=DEFAULT_UNICODE_FONT, fontSize=10, leading=12)
            title_style = ParagraphStyle("Title", parent=styles["Title"], fontName=DEFAULT_UNICODE_FONT)
            h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName=DEFAULT_UNICODE_FONT)
        else:
            body_style = styles["BodyText"]
            title_style = styles["Title"]
            h2 = styles["Heading2"]

        story = []
        story.append(Paragraph("AI Research Report", title_style))
        story.append(Spacer(1, 12))
        story.append(Paragraph(task, h2))
        story.append(Spacer(1, 12))

        story.append(Paragraph("Executive Summary", h2))
        for para in summary.split("\n"):
            story.append(Paragraph(para, body_style))
            story.append(Spacer(1, 6))

        for i, n in enumerate(notes, 1):
            story.append(PageBreak())
            story.append(Paragraph(f"{i}. {n.get('title')}", h2))
            story.append(Paragraph(f"URL: {n.get('url')}", body_style))
            story.append(Spacer(1, 6))
            story.append(Paragraph(safe_text(n.get("summary")), body_style))

        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc.build(story)
        logging.info("✅ PDF erstellt: %s", path)
        return path
    except Exception as e:
        logging.exception("❌ PDF Fehler: %s", e)
        return None

# -------------------
# Agent
# -------------------

def run_agent_stream(task, max_results, q):
    system_prompt = """
Du bist ein Research-Agent. Antworte IMMER im JSON-Format.
Aktionen:
1. {"action":"search","query":"..."} → wenn du mehr Infos brauchst, führe eine DuckDuckGo-Suche aus.
2. {"action":"finish","summary":"..."} → wenn du genug Infos hast.

Regeln:
- Plane mehrere Suchen falls nötig.
- Nutze die Knowledge aus geladenen Seiten.
- Schreibe niemals freien Text außerhalb von JSON.
"""
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Task: {task}"}]

    notes = []
    final_summary = ""

    for _ in range(20):
        resp = ollama_chat(messages)
        if not resp:
            q.put("⚠️ Modell antwortet nicht.")
            break
        action = parse_json_safe(resp)
        act = action.get("action", "").lower()

        if act == "search":
            query = action.get("query", task)
            q.put(f"🔎 Suche nach: {query}")
            results = ddg_search(query, max_results=max_results)
            for r in results:
                r_title = r["title"]
                r_url = r["url"]
                q.put(f"🌐 Lade Seite: {r_title} ({r_url})")
                text = fetch_page(r_url)
                if not text:
                    snippet = r.get("snippet", "")
                    notes.append({"title": r_title, "url": r_url, "summary": snippet})
                    q.put(f"ℹ️ Snippet verwendet für {r_title}")
                    continue
                summ = ollama_chat([
                    {"role": "system", "content": "Fasse prägnant in 5 Bulletpoints zusammen."},
                    {"role": "user", "content": text[:5000]}
                ]) or text[:500]
                notes.append({"title": r_title, "url": r_url, "summary": summ})
                q.put(f"✅ Zusammenfassung fertig: {r_title}")
            messages.append({"role": "user", "content": "Neue Knowledge hinzugefügt."})

        elif act == "finish":
            final_summary = action.get("summary", "")
            q.put("🎉 Finale Zusammenfassung erstellt!")
            filename = f"report_{now_ts()}.pdf"
            path = os.path.join(OUTPUT_DIR, filename)
            created = create_pdf(path, task, final_summary, notes)
            if created:
                with LAST_REPORT_LOCK:
                    LAST_REPORT["path"] = created
                q.put(f"📄 PDF bereit: /download?file={os.path.basename(created)}")
            return

        else:
            q.put("⚠️ Ungültige Action.")
            break

    # fallback PDF
    filename = f"report_partial_{now_ts()}.pdf"
    path = os.path.join(OUTPUT_DIR, filename)
    create_pdf(path, task, final_summary or "Kein vollständiges Ergebnis.", notes)
    with LAST_REPORT_LOCK:
        LAST_REPORT["path"] = path
    q.put(f"📄 Zwischenstand PDF: /download?file={os.path.basename(path)}")

# -------------------
# Flask
# -------------------

app = Flask(__name__)

@app.route("/")
def index():
    return """
    <h1>🔎 AI Research Bot</h1>
    <form onsubmit="startResearch(event)">
      <input id="task" placeholder="Task" style="width:60%">
      <input id="results" type="number" value="3" min="1" max="10">
      <button>Start</button>
    </form>
    <div id="output"></div>
    <script>
    let es;
    function startResearch(e){
      e.preventDefault();
      if(es){ es.close(); }
      document.getElementById("output").innerHTML="";
      let task=document.getElementById("task").value;
      let results=document.getElementById("results").value;
      es=new EventSource(`/stream?task=${encodeURIComponent(task)}&results=${results}`);
      es.onmessage=function(e){
        let out=document.getElementById("output");
        out.innerHTML+=e.data+"<br>";
        out.scrollTop=out.scrollHeight;
      }
    }
    </script>
    """

@app.route("/stream")
def stream():
    task = request.args.get("task", "")
    results = int(request.args.get("results", 3))
    q = queue.Queue()

    def gen():
        while True:
            try:
                yield f"data: {q.get(timeout=1)}\n\n"
            except queue.Empty:
                continue

    threading.Thread(target=run_agent_stream, args=(task, results, q), daemon=True).start()
    return Response(gen(), mimetype="text/event-stream")

@app.route("/download")
def download():
    filename = request.args.get("file")
    if filename:
        path = os.path.join(OUTPUT_DIR, os.path.basename(filename))
        if os.path.exists(path):
            return send_file(path, as_attachment=True)
        abort(404)
    with LAST_REPORT_LOCK:
        path = LAST_REPORT.get("path")
    if path and os.path.exists(path):
        return send_file(path, as_attachment=True)
    return "❌ Kein Report verfügbar", 404

if __name__ == "__main__":
    logging.info("Starting AI Research Bot - OLLAMA_AVAILABLE=%s", OLLAMA_AVAILABLE)
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
