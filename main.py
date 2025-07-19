import os
import io
import time
import json
import re
from datetime import datetime
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import fitz  # PyMuPDF
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from googleapiclient.errors import HttpError

# === CONFIG ===

SERVICE_ACCOUNT_FILE = "service_account.json"
USER_TOKEN_FILE = "token.json"

# Google Drive
DRIVE_FOLDER_ID = "1xN6zHO5bC7mlfhlsL8isPI4a8uXsZ5AG"

# Google Sheets
SHEET_ID = "1UWyUJ-1ptfPK8744rfbf6C3iR__Ck0AnHObuS0Hplt4"
SHEET_NAME = "InvoiceDatabase"

# Poll interval
POLL_INTERVAL_SEC = 30

# Ollama model name
OLLAMA_MODEL_PARSE = "llama3.2"
OLLAMA_MODEL_EMAIL = "gpt-4o-mini"

# Local storage for downloaded PDFs
DOWNLOAD_DIR = "invoices"

# === AUTH ===
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

# creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES )
creds = Credentials.from_authorized_user_file(USER_TOKEN_FILE, SCOPES)

drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)


# === Step 1 - Watch for new PDF files ===
def list_new_pdfs_in_folder(folder_id):
    """
    Returns a list of new PDF files in the folder
    """
    query = f"'{folder_id}' in parents and mimeType='application/pdf'"
    results = drive_service.files().list(q=query).execute()
    files = results.get('files', [])
    return files


# === Step 2 - Download PDF ===
def download_pdf(file_id, file_name):
    """
    Downloads PDF from Google Drive
    """
    request = drive_service.files().get_media(fileId=file_id)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, file_name)
    fh = io.FileIO(file_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    return file_path


# === Step 3 - Extract Text from PDF ===
def extract_text_from_pdf(pdf_path):
    text = ""
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text += page.get_text()
    return text


# === Step 4 - Parse Invoice Fields with LLM ===
def parse_invoice_fields(raw_text):
    ollama = ChatOllama(model=OLLAMA_MODEL_PARSE)

    prompt_text = f"""
You are an invoice extraction AI.

Extract these fields from the following invoice text if they exist:

- Invoice Number
- Client Name
- Client Email
- Client Address
- Client Phone
- Invoice Date
- Due Date
- Total Amount

Respond as a JSON object. If a field is missing, omit it.

INVOICE TEXT:
{raw_text}
"""

    response = ollama.invoke(prompt_text)
    return response.content


# === Step 5 - Log to Google Sheet ===

def append_invoice_data(json_data):
    """
    Appends a new row to the spreadsheet.
    """

    # Define order of columns:
    columns = [
        "Invoice Number",
        "Client Name",
        "Client Email",
        "Client Address",
        "Client Phone",
        "Invoice Date",
        "Due Date",
        "Total Amount"
    ]

    values = [json_data.get(field, "") for field in columns]

    body = {
        'values': [values]
    }

    sheets_service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_NAME}!A1",
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()


# === Step 6 - Generate Email Notification ===
def generate_billing_email(json_data):
    ollama = ChatOllama(model=OLLAMA_MODEL_EMAIL)

    prompt = f"""
You are "Greenie" from Green Grass Corp.

Compose a short email notifying the billing department that a new invoice was processed and logged in the Invoice Database.

Include the Invoice Number if available.

Here is the extracted data:
{json_data}
"""

    response = ollama.invoke(prompt)
    return response.content


# === Main Loop ===

if __name__ == "__main__":
    processed_file_ids = set()

    while True:
        print(f"[{datetime.now()}] Checking for new invoices...")

        try:
            files = list_new_pdfs_in_folder(DRIVE_FOLDER_ID)

            for file in files:
                file_id = file["id"]
                file_name = file["name"]

                if file_id in processed_file_ids:
                    continue

                print(f"Found new file: {file_name}")

                # Step 2: Download
                local_path = download_pdf(file_id, file_name)
                print(f"Downloaded: {local_path}")

                # Step 3: Extract text
                raw_text = extract_text_from_pdf(local_path)
                # print("Extracted text:", raw_text)
                print("Extracted text length:", len(raw_text))

                # Step 4: Parse invoice fields
                parsed_json_str = parse_invoice_fields(raw_text)
                # print("parsed_json_str:", parsed_json_str)
                
                # string parsing
                json_str = ''
                start_index = parsed_json_str.find("{")
                end_index = parsed_json_str.rfind("}") + 1  # Include the last }
                if start_index != -1 and end_index != -1:
                    json_str = parsed_json_str[start_index:end_index]
                    print("json_str:", json_str)                    
                else:
                    print("JSON block not found in the string.")
                
                parsed_json = {}
                try:
                    parsed_json = json.loads(json_str)
                    print("parsed_json:", parsed_json)
                except json.JSONDecodeError:
                    print("Warning: Could not parse JSON from LLM response.")
                    print(parsed_json_str)

                if parsed_json:
                    # Step 5: Log to sheet
                    append_invoice_data(parsed_json)
                    print("Logged data to Google Sheets.")

                    # Step 6: Create billing email summary
                    email_text = generate_billing_email(parsed_json)
                    print("Generated billing email:")
                    print(email_text)

                processed_file_ids.add(file_id)
                
                
        except HttpError as error:
            print(f"An error occurred: {error}")

        time.sleep(POLL_INTERVAL_SEC)
