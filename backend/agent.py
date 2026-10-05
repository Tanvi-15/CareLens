"""CareLens agent: a small plan -> tool -> observe loop that runs entirely on the local model.

Gemma-family models (incl. MedGemma) don't support Ollama's native tool calling, so each step the
model returns JSON {thought, tool, input} constrained by a schema, and we execute the tool here.
"""

import ast
import json
import operator
import re
from pathlib import Path

# Hard rule: CareLens never recommends, starts, stops, changes or doses any medicine, supplement or treatment.
SAFETY_RULES = """SAFETY RULES (must follow):
- NEVER recommend, suggest, start, stop, switch, or dose ANY medicine, supplement, vitamin or treatment.
- You may only say that a medicine the patient ALREADY takes is KNOWN to affect a lab value, as a fact to raise with their doctor.
- Never tell the patient to stop or change a medicine. Never suggest diets, supplements or treatments as fixes.
- Do not diagnose. Always point decisions to the patient's doctor or pharmacist."""

MED_ADVICE = re.compile(
    r"\b(should i (take|stop|start|switch|use|increase|decrease|reduce)|"
    r"what (medicine|medication|drug|pill|supplement|vitamin|treatment)s? should|"
    r"(which|what|recommend|suggest|prescribe)\b.*\b(medicine|medication|drug|pill|supplement|vitamin|treatment)|"
    r"\b(dose|dosage|how (much|many) (mg|pills|tablets))\b|"
    r"(stop|quit|start|switch|change) (taking|my) )",
    re.I,
)

MED_ADVICE_REPLY = (
    "I can't give advice about taking, stopping, changing or dosing any medicine, supplement or treatment. "
    "That decision has to come from your doctor or pharmacist, who knows your full history.\n\n"
    "What I **can** do:\n"
    "- Explain what your results mean\n"
    "- Show which of your current medicines are known to affect a result\n"
    "- Show how your results have changed over time\n"
    "- Help you prepare questions for your doctor"
)

DISCLAIMER = ("\n\n_This is information, not medical advice. Don't start, stop or change any medicine "
              "without talking to your doctor or pharmacist._")

KB = json.loads((Path(__file__).with_name("knowledge.json")).read_text())
MAX_STEPS = 6

TOOLS = {
    "get_results": "Read results from the current report. input: 'abnormal', 'all', or a test name.",
    "lookup_reference": "Look up a test in the offline medical reference (what it measures, causes of high/low). input: test name.",
    "check_medications": "Check whether the patient's medicines are known to affect their lab results. input: comma-separated medicine names, or 'mine' for the patient's list.",
    "compare_with_past": "Compare with the patient's earlier reports on this device. input: a test name, or 'abnormal' for trends of every out-of-range result.",
    "calculate": "Do arithmetic exactly, e.g. a ratio. input: an expression like '158/44'.",
    "final_answer": "Stop using tools and answer the patient. input: a short note of what to cover.",
}

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {"type": "string", "enum": list(TOOLS)},
        "input": {"type": "string"},
    },
    "required": ["thought", "tool", "input"],
}

SYSTEM = """You are CareLens, a medical assistant agent running fully offline on the patient's own device.
You help a patient understand THEIR lab report. You can use tools to gather facts before answering.

TOOLS:
{tools}

Each turn, reply with JSON: {{"thought": why, "tool": tool name, "input": tool input}}.
Strategy:
- Gather facts first: check the relevant results, look up references, check medicines if the patient takes any,
  and compare with past reports when a trend would help.
- Do not repeat the same tool call. Use at most {max_steps} tools, then choose "final_answer".
- Never invent values.

{safety}

Patient: {patient}
Patient's medicines: {meds}
Out-of-range results in this report (use these exact test names with tools): {flagged}
Earlier reports on this device: {past}
"""

FINAL = """Now write the answer for the patient using ONLY the observations above plus general knowledge.
Plain English, warm and calm, under 180 words. Use short bullet points where helpful.
Mention concrete facts you found: values, medicine effects, and if a trend was found, quote the numbers over time (e.g. "41 -> 22 -> 9").
Use the OUT-OF-RANGE RESULTS above exactly. Only mention results relevant to the question (never call an out-of-range result normal), and copy trend labels
exactly as given in brackets (RISING / FALLING / ROUGHLY STABLE); never re-judge a trend direction yourself.
End with what to discuss with their doctor.
If medicines came up, describe them ONLY as "known to affect" a value, and say to discuss it with their doctor.
"""


# ---------- helpers ----------

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


SYNONYMS = {
    "iron": "ferritin", "iron stores": "ferritin", "liver enzyme": "alt", "liver": "alt", "sgpt": "alt",
    "a1c": "hemoglobin a1c", "hba1c": "hemoglobin a1c", "hgba1c": "hemoglobin a1c", "sugar": "glucose",
    "blood sugar": "glucose", "ldl": "ldl cholesterol", "hdl": "hdl cholesterol", "cholesterol": "total cholesterol",
    "thyroid": "tsh", "kidney": "creatinine", "wbc": "white blood cells", "hgb": "hemoglobin", "hct": "hematocrit",
}


def best_key(name: str, keys) -> str | None:
    """Match a test name to a known key: exact match first, then the longest overlapping key."""
    name = re.sub(r"\((?:high|low|normal|critical)\)", "", name or "", flags=re.I)
    name = SYNONYMS.get(norm(name), name)
    n = f" {norm(name)} "
    keys = list(keys)
    if n.strip() in keys:
        return n.strip()
    hits = [k for k in keys if f" {k} " in n or n.strip() in k]
    return max(hits, key=len) if hits else None


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg}


def safe_eval(expr: str) -> float:
    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("only numbers and + - * / ** allowed")
    return ev(ast.parse(expr, mode="eval").body)


# ---------- tools ----------

def run_tool(tool: str, arg: str, report: dict, history: list[dict], meds: list[str]) -> str:
    results = report["analysis"].get("results", [])
    arg = (arg or "").strip()

    if tool == "get_results":
        if arg.lower() in ("", "all"):
            rows = results
        elif arg.lower() == "abnormal":
            rows = [r for r in results if r["status"] not in ("normal", "unknown")]
        else:
            key = best_key(arg, [norm(r["test"]) for r in results])
            rows = [r for r in results if norm(r["test"]) == key]
        if not rows:
            return f"No result matching '{arg}' in this report."
        return "\n".join(f"{r['test']}: {r['value']} {r['unit']} (normal {r['reference_range']}) -> {r['status']}" for r in rows)

    if tool == "lookup_reference":
        key = best_key(arg, KB["tests"])
        if not key:
            return (f"'{arg}' is not in the offline reference. Use one of the report's test names: "
                    + ", ".join(r["test"] for r in results))
        info = KB["tests"][key]
        return f"[Offline reference: {key}] " + " ".join(f"{k.upper()}: {v}" for k, v in info.items())

    if tool == "check_medications":
        names = meds if arg.lower() in ("", "mine", "my medications") else [m.strip() for m in arg.split(",")]
        if not names:
            return "The patient has not listed any medicines."
        flagged = {norm(r["test"]) for r in results if r["status"] not in ("normal", "unknown")}
        lines = []
        for name in names:
            med = KB["medications"].get(norm(name).split(" ")[0])
            if not med:
                lines.append(f"{name}: not in the offline drug reference.")
                continue
            for test, effect in med["affects"].items():
                hit = any(test == f or f" {test} " in f" {f} " for f in flagged)
                lines.append(f"{name} ({med['class']}) -> {test}: {effect}" + (" [THIS RESULT IS OUT OF RANGE]" if hit else ""))
        return "\n".join(lines)

    if tool == "compare_with_past" and arg.lower() in ("abnormal", "all", "flagged"):
        names = [r["test"] for r in results if r["status"] not in ("normal", "unknown")]
        lines = [run_tool(tool, n, report, history, meds) for n in names]
        lines = [f"{n}: {l.removeprefix('Trend: ')}" for n, l in zip(names, lines) if l.startswith("Trend")]
        return "\n".join(lines) or "No earlier reports on this device to compare with."

    if tool == "compare_with_past":
        points = []
        for past in history:
            key = best_key(arg, [norm(r["test"]) for r in past["results"]])
            for r in past["results"]:
                if key and norm(r["test"]) == key:
                    points.append(f"{past['date']}: {r['value']} {r['unit']}")
        current = best_key(arg, [norm(r["test"]) for r in results])
        for r in results:
            if current and norm(r["test"]) == current:
                points.append(f"{report.get('date') or 'this report'} (current): {r['value']} {r['unit']}")
        if len(points) < 2:
            return f"No earlier reports on this device include '{arg}'."
        # each point looks like "2025-09-15: 41 ng/mL"; take the number after the date
        nums = [float(m.group()) for p in points if (m := re.search(r"-?\d+(?:\.\d+)?", p.split(": ", 1)[1]))]
        change = (nums[-1] - nums[0]) / abs(nums[0]) if len(nums) >= 2 and nums[0] else 0
        direction = "RISING" if change > 0.05 else "FALLING" if change < -0.05 else "ROUGHLY STABLE"
        return "Trend: " + " -> ".join(points) + f"  [{direction}, {change:+.0%} overall]"

    if tool == "calculate":
        try:
            return f"{arg} = {round(safe_eval(arg), 3)}"
        except Exception as e:
            return f"Could not calculate: {e}"

    return f"Unknown tool '{tool}'."


async def dedupe_lines(chunks):
    """Stream the answer line by line and stop if the model starts repeating itself (small models can loop)."""
    seen, buf = set(), ""
    async for chunk in chunks:
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            key = norm(line)
            if len(key) > 15 and key in seen:
                return
            seen.add(key)
            yield line + "\n"
    if buf and norm(buf) not in seen:
        yield buf


# ---------- loop ----------

async def run_agent(question, chat_history, report, history, meds, llm_json, llm_stream, patient=""):
    """Yield events: {"type": "step", ...} for each tool call, then {"type": "token"} chunks of the answer."""
    if MED_ADVICE.search(question):
        # blocked in code, before the model sees it: no medicine advice, ever
        yield {"type": "step", "n": 1, "tool": "safety_check", "input": "medicine advice request",
               "thought": "This question asks for medicine or treatment advice, which CareLens never gives.",
               "observation": "Blocked: answered with the standard safety response."}
        yield {"type": "answer_start"}
        yield {"type": "token", "text": MED_ADVICE_REPLY}
        return

    system = SYSTEM.format(
        tools="\n".join(f"- {k}: {v}" for k, v in TOOLS.items()),
        max_steps=MAX_STEPS,
        safety=SAFETY_RULES,
        patient=patient or "not given",
        meds=", ".join(meds) if meds else "none listed",
        flagged="; ".join(f'"{r["test"]}" ({r["status"]})' for r in report["analysis"].get("results", [])
                          if r["status"] not in ("normal", "unknown")) or "none",
        past=", ".join(h["date"] for h in history) or "none",
    )
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in chat_history[-6:]]
    msgs.append({"role": "user", "content": question})

    seen, notes = set(), []
    for step in range(MAX_STEPS):
        decision = await llm_json(msgs, STEP_SCHEMA)
        tool, arg = decision.get("tool", "final_answer"), decision.get("input", "")
        if tool == "final_answer" or (tool, arg.lower()) in seen:
            break
        seen.add((tool, arg.lower()))
        observation = run_tool(tool, arg, report, history, meds)
        yield {"type": "step", "n": step + 1, "thought": decision.get("thought", ""), "tool": tool,
               "input": arg, "observation": observation}
        notes.append(f"[{tool}({arg})]\n{observation}")
        msgs.append({"role": "assistant", "content": json.dumps(decision)})
        msgs.append({"role": "user", "content": f"OBSERVATION from {tool}:\n{observation}\n\nNext step?"})

    # final answer from a fresh plain-text conversation, so the model doesn't keep imitating the JSON steps
    checked = "\n".join(f"- {r['test']}: {r['value']} {r['unit']} -> {r['status'].upper()}"
                         for r in report["analysis"].get("results", [])
                         if r["status"] not in ("normal", "unknown")) or "(all results normal)"
    final = [
        {"role": "system", "content": "You are CareLens, a careful medical assistant running offline on the patient's device.\n" + SAFETY_RULES},
        {"role": "user", "content": f"Patient: {patient}\nPatient's question: {question}\n\n"
                                    f"OUT-OF-RANGE RESULTS (checked by code, always correct; every other result is normal):\n{checked}\n\nFacts gathered by your tools:\n"
                                    + ("\n\n".join(notes) or "(none)") + "\n\n" + FINAL},
    ]
    yield {"type": "answer_start"}
    async for line in dedupe_lines(llm_stream(final)):
        yield {"type": "token", "text": line}
    if any(t == "check_medications" for t, _ in seen) or meds:
        yield {"type": "token", "text": DISCLAIMER}
