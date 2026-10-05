"""CareLens backend: explains medical reports with a local medical LLM (MedGemma via Ollama).

Nothing leaves the machine: reports go only to the local Ollama server, and profiles and
results are kept in a local file (data/profiles.json).
"""

import base64
import json
import os
import re
import socket
import time
from pathlib import Path

import httpx
import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import profiles as store
from agent import KB, best_key, run_agent

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("CARELENS_MODEL", "medgemma")
ROOT = Path(__file__).resolve().parent.parent
MAX_PAGES = 4

app = FastAPI(title="CareLens")


@app.middleware("http")
async def no_cache(request, call_next):
    # always serve the latest UI files (avoids stale JS after updates)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "report_type": {"type": "string"},
        "summary": {"type": "string"},
        "urgency": {"type": "string", "enum": ["routine", "see_doctor_soon", "urgent"]},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "test": {"type": "string"},
                    "value": {"type": "string"},
                    "unit": {"type": "string"},
                    "reference_range": {"type": "string"},
                    "status": {"type": "string", "enum": ["low", "normal", "high", "critical", "unknown"]},
                    "meaning": {"type": "string"},
                },
                "required": ["test", "value", "unit", "reference_range", "status", "meaning"],
            },
        },
        "key_findings": {"type": "array", "items": {"type": "string"}},
        "questions_for_doctor": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["report_type", "summary", "urgency", "results", "key_findings", "questions_for_doctor"],
}

ANALYZE_PROMPT = """You are a careful medical assistant explaining a patient's own medical report to them.
Read the report and return JSON that matches the schema.

Rules:
- Only use values that appear in the report. Never invent tests or numbers.
- "results": one entry per measured test, copying value, unit and reference range exactly as printed.
- "meaning": 1-2 plain-English sentences: what the test measures and what this result suggests. No jargon.
- "summary": 3-4 sentences a non-medical person can understand. Calm, honest, not alarming.
- "key_findings": the 2-5 most important things, abnormal results first.
- "questions_for_doctor": 3-5 specific questions the patient should ask their doctor.
- "urgency": "urgent" only for clearly critical values; "see_doctor_soon" if anything is abnormal; else "routine".
- Do not diagnose. Never recommend any medicine, supplement, diet or treatment. Suggest discussing results with a doctor.

REPORT:
"""

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "explanations": {
            "type": "array",
            "items": {"type": "object", "properties": {"test": {"type": "string"}, "meaning": {"type": "string"}},
                      "required": ["test", "meaning"]},
        },
        "questions_for_doctor": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "explanations", "questions_for_doctor"],
}

EXPLAIN_PROMPT = """You are explaining a patient's lab results to them. The flags below were CHECKED BY CODE
against the printed reference ranges and are correct. Do not contradict them.

{flagged}

Normal results: {normal}

Return JSON:
- "summary": 3-4 calm, plain-English sentences for a non-medical person. Mention the out-of-range results
  and how they might relate to each other (for example, results that often appear together). Do not diagnose.
- "explanations": for EACH out-of-range test above, 1-2 sentences: what it measures and what this result can mean.
- "questions_for_doctor": 3-5 specific questions about these results.

Never recommend, suggest or dose any medicine, supplement, vitamin, diet or treatment. Do not diagnose.
"""


# ---------- helpers ----------

_net_cache = {"t": 0.0, "online": False}


def internet_reachable() -> bool:
    if time.time() - _net_cache["t"] < 3:
        return _net_cache["online"]
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=0.8).close()
        online = True
    except OSError:
        online = False
    _net_cache.update(t=time.time(), online=online)
    return online


def page_lines(page) -> str:
    """Rebuild visual rows (table rows stay on one line, columns split by ' | ')."""
    rows: dict[int, list] = {}
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        key = next((k for k in rows if abs(k - y1) < 3), round(y1))
        rows.setdefault(key, []).append((x0, x1, word))
    out = []
    for key in sorted(rows):
        words = sorted(rows[key])
        line, prev_end = "", None
        for x0, x1, word in words:
            if prev_end is not None:
                line += " | " if x0 - prev_end > 15 else " "
            line += word
            prev_end = x1
        out.append(line)
    return "\n".join(out)


def extract_report(data: bytes, filename: str) -> tuple[str, list[str]]:
    """Return (text, base64 page images). Scanned PDFs and photos go to the model as images."""
    name = filename.lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "", [base64.b64encode(data).decode()]
    if name.endswith(".txt"):
        return data.decode(errors="ignore"), []
    if not name.endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF, image (PNG/JPG) or .txt report.")

    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = list(doc)[:MAX_PAGES]
    text = "\n".join(page_lines(p) for p in pages).strip()
    if len(text) > 80:
        return text, []
    # scanned PDF: render pages so the vision model can read them
    images = [base64.b64encode(p.get_pixmap(dpi=110).tobytes("png")).decode() for p in pages]
    return "", images


_RANGE = re.compile(r"^\s*([<>]?)\s*(-?\d+(?:\.\d+)?)\s*(?:[-–]\s*(-?\d+(?:\.\d+)?))?\s*$")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def check_status(value: str, ref: str) -> str | None:
    """Recompute low/normal/high from the printed numbers so the flag never depends on the LLM."""
    v = _NUM.search(value or "")
    r = _RANGE.match((ref or "").replace(",", ""))
    if not v or not r:
        return None
    x = float(v.group())
    op, a, b = r.group(1), float(r.group(2)), r.group(3)
    if b is not None:
        lo, hi = a, float(b)
        return "low" if x < lo else "high" if x > hi else "normal"
    if op == "<":
        return "high" if x >= a else "normal"
    if op == ">":
        return "low" if x <= a else "normal"
    return None


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def kb_meaning(test: str) -> str:
    key = best_key(test, KB["tests"])
    return KB["tests"][key]["what"] if key else ""


def clean_results(rows: list[dict], text: str) -> list[dict]:
    """Drop duplicates and non-results, then recompute every flag from the printed numbers."""
    seen, out = set(), []
    for row in rows:
        key = (row.get("test") or "").strip().lower()
        value = (row.get("value") or "").strip()
        if not key or key in seen or value.upper() in ("", "N/A", "NA", "-"):
            continue
        # for text reports, the value must actually appear in the report (no invented numbers)
        if text and _NUM.search(value) and _NUM.search(value).group() not in text:
            continue
        seen.add(key)
        computed = check_status(value, row.get("reference_range", ""))
        row["verified"] = computed is not None
        if computed and row.get("status") != "critical":
            row["status"] = computed
        out.append(row)
    return out


_DATE = re.compile(r"(?:Collected|Collection Date|Date)\s*:?\s*(\d{4}-\d{2}-\d{2})", re.I)


def report_date(text: str) -> str | None:
    m = _DATE.search(text or "")
    return m.group(1) if m else None


def parse_table(text: str) -> list[dict]:
    """Read 'test | value | unit | range' rows without the LLM: exact and instant."""
    rows = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) == 4 and _NUM.fullmatch(parts[1]):
            status = check_status(parts[1], parts[3]) or "unknown"
            rows.append({"test": parts[0], "value": parts[1], "unit": parts[2], "reference_range": parts[3], "status": status})
    return rows


def seed_demo_profile():
    """First run: create the fictional demo patient with two earlier reports (parsed without the LLM)."""
    if store.load():
        return
    pid = store.new_id()
    profile = {"id": pid, "name": "Jordan Rivera (demo)", "age": 42, "sex": "female",
               "medications": ["atorvastatin", "omeprazole"], "conditions": ["high cholesterol"], "reports": {}}
    for pdf in sorted((ROOT / "samples" / "history").glob("*.pdf")):
        text, _ = extract_report(pdf.read_bytes(), pdf.name)
        results = [r | {"verified": True, "meaning": ""} for r in parse_table(text)]
        flagged = [r for r in results if r["status"] not in ("normal", "unknown")]
        rid = store.new_id()
        profile["reports"][rid] = {
            "id": rid, "date": report_date(text), "filename": pdf.name, "text": text, "stats": {},
            "analysis": {
                "report_type": "Lab report (imported)",
                "summary": "Earlier report imported into this profile. Ask the agent to compare it with newer results.",
                "urgency": "see_doctor_soon" if flagged else "routine",
                "results": results,
                "key_findings": [f"{r['test']} is {r['status']}: {r['value']} {r['unit']} (normal {r['reference_range']})" for r in flagged],
                "questions_for_doctor": [],
            },
        }
    store.save({pid: profile})


async def ollama_chat(messages: list[dict], **kw) -> dict:
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{OLLAMA_URL}/api/chat", json={"model": MODEL, "messages": messages, "stream": False, **kw})
    if r.status_code != 200:
        raise HTTPException(502, f"Local model error: {r.text[:300]}")
    return r.json()


# ---------- API ----------

@app.get("/api/status")
async def status():
    ollama_ok, model_ready = False, False
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            tags = (await client.get(f"{OLLAMA_URL}/api/tags")).json()
        ollama_ok = True
        wanted = MODEL if ":" in MODEL else MODEL + ":latest"
        model_ready = any(m["name"] == wanted for m in tags.get("models", []))
    except Exception:
        pass
    return {"internet": internet_reachable(), "ollama": ollama_ok, "model": MODEL, "model_ready": model_ready}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...), profile_id: str = Form(...)):
    data_store = store.load()
    if profile_id not in data_store:
        raise HTTPException(404, "Profile not found.")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "File too large (20 MB max).")
    text, images = extract_report(data, file.filename or "")

    t0 = time.time()
    res = {}
    parsed = parse_table(text)
    if len(parsed) >= 3:
        # a readable table: code reads the numbers (instant, exact); the model only explains them
        analysis = {"report_type": "Lab report", "summary": "", "urgency": "routine",
                    "results": [r | {"meaning": kb_meaning(r["test"])} for r in parsed],
                    "key_findings": [], "questions_for_doctor": []}
    else:
        # photo, scan or free text: MedGemma reads the report itself (retry once if the output is broken)
        msg = {"role": "user", "content": ANALYZE_PROMPT + (text or "(see attached report image)")}
        if images:
            msg["images"] = images
        analysis = None
        for temp in (0.1, 0.4):
            res = await ollama_chat([msg], format=ANALYSIS_SCHEMA,
                                    options={"temperature": temp, "num_ctx": 8192, "num_predict": 3500, "repeat_penalty": 1.1})
            try:
                analysis = json.loads(res["message"]["content"])
                break
            except (KeyError, json.JSONDecodeError):
                continue
        if analysis is None:
            raise HTTPException(502, "The model could not read this report. Try a clearer photo or a PDF.")

    analysis["results"] = clean_results(analysis.get("results", []), text)
    flagged = [r for r in analysis["results"] if r["status"] not in ("normal", "unknown")]
    if flagged and analysis.get("urgency") == "routine":
        analysis["urgency"] = "see_doctor_soon"

    res2 = {}
    # key findings come from the verified flags, so the model can't contradict the numbers
    if any(r["verified"] for r in analysis["results"]):
        order = {"critical": 0, "high": 1, "low": 1}
        analysis["key_findings"] = [
            f"{r['test']} is {r['status']}: {r['value']} {r['unit']} (normal {r['reference_range']})".replace("  ", " ")
            for r in sorted(flagged, key=lambda r: order.get(r["status"], 2))
        ] or ["All measured values are within their reference ranges."]
        # second pass: the model writes the explanation from the verified flags only
        prompt = EXPLAIN_PROMPT.format(
            flagged="\n".join(f"- {r['test']}: {r['value']} {r['unit']} (normal {r['reference_range']}) -> {r['status'].upper()}"
                              for r in flagged) or "(none)",
            normal=", ".join(r["test"] for r in analysis["results"] if r["status"] == "normal") or "none",
        )
        for temp in (0.2, 0.5):
            res2 = await ollama_chat([{"role": "user", "content": prompt}], format=EXPLAIN_SCHEMA,
                                     options={"temperature": temp, "num_ctx": 8192, "num_predict": 1500, "repeat_penalty": 1.1})
            try:
                exp = json.loads(res2["message"]["content"])
            except (KeyError, json.JSONDecodeError):
                continue
            analysis["summary"] = exp.get("summary") or analysis.get("summary")
            analysis["questions_for_doctor"] = exp.get("questions_for_doctor") or analysis.get("questions_for_doctor")
            meanings = {norm_name(e["test"]): e["meaning"] for e in exp.get("explanations", []) if e.get("test")}
            for r in flagged:
                if norm_name(r["test"]) in meanings:
                    r["meaning"] = meanings[norm_name(r["test"])]
            break

    if not analysis.get("summary"):
        n = len(flagged)
        analysis["summary"] = (f"{n} of {len(analysis['results'])} results are outside the normal range. "
                               "See the highlighted results below and ask the agent about any of them."
                               if n else "All results are within their normal ranges.")

    tokens = res.get("eval_count", 0) + res2.get("eval_count", 0)
    gen_s = (res.get("eval_duration", 0) + res2.get("eval_duration", 0)) / 1e9
    report = {
        "id": store.new_id(),
        "date": report_date(text) or time.strftime("%Y-%m-%d"),
        "filename": file.filename,
        "text": text,
        "analysis": analysis,
        "stats": {
            "seconds": round(time.time() - t0, 1),
            "tokens_per_sec": round(tokens / gen_s, 1) if gen_s else None,
            "model": MODEL,
            "bytes_sent_to_cloud": 0,
        },
    }
    # same date + same file = re-upload of the same report: replace it instead of duplicating history
    reports = data_store[profile_id]["reports"]
    for rid in [k for k, r in reports.items() if r["date"] == report["date"] and r["filename"] == report["filename"]]:
        del reports[rid]
    reports[report["id"]] = report
    store.save(data_store)
    return report_view(report)


def report_view(report: dict) -> dict:
    return {"report_id": report["id"], "filename": report["filename"], "date": report.get("date"),
            "analysis": report["analysis"], "stats": report.get("stats") or {}}


class AgentIn(BaseModel):
    report_id: str
    question: str
    history: list[dict] = []
    medications: list[str] = []


@app.post("/api/agent")
async def agent(body: AgentIn):
    """Agentic Q&A: streams NDJSON events (tool steps, then answer tokens)."""
    profile, report = store.find_report(store.load(), body.report_id)
    if not report:
        raise HTTPException(404, "Report not found. Upload it again.")
    past = store.past_reports(profile, report)
    meds = [m.strip() for m in (body.medications or profile.get("medications", [])) if m.strip()]
    opts = {"temperature": 0.2, "num_ctx": 8192, "num_predict": 600, "repeat_penalty": 1.15}

    async def llm_json(msgs, schema):
        res = await ollama_chat(msgs, format=schema, options=opts)
        try:
            return json.loads(res["message"]["content"])
        except (KeyError, json.JSONDecodeError):
            return {"tool": "final_answer", "input": ""}

    async def llm_stream(msgs):
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", f"{OLLAMA_URL}/api/chat",
                                     json={"model": MODEL, "messages": msgs, "stream": True, "options": opts}) as r:
                async for line in r.aiter_lines():
                    if line:
                        yield json.loads(line).get("message", {}).get("content", "")

    async def stream():
        t0 = time.time()
        async for event in run_agent(body.question, body.history, report, past, meds,
                                     llm_json, llm_stream, patient=store.describe(profile)):
            yield json.dumps(event) + "\n"
        yield json.dumps({"type": "done", "seconds": round(time.time() - t0, 1)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class ProfileIn(BaseModel):
    id: str | None = None
    name: str
    age: int | None = None
    sex: str | None = None
    medications: list[str] = []
    conditions: list[str] = []


@app.get("/api/profiles")
async def list_profiles():
    return [store.summary(p) for p in store.load().values()]


@app.post("/api/profiles")
async def upsert_profile(body: ProfileIn):
    data = store.load()
    pid = body.id if body.id in data else store.new_id()
    profile = data.get(pid, {"id": pid, "reports": {}})
    profile.update(
        name=body.name.strip() or "Unnamed", age=body.age, sex=body.sex,
        medications=[m.strip() for m in body.medications if m.strip()],
        conditions=[c.strip() for c in body.conditions if c.strip()],
    )
    data[pid] = profile
    store.save(data)
    return store.summary(profile)


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    data = store.load()
    data.pop(profile_id, None)
    store.save(data)
    return {"deleted": True}


@app.get("/api/profiles/{profile_id}/reports")
async def profile_reports(profile_id: str):
    p = store.load().get(profile_id)
    if not p:
        raise HTTPException(404, "Profile not found.")
    rows = [{"report_id": r["id"], "date": r.get("date"), "filename": r["filename"],
             "total": len(r["analysis"].get("results", [])),
             "flagged": sum(x["status"] not in ("normal", "unknown") for x in r["analysis"].get("results", []))}
            for r in p["reports"].values()]
    return sorted(rows, key=lambda r: r["date"] or "", reverse=True)


@app.get("/api/reports/{report_id}")
async def get_report(report_id: str):
    _, report = store.find_report(store.load(), report_id)
    if not report:
        raise HTTPException(404, "Report not found.")
    return report_view(report)


@app.delete("/api/report/{report_id}")
async def forget(report_id: str):
    data = store.load()
    profile, report = store.find_report(data, report_id)
    if report:
        del profile["reports"][report_id]
        store.save(data)
    return {"deleted": bool(report)}


@app.get("/api/sample")
async def sample():
    path = ROOT / "samples" / "sample_lab_report.pdf"
    if not path.exists():
        raise HTTPException(404, "Run: python samples/make_sample.py")
    return FileResponse(path, media_type="application/pdf", filename="sample_lab_report.pdf")


seed_demo_profile()
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
