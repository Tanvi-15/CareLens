"""Generate SYNTHETIC lab report PDFs for demos. The patient and all values are fictional."""

from pathlib import Path

import pymupdf

OUT = Path(__file__).with_name("sample_lab_report.pdf")

PANELS = [
    ("COMPLETE BLOOD COUNT (CBC)", [
        ("Hemoglobin", "11.2", "g/dL", "12.0 - 15.5"),
        ("Hematocrit", "34.1", "%", "35.0 - 45.0"),
        ("White Blood Cells", "7.4", "10^3/uL", "4.0 - 11.0"),
        ("Platelets", "265", "10^3/uL", "150 - 400"),
        ("MCV", "76", "fL", "80 - 100"),
    ]),
    ("COMPREHENSIVE METABOLIC PANEL", [
        ("Glucose, Fasting", "112", "mg/dL", "70 - 99"),
        ("Creatinine", "0.9", "mg/dL", "0.6 - 1.1"),
        ("Sodium", "139", "mmol/L", "135 - 145"),
        ("Potassium", "4.2", "mmol/L", "3.5 - 5.1"),
        ("ALT", "58", "U/L", "7 - 35"),
    ]),
    ("LIPID PANEL", [
        ("Total Cholesterol", "236", "mg/dL", "< 200"),
        ("LDL Cholesterol", "158", "mg/dL", "< 100"),
        ("HDL Cholesterol", "44", "mg/dL", "> 40"),
        ("Triglycerides", "171", "mg/dL", "< 150"),
    ]),
    ("OTHER", [
        ("Hemoglobin A1c", "6.1", "%", "4.0 - 5.6"),
        ("TSH", "2.1", "mIU/L", "0.4 - 4.0"),
        ("Vitamin D, 25-OH", "18", "ng/mL", "30 - 100"),
        ("Ferritin", "9", "ng/mL", "15 - 150"),
    ]),
]

# older reports for the same fictional patient, so the agent can show trends
HISTORY = {
    "2025-09-15": {"Hemoglobin": "13.1", "Hematocrit": "39.0", "MCV": "84", "Glucose, Fasting": "96", "ALT": "24",
                   "Total Cholesterol": "241", "LDL Cholesterol": "165", "Triglycerides": "158",
                   "Hemoglobin A1c": "5.6", "Vitamin D, 25-OH": "24", "Ferritin": "41"},
    "2026-03-20": {"Hemoglobin": "12.4", "Hematocrit": "36.8", "MCV": "81", "Glucose, Fasting": "104", "ALT": "33",
                   "Total Cholesterol": "229", "LDL Cholesterol": "151", "Triglycerides": "162",
                   "Hemoglobin A1c": "5.8", "Vitamin D, 25-OH": "21", "Ferritin": "22"},
}


def main():
    build(OUT, "2026-09-28", {})
    hist = OUT.parent / "history"
    hist.mkdir(exist_ok=True)
    for date, overrides in HISTORY.items():
        build(hist / f"lab_report_{date}.pdf", date, overrides)


def build(out, date, overrides):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 50

    def text(x, s, size=9, bold=False, color=(0, 0, 0)):
        page.insert_text((x, y), s, fontsize=size, fontname="hebo" if bold else "helv", color=color)

    text(40, "NORTHFIELD COMMUNITY LABORATORY", 15, True, (0.1, 0.25, 0.5))
    y += 16
    text(40, "SAMPLE REPORT - SYNTHETIC DATA FOR DEMONSTRATION ONLY", 8, True, (0.75, 0.1, 0.1))
    y += 26
    for left, right in [
        ("Patient: Jordan Rivera (fictional)", f"Collected: {date} 07:42"),
        ("DOB: 1984-03-12   Sex: F", f"Reported: {date}"),
        ("MRN: 00-000-0000 (synthetic)", "Ordering: Dr. A. Patel (fictional)"),
    ]:
        text(40, left)
        text(340, right)
        y += 13
    y += 8
    page.draw_line((40, y), (572, y), color=(0.6, 0.6, 0.6))
    y += 18

    for title, rows in PANELS:
        text(40, title, 10, True, (0.1, 0.25, 0.5))
        y += 14
        for x, h in [(40, "TEST"), (250, "RESULT"), (330, "UNIT"), (420, "REFERENCE RANGE")]:
            text(x, h, 8, True, (0.4, 0.4, 0.4))
        y += 12
        for name, val, unit, ref in rows:
            text(40, name)
            text(250, overrides.get(name, val))
            text(330, unit)
            text(420, ref)
            y += 13
        y += 10

    page.draw_line((40, y), (572, y), color=(0.6, 0.6, 0.6))
    y += 16
    text(40, "Fasting specimen. Results should be interpreted by a licensed clinician in clinical context.", 8)
    doc.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
