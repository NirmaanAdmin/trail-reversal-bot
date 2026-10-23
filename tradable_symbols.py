"""
tradable_symbols.py
───────────────────
Whitelist of all NSE F&O-eligible underlyings as of mid-2026. Used by the
server to validate incoming webhook `symbol` values BEFORE hitting the
resolver — protects against typos in Pine that would otherwise reach Kite.

Single source of truth. If NSE adds/removes a symbol, edit this set and
redeploy. The /underlyings endpoint reads the live Kite instruments dump,
which is the *authoritative* runtime check; this set is a fast pre-filter
and documentation aid.

  • 5 indices  (cash-settled, weekly expiry, tight spreads)
  • 209 stocks (physically settled, monthly expiry)
"""

INDEX_UNDERLYINGS = frozenset({
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
})

STOCK_UNDERLYINGS = frozenset({
    # A
    "360ONE", "ABB", "ABCAPITAL", "ADANIENSOL", "ADANIENT", "ADANIGREEN",
    "ADANIPORTS", "ADANIPOWER", "ALKEM", "AMBER", "AMBUJACEM", "ANGELONE",
    "APLAPOLLO", "APOLLOHOSP", "ASHOKLEY", "ASIANPAINT", "ASTRAL", "AUBANK",
    "AUROPHARMA", "AXISBANK",
    # B
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHLDNG", "BAJFINANCE", "BANDHANBNK",
    "BANKBARODA", "BANKINDIA", "BDL", "BEL", "BHARATFORG", "BHARTIARTL",
    "BHEL", "BIOCON", "BLUESTARCO", "BOSCHLTD", "BPCL", "BRITANNIA", "BSE",
    # C
    "CAMS", "CANBK", "CDSL", "CGPOWER", "CHOLAFIN", "CIPLA", "COALINDIA",
    "COCHINSHIP", "COFORGE", "COLPAL", "CONCOR", "CROMPTON", "CUMMINSIND",
    # D
    "DABUR", "DALBHARAT", "DELHIVERY", "DIVISLAB", "DIXON", "DLF", "DMART",
    "DRREDDY",
    # E
    "EICHERMOT", "ETERNAL", "EXIDEIND",
    # F
    "FEDERALBNK", "FORCEMOT", "FORTIS",
    # G
    "GAIL", "GLENMARK", "GMRAIRPORT", "GODFRYPHLP", "GODREJCP", "GODREJPROP",
    "GRASIM",
    # H
    "HAL", "HAVELLS", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDPETRO", "HINDUNILVR", "HINDZINC",
    "HYUNDAI",
    # I
    "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDEA", "IDFCFIRSTB", "IEX",
    "INDHOTEL", "INDIANB", "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY",
    "INOXWIND", "IOC", "IREDA", "IRFC", "ITC",
    # J
    "JINDALSTEL", "JIOFIN", "JSWENERGY", "JSWSTEEL", "JUBLFOOD",
    # K
    "KALYANKJIL", "KAYNES", "KEI", "KFINTECH", "KOTAKBANK", "KPITTECH",
    # L
    "LAURUSLABS", "LICHSGFIN", "LICI", "LODHA", "LT", "LTF", "LTM", "LUPIN",
    # M
    "M&M", "MANAPPURAM", "MANKIND", "MARICO", "MARUTI", "MAXHEALTH",
    "MAZDOCK", "MCX", "MFSL", "MOTHERSON", "MOTILALOFS", "MPHASIS",
    "MUTHOOTFIN",
    # N
    "NAM-INDIA", "NATIONALUM", "NAUKRI", "NBCC", "NESTLEIND", "NHPC", "NMDC",
    "NTPC", "NUVAMA", "NYKAA",
    # O
    "OBEROIRLTY", "OFSS", "OIL", "ONGC",
    # P
    "PAGEIND", "PATANJALI", "PAYTM", "PERSISTENT", "PETRONET", "PFC", "PGEL",
    "PHOENIXLTD", "PIDILITIND", "PIIND", "PNB", "PNBHOUSING", "POLICYBZR",
    "POLYCAB", "POWERGRID", "POWERINDIA", "PREMIERENE", "PRESTIGE",
    # R
    "RBLBANK", "RECLTD", "RELIANCE", "RVNL",
    # S
    "SAIL", "SAMMAANCAP", "SBICARD", "SBILIFE", "SBIN", "SHREECEM",
    "SHRIRAMFIN", "SIEMENS", "SOLARINDS", "SONACOMS", "SRF", "SUNPHARMA",
    "SUPREMEIND", "SUZLON", "SWIGGY",
    # T
    "TATACONSUM", "TATAELXSI", "TATAPOWER", "TATASTEEL", "TCS", "TECHM",
    "TIINDIA", "TITAN", "TMPV", "TORNTPHARM", "TRENT", "TVSMOTOR",
    # U
    "ULTRACEMCO", "UNIONBANK", "UNITDSPR", "UNOMINDA", "UPL",
    # V
    "VBL", "VEDL", "VMM", "VOLTAS",
    # W
    "WAAREEENER", "WIPRO",
    # Y, Z
    "YESBANK", "ZYDUSLIFE",
})

TRADABLE_UNDERLYINGS = INDEX_UNDERLYINGS | STOCK_UNDERLYINGS

# ─── Spot LTP symbol mapping ───────────────────────────────────────────
# For computing rungs / SL on the underlying spot, we need to fetch the
# spot LTP. Stocks: `NSE:<NAME>`. Indices: special tradingsymbols.
INDEX_SPOT_SYMBOL = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "NIFTYNXT50": "NSE:NIFTY NEXT 50",
}


def is_tradable(underlying: str) -> bool:
    """Cheap whitelist check. Case-sensitive, must match canonical form."""
    return underlying in TRADABLE_UNDERLYINGS


def is_index(underlying: str) -> bool:
    return underlying in INDEX_UNDERLYINGS


def spot_ltp_key(underlying: str) -> str:
    """Returns the kite.ltp() key for fetching this underlying's spot LTP.
    e.g. spot_ltp_key('TATASTEEL') → 'NSE:TATASTEEL'
         spot_ltp_key('NIFTY')     → 'NSE:NIFTY 50'
    """
    if underlying in INDEX_SPOT_SYMBOL:
        return INDEX_SPOT_SYMBOL[underlying]
    return f"NSE:{underlying}"


# ─── TradingView ticker → Kite underlying ──────────────────────────────
# TradingView writes NSE symbols with '_' where NSE uses '-' or '&'
# (BAJAJ_AUTO, M_M) and uses its own names for some indices. Continuous
# futures charts append '1!' / '2!'. Everything below folds those onto the
# Kite `instruments.name` used by the option chain.
TV_INDEX_ALIASES = {
    "NIFTY": "NIFTY", "NIFTY50": "NIFTY", "NIFTY_50": "NIFTY", "CNXNIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY", "NIFTY_BANK": "BANKNIFTY",
    "CNXFINANCE": "FINNIFTY", "FINNIFTY": "FINNIFTY", "NIFTY_FIN_SERVICE": "FINNIFTY",
    "NIFTY_MID_SELECT": "MIDCPNIFTY", "MIDCPNIFTY": "MIDCPNIFTY", "NIFTYMIDSELECT": "MIDCPNIFTY",
    "NIFTYJR": "NIFTYNXT50", "NIFTYNXT50": "NIFTYNXT50", "NIFTY_NEXT_50": "NIFTYNXT50",
    "NIFTYNEXT50": "NIFTYNXT50",
}


def resolve_underlying(raw: str):
    """'NSE:BAJAJ_AUTO' / 'BAJAJ_AUTO' / 'HAVELLS1!' / 'NSE:CNXFINANCE' /
    'HAVELLS-EQ' → canonical whitelist name, or None if not tradable."""
    s = (raw or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    s = s.replace("-EQ", "").strip()
    while s.endswith("!") and len(s) > 2 and s[-2].isdigit():
        s = s[:-2]
    if s in TV_INDEX_ALIASES:
        return TV_INDEX_ALIASES[s]
    for cand in (s, s.replace("_", "-"), s.replace("_", "&")):
        if cand in TRADABLE_UNDERLYINGS:
            return cand
    return None
