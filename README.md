# Document Extraction API

This separate FastAPI service accepts Telugu/English sale-deed PDFs or images and
returns the requested sale-deed fields as structured JSON.
It uses `gemini-3.5-flash-lite` through the Gemini Interactions API by default. The existing `multi_document_api_latest`
service is unchanged.

## Run

On a new Windows desktop, install Python 3.10 or newer and enable **Add Python
to PATH** during installation. Extract the ZIP, open PowerShell in the extracted
`document_extraction` folder, then run:

```powershell
.\install.ps1
.\run.ps1
```

`run.ps1` securely prompts for the Gemini key if it is not already in the
current PowerShell environment or saved in `.env`. To save it locally, copy
`.env.example` to `.env`, replace the placeholder with the real key, and never
commit or share `.env`. Keep the server terminal open while using the API.
If PowerShell blocks local scripts, use `powershell -ExecutionPolicy Bypass -File
.\install.ps1` and then `powershell -ExecutionPolicy Bypass -File .\run.ps1`.
This changes execution policy only for each launched process.

For manual setup from this directory on Python 3.10+:

```powershell
python -m pip install -r requirements.txt
uvicorn app:app --reload --port 8001
```

Open `http://127.0.0.1:8001/docs`. Send `beneficiary_id`, `document_id`, and
exactly one of `file` or `url` to `POST /docs_data`. The file/URL may be a PDF,
JPEG, PNG, or WebP. Select `REGISTERED_DEED`, `PROPERTY_TAX`,
`ELECTRICITY_BILL`, `SADA_BAINAMA_STAMP_PAPER`, `D_FORM_PATTA`, or `PATTA` with
`document_type`. Each `beneficiary_id` may be used only once. The response includes both IDs so the client can match the
extracted fields to its original record. For `PROPERTY_TAX`, the extraction
response uses lowercase `property_tax`, omits `pages`, and returns a `data`
object containing `pt_Assessment_no`, `pt_door_no`, `pt_seal`, and
`pt_signature`. Property-tax presence values are `"YES"`, `"NO"`, or `"None"`.
For `ELECTRICITY_BILL`, the extraction response uses lowercase
`electricity_bill`, omits `pages`, and returns `data` containing
`Consumer_name`, `USC_no`, `SC_no`, `category`, `meter_no`,
`perivious_reading`, `current_reading`, `bill_month`, and numeric
`total_due`. `bill_amount` is not included in the client response.
For `REGISTERED_DEED`, the extraction response uses lowercase `register_deed`,
omits `pages`, and returns `data` containing `rd_buyer`, `rd_seller`,
`rd_doc_num`, `rd_house_no`, `rd_plot_no`, `rd_survey_no`, `rd_sro`, `rd_seal`,
`rd_sro_signature`, `rd_stamp_duty`, `rd_thumb`, and `rd_witness`. Presence
values are `"YES"`, `"NO"`, or `"None"`.
For `SADA_BAINAMA_STAMP_PAPER`, `data` contains `buyer_name`, `seller_name`,
`survey_number`, `property_boundaries`, `agreement_date`, `signatures`, and
`thumb_impressions`.
For `D_FORM_PATTA`, `data` contains `beneficiary_name`, `survey_number`,
`assignment_order_proceedings`, `tahsildar`, `revenue_divisional_officer`,
`collector`, and `mro`.
For `PATTA`, `data` contains `pattadar_name`, `survey_number`, `extent`,
`village`, `mandal`, `district`, `tahsildar_mro_seal`, `digital_signature`,
`qr_barcode`, and `dharani_ror_reference`.
Use `GET /jobs` to see the latest 100 results. Correct a result by sending its
full `data` object to `PUT /jobs/{id}`. A corrected result is marked `reviewed`.

## Response rules

Every extraction response contains `beneficiary_id`, `document_id`,
`document_type`, and `data`; it does not expose `pages` or `fields`. The `data`
object contains plain values without per-field page or note objects. Text values
are strings, with missing values returned as `"None"`;
boolean fields are returned as `"yes"`, `"no"`, or `"None"`.
Client-facing field names use document prefixes: `rd_` for registered deeds,
`pt_` for property tax, `eb_` for electricity bills, `d_patta_` for D-form
patta, `patta_` for patta, and `sada_` for Sada Bainama/stamp paper.
`stamp_duty` is `"yes"` or `"no"`, based on visible
stamp paper, e-stamp, duty amount, or payment/challan evidence in the upload.
`"None"` means unavailable or uncertain. The model must not derive names from a
filename. For full buyer and seller names, upload the **entire sale-deed PDF**;
a single registration/payment page may contain only initials and may not
establish which person bought or sold. Visual marks indicate presence only,
not authenticity.
`house_number` is the number of the sold property; for an open plot it is normally
`"None"`. Residential house numbers belonging to buyers, sellers, or GPA agents
are not returned.

The API keeps extracted JSON and token usage in `pilot.sqlite3`. Successful
requests also save the source document and response JSON under `data/<document_type>/`
using `<document_type>_<YYYYMMDD>_<HHMMSS>_<beneficiary_id>` filenames, for example
`property_tax_20260825_134444_56466546.json`. The `document_id` remains in the
JSON response and database record. Treat the database and data
folders as sensitive personal data and apply access control, encryption,
retention limits, and audit logging before production use. This pilot has no
authentication or batch scheduler.

If the main extraction misses `document_number`, the service makes one extra
Gemini call with a high-resolution crop of the first page's header. The extra
call is stored internally and costs tokens only for those documents. For open
plots, residential `H.No.` entries of buyers, vendors, and agents are excluded
from the property `house_number` field.

## Limits and cost measurement

Uploads are capped at 18 MiB and 30 PDF pages. API calls are synchronous and
use standard Gemini pricing. Internal job records contain provider token
counts; measure that on a labeled sample before estimating a 100,000-document
batch. Failed/ambiguous outputs should be reviewed manually. For a production
batch, use Gemini Batch API and add resumable jobs, rate limiting, and monitoring.

No live model call is made by the included tests. Run `python -m unittest -q`.
