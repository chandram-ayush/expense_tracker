import streamlit as st
import pandas as pd
import plotly.express as px
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import unicodedata
from datetime import datetime
from dateutil.relativedelta import relativedelta
import io
import pdfplumber
import hashlib # <-- NEW: Import hashlib

# --- CONFIGURATION ---
st.set_page_config(page_title="Expense Dashboard", page_icon="💸", layout="wide", initial_sidebar_state="collapsed")

# ⚠️ DATABASES & SECURITY ⚠️
GOOGLE_SHEET_SMS = "Extense_tracker_db"      
GOOGLE_SHEET_STMT = "Statement_DB"           

# <-- NEW: Store the HASH, not the password -->
# Paste the hash you generated in Step 1 inside these quotes
APP_PASSWORD_HASH = "98d60b268501a0d34d7d0248d8d68deb5644a7a801b1eaac8885d8d8c309624fb" 

# Initialize Session States
if 'authenticated' not in st.session_state:
    st.session_state['authenticated'] = False
if 'uploaded_stmt_df' not in st.session_state:
    st.session_state['uploaded_stmt_df'] = pd.DataFrame()

# --- LOGIN WALL ---
if not st.session_state['authenticated']:
    st.title("🔒 Access Restricted")
    st.write("Please enter the custom code to access your financial dashboard.")
    
    with st.form("login_form"):
        entered_password = st.text_input("Passcode", type="password")
        submit_button = st.form_submit_button("Unlock Dashboard")
        
        if submit_button:
            # <-- NEW: Hash the input before comparing -->
            hashed_input = hashlib.sha256(entered_password.encode()).hexdigest()
            
            if hashed_input == APP_PASSWORD_HASH:
                st.session_state['authenticated'] = True
                st.rerun() 
            else:
                st.error("Incorrect passcode. Please try again.")
    st.stop() # Halts execution of the rest of the script until logged in

# ==========================================
# EVERYTHING BELOW ONLY RUNS IF AUTHENTICATED
# ==========================================
# ... [The rest of your app remains exactly the same] ...

# ==========================================
# EVERYTHING BELOW ONLY RUNS IF AUTHENTICATED
# ==========================================

# Sidebar Logout
with st.sidebar:
    if st.button("🚪 Logout", use_container_width=True):
        st.session_state['authenticated'] = False
        st.session_state['uploaded_stmt_df'] = pd.DataFrame() # Clear session data
        st.rerun()

# --- UTILITIES ---
def normalize_text(text):
    if not isinstance(text, str): return ""
    return unicodedata.normalize('NFKC', text)

def safe_float(val):
    """Safely converts string amounts with commas, currencies, or Cr/Dr to pure floats."""
    if pd.isna(val) or val is None: return 0.0
    clean_val = str(val).upper().replace(',', '').replace('₹', '').replace('RS.', '').replace('CR.', '').replace('DR.', '').replace('CR', '').replace('DR', '').replace('-', '').strip()
    try:
        return float(clean_val)
    except ValueError:
        return 0.0

# --- ETL LOGIC: FILE UPLOAD PARSER ---
def parse_uploaded_file(uploaded_file, account_type_label):
    filename = uploaded_file.name.lower()
    raw_df = pd.DataFrame()

    try:
        if filename.endswith('.csv'):
            raw_df = pd.read_csv(uploaded_file)
        elif filename.endswith('.xlsx'):
            raw_df = pd.read_excel(uploaded_file)
        elif filename.endswith('.pdf'):
            tables = []
            with pdfplumber.open(uploaded_file) as pdf:
                for page in pdf.pages:
                    table = page.extract_table()
                    if table:
                        tables.extend(table)
            if tables:
                raw_df = pd.DataFrame(tables[1:], columns=tables[0])
            else:
                st.error("Could not find any tabular data in the PDF.")
                return pd.DataFrame()

        if raw_df.empty: return pd.DataFrame()

        raw_df.columns = [str(col).strip().lower() for col in raw_df.columns]
        
        date_col = next((c for c in raw_df.columns if 'date' in c), None)
        desc_col = next((c for c in raw_df.columns if 'narration' in c or 'particulars' in c or 'description' in c or 'details' in c), None)
        dr_col = next((c for c in raw_df.columns if 'debit' in c or 'withdrawal' in c), None)
        cr_col = next((c for c in raw_df.columns if 'credit' in c or 'deposit' in c), None)
        bal_col = next((c for c in raw_df.columns if 'balance' in c), None)

        if not all([date_col, desc_col]):
            st.warning("Could not auto-detect necessary columns (Date, Description) in the uploaded file.")
            return pd.DataFrame()

        parsed_list = []
        for index, row in raw_df.iterrows():
            if pd.isna(row[date_col]): continue
            
            dr_amt = safe_float(row[dr_col]) if dr_col and not pd.isna(row[dr_col]) else 0.0
            cr_amt = safe_float(row[cr_col]) if cr_col and not pd.isna(row[cr_col]) else 0.0
            
            if dr_amt > 0:
                txn_type = "Debited"
                amt = dr_amt
            elif cr_amt > 0:
                txn_type = "Credited"
                amt = cr_amt
            else:
                continue 
                
            bal = safe_float(row[bal_col]) if bal_col else 0.0
            desc = str(row[desc_col]).strip()
            
            if desc.startswith('UPI/'):
                parts = desc.split('/')
                if len(parts) >= 4:
                    desc = parts[3] 
                    
            parsed_list.append({
                "Account_Type": account_type_label,
                "Transaction_Type": txn_type,
                "Amount": amt,
                "Merchant_Method": desc,
                "Available_Balance": bal,
                "Date": pd.to_datetime(str(row[date_col]).strip(), dayfirst=True, errors='coerce')
            })
            
        final_df = pd.DataFrame(parsed_list)
        if not final_df.empty:
            final_df = final_df.dropna(subset=['Date']).sort_values(by="Date", ascending=False)
        return final_df

    except Exception as e:
        st.error(f"Error parsing file: {e}")
        return pd.DataFrame()

# --- ETL LOGIC: SMS PARSING ---
def parse_raw_sms(sms_text):
    sms_text = normalize_text(sms_text)
    
    credit_pattern = r"(?i)Credit Card.*?(\d+).*?(debited|credited).*?(?:Rs\.?|INR)\s*([\d\.,]+).*?at\s+(.*?)\s+on\s+(\d{2}-\d{2}-\d{2,4})\s+([\d:]+).*?limit.*?(?:Rs\.?|INR)\s*([\d\.,]+)"
    savings_pattern = r"(?i)(?:a/c|acct).*?(\d+).*?(credited|debited).*?(?:INR|Rs\.?)\s*([\d\.,]+).*?(?:on|date)?\s*(\d{2}-\d{2}-\d{2,4})\s+([\d:]+).*?(?:through|info|at|to|by)\s+(.*?)\.?\s*(?:Available|Avl)\s*[Bb]al.*?(?:INR|Rs\.?)\s*([\d\.,]+)"
    cc_payment_pattern = r"(?i)(?:Rs\.?|INR)\s*([\d\.,]+).*?received.*?payment.*?credit card.*?(\d+).*?limit.*?(?:Rs\.?|INR)\s*([\d\.,]+)"
    mandate_pattern = r"(?i)UPI-Mandate.*?(?:Rs\.?|INR)\s*([\d\.,]+).*?successfully.*?(?:A/c No|a/c).*?(\d+)"

    match_credit = re.search(credit_pattern, sms_text)
    match_savings = re.search(savings_pattern, sms_text)
    match_cc_payment = re.search(cc_payment_pattern, sms_text)
    match_mandate = re.search(mandate_pattern, sms_text)

    try:
        if match_credit:
            raw_merchant = match_credit.group(4).strip()
            clean_merchant = re.sub(r'\s+', ' ', raw_merchant)
            return {
                "Account_Type": "Credit Card SMS",
                "Transaction_Type": match_credit.group(2).capitalize(),
                "Amount": safe_float(match_credit.group(3)),
                "Merchant_Method": clean_merchant,
                "Available_Balance": safe_float(match_credit.group(7))
            }
        elif match_savings:
            raw_merchant = match_savings.group(6).strip()
            clean_merchant = re.sub(r'\s+', ' ', raw_merchant)
            return {
                "Account_Type": "Savings SMS",
                "Transaction_Type": match_savings.group(2).capitalize(),
                "Amount": safe_float(match_savings.group(3)),
                "Merchant_Method": clean_merchant,
                "Available_Balance": safe_float(match_savings.group(7))
            }
        elif match_cc_payment:
            return {
                "Account_Type": "Credit Card SMS",
                "Transaction_Type": "Credited", 
                "Amount": safe_float(match_cc_payment.group(1)),
                "Merchant_Method": "Online Bill Payment",
                "Available_Balance": safe_float(match_cc_payment.group(3))
            }
        elif match_mandate:
            return {
                "Account_Type": "Savings SMS",
                "Transaction_Type": "Debited", 
                "Amount": safe_float(match_mandate.group(1)),
                "Merchant_Method": "UPI Mandate (Auto-pay)",
                "Available_Balance": 0.0 
            }
    except Exception as e:
        return None 
    return None 

@st.cache_data(ttl=3600)
def fetch_sms_data():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        client = gspread.authorize(creds)
        
        sheet = client.open(GOOGLE_SHEET_SMS).sheet1
        records = sheet.get_all_values()
        
        if len(records) <= 1: return pd.DataFrame() 
            
        raw_df = pd.DataFrame(records)
        if len(raw_df.columns) >= 3:
            raw_df = raw_df.iloc[:, 0:3]
            raw_df.columns = ["Sender", "Raw_Text", "Timestamp"]
        else:
            return pd.DataFrame()
        
        parsed_list = []
        for index, row in raw_df.iterrows():
            ts_val = str(row["Timestamp"]).strip()
            if ts_val in ["", "Timestamp", "None"]: continue
                
            parsed = parse_raw_sms(row["Raw_Text"])
            if parsed:
                if ts_val.isdigit() and len(ts_val) > 10:
                    parsed["Date"] = pd.to_datetime(int(ts_val), unit='ms')
                else:
                    parsed["Date"] = pd.to_datetime(ts_val, errors='coerce')
                parsed_list.append(parsed)
                
        final_df = pd.DataFrame(parsed_list)
        if not final_df.empty:
            final_df = final_df.dropna(subset=['Date']).drop_duplicates().sort_values(by="Date", ascending=False)
        return final_df
    except Exception as e:
        st.error(f"Failed to process SMS data: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def fetch_statement_data(worksheet_name, account_type_label):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        client = gspread.authorize(creds)
        
        try:
            ws = client.open(GOOGLE_SHEET_STMT).worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            st.warning(f"Could not find tab '{worksheet_name}' in {GOOGLE_SHEET_STMT}.")
            return pd.DataFrame() 
            
        records = ws.get_all_values()
        if len(records) <= 1: return pd.DataFrame()
        
        raw_df = pd.DataFrame(records)
        
        if len(raw_df.columns) >= 8:
            raw_df = raw_df.iloc[:, 0:8]
            raw_df.columns = ["Txn_No", "Txn_Date", "Description", "Branch_Name", "Cheque_No", "Dr_Amount", "Cr_Amount", "Balance"]
        else:
            return pd.DataFrame()
            
        parsed_list = []
        for index, row in raw_df.iloc[1:].iterrows():
            if str(row["Txn_Date"]).strip() == "" and str(row["Description"]).strip() == "": continue
                
            dr_amt = safe_float(row["Dr_Amount"])
            cr_amt = safe_float(row["Cr_Amount"])
            
            if dr_amt > 0:
                txn_type = "Debited"
                amt = dr_amt
            elif cr_amt > 0:
                txn_type = "Credited"
                amt = cr_amt
            else: continue 
                
            bal = safe_float(row["Balance"])
            desc = str(row["Description"]).strip()
            if desc.startswith('UPI/'):
                parts = desc.split('/')
                if len(parts) >= 4: desc = parts[3] 
                    
            parsed_list.append({
                "Account_Type": account_type_label,
                "Transaction_Type": txn_type,
                "Amount": amt,
                "Merchant_Method": desc,
                "Available_Balance": bal,
                "Date": pd.to_datetime(str(row["Txn_Date"]).strip(), dayfirst=True, errors='coerce')
            })
            
        final_df = pd.DataFrame(parsed_list)
        if not final_df.empty:
            final_df = final_df.dropna(subset=['Date']).sort_values(by="Date", ascending=False)
        return final_df
    except Exception as e:
        return pd.DataFrame()

# --- KPI & RENDERING LOGIC ---
def calculate_kpis(df, current_month_start, prev_month_start):
    if df.empty: return 0, 0, 0
    balances = df[df['Available_Balance'] > 0]['Available_Balance']
    latest_balance = balances.iloc[0] if not balances.empty else 0
    debits = df[df['Transaction_Type'] == 'Debited']
    curr_month_spend = debits[debits['Date'] >= current_month_start]['Amount'].sum()
    prev_month_spend = debits[(debits['Date'] >= prev_month_start) & (debits['Date'] < current_month_start)]['Amount'].sum()
    return curr_month_spend, prev_month_spend, latest_balance

def render_expense_section(section_title, subtitle, df_subset):
    st.subheader(f"{section_title} ({subtitle})")
    total_spend = df_subset['Amount'].sum()
    
    if df_subset.empty or total_spend == 0:
        st.info("No expenditures recorded for this period.")
        st.markdown("---")
        return
        
    st.metric(label="Total Expenditure", value=f"₹{total_spend:,.2f}")
    chart_col1, chart_col2 = st.columns(2)
    
    with chart_col1:
        st.markdown("**Merchant/Method Breakdown**")
        fig_pie = px.pie(df_subset, values='Amount', names='Merchant_Method', hole=0.4)
        fig_pie.update_layout(margin=dict(t=10, b=0, l=0, r=0)) 
        st.plotly_chart(fig_pie, use_container_width=True)

    with chart_col2:
        st.markdown("**Expense Tier Distribution**")
        bins = [0, 100, 200, 300, 500, 1000, 2000, 5000, 10000, float('inf')]
        labels = ['< ₹100', '₹100-200', '₹200-300', '₹300-500', '₹500-1k', '₹1k-2k', '₹2k-5k', '₹5k-10k', '₹10k+']
        
        temp_df = df_subset.copy()
        temp_df['Tier'] = pd.cut(temp_df['Amount'], bins=bins, labels=labels, right=False)
        tier_summary = temp_df.groupby('Tier', observed=False)['Amount'].sum().reset_index()
        
        hover_texts = []
        for tier in tier_summary['Tier']:
            tier_data = temp_df[temp_df['Tier'] == tier]
            if tier_data.empty:
                hover_texts.append("No transactions")
            else:
                top_txns = tier_data.sort_values('Amount', ascending=False).head(5)
                lines = [f"{row['Merchant_Method']}: ₹{row['Amount']}" for _, row in top_txns.iterrows()]
                if len(tier_data) > 5: lines.append(f"<i>...and {len(tier_data)-5} more</i>")
                hover_texts.append("<br>".join(lines))
                
        tier_summary['Hover_Text'] = hover_texts
        tier_summary['Percentage'] = (tier_summary['Amount'] / total_spend) * 100
        
        fig_bars = px.bar(
            tier_summary, x='Tier', y='Percentage', 
            text=tier_summary['Percentage'].apply(lambda x: f"{x:.1f}%" if x > 0 else ""),
            custom_data=['Hover_Text'], labels={'Percentage': '% of Spend'},
            color='Tier', color_discrete_sequence=px.colors.sequential.Teal
        )
        
        fig_bars.update_traces(textposition='outside', hovertemplate="<b>%{x}</b><br>Share: %{y:.1f}%<br><br><b>Transactions:</b><br>%{customdata[0]}<extra></extra>")
        fig_bars.update_layout(showlegend=False, margin=dict(t=10, b=0, l=0, r=0))
        st.plotly_chart(fig_bars, use_container_width=True)
            
    st.markdown("---")

def render_account_view(acc_df, current_month_start, prev_month_start, curr_month_name, prev_month_name):
    if acc_df.empty:
        st.warning("No transactions recorded for this dataset.")
        return

    curr_spend, prev_spend, balance = calculate_kpis(acc_df, current_month_start, prev_month_start)
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("Available Balance / Limit", f"₹{balance:,.2f}")
    kpi2.metric("Current Month Spend", f"₹{curr_spend:,.2f}", f"₹{curr_spend - prev_spend:,.2f} vs last mo", delta_color="inverse")
    kpi3.metric("Previous Month Spend", f"₹{prev_spend:,.2f}")
    st.markdown("---")

    debits_df = acc_df[acc_df['Transaction_Type'] == 'Debited']
    
    curr_df = debits_df[debits_df['Date'] >= current_month_start]
    render_expense_section("Current Month Expenditure", curr_month_name, curr_df)

    prev_df = debits_df[(debits_df['Date'] >= prev_month_start) & (debits_df['Date'] < current_month_start)]
    render_expense_section("Previous Month Expenditure", prev_month_name, prev_df)

    render_expense_section("Overall Historical Expenditure", "All Time", debits_df)

    st.subheader("Recent Transactions")
    display_df = acc_df[['Date', 'Transaction_Type', 'Amount', 'Merchant_Method']].head(100).copy()
    display_df['Date'] = display_df['Date'].dt.strftime('%d %b %Y').fillna('Unknown Date')
    st.dataframe(display_df.style.format({'Amount': "₹{:.2f}"}), use_container_width=True, height=350)

# --- DASHBOARD UI INITIALIZATION ---
st.title("💸 Personal Expenditure Dashboard")

col_title, col_sync = st.columns([4, 1])
with col_sync:
    if st.button("🔄 Force Sync", use_container_width=True):
        st.cache_data.clear() 
        st.rerun()

# Fetch Data
sms_df = fetch_sms_data()
savings_stmt_df = fetch_statement_data("Savings", "Savings Statement")
cc_stmt_df = fetch_statement_data("Credit Card", "Credit Card Statement")
df = pd.concat([sms_df, savings_stmt_df, cc_stmt_df], ignore_index=True)

# Date Calculations
now = datetime.now()

# 1. Standard Calendar Cycle
std_current_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
std_prev_start = std_current_start - relativedelta(months=1)
std_curr_name = now.strftime("%B %Y")
std_prev_name = std_prev_start.strftime("%B %Y")

# 2. Credit Card Billing Cycle
if now.day >= 15:
    cc_current_start = now.replace(day=15, hour=0, minute=0, second=0, microsecond=0)
else:
    cc_current_start = (now - relativedelta(months=1)).replace(day=15, hour=0, minute=0, second=0, microsecond=0)

cc_prev_start = cc_current_start - relativedelta(months=1)
cc_curr_name = f"{cc_current_start.strftime('%d %b')} - {(cc_current_start + relativedelta(months=1) - relativedelta(days=1)).strftime('%d %b %Y')}"
cc_prev_name = f"{cc_prev_start.strftime('%d %b')} - {(cc_current_start - relativedelta(days=1)).strftime('%d %b %Y')}"

# --- RENDERING TABS ---
if df.empty and st.session_state['uploaded_stmt_df'].empty:
    st.info("No transaction data found. Please ensure Google Sheets is connected or upload a statement.")

tab1, tab2, tab3 = st.tabs(["🏦 Savings SMS", "💳 Credit Card SMS", "📄 Detailed Statements"])

with tab1:
    render_account_view(df[df['Account_Type'] == "Savings SMS"], std_current_start, std_prev_start, std_curr_name, std_prev_name)

with tab2:
    render_account_view(df[df['Account_Type'] == "Credit Card SMS"], cc_current_start, cc_prev_start, cc_curr_name, cc_prev_name)

with tab3:
    st.write("Data extracted directly from uploaded bank statements and your connected Google Sheets.")
    
    with st.expander("📤 Upload New Bank Statement", expanded=False):
        stmt_type = st.selectbox("Select Account Type for Upload", ["Uploaded Savings Statement", "Uploaded Credit Card Statement"])
        uploaded_file = st.file_uploader("Upload CSV, XLSX, or PDF", type=['csv', 'xlsx', 'pdf'])
        
        if uploaded_file is not None:
            if st.button("Process & Analyze Upload"):
                with st.spinner('Parsing document...'):
                    parsed_df = parse_uploaded_file(uploaded_file, stmt_type)
                    if not parsed_df.empty:
                        st.session_state['uploaded_stmt_df'] = parsed_df
                        st.success(f"Successfully processed {len(parsed_df)} transactions!")
                    else:
                        st.error("Could not extract valid transactions. Please check the file format.")
    
    if not st.session_state['uploaded_stmt_df'].empty:
        df = pd.concat([df, st.session_state['uploaded_stmt_df']], ignore_index=True)

    stmt_tab1, stmt_tab2, stmt_tab3 = st.tabs(["🏦 Savings (Sheet)", "💳 Credit Card (Sheet)", "📤 Uploaded Analysis (Session)"])
    
    with stmt_tab1:
        render_account_view(df[df['Account_Type'] == "Savings Statement"], std_current_start, std_prev_start, std_curr_name, std_prev_name)
        
    with stmt_tab2:
        render_account_view(df[df['Account_Type'] == "Credit Card Statement"], cc_current_start, cc_prev_start, cc_curr_name, cc_prev_name)

    with stmt_tab3:
        if st.session_state['uploaded_stmt_df'].empty:
            st.info("Upload a statement above to see instant analysis here without saving to Google Sheets.")
        else:
            if st.button("Clear Uploaded Data"):
                st.session_state['uploaded_stmt_df'] = pd.DataFrame()
                st.rerun()
            
            uploaded_types = df[df['Account_Type'].str.contains("Uploaded")]['Account_Type'].unique()
            if "Uploaded Credit Card Statement" in uploaded_types:
                render_account_view(df[df['Account_Type'].str.contains("Uploaded")], cc_current_start, cc_prev_start, cc_curr_name, cc_prev_name)
            else:
                render_account_view(df[df['Account_Type'].str.contains("Uploaded")], std_current_start, std_prev_start, std_curr_name, std_prev_name)