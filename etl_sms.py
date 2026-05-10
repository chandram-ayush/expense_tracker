import streamlit as st
import pandas as pd
import plotly.express as px
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import unicodedata
from datetime import datetime
from dateutil.relativedelta import relativedelta

# --- CONFIGURATION ---
st.set_page_config(page_title="Expense Dashboard", page_icon="💸", layout="wide", initial_sidebar_state="collapsed")

GOOGLE_SHEET_NAME = "Extense_tracker_db" 

# --- ETL LOGIC: SMS PARSING (SHEET 1) ---
def normalize_text(text):
    if not isinstance(text, str): return ""
    return unicodedata.normalize('NFKC', text)

def parse_raw_sms(sms_text):
    sms_text = normalize_text(sms_text)
    
    savings_pattern = r"a/c XX(\d+) is (credited|debited) for INR ([\d\.]+) on (\d{2}-\d{2}-\d{2}) ([\d:]+) through (.*?)\.Available Bal INR ([\d\.]+)"
    credit_pattern = r"Credit Card (\d+) (debited|credited) with Rs\.([\d\.]+) .*? at (.*?) on (\d{2}-\d{2}-\d{4}) ([\d:]+) through (.*?): .*? limit Rs\. ([\d\.]+)"
    cc_payment_pattern = r"Rs\.([\d\.]+)/- has been received as payment towards your PNB credit card XX(\d+) .*? limit is Rs\.([\d\.]+)"
    mandate_pattern = r"UPI-Mandate for Rs\.([\d\.]+) is successfully created .*? A/c No: XX(\d+)"

    match_savings = re.search(savings_pattern, sms_text)
    match_credit = re.search(credit_pattern, sms_text)
    match_cc_payment = re.search(cc_payment_pattern, sms_text)
    match_mandate = re.search(mandate_pattern, sms_text)

    try:
        if match_savings:
            return {
                "Account_Type": "Savings SMS",
                "Transaction_Type": match_savings.group(2).capitalize(),
                "Amount": float(match_savings.group(3).rstrip('.')),
                "Merchant_Method": match_savings.group(6).strip(),
                "Available_Balance": float(match_savings.group(7).rstrip('.'))
            }
        elif match_credit:
            return {
                "Account_Type": "Credit Card",
                "Transaction_Type": match_credit.group(2).capitalize(),
                "Amount": float(match_credit.group(3).rstrip('.')),
                "Merchant_Method": match_credit.group(4).strip(),
                "Available_Balance": float(match_credit.group(8).rstrip('.'))
            }
        elif match_cc_payment:
            return {
                "Account_Type": "Credit Card",
                "Transaction_Type": "Credited", 
                "Amount": float(match_cc_payment.group(1).rstrip('.')),
                "Merchant_Method": "Online Bill Payment",
                "Available_Balance": float(match_cc_payment.group(3).rstrip('.'))
            }
        elif match_mandate:
            return {
                "Account_Type": "Savings SMS",
                "Transaction_Type": "Debited", 
                "Amount": float(match_mandate.group(1).rstrip('.')),
                "Merchant_Method": "UPI Mandate (Auto-pay)",
                "Available_Balance": 0.0 
            }
    except ValueError:
        return None 
    return None 

@st.cache_data(ttl=3600)
def fetch_sms_data():
    """Fetches and parses SMS data from Sheet1"""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        client = gspread.authorize(creds)
        
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
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
            if ts_val == "" or ts_val == "Timestamp" or ts_val == "None": continue
                
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

# --- ETL LOGIC: BANK STATEMENT (SHEET 2) ---
@st.cache_data(ttl=3600)
def fetch_statement_data():
    """Fetches and parses raw bank statement data from Sheet2"""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        client = gspread.authorize(creds)
        
        try:
            sheet2 = client.open(GOOGLE_SHEET_NAME).sheet2
        except gspread.exceptions.WorksheetNotFound:
            return pd.DataFrame() # Return empty if Sheet2 doesn't exist yet
            
        records = sheet2.get_all_records() # Automatically maps row 1 to dictionary keys
        if not records: return pd.DataFrame()
            
        df = pd.DataFrame(records)
        parsed_list = []
        
        for _, row in df.iterrows():
            # 1. Parse Amounts safely
            dr_str = str(row.get('Dr Amount', '')).replace(',', '').strip()
            cr_str = str(row.get('Cr Amount', '')).replace(',', '').strip()
            
            dr_amt = float(dr_str) if dr_str.replace('.', '', 1).isdigit() else 0.0
            cr_amt = float(cr_str) if cr_str.replace('.', '', 1).isdigit() else 0.0
            
            if dr_amt > 0:
                txn_type = "Debited"
                amt = dr_amt
            elif cr_amt > 0:
                txn_type = "Credited"
                amt = cr_amt
            else:
                continue # Skip rows with no money movement
                
            # 2. Parse Balance (Clean 'Cr.', 'Dr.', and commas)
            bal_str = str(row.get('Balance', '')).replace(',', '').replace('Cr.', '').replace('Dr.', '').strip()
            bal = float(bal_str) if bal_str.replace('.', '', 1).isdigit() else 0.0
            
            # 3. Clean Description (Shorten long UPI strings to just the merchant name)
            desc = str(row.get('Description', 'Unknown')).strip()
            if desc.startswith('UPI/'):
                parts = desc.split('/')
                if len(parts) >= 4:
                    desc = parts[3] # Extracts "SWIGGY" from "UPI/DR/.../SWIGGY/..."
                    
            parsed_list.append({
                "Account_Type": "Detailed Statement",
                "Transaction_Type": txn_type,
                "Amount": amt,
                "Merchant_Method": desc,
                "Available_Balance": bal,
                "Date": pd.to_datetime(row.get('Txn Date'), dayfirst=True, errors='coerce')
            })
            
        final_df = pd.DataFrame(parsed_list)
        if not final_df.empty:
            final_df = final_df.dropna(subset=['Date']).sort_values(by="Date", ascending=False)
        return final_df
    except Exception as e:
        st.error(f"Failed to process Statement data: {e}")
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
                if len(tier_data) > 5:
                    lines.append(f"<i>...and {len(tier_data)-5} more</i>")
                hover_texts.append("<br>".join(lines))
                
        tier_summary['Hover_Text'] = hover_texts
        tier_summary['Percentage'] = (tier_summary['Amount'] / total_spend) * 100
        
        fig_bars = px.bar(
            tier_summary, x='Tier', y='Percentage', 
            text=tier_summary['Percentage'].apply(lambda x: f"{x:.1f}%" if x > 0 else ""),
            custom_data=['Hover_Text'],
            labels={'Percentage': '% of Spend'},
            color='Tier', color_discrete_sequence=px.colors.sequential.Teal
        )
        
        fig_bars.update_traces(
            textposition='outside',
            hovertemplate="<b>%{x}</b><br>Share: %{y:.1f}%<br><br><b>Transactions:</b><br>%{customdata[0]}<extra></extra>"
        )
        fig_bars.update_layout(showlegend=False, margin=dict(t=10, b=0, l=0, r=0))
        st.plotly_chart(fig_bars, use_container_width=True)
            
    st.markdown("---")

# --- DASHBOARD UI ---
st.title("💸 Personal Expenditure Dashboard")

col_title, col_sync = st.columns([4, 1])
with col_sync:
    if st.button("🔄 Force Sync", use_container_width=True):
        st.cache_data.clear() 
        st.rerun()

# Fetch both datasets
sms_df = fetch_sms_data()
stmt_df = fetch_statement_data()

# Combine them into one unified DataFrame (they share the exact same schema now)
df = pd.concat([sms_df, stmt_df], ignore_index=True)

now = datetime.now()
current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
prev_month_start = current_month_start - relativedelta(months=1)

curr_month_name = now.strftime("%B %Y")
prev_month_name = prev_month_start.strftime("%B %Y")

if df.empty:
    st.info("No transaction data found. Ensure your data exists in Sheet1 (SMS) or Sheet2 (Statement).")
else:
    # Set up 3 Tabs instead of 2
    tab1, tab2, tab3 = st.tabs(["🏦 Savings SMS", "💳 Credit Card", "📄 Detailed Statement (Sheet 2)"])

    account_types = ["Savings SMS", "Credit Card", "Detailed Statement"]
    
    for tab, acc_type in zip([tab1, tab2, tab3], account_types):
        with tab:
            acc_df = df[df['Account_Type'] == acc_type]
            
            if acc_df.empty:
                st.write(f"No transactions recorded for **{acc_type}**.")
                continue

            # Top Level KPIs
            curr_spend, prev_spend, balance = calculate_kpis(acc_df, current_month_start, prev_month_start)
            kpi1, kpi2, kpi3 = st.columns(3)
            
            kpi1.metric("Available Balance / Limit", f"₹{balance:,.2f}")
            kpi2.metric("Current Month Spend", f"₹{curr_spend:,.2f}", f"₹{curr_spend - prev_spend:,.2f} vs last mo", delta_color="inverse")
            kpi3.metric("Previous Month Spend", f"₹{prev_spend:,.2f}")

            st.markdown("---")

            debits_df = acc_df[acc_df['Transaction_Type'] == 'Debited']
            
            # 1. Current Month Section
            curr_df = debits_df[debits_df['Date'] >= current_month_start]
            render_expense_section("Current Month Expenditure", curr_month_name, curr_df)

            # 2. Previous Month Section
            prev_df = debits_df[(debits_df['Date'] >= prev_month_start) & (debits_df['Date'] < current_month_start)]
            render_expense_section("Previous Month Expenditure", prev_month_name, prev_df)

            # 3. Overall Expenditure Section
            render_expense_section("Overall Historical Expenditure", "All Time", debits_df)

            # Bottom Section: Table
            st.subheader("Recent Transactions")
            display_df = acc_df[['Date', 'Transaction_Type', 'Amount', 'Merchant_Method']].head(100).copy()
            
            # Use safe dt.strftime so NaT (Not a Time) errors don't crash the table
            display_df['Date'] = display_df['Date'].dt.strftime('%d %b %Y').fillna('Unknown Date')
            
            st.dataframe(
                display_df.style.format({'Amount': "₹{:.2f}"}),
                use_container_width=True,
                height=350
            )