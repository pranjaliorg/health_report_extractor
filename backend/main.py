from flask import Flask, request, jsonify
from flask_cors import CORS
from pypdf import PdfReader
from datetime import datetime
import json
from db import SessionLocal
from models import Report
import io
import re

app = Flask(__name__)
app.json.sort_keys = False
CORS(app)


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return ""

    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(text: str) -> str:
    remove_exact = {"Ashok One Hospital", "2249397070"}
    remove_contains = {
        "Sadguru Heights 1, Ashokvan, Dahisar East",
        "Maharashtra, India",
    }

    cleaned = []
    for line in text.splitlines():
        l = line.strip()
        low = l.lower()

        if not l:
            continue
        if l in remove_exact:
            continue
        if any(x in l for x in remove_contains):
            continue

        if re.match(r"^page\s*\|\s*\d+\s*$", low):
            continue
        if re.match(r"^\d+\s*/\s*\d+\s*$", low):
            continue

        cleaned.append(l)

    out = "\n".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out

def to_iso_datetime(s):
    if not s:
        return None

    s = re.sub(r"\s+", " ", s).strip()

    m = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{4})(?:\s+(\d{1,2}:\d{2})\s*(AM|PM|am|pm)?)?", s)
    if not m:
        return None

    date_part = m.group(1).replace("-", "/")
    time_part = m.group(2)
    ampm = m.group(3)

    try:
        if time_part and ampm:
            dt = datetime.strptime(f"{date_part} {time_part} {ampm}", "%d/%m/%Y %I:%M %p")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        elif time_part:
            dt = datetime.strptime(f"{date_part} {time_part}", "%d/%m/%Y %H:%M")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(date_part, "%d/%m/%Y")
            return dt.strftime("%Y-%m-%d")
    except:
        return None

def build_json(t: str) -> dict:
    def clean_val(x):
        if x is None:
            return None
        x = re.sub(r"\s+", " ", x).strip()
        return None if x in ("---", "-", "") else x

    def find(pattern, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
        m = re.search(pattern, t, flags)
        return clean_val(m.group(1)) if m else None

    def section(start, end=None):
        s = re.search(start, t, re.IGNORECASE)
        if not s:
            return ""
        start_i = s.end()
        if end:
            e = re.search(end, t[start_i:], re.IGNORECASE)
            end_i = start_i + e.start() if e else len(t)
        else:
            end_i = len(t)
        return t[start_i:end_i].strip()

    def list_section(start, end):
        blk = section(start, end)
        if not blk:
            return []
        return [ln.strip() for ln in blk.splitlines() if ln.strip()]

    def normalize_block_text(block: str) -> str:
        block = re.sub(r"\s*\n\s*", " ", block)
        block = re.sub(r"\s+", " ", block).strip()
        return block

    def parse_provisional(block: str):
        out = []
        if not block:
            return out

        lines = [ln.strip(" ,") for ln in block.splitlines() if ln.strip()]
        sr = 1

        for ln in lines:
            ln = re.sub(r"^Provisional Diagnosis\s*[:-]\s*", "", ln, flags=re.IGNORECASE).strip()

            m1 = re.match(r"^(\d+)\s+(.*?)-\s*([A-Z]\d[\w\.]*)$", ln)
            if m1:
                out.append(
                    {"sr_no": int(m1.group(1)), "diagnosis": clean_val(m1.group(2)), "icd_code": clean_val(m1.group(3))}
                )
                sr = int(m1.group(1)) + 1
                continue

            m2 = re.match(r"^(.*?)-\s*([A-Z]\d[\w\.]*)$", ln)
            if m2:
                out.append(
                    {"sr_no": sr, "diagnosis": clean_val(m2.group(1).lstrip("- ").strip()), "icd_code": clean_val(m2.group(2))}
                )
                sr += 1

        return out

    def parse_diagnosis(block: str):
        out = []
        if not block:
            return out

        lines = [ln.strip(" ,") for ln in block.splitlines() if ln.strip()]
        sr = 1

        for ln in lines:
            m1 = re.match(r"^(\d+)\s+(.*?)-\s*([A-Z]\d[\w\.]*)$", ln)
            if m1:
                out.append(
                    {
                        "sr_no": int(m1.group(1)),
                        "diagnosis": clean_val(m1.group(2).lstrip("- ").strip()),
                        "icd_code": clean_val(m1.group(3)),
                    }
                )
                sr = int(m1.group(1)) + 1
                continue

            m2 = re.match(r"^(.*?)-\s*([A-Z]\d[\w\.]*)$", ln)
            if m2:
                out.append(
                    {"sr_no": sr, "diagnosis": clean_val(m2.group(1).lstrip("- ").strip()), "icd_code": clean_val(m2.group(2))}
                )
                sr += 1

        seen = set()
        unique = []
        for d in out:
            key = ((d.get("diagnosis") or "").lower(), (d.get("icd_code") or "").lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(d)

        for i, d in enumerate(unique, start=1):
            d["sr_no"] = i

        return unique

    def parse_schedule(line: str):
        m = re.match(r"^\s*(\d+(?:/\d+)?)\s*-\s*(\d+(?:/\d+)?)\s*-\s*(\d+(?:/\d+)?)\s*(.*)$", line)
        if not m:
            return None

        dose_schedule = f"{m.group(1)} - {m.group(2)} - {m.group(3)}"
        rest = m.group(4).strip()
        parts = [p.strip() for p in rest.split(" - ")]

        timing = parts[0] if len(parts) >= 1 else None
        frequency = parts[1] if len(parts) >= 2 else None

        duration_days = None
        quantity = None
        if len(parts) >= 3:
            last = parts[2]
            md = re.search(r"(\d+)\s*Day\(s\)", last, re.IGNORECASE)
            if md:
                duration_days = int(md.group(1))
            mq = re.search(r"Day\(s\)\s*(\d+)\s*$", last, re.IGNORECASE)
            if mq:
                quantity = int(mq.group(1))

        return {
            "dose_schedule": dose_schedule,
            "timing": timing,
            "frequency": frequency,
            "duration_days": duration_days,
            "quantity": quantity,
        }
    
    def infer_unit(name: str):
        s = (name or "").upper()
        if "OINT" in s or "OINTMENT" in s:
            return "ointment"
        if "GEL" in s:
            return "gel"
        if "CAP" in s or "CAPSULE" in s:
            return "capsule"
        if "TAB" in s or "TABLET" in s:
            return "tablet"
        return "dose"

    def interpret_schedule(dose_schedule: str, unit="tablet"):
        if not dose_schedule:
            return None
        parts = [p.strip() for p in dose_schedule.split("-")]
        if len(parts) != 3:
            return None
        times = ["morning", "afternoon", "night"]
        phrases= []
        verb = "apply" if unit in ("ointment", "gel") else "take"

        for i, tok in enumerate(parts):
            if tok in ("0", "0.0", ""):
                continue
            qty = tok
            if tok in ("0.5", "1/2"):
                qty = "1/2"
            else:
                try:
                    qty = str(int(float(tok))) if float(tok).is_integer() else tok
                except:
                    qty = tok

            if verb == "apply":
                phrases.append(f"Apply in {times[i]}")
            else:
                phrases.append(f"Take {qty} {unit} in {times[i]}")        

        return ", ".join(phrases) if phrases else None

    def parse_drug_advice(block: str):
        out = []
        if not block:
            return out

        raw_lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        raw_lines = [ln for ln in raw_lines if not re.match(r"^No\s+Name\s+Dose", ln, re.IGNORECASE)]

        items, cur = [], []
        for ln in raw_lines:
            if re.match(r"^\d+\s+[A-Z]", ln) and cur:
                items.append(cur)
                cur = [ln]
            else:
                cur.append(ln)
        if cur:
            items.append(cur)

        for item in items:
            m0 = re.match(r"^(\d+)\s+(.*)$", item[0])
            if not m0:
                continue

            sr_no = int(m0.group(1))
            first_line = m0.group(2).strip()

            name_line = first_line
            schedule_line = None
            notes = None
            composition_lines = []

            if ("Day(s)" in first_line or "Day(S)" in first_line) and re.search(r"\b(Daily|Weekly|Monthly)\b", first_line, re.IGNORECASE):
                dose_pos = re.search(r"\d+(?:/\d+)?\s*-\s*\d+(?:/\d+)?\s*-\s*\d+(?:/\d+)?", first_line)
                if dose_pos:
                    name_line = first_line[:dose_pos.start()].strip()
                    schedule_line = first_line[dose_pos.start():].strip()

            for ln in item[1:]:
                if ln.lower().startswith("notes:"):
                    notes = clean_val(ln.replace("Notes:", "").strip())
                    continue

                if ("Day(s)" in ln or "Day(S)" in ln) and re.search(r"\b(Daily|Weekly|Monthly)\b", ln, re.IGNORECASE):
                    schedule_line = ln.strip()
                    continue

                composition_lines.append(ln)

            if composition_lines and composition_lines[0].strip().upper() == "MG":
                if not re.search(r"\bMG\b", name_line, re.IGNORECASE):
                    name_line = (name_line + " MG").strip()
                composition_lines = composition_lines[1:]

            dose_schedule = timing = frequency = duration_days = quantity = None
            if schedule_line:
                parsed = parse_schedule(schedule_line)
                if parsed:
                    dose_schedule = parsed["dose_schedule"]
                    timing = parsed["timing"]
                    frequency = parsed["frequency"]
                    duration_days = parsed["duration_days"]
                    quantity = parsed["quantity"]

            if quantity is None:
                for c in composition_lines:
                    if re.match(r"^\d+$", c.strip()):
                        quantity = int(c.strip())
                        break

            composition_lines = [c for c in composition_lines if not re.match(r"^\d+$", c.strip())]
            composition = clean_val(" ".join(composition_lines))

            unit = infer_unit(name_line)
            dose_schedule_text = interpret_schedule(dose_schedule, unit)

            out.append(
                {
                    "sr_no": sr_no,
                    "name": clean_val(name_line),
                    "composition": composition,
                    "dose_schedule": dose_schedule,
                    "dose_schedule_text": dose_schedule_text,
                    "timing": timing,
                    "frequency": frequency,
                    "duration_days": duration_days,
                    "quantity": quantity,
                    "notes": notes,
                }
            )

        return out

    def parse_discharge_notes():
        raw = section(r"Discharge notes\s*:", r"Complaints On Admission\s*:")
        raw = normalize_block_text(raw)
        if not raw:
            return {"raw": None, "investigation_advice": {"tests": [], "test_date": None},
                    "follow_up": {"date": None, "time": None, "doctor": None, "location": None},
                    "red_flags": [], "emergency_contacts": []}

        s = re.sub(r"\s+", " ", raw).strip()

        date_re = r"(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"

        m_adv = re.search(r"\bAdvice\s+(.*?)(?=\.\s*On\s+" + date_re + r"|\b(F/U|Follow\s*up)\b|Please\s+contact|$)", s, re.IGNORECASE)
        adv = (m_adv.group(1).strip(" ,.") if m_adv else "")

        m_test_date = re.search(r"\bon\s+" + date_re, adv, re.IGNORECASE)
        test_date = m_test_date.group(1) if m_test_date else None
        if test_date:
            adv = re.sub(r"\bon\s+" + date_re, "", adv, flags=re.IGNORECASE).strip(" ,.")

        adv = adv.replace(". ", ", ")
        adv = re.sub(r"\s*-\s*", "-", adv)
        tests = [x.strip(" ,.") for x in adv.split(",") if x.strip(" ,.")]

        fu = re.search(r"(\bOn\s+" + date_re + r".*?\bfollow\s*up\b.*?|\b(F/U|Follow\s*up)\b.*?)(?=Please\s+contact|$)", s, re.IGNORECASE)
        fu = fu.group(0) if fu else ""

        fu_date = None

        p1 = re.search(r"\bOn\s+(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})\s+follow\s*up\b", fu, re.I)
        p2 = re.search(r"\bfollow\s*up\b.*?\bon\s+(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})\b", fu, re.I)
        p3 = re.search(r"\bF/U\b.*?\bon\s+(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})\b", fu, re.I)

        if p1:
            fu_date = p1.group(1)
        elif p2:
            fu_date = p2.group(1)
        elif p3:
            fu_date = p3.group(1)
        else:
            any_d = re.findall(r"(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})", fu, flags=re.I)
            fu_date = any_d[-1] if any_d else None

        m_time = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm))|@\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", fu)
        fu_time = (m_time.group(1) or m_time.group(2)).strip() if m_time else None

        m_doc = re.search(r"\bDr\.?\s*([A-Za-z\.\s]+?)(?:'s\s*OPD|\bOPD\b)", fu, re.IGNORECASE)
        fu_doctor = ("Dr. " + m_doc.group(1).strip()).replace("Dr. Dr.", "Dr.") if m_doc else None

        m_loc = re.search(r"\b(?:at|in)\s+([A-Za-z ]+Hospital)\b", fu, re.IGNORECASE)
        fu_loc = m_loc.group(1).strip() if m_loc else None

        contacts = []
        for ph in re.findall(r"\b\d{8,13}\b", s):
            if ph not in contacts:
                contacts.append(ph)

        m_rf = re.search(r"\bPlease\s+contact.*?\bif\b\s+(.+)$", s, re.IGNORECASE)
        red_flags = []
        if m_rf:
            txt = m_rf.group(1).strip(" ,.")
            txt = re.sub(r"\bat\s+.*$", "", txt, flags=re.IGNORECASE).strip(" ,.")
            txt = txt.replace(" or ", ", ")
            red_flags = [x.strip(" ,.") for x in txt.split(",") if x.strip(" ,.")]

        return {
            "raw": s,
            "investigation_advice": {"tests": tests, "test_date": test_date},
            "follow_up": {"date": fu_date, "time": fu_time, "doctor": fu_doctor, "location": fu_loc},
            "red_flags": red_flags,
            "emergency_contacts": contacts,
        }

    def parse_complaints():
        lines = list_section(r"Complaints On Admission\s*:", r"Medical History|Vital on Admission\s*:")
        if not lines:
            return []
        txt = re.sub(r"\s+", " ", " ".join(lines)).strip()
        return [p.strip() for p in re.split(r"\s*,\s*", txt) if p.strip()]

    def parse_medical_history():
        block = section(r"Medical History\s*:", r"Treatment Given\s*:")
        if not block:
            return []
        txt = re.sub(r"\s+", " ", block).strip().replace(",,", ",")
        return [p.strip(" ,") for p in txt.split(",") if p.strip()]

    def parse_treatment_given():
        block = section(r"Treatment Given\s*:", r"Investigation\s*:")
        if not block:
            return []
        txt = re.sub(r"\s+", " ", block).strip().replace(",,", ",")
        return [p.strip(" ,") for p in txt.split(",") if p.strip()]

    def parse_course_in_hospital():
        block = section(r"Course In Hospital\s*:", r"Advice On Discharge\s*:")
        block = normalize_block_text(block)
        if not block:
            return []
        parts = re.split(r"\.\s+", block)
        return [p.strip() + ("." if p and not p.strip().endswith(".") else "") for p in parts if p.strip()]

    def parse_advice_on_discharge():
        block = section(r"Advice On Discharge\s*:", r"Diet Advice|Condition of patient at Discharge\s*:")
        block = normalize_block_text(block)
        if not block:
            return []
        block = re.sub(r"\s*,\s*,\s*", ", ", block).strip(" ,")
        return [block] if block else []

    def parse_diet_advice():
        block = section(r"Diet Advice\s*:", r"Condition of patient at Discharge\s*:")
        block = normalize_block_text(block)
        if not block:
            return []
        parts = re.split(r"\.\s+", block)
        return [p.strip() + ("." if p and not p.strip().endswith(".") else "") for p in parts if p.strip()]
    
    def parse_vitals_on_discharge():
        blk = section(r"VITAL\s+ON\s+DISCHARGE\s*:", r"Follow\s*Up|Signature")
        if not blk:
            return None

        s = re.sub(r"\s+", " ", blk)

        def extract(pattern):
            m = re.search(pattern, s, re.IGNORECASE)
            return clean_val(m.group(1)) if m else None

        return {
                "weight": extract(r"\bW\s*:\s*([0-9.]+\s*KG)\b"),
                "hemo_glucose_test": extract(r"HGT:\s*([0-9/]+)"),
                "blood_pressure": extract(r"BP:\s*([0-9/]+)"),
                "heart_rate": extract(r"HR:\s*([0-9]+)"),
                "temperature": extract(r"TEMPERATURE:\s*([0-9\.]+)\s*°?\s*F"),
                "respiratory_rate": extract(r"RR:\s*([0-9]+)"),
                "spo2": extract(r"SPO2:\s*([0-9]+%?)"),
                "sugar": extract(r"SUGAR:\s*([0-9]+)\s*MG/DL"),
            }

    def parse_investigation():
        inv_block = section(r"Investigation\s*:", r"Course In Hospital\s*:")
        out = []
        if not inv_block:
            return out

        current = None
        for ln in [x.strip() for x in inv_block.splitlines() if x.strip()]:
            m = re.match(r"^(\d{2}/\d{2}/\d{4})\s*[:-]\s*(.+)$", ln)
            if m:
                if current:
                    out.append(current)
                current = {"date": m.group(1), "tests": [x.strip() for x in m.group(2).split(",") if x.strip()]}
            else:
                if current:
                    current["tests"] += [x.strip() for x in ln.split(",") if x.strip()]
        if current:
            out.append(current)
        return out

    def parse_signatures():
        sig_block = section(r"Signature\s*", r"Prepared By\s*:")
        if not sig_block:
            return []
        lines = [ln.strip() for ln in sig_block.splitlines() if ln.strip()]
        doctor_lines = [ln for ln in lines if ln.startswith("Dr.")]
        names = []
        for line in doctor_lines:
            for p in line.split("Dr."):
                p = p.strip()
                if p:
                    names.append("Dr. " + p)
        return names

    def parse_procedure():
        blk = section(r"Procedure\s*:\s*", r"Diagnosis|VITAL\s+ON\s+ADMISSION|L/E|General\s*Examination|Complaints|Medical\s*History|Treatment|Investigation|Course|Advice|Diet|Discharge\s*Notes")
        blk = normalize_block_text(blk)
        return clean_val(blk) if blk else None
    
    def parse_vitals_on_admission():
        blk = section(r"VITAL\s+ON\s+ADMISSION\s*:", r"General\s*Examination|L/E|Systematic|Systemic|Complaints|Medical\s*History|Diagnosis")
        if not blk:
            return None

        s = re.sub(r"\s+", " ", blk)

        def g(p):
            m = re.search(p, s, re.IGNORECASE)
            return clean_val(m.group(1)) if m else None

        return {
            "weight": g(r"\bW\s*:\s*([0-9.]+\s*KG)\b"),
            "blood_pressure": g(r"\bBP\s*:\s*([0-9/]+)"),
            "heart_rate": g(r"\bHR\s*:\s*([0-9]+/?MIN)"),
            "temperature": g(r"\bTEMPERATURE\s*:\s*([0-9.]+)\s*°?\s*F"),
            "respiratory_rate": g(r"\bRR\s*:\s*([0-9]+)"),
            "spo2": g(r"\bSPO2\s*:\s*([0-9]+%?)"),
        }

    def parse_kv_block(text: str):
        if not text:
            return {}

        s = re.sub(r"\s+", " ", text).strip()

        m = re.search(r"\((.+?)\)", s)
        core = m.group(1) if m else s

        core = core.replace(" | ", ", ")
        parts = [p.strip(" ,") for p in core.split(",") if p.strip(" ,")]

        out = {}
        for p in parts:
            p = re.sub(r"\s+", " ", p).strip()
            m1 = re.match(r"^([^:]+)\s*:\s*(.+)$", p)
            if m1:
                k = clean_val(m1.group(1))
                v = clean_val(m1.group(2))
                if k and v:
                    out[k] = v
                continue

            m2 = re.match(r"^([^:-]+)\s*-\s*(.+)$", p)
            if m2:
                k = clean_val(m2.group(1))
                v = clean_val(m2.group(2))
                if k and v:
                    out[k] = v
                continue

        return out
    
    def parse_local_examination():
        blk = section(r"\bL/E\s*:\s*", r"General\s*Examination|Systematic|Systemic|Diagnosis|Complaints|Medical\s*History|Treatment|Investigation|Course|Advice|Diet")
        if not blk:
            return None 
        return parse_kv_block(blk)
    
    def parse_general_examination():
        blk = section(r"General\s*examination\s*:?", r"Systematic\s*examination\s*:|Systemic\s*examination\s*:|Pain\s*assessment|Diagnosis|Drug Advice|Discharge notes")
        blk = normalize_block_text(blk)    
        if not blk:
            return None 
        return parse_kv_block(blk) if blk else None
    
    def parse_systemic_examination():
        blk = section(r"(Systematic|Systemic)\s*examination\s*:?", r"Pain\s*assessment|Diagnosis|Drug Advice|Discharge notes")
        blk = normalize_block_text(blk)
        if not blk:
            return None 
        return parse_kv_block(blk) if blk else None
    
    def parse_pain_assessment():
        blk = section(r"Pain\s*assessment\s*:?", r"Diagnosis|Drug Advice|Discharge notes|Investigation|Course In Hospital|Advice On Discharge|Diet Advice|Medical History")
        blk = normalize_block_text(blk)
        if not blk:
            return None
        blk = re.sub(r"^Pain\s*assessment\s*:?", "", blk, flags=re.IGNORECASE).strip(" :-")
        return clean_val(blk) if blk else None
    
    def parse_next_follow_up():
        blk = section(r"Follow\s*Up\s*", r"Signature|Prepared By|Diagnosis|Drug Advice")
        if not blk:
            return {"date": None, "time": None, "doctor": None, "location": None}

        s = re.sub(r"\s+", " ", blk)

        date_re = r"(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"
        m_date = re.search(date_re, s)
        fu_date = m_date.group(1) if m_date else None

        m_time = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm))|@\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", s)
        fu_time = (m_time.group(1) or m_time.group(2)).strip() if m_time else None

        m_doc = re.search(r"\bDr\.?\s*([A-Za-z\.\s]+?)(?:'s\s*OPD|\bOPD\b)", s, re.IGNORECASE)
        fu_doctor = ("Dr. " + m_doc.group(1).strip()).replace("Dr. Dr.", "Dr.") if m_doc else None

        m_loc = re.search(r"\b(?:at|in)\s+([A-Za-z ]+Hospital|[A-Za-z ]+Clinic|[A-Za-z ]+OPD)\b", s, re.IGNORECASE)
        fu_loc = m_loc.group(1).strip() if m_loc else None

        return {"date": fu_date, "time": fu_time, "doctor": fu_doctor, "location": fu_loc}
    
    def parse_operative_notes():
        blk = section(
            r"Operative\s*Notes\s*:",
            r"Diagnosis|Drug Advice|Discharge notes|Complaints|Medical History|Treatment Given|Investigation|Course In Hospital|Advice On Discharge|Diet Advice|Signature|Prepared By"
        )
        blk = normalize_block_text(blk)
        if not blk:
            return None

        date = None
        m_date = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", blk)
        if m_date:
            date = m_date.group(1)

        procedure = None
        m_proc = re.search(r"\b\d{2}/\d{2}/\d{4}\s*-\s*(.*?)(?=\bby\s+Dr\b|$)", blk, re.IGNORECASE)
        if m_proc:
            procedure = clean_val(m_proc.group(1))

        surgeon = None
        m_doc = re.search(r"\bby\s+Dr\.?\s*([A-Za-z\.\s]+?)(?=\s+under\b|\s+By\s+Dr\b|,|$)", blk, re.IGNORECASE)
        if m_doc:
            surgeon = ("Dr. " + m_doc.group(1).strip()).replace("Dr. Dr.", "Dr.")

        anesthesia = None
        m_an = re.search(r"\bunder\s+([A-Za-z ]+)\b", blk, re.IGNORECASE)
        if m_an:
            anesthesia = clean_val(m_an.group(1))

        anesthetist = None
        m_an_doc = re.search(r"\bBy\s+Dr\.?\s*([A-Za-z\.\s]+?)(?:,|$)", blk, re.IGNORECASE)
        if m_an_doc:
            anesthetist = ("Dr. " + m_an_doc.group(1).strip()).replace("Dr. Dr.", "Dr.")

        red_flags = []
        m_rf = re.search(r"\b(?:Adv(?:\s*inform)?|Advice)\b.*?\bSOS\b\s*if\s*(.+)$", blk, re.IGNORECASE)
        if m_rf:
            txt = m_rf.group(1).strip(" ,.")
            txt = txt.replace(" or ", ", ")
            red_flags = [x.strip(" ,.") for x in txt.split(",") if x.strip(" ,.")]

        return {
            "raw": blk,
            "date": date,
            "procedure": procedure,
            "performed_by": surgeon,
            "anesthesia": anesthesia,
            "anesthetist": anesthetist,
            "red_flags": red_flags,
        }

    patient_name = find(r"NAME\s*:\s*([^\n]+)")
    doctor = find(
        r"Doctor\s*:\s*(.+?)(?=\n(?:IPD\s*NO|UHID|WARD/BED\s*NO|AGE|Admitted\s*Date|Discharged\s*Date|DISCHARGE\s*TYPE|SECONDARY\s*CONSULTANT)\s*:)",
        re.IGNORECASE | re.DOTALL
    )
    if doctor:
        doctor = re.sub(r"\s+", " ", doctor).strip()
    ipd_no = find(r"IPD\s*NO\s*:\s*([A-Z0-9 ]+)")
    uhid = find(r"UHID\s*:\s*([A-Z0-9]+)")
    ward_bed_no = find(r"WARD/\s*BED\s*NO\s*:\s*([^\n]+)")
    admitted_date_raw = find(r"Admitted\s*Date\s*:\s*([0-9/:\s]+)")
    admitted_date = to_iso_datetime(admitted_date_raw) if admitted_date_raw else None
    discharged_date_raw = find(r"Discharged\s*Date\s*:\s*([^\n]+)")
    discharged_date = to_iso_datetime(discharged_date_raw) if discharged_date_raw else None
    discharge_type = find(r"DISCHARGE\s*TYPE\s*:\s*([^\n]+)")
    payer_type = find(r"Payer\s*Type\s*:\s*([^\n]+)")

    age = None
    gender = None

    m = re.search(r"(?:AGE|GENDER|SEX)\s*:\s*(\d+)\s*YEARS?\s*/\s*([MF])", t, re.IGNORECASE)

    if m:
        age = int(m.group(1))
        gender = "Male" if m.group(2).upper() == "M" else "Female"

    referred_to = []
    for m in re.finditer(r"Sr\s*No:-\s*(\d+),\s*Doctor:-\s*([^,]+),\s*Department:-\s*([^,]+)", t, re.IGNORECASE):
        referred_to.append({"sr_no": int(m.group(1)), "doctor": clean_val(m.group(2)), "department": clean_val(m.group(3))})

    provisional_diagnosis = parse_provisional(section(r"Provisional Diagnosis\s*", r"\nDiagnosis\s*:"))
    diagnosis = parse_diagnosis(section(r"Diagnosis\s*:", r"Drug Advice"))
    drug_advice = parse_drug_advice(section(r"Drug Advice", r"Discharge notes\s*:"))

    discharge_notes = parse_discharge_notes()
    complaints = parse_complaints()
    medical_history = parse_medical_history()
    treatment_given = parse_treatment_given()
    investigation = parse_investigation()
    course_in_hospital = parse_course_in_hospital()
    advice_on_discharge = parse_advice_on_discharge()
    diet_advice = parse_diet_advice()
    condition_discharge = list_section(r"Condition of patient at Discharge\s*:", r"Follow Up|VITAL ON DISCHARGE\s*:")
    vitals_on_discharge = parse_vitals_on_discharge()
    signatures = parse_signatures()
    prepared_by = find(r"Prepared By:\s*([^\n]+)")
    authorized_signatory = "AUTHORIZED SIGNATORY" if re.search(r"AUTHORIZED SIGNATORY", t, re.IGNORECASE) else None

    procedure = parse_procedure()
    vitals_on_admission = parse_vitals_on_admission()
    local_examination = parse_local_examination()
    general_examination = parse_general_examination()
    systemic_examination = parse_systemic_examination()
    pain_assessment = parse_pain_assessment()
    next_follow_up = parse_next_follow_up()
    operative_notes = parse_operative_notes()

    return {
        "patient_name": patient_name,
        "age": age,
        "gender": gender,
        "doctor": clean_val(doctor),
        "ipd_no": ipd_no,
        "uhid": uhid,
        "ward_bed_no": ward_bed_no,
        "admitted_date": admitted_date,
        "discharged_date": discharged_date,
        "discharge_type": discharge_type,
        "payer_type": payer_type,
        "referred_to": referred_to,
        "provisional_diagnosis": provisional_diagnosis,
        "diagnosis": diagnosis,
        "drug_advice": drug_advice,
        "procedure": procedure,
        "discharge_notes": discharge_notes,
        "complaints_on_admission": complaints,
        "vitals_on_admission": vitals_on_admission,
        "examination": {
            "local_examination": local_examination,
            "general_examination": general_examination,
            "systemic_examination": systemic_examination,
            "pain_assessment": pain_assessment,
        },
        "medical_history": medical_history,
        "treatment_given": treatment_given,
        "investigation": investigation,
        "course_in_hospital": course_in_hospital,
        "operative_notes": operative_notes,
        "advice_on_discharge": advice_on_discharge,
        "diet_advice": diet_advice,
        "condition_of_patient_at_discharge": condition_discharge,
        "vitals_on_discharge": vitals_on_discharge,
        "next_follow_up": next_follow_up,
        "signatures": signatures,
        "prepared_by": prepared_by,
        "authorized_signatory": authorized_signatory,
    }


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF allowed (current scope)"}), 400

    pdf_bytes = file.read()
    cleaned = clean_text(extract_text_from_pdf(pdf_bytes))
    #return jsonify(build_json(cleaned)), 200
    data = build_json(cleaned)

    db = SessionLocal()

    try:
        row = Report(
            patient_name=data.get("patient_name"),
            admitted_date=data.get("admitted_date"),
            discharged_date=data.get("discharged_date"),
            discharge_notes=json.dumps(data.get("discharge_notes")),
        )

        db.add(row)
        db.commit()
        db.refresh(row)

        return jsonify({"id": row.id, "patient_name": row.patient_name, "admitted_date": row.admitted_date, "discharged_date": row.discharged_date, "discharge_notes": json.loads(row.discharge_notes)}), 200

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        db.close()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
