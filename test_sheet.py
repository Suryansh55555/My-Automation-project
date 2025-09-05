import gspread
from google.oauth2.service_account import Credentials

SERVICE_ACCOUNT_FILE = "google_credentials.json"
GOOGLE_SHEET_ID = "11t1uq-avw_WZSNXVjfagwoiLcWrrNiwQ4BqWfBBkNqU"

creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
client = gspread.authorize(creds)

try:
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    print("✅ Connected to sheet:", sheet.title)
    print("First row:", sheet.row_values(1))
except Exception as e:
    print("❌ Error:", e)
