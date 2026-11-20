"""Pilot API. Source documents are processed in memory and not retained."""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from pypdf import PdfReader

from extractor import (EXTRACTOR_VERSION, DFormPattaFields, DeedFields,
                       ElectricityBillFields, PattaFields, PropertyTaxFields,
                       SadaBainamaFields, extract_document)

LOGGER = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
DB = Path(os.getenv("DEED_DB_PATH", str(BASE / "pilot.sqlite3")))
DATA_ROOT = Path(os.getenv("DEED_DATA_DIR", str(BASE / "data")))
MAX_BYTES = 18 * 1024 * 1024
MAX_PAGES = 30
MIMES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
EXTENSION_MIMES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".png": "image/png", ".webp": "image/webp"}
MIME_EXTENSIONS = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class OpenGraphImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.image_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta" or self.image_url:
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("property", "").lower() == "og:image":
            self.image_url = values.get("content")


def detect_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@contextmanager
def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, pages INTEGER NOT NULL,
            document_type TEXT NOT NULL DEFAULT 'REGISTERED_DEED',
            beneficiary_id TEXT NOT NULL, document_id TEXT NOT NULL,
            status TEXT NOT NULL, fields_json TEXT NOT NULL,
            usage_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        if "document_type" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN document_type TEXT NOT NULL DEFAULT 'REGISTERED_DEED'")
        if "beneficiary_id" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN beneficiary_id TEXT NOT NULL DEFAULT ''")
        if "document_id" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN document_id TEXT NOT NULL DEFAULT ''")
        db.execute("""CREATE TABLE IF NOT EXISTS beneficiary_registry (
            beneficiary_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""INSERT OR IGNORE INTO beneficiary_registry (beneficiary_id)
                    SELECT DISTINCT beneficiary_id FROM jobs WHERE beneficiary_id <> ''""")
    yield


app = FastAPI(title="Document Extraction API", lifespan=lifespan)


class Job(BaseModel):
    id: str
    filename: str
    pages: int
    document_type: str
    beneficiary_id: str
    document_id: str
    status: str
    fields: DeedFields | PropertyTaxFields | ElectricityBillFields | SadaBainamaFields | DFormPattaFields | PattaFields
    usage: dict


class ExtractionResponse(BaseModel):
    beneficiary_id: str
    document_id: str
    document_type: str
    data: dict[str, float | str]


FIELD_PREFIXES = {
    "REGISTERED_DEED": "rd_",
    "PROPERTY_TAX": "pt_",
    "ELECTRICITY_BILL": "eb_",
    "D_FORM_PATTA": "d_patta_",
    "PATTA": "patta_",
    "SADA_BAINAMA_STAMP_PAPER": "sada_",
}


def client_fields(fields: BaseModel, document_type: str) -> dict[str, str]:
    prefix = FIELD_PREFIXES[document_type]
    converted = {}
    for name, value in fields.model_dump(by_alias=True).items():
        if value is True:
            converted[prefix + name] = "yes"
        elif value is False:
            converted[prefix + name] = "no"
        elif value is None:
            converted[prefix + name] = "None"
        else:
            converted[prefix + name] = str(value)
    return converted


def property_tax_data(fields: PropertyTaxFields) -> dict[str, str]:
    def value(item: str | bool | None) -> str:
        if item is True:
            return "YES"
        if item is False:
            return "NO"
        if item is None:
            return "None"
        return str(item)

    return {
        "pt_Assessment_no": value(fields.assessment_number),
        "pt_door_no": value(fields.door_number),
        "pt_seal": value(fields.seal),
        "pt_signature": value(fields.signature),
    }


def electricity_bill_data(fields: ElectricityBillFields) -> dict[str, float | str]:
    def text_value(item: str | None) -> str:
        return "None" if item is None else str(item)

    def amount_value(item: str | None) -> float | str:
        if item is None:
            return "None"
        cleaned = re.sub(r"[^0-9.\-]", "", str(item).replace(",", ""))
        try:
            return float(cleaned)
        except ValueError:
            return str(item)

    return {
        "Consumer_name": text_value(fields.consumer_name),
        "USC_no": text_value(fields.usc_number),
        "SC_no": text_value(fields.sc_number),
        "category": text_value(fields.category),
        "meter_no": text_value(fields.meter_number),
        "perivious_reading": text_value(fields.previous_reading),
        "current_reading": text_value(fields.current_reading),
        "bill_month": text_value(fields.bill_month),
        "total_due": amount_value(fields.total_due),
    }


def registered_deed_data(fields: DeedFields) -> dict[str, str]:
    def value(item: str | bool | None) -> str:
        if item is True:
            return "YES"
        if item is False:
            return "NO"
        if item is None:
            return "None"
        return str(item)

    return {
        "rd_buyer": value(fields.buyer_name),
        "rd_seller": value(fields.seller_name),
        "rd_doc_num": value(fields.document_number),
        "rd_house_no": value(fields.house_number),
        "rd_plot_no": value(fields.plot_number),
        "rd_survey_no": value(fields.survey_number),
        "rd_sro": value(fields.sro),
        "rd_seal": value(fields.registration_seal),
        "rd_sro_signature": value(fields.sro_signature),
        "rd_stamp_duty": value(fields.stamp_duty),
        "rd_thumb": value(fields.thumb_impressions),
        "rd_witness": value(fields.witness_signatures),
    }


def internal_fields(fields: dict, document_type: str) -> dict:
    prefix = FIELD_PREFIXES[document_type]
    conversion = {"yes": True, "no": False, "YES": True, "NO": False, "None": None}
    if document_type == "REGISTERED_DEED":
        registered_deed_names = {
            "rd_buyer": "buyer_name",
            "rd_seller": "seller_name",
            "rd_doc_num": "document_number",
            "rd_house_no": "house_number",
            "rd_plot_no": "plot_number",
            "rd_survey_no": "survey_number",
            "rd_sro": "sro",
            "rd_seal": "registration_seal",
            "rd_sro_signature": "sro_signature",
            "rd_stamp_duty": "stamp_duty",
            "rd_thumb": "thumb_impressions",
            "rd_witness": "witness_signatures",
        }
        return {
            registered_deed_names.get(name, name[len(prefix):] if name.startswith(prefix) else name):
            conversion.get(value, value)
            for name, value in fields.items()
        }
    if document_type == "PROPERTY_TAX":
        property_tax_names = {
            "pt_Assessment_no": "assessment_number",
            "pt_door_no": "door_number",
            "pt_seal": "seal",
            "pt_signature": "signature",
        }
        return {
            property_tax_names.get(name, name[len(prefix):] if name.startswith(prefix) else name):
            conversion.get(value, value)
            for name, value in fields.items()
        }
    if document_type == "ELECTRICITY_BILL":
        electricity_names = {
            "Consumer_name": "consumer_name",
            "USC_no": "usc_number",
            "SC_no": "sc_number",
            "category": "category",
            "meter_no": "meter_number",
            "perivious_reading": "previous_reading",
            "current_reading": "current_reading",
            "bill_month": "bill_month",
            "total_due": "total_due",
        }
        return {
            electricity_names.get(name, name[len(prefix):] if name.startswith(prefix) else name):
            (str(value) if name == "total_due" and isinstance(value, (int, float))
             else conversion.get(value, value))
            for name, value in fields.items()
        }
    return {
        name[len(prefix):] if name.startswith(prefix) else name: conversion.get(value, value)
        for name, value in fields.items()
    }


class DocumentType(str, Enum):
    REGISTERED_DEED = "REGISTERED_DEED"
    PROPERTY_TAX = "PROPERTY_TAX"
    ELECTRICITY_BILL = "ELECTRICITY_BILL"
    SADA_BAINAMA_STAMP_PAPER = "SADA_BAINAMA_STAMP_PAPER"
    D_FORM_PATTA = "D_FORM_PATTA"
    PATTA = "PATTA"


def page_count(data: bytes, mime: str) -> int:
    if mime != "application/pdf":
        return 1
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:
        raise HTTPException(400, "Invalid PDF") from exc


async def load_url(url: str) -> tuple[bytes, str, str]:
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            response_mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if response_mime == "text/html":
                parser = OpenGraphImageParser()
                parser.feed(response.text)
                if not parser.image_url:
                    raise HTTPException(415, "URL is an HTML page without a downloadable document image")
                document_url = urljoin(str(response.url), parser.image_url)
                if not document_url.lower().startswith(("http://", "https://")):
                    raise HTTPException(415, "HTML page contains an unsupported document URL")
                response = await client.get(document_url)
                response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(400, f"Could not download URL (HTTP {exc.response.status_code})") from exc
    except httpx.RequestError as exc:
        raise HTTPException(400, "Could not download URL") from exc
    data = response.content
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "URL content is empty or exceeds 18 MiB")
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    filename = Path(response.url.path).name or Path(httpx.URL(url).path).name or "url-upload"
    detected_mime = detect_mime(data)
    if detected_mime:
        mime = detected_mime
    elif mime == "application/octet-stream" or mime not in MIMES:
        mime = EXTENSION_MIMES.get(Path(filename).suffix.lower(), mime)
    if mime not in MIMES:
        raise HTTPException(415, "URL must point to a PDF, JPEG, PNG, or WebP")
    return data, mime, filename


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    if not cleaned:
        raise HTTPException(400, "IDs must contain filename-safe characters")
    return cleaned


def save_artifacts(document_type: DocumentType, beneficiary_id: str, document_id: str,
                   data: bytes, mime: str,
                   response: ExtractionResponse) -> None:
    folder = DATA_ROOT / document_type.value.lower()
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{document_type.value.lower()}_{timestamp}_{safe_component(beneficiary_id)}"
    (folder / f"{stem}{MIME_EXTENSIONS[mime]}").write_bytes(data)
    (folder / f"{stem}.json").write_text(
        json.dumps(response.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@app.get("/health")
def health():
    return {"status": "ok", "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            "extractor_version": EXTRACTOR_VERSION}


@app.post(
    "/docs_data8/",
    response_model=ExtractionResponse,
    operation_id="docs_data",
)
async def extract(
    document_type: DocumentType = Form(...),
    beneficiary_id: str = Form(...),
    document_id: str = Form(...),
    file: UploadFile | None = File(default=None),
    url: str | None = Form(default=None),
):
    if (file is None) == (not url):
        raise HTTPException(400, "Provide exactly one file or URL")
    beneficiary_id = beneficiary_id.strip()
    document_id = document_id.strip()
    if not beneficiary_id or not document_id:
        raise HTTPException(400, "beneficiary_id and document_id cannot be empty")
    with connect() as db:
        duplicate = db.execute(
            "SELECT 1 FROM beneficiary_registry WHERE beneficiary_id = ? LIMIT 1", (beneficiary_id,)
        ).fetchone()
    if duplicate:
        if file is not None:
            await file.close()
        raise HTTPException(409, f"beneficiary_id '{beneficiary_id}' already exists")
    if url:
        data, mime, source_name = await load_url(url)
    else:
        mime = (file.content_type or "").split(";", 1)[0].lower()
        source_name = Path(file.filename or "upload").name
        data = await file.read(MAX_BYTES + 1)
        await file.close()
    if mime not in MIMES:
        raise HTTPException(415, "Upload PDF, JPEG, PNG, or WebP")
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "File is empty or exceeds 18 MiB")
    if mime == "application/pdf" and not data.startswith(b"%PDF-"):
        raise HTTPException(400, "Invalid PDF")
    if mime == "image/jpeg" and not data.startswith(b"\xff\xd8"):
        raise HTTPException(400, "Invalid JPEG")
    if mime == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "Invalid PNG")
    if mime == "image/webp" and data[8:12] != b"WEBP":
        raise HTTPException(400, "Invalid WebP")
    pages = page_count(data, mime)
    if pages < 1 or pages > MAX_PAGES:
        raise HTTPException(413, "PDF must contain 1–30 pages")
    try:
        fields, usage = await extract_document(data, mime, document_type.value)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        try:
            provider_error = exc.response.json().get("error", {})
            provider_status = str(provider_error.get("status", "UNKNOWN"))
            provider_message = str(provider_error.get("message", ""))
        except ValueError:
            provider_status, provider_message = "UNKNOWN", ""
        secret = os.getenv("GEMINI_API_KEY", "")
        if secret:
            provider_message = provider_message.replace(secret, "[redacted]")
        provider_message = provider_message[:400]
        LOGGER.warning("Gemini HTTP %s %s: %s", code, provider_status, provider_message)
        raise HTTPException(502, f"Gemini HTTP {code} ({provider_status}): {provider_message}") from exc
    except httpx.RequestError as exc:
        LOGGER.warning("Gemini connection failed: %s", type(exc).__name__)
        raise HTTPException(502, "Could not connect to Gemini. Check network access from the server.") from exc
    except ValueError as exc:
        LOGGER.warning("Gemini response could not be parsed: %s", exc)
        raise HTTPException(502, "Gemini returned invalid extraction JSON.") from exc
    job = Job(id=str(uuid.uuid4()), filename=source_name,
              pages=pages, document_type=document_type.value,
              beneficiary_id=beneficiary_id, document_id=document_id,
              status="needs_review", fields=fields, usage=usage)
    try:
        with connect() as db:
            db.execute("INSERT INTO beneficiary_registry (beneficiary_id) VALUES (?)", (beneficiary_id,))
            db.execute("INSERT INTO jobs (id, filename, pages, document_type, beneficiary_id, document_id, status, fields_json, usage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (job.id, job.filename, job.pages, job.document_type, job.beneficiary_id, job.document_id, job.status,
                        fields.model_dump_json(), __import__("json").dumps(usage)))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"beneficiary_id '{beneficiary_id}' already exists") from exc
    if document_type == DocumentType.REGISTERED_DEED:
        response = ExtractionResponse(
            beneficiary_id=job.beneficiary_id,
            document_id=job.document_id,
            document_type="register_deed",
            data=registered_deed_data(fields),
        )
    elif document_type == DocumentType.PROPERTY_TAX:
        response = ExtractionResponse(
            beneficiary_id=job.beneficiary_id,
            document_id=job.document_id,
            document_type=job.document_type.lower(),
            data=property_tax_data(fields),
        )
    elif document_type == DocumentType.ELECTRICITY_BILL:
        response = ExtractionResponse(
            beneficiary_id=job.beneficiary_id,
            document_id=job.document_id,
            document_type=job.document_type.lower(),
            data=electricity_bill_data(fields),
        )
    else:
        response = ExtractionResponse(
            beneficiary_id=job.beneficiary_id,
            document_id=job.document_id,
            document_type=job.document_type,
            data=client_fields(job.fields, job.document_type),
        )
    try:
        save_artifacts(document_type, beneficiary_id, document_id, data, mime, response)
    except OSError as exc:
        LOGGER.exception("Could not save extraction artifacts")
        raise HTTPException(500, "Extraction succeeded but saving the result failed") from exc
    return response


@app.get("/jobs", response_model=list[Job])
def list_jobs():
    import json
    with connect() as db:
        rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
    return [Job(id=r["id"], filename=r["filename"], pages=r["pages"], document_type=r["document_type"],
                beneficiary_id=r["beneficiary_id"], document_id=r["document_id"], status=r["status"],
                fields=(PropertyTaxFields if r["document_type"] == DocumentType.PROPERTY_TAX.value else
                        ElectricityBillFields if r["document_type"] == DocumentType.ELECTRICITY_BILL.value else
                        SadaBainamaFields if r["document_type"] == DocumentType.SADA_BAINAMA_STAMP_PAPER.value else
                        DFormPattaFields if r["document_type"] == DocumentType.D_FORM_PATTA.value else
                        PattaFields if r["document_type"] == DocumentType.PATTA.value else
                        DeedFields).model_validate_json(r["fields_json"]),
                usage=json.loads(r["usage_json"])) for r in rows]


@app.put("/jobs/{job_id}", response_model=Job)
def review_job(job_id: str, fields: dict):
    import json
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found")
        field_model = (PropertyTaxFields if row["document_type"] == DocumentType.PROPERTY_TAX.value else
                       ElectricityBillFields if row["document_type"] == DocumentType.ELECTRICITY_BILL.value else
                       SadaBainamaFields if row["document_type"] == DocumentType.SADA_BAINAMA_STAMP_PAPER.value else
                       DFormPattaFields if row["document_type"] == DocumentType.D_FORM_PATTA.value else
                       PattaFields if row["document_type"] == DocumentType.PATTA.value else
                       DeedFields)
        fields = field_model.model_validate(internal_fields(fields, row["document_type"]))
        db.execute("UPDATE jobs SET fields_json = ?, status = 'reviewed' WHERE id = ?",
                   (fields.model_dump_json(), job_id))
    return Job(id=job_id, filename=row["filename"], pages=row["pages"], document_type=row["document_type"],
               beneficiary_id=row["beneficiary_id"], document_id=row["document_id"], status="reviewed",
               fields=fields, usage=json.loads(row["usage_json"]))
