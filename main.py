#!/usr/bin/env python3
# main.py
"""
AI Research Bot - single summary version
- Alles in eine PDF-Zusammenfassung
- Robust gegen Agenten-Timeouts
- DuckDuckGo-Suche, Header-Rotation
- PDF mit Unicode-Support
"""

import os, json, time, threading, queue, datetime, logging, requests
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

# Optional Ollama
try:
    import ollama
    OLLAMA_AVAILABLE = True
except Exception:
    OLLAMA_AVAILABLE = False

OUTPUT_DIR = "reports"
os.makedirs(OUTPUT_DIR, exist_ok=True)
OLLAMA_MODEL = "command-r"

LAST_REPORT = {"path": None}
LAST_REPORT_LOCK = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

COMMON_HEADERS = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/14.0 Safari/605.1.15", "Accept-Language": "en-US,en;q=0.9"},
]

def safe_text(s):
    if s is None: return ""
    return unescape(str(s))

def now_ts():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def register_dejavu_font():
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/local/share/fonts/DejaVuSans.ttf"]:
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont("DejaVuSans", p))
                return "DejaVuSans"
            except: pass
    return None

DEFAULT_UNICODE_FONT = register_dejavu_font()

def ddg_search(query, max_results=5):
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=COMMON_HEADERS[0], timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http"):
                results.append({"title": safe_text(a.get_text(strip=True) or href),
                                "url": href,
                                "snippet": safe_text(a.parent.get_text(" ", strip=True) if a.parent else "")})
            if len(results) >= max_results: break
        return results
    except Exception as e:
        logging.warning("DDG search failed: %s", e)
        return []

def fetch_page(url, max_attempts=3):
    url = safe_text(url)
    for attempt in range(max_attempts):
        try:
            r = requests.get(url, headers=COMMON_HEADERS[attempt % len(COMMON_HEADERS)], timeout=20)
            r.raise_for_status()
            raw = r.text or ""
            text = trafilatura.extract(raw, url=url)
            return text or BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)
        except Exception: time.sleep(1)
    return ""

def ollama_chat(messages):
    if not OLLAMA_AVAILABLE: return ""
    try:
        resp = ollama.chat(model=OLLAMA_MODEL, messages=messages)
        if isinstance(resp, dict):
            return safe_text(resp.get("message", {}).get("content") or resp.get("content"))
        return safe_text(resp)
    except Exception as e:
        logging.warning("Ollama failed: %s", e)
        return ""

def parse_json_safe(s):
    try: return json.loads(s)
    except:
        if "search" in s.lower(): return {"action":"search","query":s}
        if "finish" in s.lower(): return {"action":"finish","summary":s}
    return {}

def create_pdf(path, task, summary, notes):
    doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=20*mm,leftMargin=20*mm,topMargin=20*mm,bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("Body", parent=styles["BodyText"], fontName=DEFAULT_UNICODE_FONT or styles["BodyText"].fontName, fontSize=10, leading=12)
    title_style = ParagraphStyle("Title", parent=styles["Title"], fontName=DEFAULT_UNICODE_FONT or styles["Title"].fontName)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName=DEFAULT_UNICODE_FONT or styles["Heading2"].fontName)
    
    story = [Paragraph("AI Research Report", title_style), Spacer(1,12), Paragraph(task, h2), Spacer(1,12),
             Paragraph("Executive Summary", h2), Spacer(1,6)]
    
    for para in summary.split("\n"): story.extend([Paragraph(para, body_style), Spacer(1,6)])
    
    for i,n in enumerate(notes,1):
        story.append(PageBreak())
        story.extend([Paragraph(f"{i}. {n.get('title')}", h2), Paragraph(f"URL: {n.get('url')}", body_style), Spacer(1,6), Paragraph(safe_text(n.get("summary")), body_style)])
    
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc.build(story)
    return path

def run_agent_stream(task, max_results, q):
    system_prompt = """
Du bist ein Research-Agent. Antworte IMMER im JSON-Format.
Aktionen: 1. {"action":"search","query":"..."} 2. {"action":"finish","summary":"..."}
"""
    messages = [{"role":"system","content":system_prompt},{"role":"user","content":f"Task: {task}"}]
    notes, final_summary = [], ""
    
    for _ in range(25):  # mehr Versuche für Timeout
        resp = ollama_chat(messages)
        if not resp: 
            q.put("⚠️ Modell antwortet nicht, nächster Versuch...")
            time.sleep(2)
            continue
        action = parse_json_safe(resp)
        act = action.get("action","").lower()
        
        if act == "search":
            query = action.get("query", task)
            results = ddg_search(query, max_results)
            for r in results:
                text = fetch_page(r["url"])
                summ = ollama_chat([{"role":"system","content":"Fasse prägnant zusammen."},{"role":"user","content":text[:5000]}]) or text[:500]
                notes.append({"title": r["title"], "url": r["url"], "summary": summ})
            messages.append({"role":"user","content":"Neue Knowledge hinzugefügt."})
        elif act == "finish":
            final_summary = action.get("summary","")
            break
    
    filename = f"report_{now_ts()}.pdf"
    path = os.path.join(OUTPUT_DIR, filename)
    create_pdf(path, task, final_summary or "Kein vollständiges Ergebnis.", notes)
    with LAST_REPORT_LOCK: LAST_REPORT["path"] = path
    q.put(f"📄 PDF bereit: /download?file={os.path.basename(path)}")

# ------------------- Flask -------------------
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
    task = request.args.get("task","")
    results = int(request.args.get("results",3))
    q = queue.Queue()
    def gen():
        while True:
            try: yield f"data: {q.get(timeout=1)}\n\n"
            except queue.Empty: continue
    threading.Thread(target=run_agent_stream,args=(task,results,q),daemon=True).start()
    return Response(gen(), mimetype="text/event-stream")

@app.route("/download")
def download():
    filename = request.args.get("file")
    if filename:
        path = os.path.join(OUTPUT_DIR, os.path.basename(filename))
        if os.path.exists(path): return send_file(path, as_attachment=True)
        abort(404)
    with LAST_REPORT_LOCK: path = LAST_REPORT.get("path")
    if path and os.path.exists(path): return send_file(path, as_attachment=True)
    return "❌ Kein Report verfügbar",404

if __name__=="__main__":
    logging.info("Starting AI Research Bot - OLLAMA_AVAILABLE=%s", OLLAMA_AVAILABLE)
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
