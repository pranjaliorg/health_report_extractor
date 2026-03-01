import code
from unicodedata import name

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
        if not block:
            return []

        def clean_diag(s: str):
            if s is None:
                return None
            s = re.sub(r"\bDiagnosis\s*:\s*", "", s, flags=re.I)
            s = re.sub(r"\bDiagnosis\s*:\s*$", "", s, flags=re.I)
            s = re.sub(r"\s+", " ", s).strip(" ,:-")
            return clean_val(s)

        raw = [x.strip().strip(" ,") for x in block.splitlines() if x.strip()]

        joined = []
        i = 0
        while i < len(raw):
            a = raw[i]
            b = raw[i + 1] if i + 1 < len(raw) else ""

            a0 = a.strip()
            b0 = b.strip()

            if b0:
                if re.fullmatch(r"[A-Z]{1,2}\d{1,3}", a0, re.I) and re.fullmatch(r"\.\d{1,2}", b0):
                    joined.append((a0 + b0).strip())
                    i += 2
                    continue

                if re.fullmatch(r"[A-Z]{1,2}\d{1,2}", a0, re.I) and re.fullmatch(r"\d{1,2}", b0):
                    joined.append((a0 + b0).strip())
                    i += 2
                    continue

            a_ends = a.lower().rstrip(".").endswith("approx")
            a_open = a.count("(") > a.count(")")
            b_cont = bool(b) and (re.match(r"^\d", b) or "cc" in b.lower() or b.startswith(("-", ";", ")")))

            if b and (a_ends or a_open) and b_cont:
                joined.append((a + " " + b).strip())
                i += 2
            else:
                joined.append(a)
                i += 1

        code = r"(?:(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{2,7}(?:\.[A-Z0-9]{1,6})?|[0-9]{5})"

        res = []
        idx = 1

        def push(text, c):
            nonlocal idx
            text = clean_diag((text or "").strip(" -–—:").strip())
            c = clean_val((c or "").strip())
            if not text and not c:
                return
            res.append({"sr_no": idx, "diagnosis": text or None, "icd_code": c or None})

        for line in joined:
            if "," in line and len(re.findall(code, line, flags=re.I)) >= 2:
                chunks = [p.strip().strip(" ,") for p in re.split(r"\s*,\s*", line) if p.strip()]
                for ch in chunks:
                    m = re.match(rf"^(\d+)\s+(.*?)[\s]*[-–—:][\s]*({code})$", ch, flags=re.I)
                    if m:
                        idx = int(m.group(1))
                        push(m.group(2), m.group(3))
                        idx += 1
                        continue

                    m = re.match(rf"^({code})\s*[-–—:]?\s*(.+)$", ch, flags=re.I)
                    if m and m.group(2).strip():
                        push(m.group(2), m.group(1))
                        idx += 1
                        continue

                    m = re.match(rf"^(.*?)[\s]*[-–—:][\s]*({code})$", ch, flags=re.I)
                    if m and m.group(1).strip():
                        push(m.group(1), m.group(2))
                        idx += 1
                continue

            m = re.match(rf"^(\d+)\s+(.*?)[\s]*[-–—:][\s]*({code})$", line, flags=re.I)
            if m:
                idx = int(m.group(1))
                push(m.group(2), m.group(3))
                idx += 1
                continue

            m = re.match(rf"^({code})\s*[-–—:]?\s*(.+)$", line, flags=re.I)
            if m and m.group(2).strip():
                push(m.group(2), m.group(1))
                idx += 1
                continue

            m = re.match(rf"^(.*?)[\s]*[-–—:][\s]*({code})$", line, flags=re.I)
            if m and m.group(1).strip():
                push(m.group(1), m.group(2))
                idx += 1
                continue

            m = re.match(rf"^({code})\s+(.*)$", line, flags=re.I)
            if m and m.group(2).strip():
                push(m.group(2), m.group(1))
                idx += 1
                continue

            if res:
                res[-1]["diagnosis"] = clean_diag(((res[-1].get("diagnosis") or "") + " " + line).strip())

        seen = set()
        final = []
        for d in res:
            diag = clean_diag(d.get("diagnosis") or "")
            codev = clean_val(d.get("icd_code") or "")
            k = (diag.lower(), codev.lower())
            if k in seen:
                continue
            seen.add(k)
            final.append({"sr_no": None, "diagnosis": diag or None, "icd_code": codev or None})

        for n, d in enumerate(final, start=1):
            d["sr_no"] = n

        return final

    def parse_schedule(line: str):
        line = re.sub(r"\s+", " ", (line or "")).strip()
        if not line:
            return None

        if "SOS" in line.upper() and not re.search(r"\d+\s*-\s*\d+", line):
            return {
                "dose_schedule": None,
                "timing": None,
                "frequency": "SOS",
                "duration_days": None,
                "quantity": None,
            }

        m = re.match(r"^(\d+(?:/\d+)?(?:\s*-\s*\d+(?:/\d+)?){2,3})\s*(.*)$", line)
        if not m:
            return None

        dose_schedule = m.group(1).strip()
        rest = m.group(2).strip()

        parts = [p.strip() for p in rest.split(" - ") if p.strip()]
        timing = parts[0] if len(parts) >= 1 and parts[0].strip("_ ") else None
        frequency = parts[1] if len(parts) >= 2 else None

        duration_days = None
        quantity = None

        if len(parts) >= 3:
            last = parts[2]

            md = re.search(r"(\d+)\s*Day\(s\)", last, re.I)
            if md:
                duration_days = int(md.group(1))

            mm = re.search(r"(\d+)\s*Month\(s\)", last, re.I)
            if mm:
                duration_days = int(mm.group(1)) * 30

            mq = re.search(r"(?:Day|Month)\(s\)\s*(\d+)", last, re.I)
            if mq:
                quantity = int(mq.group(1))

        if "SOS" in rest.upper():
            frequency = "SOS"

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
        if "DROP" in s or "DROPS" in s or "DRP" in s or "DRPS" in s:
            return "drop"
        if "SYRUP" in s or "SYP" in s or "SUSP" in s:
            return "dose"
        if "LOZENGE" in s or "LOZENGES" in s or "LOZ" in s:
            return "lozenge"
        if "PATCH" in s:
            return "patch"
        if "CAP" in s or "CAPSULE" in s:
            return "capsule"
        if "TAB" in s or "TABLET" in s:
            return "tablet"
        return "dose"


    def interpret_schedule(dose_schedule: str, unit="tablet"):
        if not dose_schedule:
            return None

        parts = [p.strip() for p in dose_schedule.split("-")]
        if len(parts) not in (3, 4):
            return None

        times = ["in morning", "in afternoon", "at night"] if len(parts) == 3 else ["in morning", "in afternoon", "in evening", "at night"]

        if unit in ("ointment", "gel", "patch"):
            verb = "Apply"
        elif unit == "drop":
            verb = "Instill"
        else:
            verb = "Take"

        phrases = []
        for i, tok in enumerate(parts):
            tok = tok.strip()
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

            if verb == "Apply":
                phrases.append(f"Apply {times[i]}")
            elif verb == "Instill":
                phrases.append(f"Instill {qty} {unit} {times[i]}")
            else:
                phrases.append(f"Take {qty} {unit} {times[i]}")

        return ", ".join(phrases) if phrases else None

    def strength_prefix(s: str):
        s = s.strip()
        if re.match(r"^(?:TOTAL\s+|PLUS\s+|R\s*)?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?\s*(?:MG|MCG|GM|G|ML|IU)\b", s, re.I):
            return True
        if not re.search(r"\b(MG|MCG|GM|G|ML|IU)\b", s, re.I):
            return False
        #if re.match(r"^(?:TOTAL\s+|PLUS\s+|R\s+)?\d", s, re.I):
            #return True
        return False


    def move_descriptor_from_composition(name_parts, composition_parts):
        if not composition_parts:
            return

        first_line = composition_parts[0].strip()

        if first_line.upper().startswith("EMULGEL"):
            name_parts.append("EMULGEL")
            composition_parts[0] = first_line[len("EMULGEL"):].strip()
            return

        if first_line.upper().startswith("QPS"):
            name_parts.append("QPS")
            composition_parts[0] = first_line[len("QPS"):].strip()
            return

        m_plus = re.match(r"^PLUS\s+\d+(?:/\d+)?\s*(?:MG|MCG|GM|G|ML|IU)\b", first_line, re.I)
        if m_plus:
            prefix = m_plus.group(0)
            name_parts.append(prefix.strip())
            composition_parts[0] = first_line[len(prefix):].strip()
            return

        m_total = re.match(r"^TOTAL\s+\d+(?:/\d+)?\s*(?:MG|MCG|GM|G|ML|IU)\b(?:\s*[A-Z]+)?", first_line, re.I)
        if m_total:
            prefix = m_total.group(0)
            name_parts.append(prefix.strip())
            composition_parts[0] = first_line[len(prefix):].strip()
            return

    def parse_drug_advice(block: str):
        if not block:
            return []

        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        filtered = []
        for ln in lines:
            if re.match(r"^No\s+Name\s+Dose", ln, re.I):
                continue
            if re.match(r"^Page\s*\|\s*\d+", ln, re.I):
                continue
            if re.match(r"^(Discharge\s*notes|Procedure)\s*:", ln, re.I):
                break
            filtered.append(ln)

        start_pat = re.compile(
            r"^(\d{1,2})\s+(?:"
            r"(?:TAB|TABLET|CAP|CAPSULE|OINT|OINTMENT|GEL|DRP|DROP|SYRUP|SYP|LOZENGE|LOZENGES|PATCH|ORAL|SOLUTION|DISKETTES)\b"
            r"|[A-Z]+[A-Z0-9()./-]*\.)",
            re.I
        )

        items = []
        cur = []
        for ln in filtered:
            if start_pat.match(ln) and cur:
                items.append(cur)
                cur = [ln]
            else:
                cur.append(ln)
        if cur:
            items.append(cur)

        dose_pat = re.compile(r"\d+(?:/\d+)?\s*-\s*\d+(?:/\d+)?\s*-\s*\d+(?:/\d+)?(?:\s*-\s*\d+(?:/\d+)?)?")
        meta_pat = re.compile(r"\b(After|Before|On|Daily|Weekly|Monthly|SOS|Day\(s\)|Month\(s\))\b", re.I)

        out = []

        for item in items:
            m0 = re.match(r"^(\d{1,2})\s+(.+)$", item[0])
            if not m0:
                continue

            sr_no = int(m0.group(1))
            first = m0.group(2).strip()

            name_parts = []
            schedule_parts = []
            composition_parts = []
            notes = None
            notes_open = False
            quantity = None

            def add_name(x):
                x = re.sub(r"\s+", " ", x).strip()
                if x:
                    name_parts.append(x)

            def add_schedule(x):
                x = re.sub(r"\s+", " ", x).strip()
                if x:
                    schedule_parts.append(x)

            inline = dose_pat.search(first)
            if inline:
                add_name(first[:inline.start()].strip())
                add_schedule(first[inline.start():].strip())
            else:
                add_name(first)

            for ln in item[1:]:
                s = ln.strip()

                if s.lower().startswith("notes:"):
                    notes = clean_val(s.split(":", 1)[1].strip())
                    notes_open = True
                    continue

                if notes_open:
                    if s == "___" or dose_pat.search(s) or meta_pat.search(s) or re.fullmatch(r"\d+", s):
                        notes_open = False
                    else:
                        notes = re.sub(r"\s+", " ", (notes or "") + " " + s).strip()
                        continue

                if s == "___":
                    continue


                if re.fullmatch(r"\d+", s):
                    quantity = int(s)
                    continue

                if s.upper() == "MG":
                    if name_parts and re.search(r"\d$", name_parts[-1]):
                        name_parts[-1] = (name_parts[-1] + " MG").strip()
                        continue
                    add_name("MG")
                    continue

                if re.fullmatch(r"\d+(\.\d+)?", s):
                    add_name(s)
                    continue

                if s.upper() in ("DROPS", "EYE DROPS", "(DRPS)"):
                    add_name(s)
                    continue

                if dose_pat.search(s):
                    add_schedule(s)
                    continue

                if meta_pat.search(s):
                    add_schedule(s)
                    continue
                
                if strength_prefix(s):
                    add_name(s)
                    continue

                composition_parts.append(s)

            move_descriptor_from_composition(name_parts, composition_parts)
            name = clean_val(" ".join(name_parts))

            schedule_line = " ".join(schedule_parts).strip()
            if not schedule_line and "SOS" in (name or "").upper():
                schedule_line = "SOS"

            if name and re.search(r"\bSOS|\(?DRPS\)?\b", name.upper()):
                name = re.sub(r"SOS|\(?DRPS\)?", "", name, flags=re.I)
                name = re.sub(r"\s+", " ", name).strip()

            parsed = parse_schedule(schedule_line) if schedule_line else None
            dose_schedule = timing = frequency = duration_days = qty_from_schedule = None
            if parsed:
                dose_schedule = parsed["dose_schedule"]
                timing = parsed["timing"]
                frequency = parsed["frequency"]
                duration_days = parsed["duration_days"]
                qty_from_schedule = parsed["quantity"]

            if qty_from_schedule:
                quantity = qty_from_schedule

            composition = clean_val(" ".join(composition_parts)) if composition_parts else None

            unit = infer_unit(name)
            dose_schedule_text = interpret_schedule(dose_schedule, unit) if dose_schedule else None
    
            out.append(
                {
                    "sr_no": sr_no,
                    "name": name,
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

        m_adv = re.search(r"\bAdvice\s+(.*?)(?=\bOn\s+(?:F/U|Follow\s*up)\b|\.\s*On\s+" + date_re + r"|\b(F/U|Follow\s*up)\b|Please\s+contact|$)", s, re.IGNORECASE)        
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
            txt = txt.replace("@ ", "at ")
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

        txt = normalize_block_text(block).replace(",,", ",")

        txt = re.sub(r",\s*Unspecified\b", " TEMP_UNSPECIFIED", txt, flags=re.I)
        txt = re.sub(r",\s*(Unspecified\s*-\s*[A-Z]\d)", r" TEMP_UNSPECIFIED \1", txt, flags=re.I)

        txt = re.sub(r"\bDid you undergo any surgery\?\s*:\s*Yes-?", "", txt, flags=re.I)
        txt = re.sub(r"\bDid you undergo any surgery\?\s*:\s*", "", txt, flags=re.I)

        txt = re.sub(r"\bND\s+S/P\b", " S/P", txt, flags=re.I)

        txt = re.sub(r"\s+(?=(?:[A-Z][A-Z /]+)\s*-\s*(?:Details|Suffering Since)\s*:)", "\n", txt)
        txt = re.sub(r"\s+(?=S/P\b)", "\n", txt, flags=re.I)
        txt = re.sub(r"\s+(?=RECENTLY PATIENT ADMITTED\b)", "\n", txt, flags=re.I)

        txt = re.sub(r"\s+(?=(?:\d{4,5}|[A-Z]{1,3}\d[A-Z0-9.]{0,6}|\d[A-Z0-9]{2,5})\s*-\s*)", "\n", txt)
        txt = re.sub(r"\s+(?=(?:[A-Z]{1,3}\d[A-Z0-9.]{0,6}|\d{4,5})\b(?!\s*-\s*)\s+[A-Z])", "\n", txt)

        raw = []
        for line in txt.splitlines():
            for part in line.split(","):
                part = part.strip(" ,.-")
                if part:
                    raw.append(part)

        out = []
        i = 0
        while i < len(raw):
            x = re.sub(r"\s+", " ", raw[i]).strip()
            x = x.replace("TEMP_UNSPECIFIED", ", Unspecified").strip()

            if x.lower() == "in" and i + 1 < len(raw):
                out[-1] = clean_val((out[-1] + " in " + raw[i + 1].strip()).strip(" ,.-")) if out else clean_val(x)
                i += 2
                continue

            if x.upper() == "S/P" and i + 1 < len(raw):
                x = "S/P " + raw[i + 1].strip()
                i += 1

            if re.match(r"^[A-Z]{2,20}$", x) and i + 1 < len(raw) and re.search(r"-\s*(?:Details|Suffering Since)\s*:", raw[i + 1], flags=re.I):
                x = x + " " + raw[i + 1].strip()
                i += 1

            if out and re.match(r"^(?:\d{4,5}|[A-Z]{1,3}\d[A-Z0-9.]{0,6}|\d[A-Z0-9]{2,5})\s*-\s*", x):
                out[-1] = clean_val((out[-1] + " " + x).strip(" ,.-"))
            else:
                x = clean_val(x)
                if x:
                    out.append(x)

            i += 1

        return out

    def parse_treatment_given():
        block = section(r"Treatment Given\s*:", r"Investigation\s*:")
        if not block:
            return []
        txt = re.sub(r"\s+", " ", block).strip().replace(",,", ",")
        return [p.strip(" ,") for p in txt.split(",") if p.strip()]

    def parse_course_in_hospital():
        block = section(r"Course In Hospital\s*:", r"Advice On Discharge\s*:|OPerative\s+Notes")
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
        block = section(r"Diet Advice\s*:", r"Condition of patient at Discharge\s*:|VITAL\s+ON\s+DISCHARGE\s*:|Follow\s+Up")
        block = normalize_block_text(block)
        if not block:
            return []
        parts = re.split(r"\.\s+", block)
        return [p.strip() + ("." if p and not p.strip().endswith(".") else "") for p in parts if p.strip()]
    
    def parse_condition_on_discharge():
        blk = section(
            r"Condition of patient at Discharge\s*:",
            r"Follow Up|VITAL\s+ON\s+DISCHARGE\s*:|Signature|Prepared By"
        )
        blk = re.sub(r"\s+", " ", blk).strip()
        if not blk:
            return {"text": None, "vitals": None}

        def grab(p):
            m = re.search(p, blk, re.IGNORECASE)
            return clean_val(m.group(1)) if m else None

        vitals = {
            "hgt": grab(r"\bHGT\s*[-:]\s*([0-9/]+)"),
            "bp": grab(r"\bBP\s*[-:]\s*([0-9/]+)"),
            "spo2": grab(r"\bSpO2\s*[-:]\s*([0-9]+)\s*%?"),
            "weight": grab(r"\b(?:Wt|W)\s*[-:]\s*([0-9.]+)\s*Kg"),
        }

        txt = re.sub(r"\b(HGT|BP|SpO2|Wt|W)\s*[-:]\s*[^,\.]+", "", blk, flags=re.I)
        txt = re.sub(r"\s+,", ",", txt).strip(" ,.")

        return {"text": clean_val(txt), "vitals": vitals}
    
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
                "blood_pressure": extract(r"BP:\s*([0-9/]+)"),
                "heart_rate": extract(r"HR:\s*([0-9]+)"),
                "temperature": extract(r"TEMPERATURE:\s*([0-9\.]+)\s*°?\s*F"),
                "respiratory_rate": extract(r"RR:\s*([0-9]+)"),
                "spo2": extract(r"SPO2:\s*([0-9]+%?)"),
                "sugar": extract(r"SUGAR:\s*([0-9]+)\s*MG/DL"),
                "general_rbs": extract(r"RBS:\s*([0-9]+)\s*MG/DL"),
                "urine_output": extract(r"URINE\s*OUTPUT:\s*([0-9]+)")
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
                current = {
                    "date": to_iso_datetime(m.group(1)) or m.group(1),
                    "tests": []
                }
                line_text = m.group(2)
            else:
                line_text = ln

            if not current:
                continue

            parts = [p.strip() for p in line_text.split(",") if p.strip()]

            for part in parts:
                part = re.sub(r"^\-\s*", "", part).strip()

                if not part:
                    continue

                if current["tests"]:
                    prev = current["tests"][-1]
                    prev_u = prev.upper()
                    if (
                        re.match(r"^[a-z]", part)
                        or (("CT" in prev_u or "NON CONTRAST" in prev_u) and (part[:1].isdigit() or re.match(r"^(A|AN|THE|VOLUME|DIFFUSE|SLUDGE|NO|ATHER|SIMPLE|HAE|HEMA)\b", part, re.I)))
                        or prev.lower().endswith("measuring")
                    ):
                        current["tests"][-1] += " " + part
                        continue

                current["tests"].append(part)

        if current:
            out.append(current)

        return out

    def parse_signatures():
        blk = section(r"Signature\s*", r"Prepared By|Patient/Relative Signature|Patient\s*/\s*Relative\s*Signature|Authorized|AUTHORISED")
        if not blk:
            return []

        lines = [re.sub(r"\s+", " ", l).strip() for l in blk.splitlines() if l.strip()]

        names = []
        for line in lines:
            if re.search(r"\b(incharge|consultant|resident|typed\s*by|nurse)\b", line, re.I):
                continue
            if line.lower().startswith("signature"):
                continue
            if re.search(r"signature|incharge|consultant|resident|typed|nurse", line, re.I):
                continue
            names.append(line)

        if not names:
            return []

        name_line = names[0]

        parts = re.split(r"\bDr\.?\s*", name_line)
        result = []

        for i, p in enumerate(parts):
            p = p.strip(" ,.-")
            if not p:
                continue
            result.append(p if i == 0 else "Dr. " + p)

        return result

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
    
    def clean_lines(blk: str):
        blk = blk.replace(" | ", "\n").replace("|", "\n")
        blk = re.sub(r"[ \t]+", " ", blk).strip()

        if ":" in blk and "," in blk:
            blk = re.sub(r"^[^(]*\(\s*", "", blk).strip()
            blk = re.sub(r"\)\s*$", "", blk).strip()
            blk = re.sub(r"\s*,\s*", "\n", blk)

        lines = [x.strip(" ,") for x in blk.splitlines() if x.strip()]

        merged = []
        for x in lines:
            if merged and merged[-1].endswith(":"):
                merged[-1] = f"{merged[-1]} {x}"
            elif merged and merged[-1].count("(") > merged[-1].count(")"):
                merged[-1] = f"{merged[-1]} {x}"
            else:
                merged.append(x)

        merged = [m + ")" if m.count("(") > m.count(")") else m for m in merged]
        merged = [m for m in merged if m.lower() not in ("no", "yes")]

        out, i = [], 0
        while i < len(merged):
            if i + 1 < len(merged) and re.fullmatch(r"[A-Za-z]{2,}", merged[i]) and ":" in merged[i + 1]:
                out.append(f"{merged[i]} {merged[i+1]}")
                i += 2
            else:
                out.append(merged[i])
                i += 1

        return out or None

    def parse_local_examination():
        blk = section(
            r"\bL/E\s*:\s*",
            r"General\s*Examination|Systematic|Systemic|Diagnosis|Complaints|Medical\s*History|Treatment|Investigation|Course|Advice|Diet|Pain\s*assessment|Drug Advice|Discharge notes"
        )
        blk = blk.strip()
        if not blk:
            return None
        blk = re.sub(r"^\s*L/E\s*:\s*", "", blk, flags=re.I).strip()
        blk = blk.replace(" | ", "\n")
        lines = [re.sub(r"\s+", " ", x).strip(" ,") for x in blk.splitlines() if x.strip()]
        return lines or None


    def parse_general_examination():
        blk = section(
            r"General\s*examination\s*[:-]?\s*",
            r"(Systematic|Systemic)\s*examination\b|Pain\s*assessment\b|Diagnosis\b|Drug Advice\b|Discharge notes\b|Medical\s*History\b|Treatment\s*Given\b|Investigation\b|Course\s*In\s*Hospital\b|Advice\s*On\s*Discharge\b|Diet\s*Advice\b|Condition\b|VITAL\b|Follow\s*Up\b|Signature\b|Prepared\s*By\b"
        )
        blk = blk.strip()
        if not blk:
            return None
        blk = re.sub(r"^\s*General\s*examination\s*[:-]?\s*", "", blk, flags=re.I).strip()
        lines = clean_lines(blk)
        if not lines:
            return None
        drop = {"general examination", "general examination:-general examination", "- general examination"}
        lines = [x for x in lines if x.lower() not in drop]
        return lines or None


    def parse_systemic_examination():
        blk = section(
            r"(Systematic|Systemic)\s*examination\s*[:-]?\s*",
            r"Pain\s*assessment\b|Diagnosis\b|Drug Advice\b|Discharge notes\b|Medical\s*History\b|Treatment\s*Given\b|Investigation\b|Course\s*In\s*Hospital\b|Advice\s*On\s*Discharge\b|Diet\s*Advice\b|Condition\b|VITAL\b|Follow\s*Up\b|Signature\b|Prepared\s*By\b"
        )
        blk = blk.strip()
        if not blk:
            return None
        blk = re.sub(r"^\s*(Systematic|Systemic)\s*examination\s*[:-]?\s*", "", blk, flags=re.I).strip()
        lines = clean_lines(blk)
        if not lines:
            return None
        drop = {"systematic examination", "systemic examination"}
        lines = [x for x in lines if x.lower() not in drop]
        return lines or None

    def parse_pain_assessment():
        blk = section(
            r"Pain\s*assessment\s*:?",
            r"Diagnosis|Drug Advice|Discharge notes|Investigation|Course In Hospital|Advice On Discharge|Diet Advice|Medical History|Systematic|Systemic|General\s*Examination"
        )
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

    age = None
    gender = None

    m = re.search(r"(?:AGE|GENDER|SEX)\s*:\s*(\d+)\s*YEARS?\s*/\s*([MF])", t, re.IGNORECASE)

    if m:
        age = int(m.group(1))
        gender = "Male" if m.group(2).upper() == "M" else "Female"

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
    referred_by = find(r"Referred\s*By\s*:\s*([^\n]+)")
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
    condition_discharge = parse_condition_on_discharge()
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
        "referred_by": referred_by,
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
            age=data.get("age"),
            gender=data.get("gender"),
            doctor=data.get("doctor"),
            ipd_no=data.get("ipd_no"),
            uhid=data.get("uhid"),
            ward_bed_no=data.get("ward_bed_no"),
            admitted_date=data.get("admitted_date"),
            discharged_date=data.get("discharged_date"),
            discharge_type=data.get("discharge_type"),
            referred_by=data.get("referred_by"),
            payer_type=data.get("payer_type"),
            referred_to=json.dumps(data.get("referred_to")),
            provisional_diagnosis=json.dumps(data.get("provisional_diagnosis")),
            diagnosis=json.dumps(data.get("diagnosis")),
            drug_advice=json.dumps(data.get("drug_advice")),
            procedure=json.dumps(data.get("procedure")),
            discharge_notes=json.dumps(data.get("discharge_notes")),
            complaints_on_admission=json.dumps(data.get("complaints_on_admission")),
            vitals_on_admission=json.dumps(data.get("vitals_on_admission")),
            local_examination=json.dumps(data.get("examination", {}).get("local_examination")),
            general_examination=json.dumps(data.get("examination", {}).get("general_examination")),
            systemic_examination=json.dumps(data.get("examination", {}).get("systemic_examination")),
            pain_assessment=json.dumps(data.get("examination", {}).get("pain_assessment")),
            medical_history=json.dumps(data.get("medical_history")),
            treatment_given=json.dumps(data.get("treatment_given")),
            investigation=json.dumps(data.get("investigation")),
            course_in_hospital=json.dumps(data.get("course_in_hospital")),
            operative_notes=json.dumps(data.get("operative_notes")),
            advice_on_discharge=json.dumps(data.get("advice_on_discharge")),
            diet_advice=json.dumps(data.get("diet_advice")),
            condition_of_patient_at_discharge=json.dumps(data.get("condition_of_patient_at_discharge")),
            vitals_on_discharge=json.dumps(data.get("vitals_on_discharge")),
            next_follow_up=json.dumps(data.get("next_follow_up")),
            signatures=json.dumps(data.get("signatures")),
            prepared_by=data.get("prepared_by"),
            authorized_signatory=data.get("authorized_signatory"),
        )

        db.add(row)
        db.commit()
        db.refresh(row)

        return jsonify({"id": row.id, 
                        "patient_name": row.patient_name,
                        "age": row.age,
                        "gender": row.gender,
                        "doctor": row.doctor,
                        "ipd_no": row.ipd_no,
                        "uhid": row.uhid,
                        "ward_bed_no": row.ward_bed_no,
                        "admitted_date": row.admitted_date, 
                        "discharged_date": row.discharged_date, 
                        "discharge_type": row.discharge_type,
                        "referred_by": row.referred_by,
                        "payer_type": row.payer_type,
                        "referred_to": json.loads(row.referred_to),
                        "provisional_diagnosis": json.loads(row.provisional_diagnosis),
                        "diagnosis": json.loads(row.diagnosis),
                        "drug_advice": json.loads(row.drug_advice),
                        "procedure": json.loads(row.procedure),
                        "discharge_notes": json.loads(row.discharge_notes),
                        "complaints_on_admission": json.loads(row.complaints_on_admission),
                        "vitals_on_admission": json.loads(row.vitals_on_admission),
                        "local_examination": json.loads(row.local_examination),
                        "general_examination": json.loads(row.general_examination),
                        "systemic_examination": json.loads(row.systemic_examination),
                        "pain_assessment": json.loads(row.pain_assessment),
                        "medical_history": json.loads(row.medical_history),
                        "treatment_given": json.loads(row.treatment_given),
                        "investigation": json.loads(row.investigation),
                        "course_in_hospital": json.loads(row.course_in_hospital),
                        "operative_notes": json.loads(row.operative_notes),
                        "advice_on_discharge": json.loads(row.advice_on_discharge),
                        "diet_advice": json.loads(row.diet_advice),
                        "condition_of_patient_at_discharge": json.loads(row.condition_of_patient_at_discharge),
                        "vitals_on_discharge": json.loads(row.vitals_on_discharge),
                        "next_follow_up": json.loads(row.next_follow_up),
                        "signatures": json.loads(row.signatures),
                        "prepared_by": row.prepared_by,
                        "authorized_signatory": row.authorized_signatory}), 200

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        db.close()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
