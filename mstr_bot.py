import requests
from bs4 import BeautifulSoup
import json
import datetime
import os

# (Tidak ada lagi konfigurasi statis, 100% dinamis)

# ==========================================
# 2. DATA FETCHERS (API & NEXT.JS SCRAPER)
# ==========================================
def fetch_dashboard_data():
    headers = {
        'accept': '*/*',
        'accept-language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
        'origin': 'https://www.strategy.com',
        'referer': 'https://www.strategy.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    try:
        res_mstr = requests.get('https://api.strategy.com/btc/mstrKpiData', headers=headers, timeout=10)
        res_btc = requests.get('https://api.strategy.com/btc/bitcoinKpis', headers=headers, timeout=10)
        
        if res_mstr.status_code == 200 and res_btc.status_code == 200:
            data_mstr = res_mstr.json()[0]
            data_btc = res_btc.json().get('results', {})
            
            return {
                'live_btc_price': float(data_btc.get('ufPrice', 0)),
                'live_mstr_price': float(data_mstr.get('ufPrice', 0)),
                'btc_holdings': int(str(data_btc.get('btcHoldings', '0')).replace(',', '')),
                'total_debt_m': float(str(data_mstr.get('debt', '0')).replace(',', '')),
                'annual_dividends_raw': data_btc.get('totalAnnualDividends', 0),
                'market_cap_m': float(str(data_mstr.get('marketCap', '0')).replace(',', '')),
                'enterprise_value_m': float(str(data_mstr.get('entVal', '0')).replace(',', '')),
                'preferred_equity_m': float(str(data_mstr.get('pref', '0')).replace(',', '')),
                'net_leverage': float(data_btc.get('debtByBN', 0)),
                'amplification': float(data_btc.get('debtPrefByBN', 0)),
                'btc_years_dividend_coverage': float(data_btc.get('btcYearsOfDividends', 0)),
                'usd_months_dividend_coverage': float(data_btc.get('usdMonthsOfDividends', 0)),
            }
    except Exception as e:
        print(f"Error fetching dashboard: {e}")
    return None

def fetch_shares_data():
    headers = {
        'accept': 'text/html',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    try:
        res = requests.get('https://www.strategy.com/shares', headers=headers, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            next_data_script = soup.find('script', id='__NEXT_DATA__')
            
            if next_data_script:
                json_data = json.loads(next_data_script.string)
                shares_list = json_data.get('props', {}).get('pageProps', {}).get('shares', [])
                
                if shares_list:
                    latest = shares_list[-1]
                    return {
                        'basic_shares': latest.get('basic_shares_outstanding', 0) * 1000,
                        'diluted_shares': latest.get('assumed_diluted_shares_outstanding', 0) * 1000,
                        'btc_yield_ytd': float(latest.get('btc_yield_ytd', 0))
                    }
    except Exception as e:
        print(f"Error fetching shares: {e}")
    return None

# ==========================================
# 3. CALCULATOR (FINANCIAL MODELING)
# ==========================================
def calculate_metrics(dash_data, shares_data):
    # Gabungkan raw input
    raw = {**dash_data, **shares_data}
    
    mc_basic = raw['market_cap_m']
    mc_diluted = (raw['diluted_shares'] * raw['live_mstr_price']) / 1000000
    nav_btc = raw['btc_holdings'] * raw['live_btc_price']
    
    # Safe division helpers
    def safe_div(num, den): return num / den if den else 0
    
    usd_reserve = mc_basic + raw['total_debt_m'] + raw['preferred_equity_m'] - raw['enterprise_value_m']
    btc_per_share = safe_div(raw['btc_holdings'], raw['diluted_shares'])
    
    return {
        'live_btc_price': raw['live_btc_price'],
        'live_mstr_price': raw['live_mstr_price'],
        'market_cap_basic_m': mc_basic,
        'market_cap_diluted_m': round(mc_diluted, 2),
        'enterprise_value_m': raw['enterprise_value_m'],
        'total_btc': raw['btc_holdings'],
        'nav_btc_reserve_m': round(nav_btc / 1000000, 2),
        'usd_reserve_m': round(usd_reserve, 2),
        'total_debt_m': raw['total_debt_m'],
        'btc_per_share': btc_per_share,
        'value_per_share_usd': round(btc_per_share * raw['live_btc_price'], 2),
        'btc_yield_percent': raw.get('btc_yield_ytd', 0),
        'mnav_basic': round(safe_div(mc_basic * 1000000, nav_btc), 2),
        'mnav_diluted': round(safe_div(mc_diluted * 1000000, nav_btc), 2),
        'mnav_ev': round(safe_div(raw['enterprise_value_m'] * 1000000, nav_btc), 2),
        'net_leverage': raw['net_leverage'],
        'amplification': raw['amplification'],
        'btc_years_dividend_coverage': raw['btc_years_dividend_coverage'],
        'usd_months_dividend_coverage': raw['usd_months_dividend_coverage'],
        'basic_shares': raw['basic_shares'],
        'diluted_shares': raw['diluted_shares'],
        'annual_dividend': raw['annual_dividends_raw']
    }

# ==========================================
# 4. FORMATTER & SENDER TELEGRAM
# ==========================================
def format_and_send(metrics):
    wib = datetime.timezone(datetime.timedelta(hours=7))
    timestamp = datetime.datetime.now(wib).strftime("%d %b %Y | %H:%M WIB")
    
    def fmt_num(num):
        return f"{num:,.2f}" if isinstance(num, float) else f"{num:,}"

    def fmt_int(num):
        return f"{int(num):,}"
    
    html = f"""
🏦 <b>NEVETS HOLDING INVESTMENT - MSTR TRACKER</b> 🏦
📅 <i>{timestamp}</i>
────────────────────────

📈 <b>LIVE MARKET DATA</b>
• <b>Live BTC Price:</b> ${fmt_num(metrics['live_btc_price'])}
• <b>Live MSTR Price:</b> ${fmt_num(metrics['live_mstr_price'])}
• <b>Market Cap (Basic):</b> ${fmt_int(metrics['market_cap_basic_m'])} M
• <b>Market Cap (Diluted):</b> ${fmt_int(metrics['market_cap_diluted_m'])} M
• <b>Enterprise Value:</b> ${fmt_int(metrics['enterprise_value_m'])} M

💰 <b>TREASURY & VALUATION</b>
• <b>Total BTC Holdings:</b> ₿ {fmt_num(metrics['total_btc'])}
• <b>NAV (BTC Reserve):</b> ${fmt_int(metrics['nav_btc_reserve_m'])} M
• <b>USD Reserve:</b> ${fmt_int(metrics['usd_reserve_m'])} M
• <b>Total Debt:</b> ${fmt_int(metrics['total_debt_m'])} M

📊 <b>PER-SHARE METRICS (DILUTED)</b>
• <b>BTC per Share:</b> ₿ {metrics['btc_per_share']:.8f}
• <b>Value per Share:</b> ${fmt_num(metrics['value_per_share_usd'])}
• <b>BTC Yield (YTD):</b> {fmt_num(metrics['btc_yield_percent'])}%

⚖️ <b>PREMIUM / DISCOUNT (mNAV)</b>
• <b>mNAV (Basic vs NAV):</b> {fmt_num(metrics['mnav_basic'])}
• <b>mNAV (Diluted vs NAV):</b> {fmt_num(metrics['mnav_diluted'])}
• <b>mNAV (EV vs NAV):</b> {fmt_num(metrics['mnav_ev'])}

🛡️ <b>RISK & COVERAGE METRICS</b>
• <b>Net Leverage:</b> {fmt_num(metrics['net_leverage'])}%
• <b>Amplification:</b> {fmt_num(metrics['amplification'])}%
• <b>BTC Yrs Div Coverage:</b> {fmt_num(metrics['btc_years_dividend_coverage'])} 
• <b>USD Mos Div Coverage:</b> {fmt_num(metrics['usd_months_dividend_coverage'])} 

🏛 <b>SHARES STRUCTURE</b>
• <b>Basic Shares:</b> {fmt_num(metrics['basic_shares'])}
• <b>Diluted Shares:</b> {fmt_num(metrics['diluted_shares'])}
• <b>Annual Dividend:</b> ${fmt_int(metrics['annual_dividend'] / 1000000)} M

────────────────────────
🤖 <i>Auto-generated tracker</i>
""".strip()

    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')

    if not bot_token or not chat_id:
        print("❌ Error: Token atau Chat ID Telegram belum diset di Environment Variable.")
        return

    print("Mengirim pesan ke Telegram...")
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
        if res.status_code == 200:
            print("✅ Berhasil dikirim!")
        else:
            print(f"❌ Gagal: {res.text}")
    except Exception as e:
        print(f"❌ Error API Telegram: {e}")

# ==========================================
# 5. ORKESTRASI UTAMA
# ==========================================
if __name__ == "__main__":
    print("Memulai ekstraksi data MSTR...")
    dash_data = fetch_dashboard_data()
    shares_data = fetch_shares_data()
    
    if dash_data and shares_data:
        print("Data berhasil ditarik. Melakukan kalkulasi...")
        final_metrics = calculate_metrics(dash_data, shares_data)
        format_and_send(final_metrics)
    else:
        print("❌ Gagal menarik data dari sumber. Proses dihentikan.")
