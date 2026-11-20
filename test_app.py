import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pypdf import PdfWriter

from app import app, client_fields, load_url
from extractor import (DFormPattaFields, DeedFields, ElectricityBillFields,
                       PattaFields, PropertyTaxFields, SadaBainamaFields,
                       extract_document)


class PilotTest(unittest.TestCase):
    def test_stamp_duty_requires_yes_or_no(self):
        self.assertIs(DeedFields(stamp_duty=True).stamp_duty, True)
        with self.assertRaises(ValidationError):
            DeedFields(stamp_duty=None)

    def test_old_job_fields_remain_readable(self):
        old = {"buyer_name": {"value": "పరీక్ష", "page": 2, "note": "old note"},
               "stamp_duty": {"value": True, "page": 1, "note": "stamp"}}
        fields = DeedFields.model_validate(old)
        self.assertEqual(fields.buyer_name, "పరీక్ష")
        self.assertIs(fields.stamp_duty, True)
        self.assertNotIn("note", fields.model_dump_json())

    def test_pdf_validation_extraction_and_review(self):
        async def fake_extract(data, mime, document_type="REGISTERED_DEED"):
            self.assertEqual(mime, "application/pdf")
            return DeedFields(buyer_name="పరీక్ష", sro="S.R.O. Adilabad"), {"promptTokenCount": 300}

        writer = PdfWriter()
        writer.add_blank_page(width=500, height=700)
        stream = io.BytesIO()
        writer.write(stream)
        with tempfile.TemporaryDirectory() as directory:
            with patch("app.DB", Path(directory) / "test.sqlite3"), patch("app.DATA_ROOT", Path(directory) / "data"), patch("app.extract_document", fake_extract):
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").json()["extractor_version"], "header-retry-v2")
                    bad = client.post(
                        "/docs_data",
                        data={"document_type": "REGISTERED_DEED", "beneficiary_id": "BEN-123", "document_id": "DOC-456"},
                        files={"file": ("bad.pdf", b"not a pdf", "application/pdf")},
                    )
                    self.assertEqual(bad.status_code, 400)
                    result = client.post(
                        "/docs_data",
                        data={"document_type": "REGISTERED_DEED", "beneficiary_id": "BEN-123", "document_id": "DOC-456"},
                        files={"file": ("deed.pdf", stream.getvalue(), "application/pdf")},
                    )
                    self.assertEqual(result.status_code, 200)
                    body = result.json()
                    self.assertEqual(body["beneficiary_id"], "BEN-123")
                    self.assertEqual(body["document_id"], "DOC-456")
                    self.assertEqual(body["document_type"], "register_deed")
                    self.assertNotIn("pages", body)
                    self.assertNotIn("fields", body)
                    self.assertEqual(body["data"]["rd_buyer"], "పరీక్ష")
                    self.assertEqual(body["data"]["rd_seller"], "None")
                    self.assertEqual(body["data"]["rd_doc_num"], "None")
                    self.assertEqual(body["data"]["rd_sro"], "S.R.O. Adilabad")
                    self.assertEqual(body["data"]["rd_stamp_duty"], "NO")
                    self.assertNotIn("confidence", body)
                    self.assertNotIn("usage", body)
                    self.assertNotIn("note", body["data"])
                    self.assertNotIn("page", body["data"])
                    self.assertNotIn("id", body)
                    self.assertNotIn("filename", body)
                    self.assertNotIn("status", body)
                    artifacts = list((Path(directory) / "data" / "registered_deed").iterdir())
                    self.assertTrue(any(re.fullmatch(r"registered_deed_\d{8}_\d{6}_BEN-123\.json", item.name) for item in artifacts))
                    self.assertTrue(any(re.fullmatch(r"registered_deed_\d{8}_\d{6}_BEN-123\.pdf", item.name) for item in artifacts))
                    self.assertEqual(len(client.get("/jobs").json()), 1)
                    job_id = client.get("/jobs").json()[0]["id"]
                    reviewed = client.put("/jobs/" + job_id, json=body["data"])
                    self.assertEqual(reviewed.json()["status"], "reviewed")

    def test_requires_exactly_one_file_or_url(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("app.DB", Path(directory) / "test.sqlite3"):
                with TestClient(app) as client:
                    response = client.post("/docs_data", data={"document_type": "REGISTERED_DEED", "beneficiary_id": "B", "document_id": "D"})
                    self.assertEqual(response.status_code, 400)

    def test_beneficiary_id_must_be_unique(self):
        calls = 0

        async def fake_extract(data, mime, document_type="REGISTERED_DEED"):
            nonlocal calls
            calls += 1
            return PropertyTaxFields(assessment_number="A-123"), {}

        with tempfile.TemporaryDirectory() as directory:
            with patch("app.DB", Path(directory) / "test.sqlite3"), patch("app.DATA_ROOT", Path(directory) / "data"), patch("app.extract_document", fake_extract):
                with TestClient(app) as client:
                    first = client.post(
                        "/docs_data",
                        data={"document_type": "PROPERTY_TAX", "beneficiary_id": "UNIQUE-1", "document_id": "DOC-1"},
                        files={"file": ("tax.jpg", b"\xff\xd8tax", "image/jpeg")},
                    )
                    duplicate = client.post(
                        "/docs_data",
                        data={"document_type": "PROPERTY_TAX", "beneficiary_id": "UNIQUE-1", "document_id": "DOC-2"},
                        files={"file": ("tax.jpg", b"\xff\xd8tax", "image/jpeg")},
                    )
                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(duplicate.status_code, 409)
                    self.assertIn("already exists", duplicate.json()["detail"])
                    self.assertEqual(calls, 1)

    def test_property_tax_response(self):
        async def fake_extract(data, mime, document_type="REGISTERED_DEED"):
            self.assertEqual(document_type, "PROPERTY_TAX")
            return PropertyTaxFields(assessment_number="A-123", door_number="4-56", seal=True, signature=False), {}

        with tempfile.TemporaryDirectory() as directory:
            with patch("app.DB", Path(directory) / "test.sqlite3"), patch("app.DATA_ROOT", Path(directory) / "data"), patch("app.extract_document", fake_extract):
                with TestClient(app) as client:
                    result = client.post(
                        "/docs_data",
                        data={"document_type": "PROPERTY_TAX", "beneficiary_id": "B", "document_id": "D"},
                        files={"file": ("tax.jpg", b"\xff\xd8tax", "image/jpeg")},
                    )
                    self.assertEqual(result.status_code, 200)
                    body = result.json()
                    self.assertEqual(body, {
                        "beneficiary_id": "B",
                        "document_id": "D",
                        "document_type": "property_tax",
                        "data": {
                            "pt_Assessment_no": "A-123",
                            "pt_door_no": "4-56",
                            "pt_seal": "YES",
                            "pt_signature": "NO",
                        },
                    })
                    self.assertNotIn("pages", body)
                    self.assertNotIn("fields", body)
                    self.assertNotIn("confidence", body)
                    self.assertNotIn("usage", body)
                    artifacts = list((Path(directory) / "data" / "property_tax").iterdir())
                    self.assertTrue(any(re.fullmatch(r"property_tax_\d{8}_\d{6}_B\.json", item.name) for item in artifacts))
                    self.assertTrue(any(re.fullmatch(r"property_tax_\d{8}_\d{6}_B\.jpg", item.name) for item in artifacts))

    def test_d_form_patta_response(self):
        async def fake_extract(data, mime, document_type="REGISTERED_DEED"):
            self.assertEqual(document_type, "D_FORM_PATTA")
            return DFormPattaFields(
                beneficiary_name="Athram Laxmi",
                survey_number="12",
                assignment_order_proceedings="B/511/2025",
                tahsildar=True,
                revenue_divisional_officer=False,
                collector=None,
                mro=False,
            ), {}

        with tempfile.TemporaryDirectory() as directory:
            with patch("app.DB", Path(directory) / "test.sqlite3"), patch("app.DATA_ROOT", Path(directory) / "data"), patch("app.extract_document", fake_extract):
                with TestClient(app) as client:
                    result = client.post(
                        "/docs_data",
                        data={"document_type": "D_FORM_PATTA", "beneficiary_id": "DFORM-1", "document_id": "DOC-1"},
                        files={"file": ("patta.jpg", b"\xff\xd8patta", "image/jpeg")},
                    )
                    self.assertEqual(result.status_code, 200)
                    body = result.json()
                    self.assertEqual(body["document_type"], "D_FORM_PATTA")
                    self.assertNotIn("pages", body)
                    self.assertNotIn("fields", body)
                    self.assertEqual(body["data"]["d_patta_beneficiary_name"], "Athram Laxmi")
                    self.assertEqual(body["data"]["d_patta_tahsildar"], "yes")
                    self.assertEqual(body["data"]["d_patta_revenue_divisional_officer"], "no")
                    self.assertEqual(body["data"]["d_patta_collector"], "None")

    def test_patta_response(self):
        async def fake_extract(data, mime, document_type="REGISTERED_DEED"):
            self.assertEqual(document_type, "PATTA")
            return PattaFields(
                pattadar_name="Atla Anji Reddy",
                survey_number=None,
                extent=None,
                village="Gangipally",
                mandal="Jaipur",
                district="Mancherial",
                tahsildar_mro_seal=True,
                digital_signature=False,
                qr_barcode=True,
                dharani_ror_reference=None,
            ), {}

        with tempfile.TemporaryDirectory() as directory:
            with patch("app.DB", Path(directory) / "test.sqlite3"), patch("app.DATA_ROOT", Path(directory) / "data"), patch("app.extract_document", fake_extract):
                with TestClient(app) as client:
                    result = client.post(
                        "/docs_data",
                        data={"document_type": "PATTA", "beneficiary_id": "PATTA-1", "document_id": "DOC-1"},
                        files={"file": ("patta.jpg", b"\xff\xd8patta", "image/jpeg")},
                    )
                    self.assertEqual(result.status_code, 200)
                    body = result.json()
                    self.assertEqual(body["document_type"], "PATTA")
                    self.assertNotIn("pages", body)
                    self.assertNotIn("fields", body)
                    self.assertEqual(body["data"]["patta_pattadar_name"], "Atla Anji Reddy")
                    self.assertEqual(body["data"]["patta_survey_number"], "None")
                    self.assertEqual(body["data"]["patta_tahsildar_mro_seal"], "yes")
                    self.assertEqual(body["data"]["patta_digital_signature"], "no")
                    self.assertEqual(body["data"]["patta_qr_barcode"], "yes")

    def test_electricity_bill_response(self):
        async def fake_extract(data, mime, document_type="REGISTERED_DEED"):
            self.assertEqual(document_type, "ELECTRICITY_BILL")
            return ElectricityBillFields(
                consumer_name="A MALLAIAH",
                usc_number="12972763",
                sc_number="60111-00117",
                category="1-1A Domestic",
                meter_number="02562712",
                previous_reading="632",
                current_reading="691",
                bill_month="11/07/2026",
                bill_amount="0.00",
                total_due="8.0",
            ), {}

        with tempfile.TemporaryDirectory() as directory:
            with patch("app.DB", Path(directory) / "test.sqlite3"), patch("app.DATA_ROOT", Path(directory) / "data"), patch("app.extract_document", fake_extract):
                with TestClient(app) as client:
                    result = client.post(
                        "/docs_data",
                        data={"document_type": "ELECTRICITY_BILL", "beneficiary_id": "56466546", "document_id": "458"},
                        files={"file": ("bill.jpg", b"\xff\xd8bill", "image/jpeg")},
                    )
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json(), {
                        "beneficiary_id": "56466546",
                        "document_id": "458",
                        "document_type": "electricity_bill",
                        "data": {
                            "Consumer_name": "A MALLAIAH",
                            "USC_no": "12972763",
                            "SC_no": "60111-00117",
                            "category": "1-1A Domestic",
                            "meter_no": "02562712",
                            "perivious_reading": "632",
                            "current_reading": "691",
                            "bill_month": "11/07/2026",
                            "total_due": 8.0,
                        },
                    })

    def test_electricity_and_sada_response_prefixes(self):
        electricity = client_fields(
            ElectricityBillFields(consumer_name="Test Consumer"), "ELECTRICITY_BILL"
        )
        sada = client_fields(
            SadaBainamaFields(buyer_name="Buyer", signatures=True),
            "SADA_BAINAMA_STAMP_PAPER",
        )
        self.assertEqual(electricity["eb_consumer_name"], "Test Consumer")
        self.assertEqual(electricity["eb_usc_number"], "None")
        self.assertEqual(electricity["eb_total_due"], "None")
        self.assertEqual(sada["sada_buyer_name"], "Buyer")
        self.assertEqual(sada["sada_signatures"], "yes")


class UrlDownloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_html_share_page_resolves_open_graph_image(self):
        def handler(request):
            if request.url.path == "/share/123":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text='<meta property="og:image" content="https://cdn.example/document.jpg">',
                )
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\xff\xd8\xffimage")

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch("app.httpx.AsyncClient", mock_client):
            data, mime, filename = await load_url("https://example.com/share/123")
        self.assertEqual(data, b"\xff\xd8\xffimage")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(filename, "document.jpg")


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_interactions_request_and_response(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={
                "steps": [{"type": "model_output", "content": [{"type": "text", "text":
                    DeedFields(document_number="123").model_dump_json()}]}],
                "usage": {"total_input_tokens": 250, "total_output_tokens": 100},
            })

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict("extractor.os.environ", {"GEMINI_API_KEY": "test-key"}), patch("extractor.httpx.AsyncClient", mock_client):
            fields, usage = await extract_document(b"%PDF-sample", "application/pdf")
        self.assertEqual(fields.document_number, "123")
        self.assertIs(fields.stamp_duty, False)
        self.assertEqual(usage["total_input_tokens"], 250)
        self.assertTrue(str(captured["request"].url).endswith("/v1beta/interactions"))
        self.assertEqual(captured["request"].headers["x-goog-api-key"], "test-key")
        self.assertIn(b'"model":"gemini-3.5-flash-lite"', captured["request"].content)
        self.assertIn(b'"store":false', captured["request"].content)

    async def test_missing_deed_number_uses_header_crop_and_cleans_fields(self):
        writer = PdfWriter()
        writer.add_blank_page(width=500, height=700)
        stream = io.BytesIO()
        writer.write(stream)
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                content = DeedFields(
                    buyer_name="Sri. SK. IMRAN, S/o Sri. SK. DADE",
                    seller_name="Sri. ARUMILLI RAMA KRISHNA (represented by GPA agent)",
                    plot_number="Open Plot No.10", house_number="H.No. 5-83 (Agent)",
                ).model_dump_json()
            else:
                content = '{"document_number":"8585/2021"}'
            return httpx.Response(200, json={
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": content}]}],
                "usage": {"total_input_tokens": 100, "total_output_tokens": 20, "total_tokens": 120},
            })

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict("extractor.os.environ", {"GEMINI_API_KEY": "test-key"}), patch("extractor.httpx.AsyncClient", mock_client):
            fields, usage = await extract_document(stream.getvalue(), "application/pdf")
        self.assertEqual(len(calls), 2)
        self.assertEqual(fields.document_number, "8585/2021")
        self.assertEqual(fields.buyer_name, "SK. IMRAN")
        self.assertEqual(fields.seller_name, "ARUMILLI RAMA KRISHNA")
        self.assertIsNone(fields.house_number)
        self.assertEqual(usage["total_input_tokens"], 200)

    async def test_property_tax_extraction_does_not_run_deed_cleanup(self):
        def handler(request):
            content = PropertyTaxFields(
                assessment_number="A-123", door_number="4-56", seal=True, signature=False,
            ).model_dump_json()
            return httpx.Response(200, json={
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": content}]}],
                "usage": {"total_input_tokens": 50, "total_output_tokens": 20, "total_tokens": 70},
            })

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict("extractor.os.environ", {"GEMINI_API_KEY": "test-key"}), patch("extractor.httpx.AsyncClient", mock_client):
            fields, usage = await extract_document(b"image", "image/jpeg", "PROPERTY_TAX")
        self.assertEqual(fields.assessment_number, "A-123")
        self.assertEqual(fields.door_number, "4-56")
        self.assertIs(fields.seal, True)
        self.assertIs(fields.signature, False)

    async def test_electricity_bill_extraction(self):
        def handler(request):
            content = ElectricityBillFields(
                consumer_name="Test Consumer", usc_number="USC123", sc_number="SC456",
                category="DOMESTIC", meter_number="M789", previous_reading="100",
                current_reading="150", bill_month="September 2026", bill_amount="1250",
                total_due="1450",
            ).model_dump_json()
            return httpx.Response(200, json={
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": content}]}],
                "usage": {"total_input_tokens": 50, "total_output_tokens": 20, "total_tokens": 70},
            })

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict("extractor.os.environ", {"GEMINI_API_KEY": "test-key"}), patch("extractor.httpx.AsyncClient", mock_client):
            fields, usage = await extract_document(b"image", "image/jpeg", "ELECTRICITY_BILL")
        self.assertEqual(fields.consumer_name, "Test Consumer")
        self.assertEqual(fields.usc_number, "USC123")
        self.assertEqual(fields.current_reading, "150")
        self.assertEqual(fields.bill_amount, "1250")
        self.assertEqual(fields.total_due, "1450")

    async def test_sada_bainama_extraction(self):
        def handler(request):
            content = SadaBainamaFields(
                buyer_name="Buyer Name", seller_name="Seller Name", survey_number="123/4",
                property_boundaries="North: Road; South: Canal", agreement_date="2025-01-15",
                signatures=True, thumb_impressions=True,
            ).model_dump_json()
            return httpx.Response(200, json={
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": content}]}],
                "usage": {"total_input_tokens": 50, "total_output_tokens": 20, "total_tokens": 70},
            })

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict("extractor.os.environ", {"GEMINI_API_KEY": "test-key"}), patch("extractor.httpx.AsyncClient", mock_client):
            fields, usage = await extract_document(b"image", "image/jpeg", "SADA_BAINAMA_STAMP_PAPER")
        self.assertEqual(fields.buyer_name, "Buyer Name")
        self.assertEqual(fields.survey_number, "123/4")
        self.assertIs(fields.thumb_impressions, True)

    async def test_d_form_patta_extraction(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            content = DFormPattaFields(
                beneficiary_name="Athram Laxmi", survey_number="12",
                assignment_order_proceedings="B/511/2025", tahsildar=True,
                revenue_divisional_officer=False, collector=False, mro=False,
            ).model_dump_json()
            return httpx.Response(200, json={
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": content}]}],
                "usage": {"total_input_tokens": 50, "total_output_tokens": 20, "total_tokens": 70},
            })

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict("extractor.os.environ", {"GEMINI_API_KEY": "test-key"}), patch("extractor.httpx.AsyncClient", mock_client):
            fields, usage = await extract_document(b"image", "image/jpeg", "D_FORM_PATTA")
        self.assertEqual(fields.beneficiary_name, "Athram Laxmi")
        self.assertEqual(fields.assignment_order_proceedings, "B/511/2025")
        self.assertIs(fields.tahsildar, True)
        self.assertNotIn(b"Read ONLY the handwritten", captured["request"].content)

    async def test_patta_extraction(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            content = PattaFields(
                pattadar_name="Atla Anji Reddy", village="Gangipally",
                mandal="Jaipur", district="Mancherial",
                tahsildar_mro_seal=True, digital_signature=False,
                qr_barcode=True,
            ).model_dump_json()
            return httpx.Response(200, json={
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": content}]}],
                "usage": {"total_input_tokens": 50, "total_output_tokens": 20, "total_tokens": 70},
            })

        real_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict("extractor.os.environ", {"GEMINI_API_KEY": "test-key"}), patch("extractor.httpx.AsyncClient", mock_client):
            fields, usage = await extract_document(b"image", "image/jpeg", "PATTA")
        self.assertEqual(fields.pattadar_name, "Atla Anji Reddy")
        self.assertEqual(fields.village, "Gangipally")
        self.assertIs(fields.qr_barcode, True)
        self.assertIn(b"dharani_ror_reference", captured["request"].content)


if __name__ == "__main__":
    unittest.main()
