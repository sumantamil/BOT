"""
Market Research Module

Provides deeper analysis for:
1. Smart strike selection for options (OI analysis, IV, Greeks estimation)
2. Equity/Stock analysis and screening
3. Support/Resistance levels
4. Trade plan with entry, targets, and stop-loss

Now integrated with real NSE data for:
- Actual Open Interest
- Real option LTP (Last Traded Price)
- Actual PCR ratio
- Real IV (Implied Volatility)
"""

import re
import yfinance as yf
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from loguru import logger

# Import NSE scraper for real data
try:
    from .nse_scraper import nse_scraper, OptionChainData, OptionData
    NSE_AVAILABLE = True
except ImportError:
    NSE_AVAILABLE = False
    logger.warning("NSE scraper not available, using estimated data")

# Import BSE scraper for SENSEX
try:
    from .bse_scraper import bse_scraper
    BSE_AVAILABLE = True
except ImportError:
    BSE_AVAILABLE = False

from .index_config import IndexConfig, NIFTY, get_index


class RiskProfile(Enum):
    """Risk profile for trade recommendations"""
    CONSERVATIVE = "conservative"  # ITM options, lower risk
    MODERATE = "moderate"          # ATM options, balanced
    AGGRESSIVE = "aggressive"      # OTM options, higher risk/reward


@dataclass
class StrikeRecommendation:
    """Detailed strike recommendation"""
    strike: int
    option_type: str  # CE or PE
    moneyness: str    # ITM, ATM, OTM
    distance_from_spot: float
    estimated_delta: float
    risk_level: str
    rationale: str
    confidence: float  # 0-100


@dataclass
class OptionChainAnalysis:
    """Analysis of option chain data"""
    spot_price: float
    atm_strike: int
    
    # OI Analysis
    max_call_oi_strike: int
    max_put_oi_strike: int
    pcr_ratio: float  # Put-Call Ratio
    
    # Support/Resistance from OI
    resistance_levels: List[int]
    support_levels: List[int]
    
    # Recommendations by risk profile
    conservative_strike: StrikeRecommendation
    moderate_strike: StrikeRecommendation
    aggressive_strike: StrikeRecommendation
    
    analysis_time: datetime
    expiry: str


@dataclass
class TradePlan:
    """Trade plan with entry, targets, and stop-loss"""
    # Entry
    entry_price: float  # Option premium
    entry_range_low: float
    entry_range_high: float
    
    # Targets
    target_1: float
    target_1_pct: float
    target_2: float
    target_2_pct: float
    
    # Stop-loss
    stop_loss: float
    stop_loss_pct: float
    
    # Risk-Reward
    risk_reward_ratio: float
    max_loss_per_lot: float
    max_profit_per_lot: float
    
    # Metadata
    lot_size: int
    is_real_data: bool  # True if from NSE, False if estimated
    data_source: str  # "NSE" or "Estimated"


@dataclass 
class StockAnalysis:
    """Stock/Equity analysis result"""
    symbol: str
    name: str
    current_price: float
    change_percent: float
    
    # Technical indicators
    rsi: float
    sma_20: float
    sma_50: float
    sma_200: float
    trend: str  # BULLISH, BEARISH, NEUTRAL
    
    # Support/Resistance
    support: float
    resistance: float
    
    # Recommendation
    action: str  # BUY, SELL, HOLD
    target_price: float
    stop_loss: float
    confidence: float
    rationale: str


class MarketResearch:
    """
    Deep market research for smarter trading decisions.
    
    Features:
    - Option chain analysis with OI data
    - Smart strike selection based on risk profile
    - Support/Resistance calculation
    - Equity screening and analysis
    """
    
    # Complete NSE stocks for equity analysis (NIFTY 50, NIFTY NEXT 50, and popular stocks)
    NSE_STOCKS = {
        # NIFTY 50 Components
        'RELIANCE.NS': 'Reliance Industries',
        'TCS.NS': 'Tata Consultancy Services',
        'HDFCBANK.NS': 'HDFC Bank',
        'INFY.NS': 'Infosys',
        'ICICIBANK.NS': 'ICICI Bank',
        'HINDUNILVR.NS': 'Hindustan Unilever',
        'SBIN.NS': 'State Bank of India',
        'BHARTIARTL.NS': 'Bharti Airtel',
        'ITC.NS': 'ITC Limited',
        'KOTAKBANK.NS': 'Kotak Mahindra Bank',
        'LT.NS': 'Larsen & Toubro',
        'AXISBANK.NS': 'Axis Bank',
        'ASIANPAINT.NS': 'Asian Paints',
        'MARUTI.NS': 'Maruti Suzuki',
        'TITAN.NS': 'Titan Company',
        'BAJFINANCE.NS': 'Bajaj Finance',
        'WIPRO.NS': 'Wipro',
        'SUNPHARMA.NS': 'Sun Pharma',
        'TATAMOTORS.NS': 'Tata Motors',
        'TATASTEEL.NS': 'Tata Steel',
        'HCLTECH.NS': 'HCL Technologies',
        'NTPC.NS': 'NTPC Limited',
        'POWERGRID.NS': 'Power Grid Corporation',
        'ULTRACEMCO.NS': 'UltraTech Cement',
        'BAJAJFINSV.NS': 'Bajaj Finserv',
        'NESTLEIND.NS': 'Nestle India',
        'ONGC.NS': 'Oil & Natural Gas Corporation',
        'M&M.NS': 'Mahindra & Mahindra',
        'JSWSTEEL.NS': 'JSW Steel',
        'TECHM.NS': 'Tech Mahindra',
        'ADANIENT.NS': 'Adani Enterprises',
        'ADANIPORTS.NS': 'Adani Ports & SEZ',
        'COALINDIA.NS': 'Coal India',
        'BPCL.NS': 'Bharat Petroleum',
        'GRASIM.NS': 'Grasim Industries',
        'DIVISLAB.NS': 'Divis Laboratories',
        'DRREDDY.NS': 'Dr Reddys Laboratories',
        'CIPLA.NS': 'Cipla',
        'EICHERMOT.NS': 'Eicher Motors',
        'BRITANNIA.NS': 'Britannia Industries',
        'HEROMOTOCO.NS': 'Hero MotoCorp',
        'INDUSINDBK.NS': 'IndusInd Bank',
        'APOLLOHOSP.NS': 'Apollo Hospitals',
        'SBILIFE.NS': 'SBI Life Insurance',
        'HDFCLIFE.NS': 'HDFC Life Insurance',
        'TATACONSUM.NS': 'Tata Consumer Products',
        'HINDALCO.NS': 'Hindalco Industries',
        'UPL.NS': 'UPL Limited',
        'BAJAJ-AUTO.NS': 'Bajaj Auto',
        
        # NIFTY NEXT 50 Components
        'ADANIGREEN.NS': 'Adani Green Energy',
        'ADANITRANS.NS': 'Adani Transmission',
        'AMBUJACEM.NS': 'Ambuja Cements',
        'AUROPHARMA.NS': 'Aurobindo Pharma',
        'BANKBARODA.NS': 'Bank of Baroda',
        'BERGEPAINT.NS': 'Berger Paints',
        'BIOCON.NS': 'Biocon',
        'BOSCHLTD.NS': 'Bosch',
        'CHOLAFIN.NS': 'Cholamandalam Investment',
        'COLPAL.NS': 'Colgate Palmolive',
        'DABUR.NS': 'Dabur India',
        'DLF.NS': 'DLF Limited',
        'GAIL.NS': 'GAIL India',
        'GODREJCP.NS': 'Godrej Consumer Products',
        'GODREJPROP.NS': 'Godrej Properties',
        'HAVELLS.NS': 'Havells India',
        'ICICIPRULI.NS': 'ICICI Prudential Life',
        'ICICIGI.NS': 'ICICI Lombard General',
        'INDUSTOWER.NS': 'Indus Towers',
        'IOC.NS': 'Indian Oil Corporation',
        'IRCTC.NS': 'IRCTC',
        'IGL.NS': 'Indraprastha Gas',
        'JINDALSTEL.NS': 'Jindal Steel & Power',
        'LICI.NS': 'LIC India',
        'LTIM.NS': 'LTIMindtree',
        'LUPIN.NS': 'Lupin',
        'MARICO.NS': 'Marico',
        'MCDOWELL-N.NS': 'United Spirits',
        'MUTHOOTFIN.NS': 'Muthoot Finance',
        'NAUKRI.NS': 'Info Edge (Naukri)',
        'NMDC.NS': 'NMDC',
        'OBEROIRLTY.NS': 'Oberoi Realty',
        'OFSS.NS': 'Oracle Financial Services',
        'PAGEIND.NS': 'Page Industries',
        'PEL.NS': 'Piramal Enterprises',
        'PERSISTENT.NS': 'Persistent Systems',
        'PETRONET.NS': 'Petronet LNG',
        'PFC.NS': 'Power Finance Corporation',
        'PIDILITIND.NS': 'Pidilite Industries',
        'PNB.NS': 'Punjab National Bank',
        'POLYCAB.NS': 'Polycab India',
        'RECLTD.NS': 'REC Limited',
        'SAIL.NS': 'Steel Authority of India',
        'SBICARD.NS': 'SBI Cards',
        'SHREECEM.NS': 'Shree Cement',
        'SIEMENS.NS': 'Siemens',
        'SRF.NS': 'SRF Limited',
        'TATAPOWER.NS': 'Tata Power',
        'TATACOMM.NS': 'Tata Communications',
        'TATAELXSI.NS': 'Tata Elxsi',
        'TORNTPHARM.NS': 'Torrent Pharma',
        'TRENT.NS': 'Trent',
        'VEDL.NS': 'Vedanta',
        'VOLTAS.NS': 'Voltas',
        'ZOMATO.NS': 'Zomato',
        'ZYDUSLIFE.NS': 'Zydus Lifesciences',
        
        # Other Popular Stocks
        'ABB.NS': 'ABB India',
        'ACC.NS': 'ACC Limited',
        'ABCAPITAL.NS': 'Aditya Birla Capital',
        'ABFRL.NS': 'Aditya Birla Fashion',
        'ALKEM.NS': 'Alkem Laboratories',
        'APLLTD.NS': 'Alembic Pharmaceuticals',
        'ASHOKLEY.NS': 'Ashok Leyland',
        'ASTRAL.NS': 'Astral',
        'ATUL.NS': 'Atul Ltd',
        'AUBANK.NS': 'AU Small Finance Bank',
        'BANDHANBNK.NS': 'Bandhan Bank',
        'BEL.NS': 'Bharat Electronics',
        'BHARATFORG.NS': 'Bharat Forge',
        'BHEL.NS': 'Bharat Heavy Electricals',
        'CANBK.NS': 'Canara Bank',
        'CGPOWER.NS': 'CG Power & Industrial',
        'COFORGE.NS': 'Coforge',
        'CONCOR.NS': 'Container Corporation',
        'CUMMINSIND.NS': 'Cummins India',
        'DEEPAKNTR.NS': 'Deepak Nitrite',
        'DELHIVERY.NS': 'Delhivery',
        'DIXON.NS': 'Dixon Technologies',
        'ESCORTS.NS': 'Escorts Kubota',
        'EXIDEIND.NS': 'Exide Industries',
        'FEDERALBNK.NS': 'Federal Bank',
        'FORTIS.NS': 'Fortis Healthcare',
        'GLAND.NS': 'Gland Pharma',
        'GLENMARK.NS': 'Glenmark Pharma',
        'GMRAIRPORT.NS': 'GMR Airports',
        'GNFC.NS': 'GNFC',
        'GSPL.NS': 'Gujarat State Petronet',
        'HAL.NS': 'Hindustan Aeronautics',
        'HDFCAMC.NS': 'HDFC Asset Management',
        'HINDCOPPER.NS': 'Hindustan Copper',
        'HINDPETRO.NS': 'Hindustan Petroleum',
        'IDFCFIRSTB.NS': 'IDFC First Bank',
        'IEX.NS': 'Indian Energy Exchange',
        'INDHOTEL.NS': 'Indian Hotels',
        'INDIACEM.NS': 'India Cements',
        'INDIAMART.NS': 'IndiaMART InterMESH',
        'INDIANB.NS': 'Indian Bank',
        'IPCA.NS': 'IPCA Laboratories',
        'IRFC.NS': 'Indian Railway Finance',
        'KOVAI.NS': 'Kovai Medical Centre',
        'JKCEMENT.NS': 'JK Cement',
        'JSWENERGY.NS': 'JSW Energy',
        'JUBLFOOD.NS': 'Jubilant FoodWorks',
        'KAJARIACER.NS': 'Kajaria Ceramics',
        'KEI.NS': 'KEI Industries',
        'L&TFH.NS': 'L&T Finance Holdings',
        'LAURUSLABS.NS': 'Laurus Labs',
        'LICHSGFIN.NS': 'LIC Housing Finance',
        'LTI.NS': 'L&T Infotech',
        'M&MFIN.NS': 'Mahindra & Mahindra Financial',
        'MANAPPURAM.NS': 'Manappuram Finance',
        'MAXHEALTH.NS': 'Max Healthcare',
        'MCX.NS': 'Multi Commodity Exchange',
        'METROPOLIS.NS': 'Metropolis Healthcare',
        'MGL.NS': 'Mahanagar Gas',
        'MINDTREE.NS': 'Mindtree',
        'MOTHERSON.NS': 'Motherson Sumi',
        'MPHASIS.NS': 'Mphasis',
        'MRF.NS': 'MRF Limited',
        'NAM-INDIA.NS': 'Nippon Life India AMC',
        'NATIONALUM.NS': 'National Aluminium',
        'NIACL.NS': 'New India Assurance',
        'NHPC.NS': 'NHPC Limited',
        'NOCIL.NS': 'NOCIL',
        'PAYTM.NS': 'One 97 Communications',
        'PI.NS': 'PI Industries',
        'PIIND.NS': 'PI Industries',
        'PRESTIGE.NS': 'Prestige Estates',
        'PVRINOX.NS': 'PVR INOX',
        'RAMCOCEM.NS': 'Ramco Cements',
        'RBLBANK.NS': 'RBL Bank',
        'RELAXO.NS': 'Relaxo Footwears',
        'SANOFI.NS': 'Sanofi India',
        'SCHAEFFLER.NS': 'Schaeffler India',
        'SJVN.NS': 'SJVN Limited',
        'SONACOMS.NS': 'Sona BLW Precision',
        'STAR.NS': 'Star Health Insurance',
        'SUNTV.NS': 'Sun TV Network',
        'SUPREMEIND.NS': 'Supreme Industries',
        'SYNGENE.NS': 'Syngene International',
        'TATACHEM.NS': 'Tata Chemicals',
        'TATATECH.NS': 'Tata Technologies',
        'THERMAX.NS': 'Thermax',
        'TIINDIA.NS': 'Tube Investments',
        'TIMKEN.NS': 'Timken India',
        'TORNTPOWER.NS': 'Torrent Power',
        'TVSMOTOR.NS': 'TVS Motor',
        'UBL.NS': 'United Breweries',
        'UNIONBANK.NS': 'Union Bank of India',
        'UNITDSPR.NS': 'United Spirits',
        'VBL.NS': 'Varun Beverages',
        'VINATIORGA.NS': 'Vinati Organics',
        'WHIRLPOOL.NS': 'Whirlpool of India',
        'YESBANK.NS': 'Yes Bank',
        'ZEEL.NS': 'Zee Entertainment',
        
        # Additional Healthcare & Regional Stocks
        'KIMS.NS': 'Krishna Institute of Medical Sciences',
        'APOLLOTYRE.NS': 'Apollo Tyres',
        'AFFLE.NS': 'Affle India',
        'AAVAS.NS': 'Aavas Financiers',
        'AARTIIND.NS': 'Aarti Industries',
        'AARTISURF.NS': 'Aarti Surfactants',
        'ABCAPITAL.NS': 'Aditya Birla Capital',
        'AEGISLOG.NS': 'Aegis Logistics',
        'AIAENG.NS': 'AIA Engineering',
        'AJANTPHARM.NS': 'Ajanta Pharma',
        'ALKYLAMINE.NS': 'Alkyl Amines Chemicals',
        'ANGELONE.NS': 'Angel One',
        'APLAPOLLO.NS': 'APL Apollo Tubes',
        'ARE&M.NS': 'Amara Raja Energy',
        'ATUL.NS': 'Atul Limited',
        'BALAMINES.NS': 'Balaji Amines',
        'BASF.NS': 'BASF India',
        'BAYERCROP.NS': 'Bayer CropScience',
        'BDL.NS': 'Bharat Dynamics',
        'BLS.NS': 'BLS International',
        'BLUEDART.NS': 'Blue Dart Express',
        'CANFINHOME.NS': 'Can Fin Homes',
        'CARBORUNIV.NS': 'Carborundum Universal',
        'CASTROLIND.NS': 'Castrol India',
        'CCL.NS': 'CCL Products',
        'CDSL.NS': 'Central Depository Services',
        'CENTURYTEX.NS': 'Century Textiles',
        'CERA.NS': 'Cera Sanitaryware',
        'CHALET.NS': 'Chalet Hotels',
        'CLEAN.NS': 'Clean Science',
        'COCHINSHIP.NS': 'Cochin Shipyard',
        'CROMPTON.NS': 'Crompton Greaves Consumer',
        'CYIENT.NS': 'Cyient',
        'DATAPATTNS.NS': 'Data Patterns India',
        'DCMSHRIRAM.NS': 'DCM Shriram',
        'DEVYANI.NS': 'Devyani International',
        'DMART.NS': 'Avenue Supermarts (DMart)',
        'ERIS.NS': 'Eris Lifesciences',
        'FINCABLES.NS': 'Finolex Cables',
        'FINPIPE.NS': 'Finolex Industries',
        'FLUOROCHEM.NS': 'Gujarat Fluorochemicals',
        'FRETAIL.NS': 'Future Retail',
        'GALAXY.NS': 'Galaxy Surfactants',
        'GARFIBRES.NS': 'Garware Technical Fibres',
        'GILLETTE.NS': 'Gillette India',
        'GLAXO.NS': 'GSK Pharma',
        'GLOBUSSPR.NS': 'Globus Spirits',
        'GRINDWELL.NS': 'Grindwell Norton',
        'GSFC.NS': 'Gujarat State Fertilizers',
        'HAPPSTMNDS.NS': 'Happiest Minds Technologies',
        'HATSUN.NS': 'Hatsun Agro',
        'HFCL.NS': 'HFCL Limited',
        'HIKAL.NS': 'Hikal',
        'HGS.NS': 'Hinduja Global Solutions',
        'HINDWAREAP.NS': 'Hindware Home Innovation',
        'HOMEFIRST.NS': 'Home First Finance',
        'HONAUT.NS': 'Honeywell Automation',
        'IBULHSGFIN.NS': 'Indiabulls Housing Finance',
        'IBREALEST.NS': 'Indiabulls Real Estate',
        'IIFL.NS': 'IIFL Finance',
        'IIFLWAM.NS': 'IIFL Wealth Management',
        'INDIGOPNTS.NS': 'Indigo Paints',
        'INTELLECT.NS': 'Intellect Design Arena',
        'IRB.NS': 'IRB Infrastructure',
        'ISEC.NS': 'ICICI Securities',
        'JBCHEPHARM.NS': 'JB Chemicals',
        'JKLAKSHMI.NS': 'JK Lakshmi Cement',
        'JKPAPER.NS': 'JK Paper',
        'JMFINANCIL.NS': 'JM Financial',
        'JYOTHYLAB.NS': 'Jyothy Labs',
        'KALYANKJIL.NS': 'Kalyan Jewellers',
        'KANSAINER.NS': 'Kansai Nerolac Paints',
        'KARURVYSYA.NS': 'Karur Vysya Bank',
        'KEC.NS': 'KEC International',
        'KFINTECH.NS': 'KFin Technologies',
        'KIRLOSENG.NS': 'Kirloskar Oil Engines',
        'KNRCON.NS': 'KNR Constructions',
        'KPITTECH.NS': 'KPIT Technologies',
        'KRBL.NS': 'KRBL',
        'KSCL.NS': 'Kaveri Seed Company',
        'LALPATHLAB.NS': 'Dr Lal PathLabs',
        'LATENTVIEW.NS': 'Latent View Analytics',
        'LEMONTREE.NS': 'Lemon Tree Hotels',
        'LINDEINDIA.NS': 'Linde India',
        'LUXIND.NS': 'Lux Industries',
        'MAPMYINDIA.NS': 'CE Info Systems',
        'MASTEK.NS': 'Mastek',
        'MAZDOCK.NS': 'Mazagon Dock Shipbuilders',
        'MEDANTA.NS': 'Global Health (Medanta)',
        'METROPOLIS.NS': 'Metropolis Healthcare',
        'MMTC.NS': 'MMTC',
        'MOIL.NS': 'MOIL',
        'MRPL.NS': 'Mangalore Refinery',
        'MSTCLTD.NS': 'MSTC',
        'NATCOPHARM.NS': 'Natco Pharma',
        'NAVINFLUOR.NS': 'Navin Fluorine',
        'NESCO.NS': 'Nesco',
        'NETWORK18.NS': 'Network18 Media',
        'NEWGEN.NS': 'Newgen Software',
        'NCC.NS': 'NCC Limited',
        'NILKAMAL.NS': 'Nilkamal',
        'NLCINDIA.NS': 'NLC India',
        'OLECTRA.NS': 'Olectra Greentech',
        'ORIENTCEM.NS': 'Orient Cement',
        'ORIENTELEC.NS': 'Orient Electric',
        'PGHH.NS': 'Procter & Gamble Hygiene',
        'PNBHOUSING.NS': 'PNB Housing Finance',
        'POONAWALLA.NS': 'Poonawalla Fincorp',
        'POWERINDIA.NS': 'Hitachi Energy India',
        'PRINCEPIPE.NS': 'Prince Pipes',
        'PRSMJOHNSN.NS': 'Prism Johnson',
        'QUESS.NS': 'Quess Corp',
        'RADICO.NS': 'Radico Khaitan',
        'RAILTEL.NS': 'RailTel Corporation',
        'RAIN.NS': 'Rain Industries',
        'RAJESHEXPO.NS': 'Rajesh Exports',
        'RALLIS.NS': 'Rallis India',
        'RATNAMANI.NS': 'Ratnamani Metals',
        'RAYMOND.NS': 'Raymond',
        'REDINGTON.NS': 'Redington India',
        'RITES.NS': 'RITES',
        'ROSSARI.NS': 'Rossari Biotech',
        'ROUTE.NS': 'Route Mobile',
        'RVNL.NS': 'Rail Vikas Nigam',
        'SAFARI.NS': 'Safari Industries',
        'SAPPHIRE.NS': 'Sapphire Foods',
        'SCHNEIDER.NS': 'Schneider Electric',
        'SFL.NS': 'Sheela Foam',
        'SHARDACROP.NS': 'Sharda Cropchem',
        'SHRIRAMFIN.NS': 'Shriram Finance',
        'SKFINDIA.NS': 'SKF India',
        'SOBHA.NS': 'Sobha',
        'SOLARA.NS': 'Solara Active Pharma',
        'SONATSOFTW.NS': 'Sonata Software',
        'SPARC.NS': 'Sun Pharma Advanced',
        'SIS.NS': 'SIS Limited',
        'STARCEMENT.NS': 'Star Cement',
        'SUBEX.NS': 'Subex',
        'SUMICHEM.NS': 'Sumitomo Chemical India',
        'SUNDARMFIN.NS': 'Sundaram Finance',
        'SUNFLAG.NS': 'Sunflag Iron & Steel',
        'SUVENPHAR.NS': 'Suven Pharmaceuticals',
        'SYMPHONY.NS': 'Symphony',
        'TANLA.NS': 'Tanla Platforms',
        'TASTYBIT.NS': 'Tasty Bite Eatables',
        'TATAINVEST.NS': 'Tata Investment Corp',
        'TCI.NS': 'Transport Corporation of India',
        'TEAMLEASE.NS': 'TeamLease Services',
        'THYROCARE.NS': 'Thyrocare Technologies',
        'TRIDENT.NS': 'Trident',
        'TTKPRESTIG.NS': 'TTK Prestige',
        'TV18BRDCST.NS': 'TV18 Broadcast',
        'UTIAMC.NS': 'UTI Asset Management',
        'UJJIVANSFB.NS': 'Ujjivan Small Finance Bank',
        'UCOBANK.NS': 'UCO Bank',
        'VAIBHAVGBL.NS': 'Vaibhav Global',
        'VGUARD.NS': 'V-Guard Industries',
        'VIPIND.NS': 'VIP Industries',
        'VSTIND.NS': 'VST Industries',
        'WELCORP.NS': 'Welspun Corp',
        'WELSPUNLIV.NS': 'Welspun Living',
        'WESTLIFE.NS': 'Westlife Foodworld',
        'WOCKPHARMA.NS': 'Wockhardt',
        'ZENSARTECH.NS': 'Zensar Technologies',
        'ZFCVINDIA.NS': 'ZF Commercial Vehicle',
    }

    # Complete BSE stocks for equity analysis (BSE SENSEX, BSE 100, and popular stocks)
    BSE_STOCKS = {
        # BSE SENSEX Components
        'RELIANCE.BO': 'Reliance Industries',
        'TCS.BO': 'Tata Consultancy Services',
        'HDFCBANK.BO': 'HDFC Bank',
        'INFY.BO': 'Infosys',
        'ICICIBANK.BO': 'ICICI Bank',
        'HINDUNILVR.BO': 'Hindustan Unilever',
        'SBIN.BO': 'State Bank of India',
        'BHARTIARTL.BO': 'Bharti Airtel',
        'ITC.BO': 'ITC Limited',
        'KOTAKBANK.BO': 'Kotak Mahindra Bank',
        'LT.BO': 'Larsen & Toubro',
        'AXISBANK.BO': 'Axis Bank',
        'ASIANPAINT.BO': 'Asian Paints',
        'MARUTI.BO': 'Maruti Suzuki',
        'TITAN.BO': 'Titan Company',
        'BAJFINANCE.BO': 'Bajaj Finance',
        'WIPRO.BO': 'Wipro',
        'SUNPHARMA.BO': 'Sun Pharma',
        'TATAMOTORS.BO': 'Tata Motors',
        'TATASTEEL.BO': 'Tata Steel',
        'HCLTECH.BO': 'HCL Technologies',
        'NTPC.BO': 'NTPC Limited',
        'POWERGRID.BO': 'Power Grid Corporation',
        'ULTRACEMCO.BO': 'UltraTech Cement',
        'BAJAJFINSV.BO': 'Bajaj Finserv',
        'NESTLEIND.BO': 'Nestle India',
        'ONGC.BO': 'Oil & Natural Gas Corporation',
        'M&M.BO': 'Mahindra & Mahindra',
        'JSWSTEEL.BO': 'JSW Steel',
        'TECHM.BO': 'Tech Mahindra',
        
        # Additional BSE 100 Components
        'ADANIENT.BO': 'Adani Enterprises',
        'ADANIPORTS.BO': 'Adani Ports & SEZ',
        'COALINDIA.BO': 'Coal India',
        'BPCL.BO': 'Bharat Petroleum',
        'GRASIM.BO': 'Grasim Industries',
        'DIVISLAB.BO': 'Divis Laboratories',
        'DRREDDY.BO': 'Dr Reddys Laboratories',
        'CIPLA.BO': 'Cipla',
        'EICHERMOT.BO': 'Eicher Motors',
        'BRITANNIA.BO': 'Britannia Industries',
        'HEROMOTOCO.BO': 'Hero MotoCorp',
        'INDUSINDBK.BO': 'IndusInd Bank',
        'APOLLOHOSP.BO': 'Apollo Hospitals',
        'SBILIFE.BO': 'SBI Life Insurance',
        'HDFCLIFE.BO': 'HDFC Life Insurance',
        'TATACONSUM.BO': 'Tata Consumer Products',
        'HINDALCO.BO': 'Hindalco Industries',
        'UPL.BO': 'UPL Limited',
        'BAJAJ-AUTO.BO': 'Bajaj Auto',
        'ADANIGREEN.BO': 'Adani Green Energy',
        'ADANITRANS.BO': 'Adani Transmission',
        'AMBUJACEM.BO': 'Ambuja Cements',
        'AUROPHARMA.BO': 'Aurobindo Pharma',
        'BANKBARODA.BO': 'Bank of Baroda',
        'BERGEPAINT.BO': 'Berger Paints',
        'BIOCON.BO': 'Biocon',
        'BOSCHLTD.BO': 'Bosch',
        'CHOLAFIN.BO': 'Cholamandalam Investment',
        'COLPAL.BO': 'Colgate Palmolive',
        'DABUR.BO': 'Dabur India',
        'DLF.BO': 'DLF Limited',
        'GAIL.BO': 'GAIL India',
        'GODREJCP.BO': 'Godrej Consumer Products',
        'GODREJPROP.BO': 'Godrej Properties',
        'HAVELLS.BO': 'Havells India',
        'ICICIPRULI.BO': 'ICICI Prudential Life',
        'ICICIGI.BO': 'ICICI Lombard General',
        'INDUSTOWER.BO': 'Indus Towers',
        'IOC.BO': 'Indian Oil Corporation',
        'IRCTC.BO': 'IRCTC',
        'IGL.BO': 'Indraprastha Gas',
        'JINDALSTEL.BO': 'Jindal Steel & Power',
        'LICI.BO': 'LIC India',
        'LTIM.BO': 'LTIMindtree',
        'LUPIN.BO': 'Lupin',
        'MARICO.BO': 'Marico',
        'MCDOWELL-N.BO': 'United Spirits',
        'MUTHOOTFIN.BO': 'Muthoot Finance',
        'NAUKRI.BO': 'Info Edge (Naukri)',
        'NMDC.BO': 'NMDC',
        'OBEROIRLTY.BO': 'Oberoi Realty',
        'OFSS.BO': 'Oracle Financial Services',
        'PAGEIND.BO': 'Page Industries',
        'PEL.BO': 'Piramal Enterprises',
        'PERSISTENT.BO': 'Persistent Systems',
        'PETRONET.BO': 'Petronet LNG',
        'PFC.BO': 'Power Finance Corporation',
        'PIDILITIND.BO': 'Pidilite Industries',
        'PNB.BO': 'Punjab National Bank',
        'POLYCAB.BO': 'Polycab India',
        'RECLTD.BO': 'REC Limited',
        'SAIL.BO': 'Steel Authority of India',
        'SBICARD.BO': 'SBI Cards',
        'SHREECEM.BO': 'Shree Cement',
        'SIEMENS.BO': 'Siemens',
        'SRF.BO': 'SRF Limited',
        'TATAPOWER.BO': 'Tata Power',
        'TATACOMM.BO': 'Tata Communications',
        'TATAELXSI.BO': 'Tata Elxsi',
        'TORNTPHARM.BO': 'Torrent Pharma',
        'TRENT.BO': 'Trent',
        'VEDL.BO': 'Vedanta',
        'VOLTAS.BO': 'Voltas',
        'ZOMATO.BO': 'Zomato',
        'ZYDUSLIFE.BO': 'Zydus Lifesciences',
        
        # Other Popular BSE Stocks
        'ABB.BO': 'ABB India',
        'ACC.BO': 'ACC Limited',
        'ABCAPITAL.BO': 'Aditya Birla Capital',
        'ABFRL.BO': 'Aditya Birla Fashion',
        'ALKEM.BO': 'Alkem Laboratories',
        'APLLTD.BO': 'Alembic Pharmaceuticals',
        'ASHOKLEY.BO': 'Ashok Leyland',
        'ASTRAL.BO': 'Astral',
        'ATUL.BO': 'Atul Ltd',
        'AUBANK.BO': 'AU Small Finance Bank',
        'BANDHANBNK.BO': 'Bandhan Bank',
        'BEL.BO': 'Bharat Electronics',
        'BHARATFORG.BO': 'Bharat Forge',
        'BHEL.BO': 'Bharat Heavy Electricals',
        'CANBK.BO': 'Canara Bank',
        'CGPOWER.BO': 'CG Power & Industrial',
        'COFORGE.BO': 'Coforge',
        'CONCOR.BO': 'Container Corporation',
        'CUMMINSIND.BO': 'Cummins India',
        'DEEPAKNTR.BO': 'Deepak Nitrite',
        'DELHIVERY.BO': 'Delhivery',
        'DIXON.BO': 'Dixon Technologies',
        'ESCORTS.BO': 'Escorts Kubota',
        'EXIDEIND.BO': 'Exide Industries',
        'FEDERALBNK.BO': 'Federal Bank',
        'FORTIS.BO': 'Fortis Healthcare',
        'GLAND.BO': 'Gland Pharma',
        'GLENMARK.BO': 'Glenmark Pharma',
        'GMRAIRPORT.BO': 'GMR Airports',
        'GNFC.BO': 'GNFC',
        'GSPL.BO': 'Gujarat State Petronet',
        'HAL.BO': 'Hindustan Aeronautics',
        'HDFCAMC.BO': 'HDFC Asset Management',
        'HINDCOPPER.BO': 'Hindustan Copper',
        'HINDPETRO.BO': 'Hindustan Petroleum',
        'IDFCFIRSTB.BO': 'IDFC First Bank',
        'IEX.BO': 'Indian Energy Exchange',
        'INDHOTEL.BO': 'Indian Hotels',
        'INDIACEM.BO': 'India Cements',
        'INDIAMART.BO': 'IndiaMART InterMESH',
        'INDIANB.BO': 'Indian Bank',
        'IPCA.BO': 'IPCA Laboratories',
        'IRFC.BO': 'Indian Railway Finance',
        'KOVAI.BO': 'Kovai Medical Centre',
        'JKCEMENT.BO': 'JK Cement',
        'JSWENERGY.BO': 'JSW Energy',
        'JUBLFOOD.BO': 'Jubilant FoodWorks',
        'KAJARIACER.BO': 'Kajaria Ceramics',
        'KEI.BO': 'KEI Industries',
        'L&TFH.BO': 'L&T Finance Holdings',
        'LAURUSLABS.BO': 'Laurus Labs',
        'LICHSGFIN.BO': 'LIC Housing Finance',
        'LTI.BO': 'L&T Infotech',
        'M&MFIN.BO': 'Mahindra & Mahindra Financial',
        'MANAPPURAM.BO': 'Manappuram Finance',
        'MAXHEALTH.BO': 'Max Healthcare',
        'MCX.BO': 'Multi Commodity Exchange',
        'METROPOLIS.BO': 'Metropolis Healthcare',
        'MGL.BO': 'Mahanagar Gas',
        'MINDTREE.BO': 'Mindtree',
        'MOTHERSON.BO': 'Motherson Sumi',
        'MPHASIS.BO': 'Mphasis',
        'MRF.BO': 'MRF Limited',
        'NAM-INDIA.BO': 'Nippon Life India AMC',
        'NATIONALUM.BO': 'National Aluminium',
        'NIACL.BO': 'New India Assurance',
        'NHPC.BO': 'NHPC Limited',
        'NOCIL.BO': 'NOCIL',
        'PAYTM.BO': 'One 97 Communications',
        'PI.BO': 'PI Industries',
        'PIIND.BO': 'PI Industries',
        'PRESTIGE.BO': 'Prestige Estates',
        'PVRINOX.BO': 'PVR INOX',
        'RAMCOCEM.BO': 'Ramco Cements',
        'RBLBANK.BO': 'RBL Bank',
        'RELAXO.BO': 'Relaxo Footwears',
        'SANOFI.BO': 'Sanofi India',
        'SCHAEFFLER.BO': 'Schaeffler India',
        'SJVN.BO': 'SJVN Limited',
        'SONACOMS.BO': 'Sona BLW Precision',
        'STAR.BO': 'Star Health Insurance',
        'SUNTV.BO': 'Sun TV Network',
        'SUPREMEIND.BO': 'Supreme Industries',
        'SYNGENE.BO': 'Syngene International',
        'TATACHEM.BO': 'Tata Chemicals',
        'TATATECH.BO': 'Tata Technologies',
        'THERMAX.BO': 'Thermax',
        'TIINDIA.BO': 'Tube Investments',
        'TIMKEN.BO': 'Timken India',
        'TORNTPOWER.BO': 'Torrent Power',
        'TVSMOTOR.BO': 'TVS Motor',
        'UBL.BO': 'United Breweries',
        'UNIONBANK.BO': 'Union Bank of India',
        'UNITDSPR.BO': 'United Spirits',
        'VBL.BO': 'Varun Beverages',
        'VINATIORGA.BO': 'Vinati Organics',
        'WHIRLPOOL.BO': 'Whirlpool of India',
        'YESBANK.BO': 'Yes Bank',
        'ZEEL.BO': 'Zee Entertainment',
    }
    
    def __init__(self):
        self._cache = {}
        self._cache_duration = 300  # 5 minutes

    def resolve_symbol(self, base_symbol: str, exchange: Optional[str] = None) -> Optional[str]:
        """
        Resolve a base symbol to NSE (.NS) or BSE (.BO) symbol.

        Args:
            base_symbol: Base symbol like HDFCBANK
            exchange: Optional exchange hint: NSE, BSE, or None

        Returns:
            Resolved symbol with suffix, or None if not found
        """
        if not base_symbol:
            return None

        symbol = base_symbol.upper().strip()

        if symbol.endswith(".NS") or symbol.endswith(".BO"):
            return symbol

        exchange_hint = exchange.upper() if exchange else ""

        if exchange_hint == "BSE":
            candidate = f"{symbol}.BO"
            return candidate if candidate in self.BSE_STOCKS else None

        if exchange_hint == "NSE":
            candidate = f"{symbol}.NS"
            return candidate if candidate in self.NSE_STOCKS else None

        nse_candidate = f"{symbol}.NS"
        if nse_candidate in self.NSE_STOCKS:
            return nse_candidate

        bse_candidate = f"{symbol}.BO"
        if bse_candidate in self.BSE_STOCKS:
            return bse_candidate

        return None

    def list_symbols(self, query: str = "", exchange: str = "ALL", limit: int = 100) -> List[Dict[str, str]]:
        """
        List symbols for auto-suggest.

        Args:
            query: Partial query to filter by symbol or name
            exchange: NSE, BSE, or ALL
            limit: Max number of results

        Returns:
            List of symbol dicts
        """
        exchange = (exchange or "ALL").upper()
        query = (query or "").strip().upper()

        entries: List[Dict[str, str]] = []

        def add_entries(source: Dict[str, str], exchange_label: str):
            for symbol, name in source.items():
                base = symbol.replace(".NS", "").replace(".BO", "")
                if query and query not in base and query not in name.upper():
                    continue
                entries.append({
                    "symbol": symbol,
                    "base": base,
                    "name": name,
                    "exchange": exchange_label
                })

        if exchange in ["ALL", "NSE"]:
            add_entries(self.NSE_STOCKS, "NSE")
        if exchange in ["ALL", "BSE"]:
            add_entries(self.BSE_STOCKS, "BSE")

        entries.sort(key=lambda item: (item["base"], item["exchange"]))

        return entries[:max(5, min(limit, 500))]
    
    def analyze_option_chain(
        self, 
        spot_price: float,
        trend: str,
        trend_strength: float,
        rsi: float,
        strike_interval: int = 50,
    ) -> OptionChainAnalysis:
        """
        Analyze option chain and provide smart strike recommendations.
        
        Since Yahoo Finance doesn't provide real NSE option chain data,
        we simulate intelligent analysis based on:
        - Spot price and trend
        - Standard option pricing principles
        - Risk-reward calculations
        
        Args:
            spot_price: Current NIFTY spot price
            trend: BULLISH, BEARISH, or NEUTRAL
            trend_strength: Signal strength 0-100
            rsi: Current RSI value
            
        Returns:
            OptionChainAnalysis with recommendations
        """
        atm_strike = round(spot_price / strike_interval) * strike_interval
        
        # Estimate support/resistance based on round numbers and ATM
        resistance_levels = [
            atm_strike + strike_interval,
            atm_strike + (2 * strike_interval),
            atm_strike + (3 * strike_interval)
        ]
        support_levels = [
            atm_strike - strike_interval,
            atm_strike - (2 * strike_interval),
            atm_strike - (3 * strike_interval)
        ]
        
        # Simulated max OI strikes (typically at round numbers slightly away from spot)
        if trend == "BULLISH":
            max_call_oi = atm_strike + (2 * strike_interval)  # Resistance
            max_put_oi = atm_strike - strike_interval  # Support
            pcr = 0.8  # Lower PCR in bullish markets
        elif trend == "BEARISH":
            max_call_oi = atm_strike + strike_interval
            max_put_oi = atm_strike - (2 * strike_interval)
            pcr = 1.3  # Higher PCR in bearish markets
        else:
            max_call_oi = atm_strike + strike_interval
            max_put_oi = atm_strike - strike_interval
            pcr = 1.0
        
        # Generate recommendations based on risk profile
        option_type = "CE" if trend == "BULLISH" else "PE" if trend == "BEARISH" else None
        
        if option_type:
            conservative = self._create_strike_recommendation(
                spot_price, atm_strike, option_type, RiskProfile.CONSERVATIVE,
                trend_strength, rsi, strike_interval
            )
            moderate = self._create_strike_recommendation(
                spot_price, atm_strike, option_type, RiskProfile.MODERATE,
                trend_strength, rsi, strike_interval
            )
            aggressive = self._create_strike_recommendation(
                spot_price, atm_strike, option_type, RiskProfile.AGGRESSIVE,
                trend_strength, rsi, strike_interval
            )
        else:
            # Neutral - recommend staying out
            conservative = moderate = aggressive = StrikeRecommendation(
                strike=atm_strike,
                option_type="NONE",
                moneyness="ATM",
                distance_from_spot=0,
                estimated_delta=0.5,
                risk_level="N/A",
                rationale="Neutral trend - avoid trading, wait for clear direction",
                confidence=0
            )
        
        return OptionChainAnalysis(
            spot_price=spot_price,
            atm_strike=atm_strike,
            max_call_oi_strike=max_call_oi,
            max_put_oi_strike=max_put_oi,
            pcr_ratio=pcr,
            resistance_levels=resistance_levels,
            support_levels=support_levels,
            conservative_strike=conservative,
            moderate_strike=moderate,
            aggressive_strike=aggressive,
            analysis_time=datetime.now(),
            expiry="Weekly"
        )
    
    def _create_strike_recommendation(
        self,
        spot_price: float,
        atm_strike: int,
        option_type: str,
        risk_profile: RiskProfile,
        trend_strength: float,
        rsi: float,
        strike_interval: int
    ) -> StrikeRecommendation:
        """Create a strike recommendation based on risk profile"""
        
        if option_type == "CE":
            # Call options
            if risk_profile == RiskProfile.CONSERVATIVE:
                # ITM Call - higher premium, lower risk
                strike = atm_strike - strike_interval
                moneyness = "ITM"
                delta = 0.65
                risk = "LOW"
                rationale = f"ITM Call at {strike}: Higher delta ({delta:.2f}), moves more with NIFTY. Safer choice with strong trend (strength: {trend_strength:.0f}%)"
            elif risk_profile == RiskProfile.MODERATE:
                # ATM Call - balanced
                strike = atm_strike
                moneyness = "ATM"
                delta = 0.50
                risk = "MEDIUM"
                rationale = f"ATM Call at {strike}: Balanced risk-reward. Delta ~{delta:.2f}. Good for moderate conviction trades."
            else:
                # OTM Call - lower premium, higher risk
                strike = atm_strike + strike_interval
                moneyness = "OTM"
                delta = 0.35
                risk = "HIGH"
                rationale = f"OTM Call at {strike}: Lower premium, higher leverage. Delta ~{delta:.2f}. Only if highly confident in upside."
        else:
            # Put options
            if risk_profile == RiskProfile.CONSERVATIVE:
                # ITM Put
                strike = atm_strike + strike_interval
                moneyness = "ITM"
                delta = -0.65
                risk = "LOW"
                rationale = f"ITM Put at {strike}: Higher delta, moves more with NIFTY downside. Safer for bearish view."
            elif risk_profile == RiskProfile.MODERATE:
                # ATM Put
                strike = atm_strike
                moneyness = "ATM"
                delta = -0.50
                risk = "MEDIUM"
                rationale = f"ATM Put at {strike}: Balanced risk-reward for bearish trades."
            else:
                # OTM Put
                strike = atm_strike - strike_interval
                moneyness = "OTM"
                delta = -0.35
                risk = "HIGH"
                rationale = f"OTM Put at {strike}: Cheaper premium but needs strong downmove to profit."
        
        # Adjust confidence based on RSI extremes
        confidence = trend_strength
        if option_type == "CE" and rsi < 30:
            confidence = min(confidence + 15, 100)
            rationale += " RSI oversold - good entry for calls."
        elif option_type == "PE" and rsi > 70:
            confidence = min(confidence + 15, 100)
            rationale += " RSI overbought - good entry for puts."
        
        distance = abs(spot_price - strike)
        
        return StrikeRecommendation(
            strike=strike,
            option_type=option_type,
            moneyness=moneyness,
            distance_from_spot=distance,
            estimated_delta=abs(delta),
            risk_level=risk,
            rationale=rationale,
            confidence=confidence
        )
    
    def analyze_stock(self, symbol: str, exchange: Optional[str] = None) -> Optional[StockAnalysis]:
        """
        Perform technical analysis on a stock.
        
        Args:
            symbol: Stock symbol (e.g., 'RELIANCE' or 'RELIANCE.NS')
            exchange: Optional exchange hint: NSE or BSE
            
        Returns:
            StockAnalysis with buy/sell recommendation
        """
        # Normalize symbol
        symbol = symbol.upper().strip()
        if not symbol.endswith('.NS') and not symbol.endswith('.BO'):
            resolved = self.resolve_symbol(symbol, exchange=exchange)
            if resolved:
                symbol = resolved
            else:
                symbol = f"{symbol}.NS"
        
        try:
            logger.info(f"Analyzing stock: {symbol}")
            
            # Fetch data
            ticker = yf.Ticker(symbol)
            data = ticker.history(period="3mo", interval="1d")
            
            if data.empty or len(data) < 50:
                logger.warning(f"Insufficient data for {symbol}")
                return None
            
            # Get company name
            try:
                info = ticker.info
                name = info.get('shortName', symbol.replace('.NS', ''))
            except:
                name = symbol.replace('.NS', '')
            
            # Current price
            current_price = data['Close'].iloc[-1]
            prev_close = data['Close'].iloc[-2]
            change_percent = ((current_price - prev_close) / prev_close) * 100
            
            # Calculate indicators
            sma_20 = data['Close'].rolling(window=20).mean().iloc[-1]
            sma_50 = data['Close'].rolling(window=50).mean().iloc[-1]
            sma_200 = data['Close'].rolling(window=200).mean().iloc[-1] if len(data) >= 200 else sma_50
            
            # RSI
            delta = data['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = (100 - (100 / (1 + rs))).iloc[-1]
            
            # Support/Resistance (simple: recent low/high)
            support = data['Low'].tail(20).min()
            resistance = data['High'].tail(20).max()
            
            # Determine trend
            bullish_signals = 0
            if current_price > sma_20:
                bullish_signals += 1
            if current_price > sma_50:
                bullish_signals += 1
            if sma_20 > sma_50:
                bullish_signals += 1
            if rsi < 70:  # Not overbought
                bullish_signals += 1
            
            if bullish_signals >= 3:
                trend = "BULLISH"
            elif bullish_signals <= 1:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"
            
            # Generate recommendation
            if trend == "BULLISH" and rsi < 65:
                action = "BUY"
                target = current_price * 1.08  # 8% target
                stop_loss = max(support, current_price * 0.95)  # 5% or support
                confidence = 60 + (3 - abs(3 - bullish_signals)) * 10
                rationale = f"Bullish trend with price above SMAs. RSI at {rsi:.1f} shows room for upside."
            elif trend == "BEARISH" or rsi > 75:
                action = "SELL" if rsi > 75 else "AVOID"
                target = current_price * 0.92
                stop_loss = min(resistance, current_price * 1.05)
                confidence = 50
                rationale = f"Bearish signals or RSI overbought ({rsi:.1f}). Consider avoiding or booking profits."
            else:
                action = "HOLD"
                target = resistance
                stop_loss = support
                confidence = 40
                rationale = f"Mixed signals. Wait for clearer trend direction."
            
            return StockAnalysis(
                symbol=symbol,
                name=name,
                current_price=current_price,
                change_percent=change_percent,
                rsi=rsi,
                sma_20=sma_20,
                sma_50=sma_50,
                sma_200=sma_200,
                trend=trend,
                support=support,
                resistance=resistance,
                action=action,
                target_price=target,
                stop_loss=stop_loss,
                confidence=confidence,
                rationale=rationale
            )
            
        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}")
            return None
    
    def screen_stocks(self, criteria: str = "bullish") -> List[StockAnalysis]:
        """
        Screen stocks based on criteria.
        
        Args:
            criteria: 'bullish', 'bearish', 'oversold', 'overbought'
            
        Returns:
            List of stocks matching criteria
        """
        results = []
        
        logger.info(f"Screening stocks with criteria: {criteria}")
        
        for symbol in self.NSE_STOCKS.keys():
            analysis = self.analyze_stock(symbol)
            
            if not analysis:
                continue
            
            if criteria == "bullish" and analysis.trend == "BULLISH" and analysis.action == "BUY":
                results.append(analysis)
            elif criteria == "bearish" and analysis.trend == "BEARISH":
                results.append(analysis)
            elif criteria == "oversold" and analysis.rsi < 30:
                results.append(analysis)
            elif criteria == "overbought" and analysis.rsi > 70:
                results.append(analysis)
        
        # Sort by confidence
        results.sort(key=lambda x: x.confidence, reverse=True)
        
        return results[:10]  # Return top 10
    
    def _generate_trade_plan(
        self,
        entry_premium: float,
        moneyness: str,
        trend: str,
        confidence: int,
        is_real_data: bool = False,
        data_source: str = "Estimated",
        lot_size: int = 75,
    ) -> Dict:
        """
        Generate a trade plan with entry, targets, and stop-loss.
        
        Args:
            entry_premium: Current/estimated option premium
            moneyness: ITM, ATM, or OTM
            trend: Market trend
            confidence: Trade confidence score
            is_real_data: Whether premium is from NSE
            data_source: Source of data
            
        Returns:
            Trade plan dictionary
        """
        # lot_size is now passed as a parameter from the index config
        
        # Entry range (±5%)
        entry_range_low = entry_premium * 0.95
        entry_range_high = entry_premium * 1.05
        
        # Calculate targets based on moneyness and trend alignment
        if moneyness == "ITM":
            # ITM options: smaller % moves, safer
            target_1_pct = 20  # 20% target
            target_2_pct = 35  # 35% target
            sl_pct = 15  # 15% stop-loss
        elif moneyness == "ATM":
            # ATM options: moderate moves
            target_1_pct = 25  # 25% target
            target_2_pct = 50  # 50% target
            sl_pct = 20  # 20% stop-loss
        else:  # OTM
            # OTM options: need bigger moves, higher risk
            target_1_pct = 40  # 40% target
            target_2_pct = 80  # 80% target
            sl_pct = 30  # 30% stop-loss
        
        # Adjust based on confidence
        if confidence >= 75:
            # High confidence: can aim for higher targets
            target_1_pct *= 1.1
            target_2_pct *= 1.1
        elif confidence < 50:
            # Low confidence: tighter targets and SL
            target_1_pct *= 0.8
            target_2_pct *= 0.8
            sl_pct *= 1.2
        
        # Calculate actual prices
        target_1 = entry_premium * (1 + target_1_pct / 100)
        target_2 = entry_premium * (1 + target_2_pct / 100)
        stop_loss = entry_premium * (1 - sl_pct / 100)
        
        # Risk-reward calculation
        risk = entry_premium - stop_loss
        reward = target_1 - entry_premium
        risk_reward = reward / risk if risk > 0 else 0
        
        # Per lot calculations
        max_loss_per_lot = risk * lot_size
        max_profit_per_lot = (target_2 - entry_premium) * lot_size
        
        return {
            "entry_price": round(entry_premium, 2),
            "entry_range_low": round(entry_range_low, 2),
            "entry_range_high": round(entry_range_high, 2),
            "target_1": round(target_1, 2),
            "target_1_pct": round(target_1_pct, 1),
            "target_2": round(target_2, 2),
            "target_2_pct": round(target_2_pct, 1),
            "stop_loss": round(stop_loss, 2),
            "stop_loss_pct": round(sl_pct, 1),
            "risk_reward_ratio": round(risk_reward, 2),
            "lot_size": lot_size,
            "max_loss_per_lot": round(max_loss_per_lot, 2),
            "max_profit_per_lot": round(max_profit_per_lot, 2),
            "is_real_data": is_real_data,
            "data_source": data_source
        }
    
    def analyze_specific_strike(
        self,
        strike: int,
        option_type: str,
        spot_price: float,
        trend: str,
        rsi: float,
        expiry_date_str: str = None,
        index_name: str = "NIFTY",
    ) -> Dict:
        """
        Analyze a specific strike price for trading.
        
        Args:
            strike: Strike price to analyze
            option_type: 'CE' or 'PE'
            spot_price: Current spot price
            trend: Market trend
            rsi: Current RSI
            expiry_date_str: Specific expiry date like '27jan', '3feb', '17feb'
            index_name: Index name ('NIFTY', 'BANKNIFTY', 'SENSEX')
        """
        idx_cfg = get_index(index_name) or NIFTY
        strike_interval = idx_cfg.strike_interval
        lot_size = idx_cfg.lot_size
        atm_strike = round(spot_price / strike_interval) * strike_interval
        distance = strike - spot_price
        distance_points = abs(distance)
        
        # Determine moneyness
        if option_type == "CE":
            if strike < spot_price:
                moneyness = "ITM"
                estimated_delta = min(0.85, 0.5 + (spot_price - strike) / 500)
            elif strike > spot_price:
                moneyness = "OTM"
                estimated_delta = max(0.15, 0.5 - (strike - spot_price) / 500)
            else:
                moneyness = "ATM"
                estimated_delta = 0.50
        else:  # PE
            if strike > spot_price:
                moneyness = "ITM"
                estimated_delta = min(0.85, 0.5 + (strike - spot_price) / 500)
            elif strike < spot_price:
                moneyness = "OTM"
                estimated_delta = max(0.15, 0.5 - (spot_price - strike) / 500)
            else:
                moneyness = "ATM"
                estimated_delta = 0.50
        
        # Calculate days to expiry based on expiry date
        today = datetime.now()
        
        # Parse expiry date if provided (e.g., '27jan', '3feb', '17feb')
        if expiry_date_str:
            try:
                # Parse formats like '27jan', '3feb', '17feb'
                match = re.match(r'(\d{1,2})([a-zA-Z]{3})', expiry_date_str.strip())
                if match:
                    day = int(match.group(1))
                    month_str = match.group(2).lower()
                    
                    # Map month abbreviations
                    month_map = {
                        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
                        'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
                        'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
                    }
                    month = month_map.get(month_str)
                    
                    if month:
                        # Determine year (current or next if month has passed)
                        year = today.year
                        if month < today.month or (month == today.month and day < today.day):
                            year += 1
                        
                        expiry_date = datetime(year, month, day)
                        days_to_expiry = max(0, (expiry_date - today).days)
                        
                        # Determine if it's weekly or monthly
                        from bot.trend_analyzer import TrendAnalyzer
                        last_exp_of_month = TrendAnalyzer._last_weekday_of_month(
                            expiry_date, idx_cfg.expiry_weekday
                        )
                        
                        if expiry_date.date() == last_exp_of_month:
                            expiry_label = "MONTHLY"
                            time_multiplier = 1.0 + (days_to_expiry / 30)
                        else:
                            expiry_label = "WEEKLY"
                            time_multiplier = 0.5 + (days_to_expiry / 14)
                        
                        expiry_date_display = expiry_date.strftime("%d-%b-%Y")
                    else:
                        raise ValueError(f"Invalid month: {month_str}")
                else:
                    raise ValueError(f"Could not parse: {expiry_date_str}")
            except Exception as e:
                logger.warning(f"Could not parse expiry date '{expiry_date_str}': {e}, using next expiry")
                expiry_date_str = None
        
        # If no expiry date provided, calculate from index config
        if not expiry_date_str:
            weekday = today.weekday()
            exp_wd = idx_cfg.expiry_weekday
            if idx_cfg.weekly_expiry:
                days_until_exp = (exp_wd - weekday) % 7
                if days_until_exp == 0 and today.hour >= 15:
                    days_until_exp = 7
            else:
                # Monthly only: find last <exp_wd> of month
                from bot.trend_analyzer import TrendAnalyzer
                last_exp = TrendAnalyzer._last_weekday_of_month(today, exp_wd)
                if last_exp < today or (last_exp == today and today.hour >= 15):
                    if today.month == 12:
                        nm = today.replace(year=today.year + 1, month=1, day=1)
                    else:
                        nm = today.replace(month=today.month + 1, day=1)
                    last_exp = TrendAnalyzer._last_weekday_of_month(nm, exp_wd)
                days_until_exp = (last_exp - today).days
            days_to_expiry = max(0, days_until_exp)
            expiry_date = today + timedelta(days=days_to_expiry)
            expiry_label = "MONTHLY" if not idx_cfg.weekly_expiry else "WEEKLY"
            expiry_date_display = expiry_date.strftime("%d-%b-%Y")
            time_multiplier = 0.6
        
        # Try to fetch real NSE data
        nse_data = None
        real_ltp = None
        real_iv = None
        real_oi = None
        is_real_data = False
        data_source = "Estimated"
        
        # Route to the right scraper based on exchange
        if idx_cfg.exchange == "NSE" and NSE_AVAILABLE:
            try:
                option_data = nse_scraper.get_option_data(strike, option_type, symbol=idx_cfg.name)
                
                if option_data and option_data.ltp > 0:
                    real_ltp = option_data.ltp
                    real_iv = option_data.iv
                    real_oi = option_data.open_interest
                    is_real_data = True
                    data_source = "NSE Live"
                    logger.info(f"Got real NSE data: LTP={real_ltp}, IV={real_iv}, OI={real_oi}")
                
                chain = nse_scraper.fetch_option_chain(idx_cfg.name)
                if chain:
                    levels = nse_scraper.get_support_resistance(idx_cfg.name)
                    if levels['resistance']:
                        resistance_levels = levels['resistance']
                    if levels['support']:
                        support_levels = levels['support']
                    real_pcr = chain.pcr_ratio
                    
            except Exception as e:
                logger.warning(f"Could not fetch NSE data: {e}, using estimated values")

        elif idx_cfg.exchange == "BSE" and BSE_AVAILABLE:
            try:
                option_data = bse_scraper.get_option_data(strike, option_type, symbol=idx_cfg.name)
                
                if option_data and option_data.ltp > 0:
                    real_ltp = option_data.ltp
                    real_iv = option_data.iv
                    real_oi = option_data.open_interest
                    is_real_data = True
                    data_source = "BSE Live"
                    logger.info(f"Got real BSE data: LTP={real_ltp}, IV={real_iv}, OI={real_oi}")
                
                chain = bse_scraper.fetch_option_chain(idx_cfg.name)
                if chain:
                    levels = bse_scraper.get_support_resistance(idx_cfg.name)
                    if levels['resistance']:
                        resistance_levels = levels['resistance']
                    if levels['support']:
                        support_levels = levels['support']
                    real_pcr = chain.pcr_ratio
                    
            except Exception as e:
                logger.warning(f"Could not fetch BSE data: {e}, using estimated values")
        
        # Use real LTP if available, otherwise estimate
        if real_ltp and real_ltp > 0:
            estimated_premium = real_ltp
        else:
            # Estimate premium range (simplified Black-Scholes approximation)
            base_time_value = 50
            time_value = base_time_value * time_multiplier * (days_to_expiry / 7) ** 0.5
            intrinsic_value = max(0, spot_price - strike) if option_type == "CE" else max(0, strike - spot_price)
            estimated_premium = intrinsic_value + time_value * (0.5 + estimated_delta)
        
        # Calculate support/resistance levels (fallback if NSE data not available)
        if 'resistance_levels' not in dir() or not resistance_levels:
            resistance_levels = [atm_strike + i * strike_interval for i in range(1, 4)]
        if 'support_levels' not in dir() or not support_levels:
            support_levels = [atm_strike - i * strike_interval for i in range(1, 4)]
        
        # Risk assessment
        if moneyness == "ITM":
            risk_level = "LOW"
            risk_desc = "Higher premium but safer. Good for conservative trades."
            breakeven = strike + estimated_premium if option_type == "CE" else strike - estimated_premium
        elif moneyness == "ATM":
            risk_level = "MEDIUM"
            risk_desc = "Balanced risk-reward. Good for moderate positions."
            breakeven = strike + estimated_premium if option_type == "CE" else strike - estimated_premium
        else:
            risk_level = "HIGH"
            risk_desc = "Lower premium but needs significant move. Aggressive trade."
            breakeven = strike + estimated_premium if option_type == "CE" else strike - estimated_premium
        
        # Trade recommendation
        if option_type == "CE" and trend == "BEARISH":
            recommendation = "AVOID"
            reason = "Buying CALL in bearish trend is risky"
            confidence = 20
        elif option_type == "PE" and trend == "BULLISH":
            recommendation = "AVOID"
            reason = "Buying PUT in bullish trend is risky"
            confidence = 20
        elif option_type == "CE" and trend == "BULLISH":
            if moneyness == "ITM":
                recommendation = "BUY"
                reason = "ITM Call in bullish trend - safe play"
                confidence = 75
            elif moneyness == "ATM":
                recommendation = "BUY"
                reason = "ATM Call in bullish trend - good entry"
                confidence = 70
            else:
                recommendation = "RISKY BUY"
                reason = "OTM Call needs strong move to profit"
                confidence = 50
        elif option_type == "PE" and trend == "BEARISH":
            if moneyness == "ITM":
                recommendation = "BUY"
                reason = "ITM Put in bearish trend - safe play"
                confidence = 75
            elif moneyness == "ATM":
                recommendation = "BUY"
                reason = "ATM Put in bearish trend - good entry"
                confidence = 70
            else:
                recommendation = "RISKY BUY"
                reason = "OTM Put needs strong move to profit"
                confidence = 50
        else:  # NEUTRAL trend
            recommendation = "WAIT"
            reason = "Neutral trend - wait for clear direction"
            confidence = 30
        
        # RSI consideration
        rsi_note = ""
        if rsi < 30:
            rsi_note = "RSI oversold - potential bounce"
            if option_type == "CE":
                confidence = min(90, confidence + 15)
        elif rsi > 70:
            rsi_note = "RSI overbought - potential pullback"
            if option_type == "PE":
                confidence = min(90, confidence + 15)
        
        # Expiry-specific advice based on days to expiry
        if days_to_expiry <= 0:
            expiry_advice = "EXPIRY DAY! Maximum theta decay. Only intraday scalps. Exit before 3:00 PM."
        elif days_to_expiry <= 3:
            expiry_advice = "Very short expiry! High theta decay. Only for quick scalps."
        elif days_to_expiry <= 7:
            expiry_advice = "Weekly: Fast decay, need quick move. Best for directional bets."
        elif days_to_expiry <= 14:
            expiry_advice = "2 weeks out: Moderate decay. Good balance of time and premium."
        else:
            expiry_advice = "Monthly/Far expiry: More time value, slower decay. Better for swing trades."
        
        # Generate Trade Plan
        trade_plan = self._generate_trade_plan(
            entry_premium=estimated_premium,
            moneyness=moneyness,
            trend=trend,
            confidence=confidence,
            is_real_data=is_real_data,
            data_source=data_source,
            lot_size=lot_size,
        )
        
        return {
            "strike": strike,
            "option_type": option_type,
            "spot_price": spot_price,
            "atm_strike": atm_strike,
            "distance_from_spot": distance,
            "distance_points": distance_points,
            "moneyness": moneyness,
            "estimated_delta": estimated_delta,
            "estimated_premium": estimated_premium,
            "breakeven": breakeven,
            "risk_level": risk_level,
            "risk_desc": risk_desc,
            "support_levels": support_levels,
            "resistance_levels": resistance_levels,
            "trend": trend,
            "rsi": rsi,
            "rsi_note": rsi_note,
            "recommendation": recommendation,
            "reason": reason,
            "confidence": confidence,
            "expiry_type": expiry_label,
            "expiry_date": expiry_date_display,
            "days_to_expiry": days_to_expiry,
            "expiry_advice": expiry_advice,
            # Exchange data fields
            "is_real_data": is_real_data,
            "data_source": data_source,
            "real_iv": real_iv,
            "real_oi": real_oi,
            # Trade plan
            "trade_plan": trade_plan,
            # Index info
            "index_name": idx_cfg.name,
            "exchange": idx_cfg.exchange,
        }
    
    def format_strike_analysis(self, analysis: Dict) -> str:
        """Format specific strike analysis as readable report"""
        
        direction = "above" if analysis['distance_from_spot'] > 0 else "below"
        
        # Determine clear GO / NO-GO decision
        confidence = analysis['confidence']
        recommendation = analysis['recommendation']
        
        if recommendation == "BUY" and confidence >= 65:
            decision = "GO"
            decision_text = ">>> GO - TRADE RECOMMENDED <<<"
        elif recommendation == "RISKY BUY" and confidence >= 50:
            decision = "CAUTION"
            decision_text = ">>> CAUTION - HIGH RISK TRADE <<<"
        elif recommendation == "AVOID":
            decision = "NO-GO"
            decision_text = ">>> NO-GO - AVOID THIS TRADE <<<"
        else:  # WAIT or low confidence
            decision = "NO-GO"
            decision_text = ">>> NO-GO - WAIT FOR BETTER SETUP <<<"
        
        # Get expiry info (with defaults for backward compatibility)
        expiry_type = analysis.get('expiry_type', 'WEEKLY')
        expiry_date = analysis.get('expiry_date', 'N/A')
        days_to_expiry = analysis.get('days_to_expiry', 0)
        expiry_advice = analysis.get('expiry_advice', '')
        
        # Dynamic human-readable expiry text
        if days_to_expiry <= 0:
            days_display = "TODAY (Expiry Day!)"
        elif days_to_expiry == 1:
            days_display = "Tomorrow"
        else:
            days_display = f"{days_to_expiry} days away"
        
        # Get trade plan
        trade_plan = analysis.get('trade_plan', {})
        data_source = analysis.get('data_source', 'Estimated')
        is_real_data = analysis.get('is_real_data', False)
        real_iv = analysis.get('real_iv')
        real_oi = analysis.get('real_oi')
        
        # Data source indicator
        data_indicator = "[LIVE]" if is_real_data else "[EST]"
        
        exchange_label = analysis.get('exchange', 'NSE')
        live_section = ""
        if is_real_data:
            live_section = f"""
  {exchange_label} LIVE DATA:
  - Real LTP: Rs.{analysis['estimated_premium']:.2f}
  - Implied Volatility: {real_iv:.1f}%
  - Open Interest: {real_oi:,}
"""
        
        # Trade plan section
        trade_plan_section = ""
        if trade_plan:
            tp = trade_plan
            trade_plan_section = f"""
----------------------------------------------------------------
  TRADE PLAN {data_indicator}
----------------------------------------------------------------
  ENTRY:
  - Premium: Rs.{tp.get('entry_price', 0):.2f}
  - Entry Range: Rs.{tp.get('entry_range_low', 0):.2f} - Rs.{tp.get('entry_range_high', 0):.2f}

  TARGETS:
  - Target 1: Rs.{tp.get('target_1', 0):.2f} (+{tp.get('target_1_pct', 0):.0f}%)
  - Target 2: Rs.{tp.get('target_2', 0):.2f} (+{tp.get('target_2_pct', 0):.0f}%)

  STOP-LOSS:
  - Stop Loss: Rs.{tp.get('stop_loss', 0):.2f} (-{tp.get('stop_loss_pct', 0):.0f}%)

  RISK-REWARD:
  - Ratio: 1:{tp.get('risk_reward_ratio', 0):.1f}
  - Max Loss/Lot: Rs.{tp.get('max_loss_per_lot', 0):.0f}
  - Max Profit/Lot: Rs.{tp.get('max_profit_per_lot', 0):.0f}
  - Lot Size: {tp.get('lot_size', 25)}

  Data Source: {tp.get('data_source', 'Estimated')}
"""
        
        return f"""
================================================================
  STRIKE ANALYSIS: {analysis['strike']} {analysis['option_type']} ({expiry_type}) {data_indicator}
================================================================

  ************************************************************
  *                                                          *
  *   STRIKE:  {analysis['strike']} {analysis['option_type']}  ({analysis['moneyness']})
  *   EXPIRY:  {expiry_date} ({expiry_type}) -- {days_display}
  *                                                          *
  *   {decision_text}
  *                                                          *
  *   Confidence: {confidence}%
  *                                                          *
  ************************************************************

  CURRENT MARKET:
  - Spot Price: Rs.{analysis['spot_price']:,.2f}
  - ATM Strike: {analysis['atm_strike']}
  - Trend: {analysis['trend']}
  - RSI: {analysis['rsi']:.1f} {analysis['rsi_note']}
{live_section}
  EXPIRY INFO:
  - Type: {expiry_type}
  - Expiry Date: {expiry_date}
  - Time Left: {days_display}
  - {expiry_advice}

  STRIKE DETAILS:
  - Distance: {analysis['distance_points']:.0f} points {direction} spot
  - Moneyness: {analysis['moneyness']} (In/At/Out of the Money)
  - Est. Delta: ~{analysis['estimated_delta']:.2f}
  - Premium: Rs.{analysis['estimated_premium']:.0f} {data_indicator}
  - Breakeven: Rs.{analysis['breakeven']:,.2f}

  KEY LEVELS (from OI):
  - Resistance: {', '.join(map(str, analysis['resistance_levels']))}
  - Support: {', '.join(map(str, analysis['support_levels']))}

  RISK: {analysis['risk_level']}
  {analysis['risk_desc']}

  REASON: {analysis['reason']}
{trade_plan_section}
================================================================
  FINAL VERDICT: {decision}
================================================================

  DISCLAIMER: Targets and stop-loss are estimates based on
  technical analysis. Options trading involves significant
  risk. Past performance does not guarantee future results.
  Always use proper risk management. Data source: {data_source}
================================================================
"""

    def format_option_analysis(self, analysis: OptionChainAnalysis, trend: str) -> str:
        """Format option chain analysis as readable report"""
        
        pcr_sentiment = 'Bullish' if analysis.pcr_ratio < 1 else 'Bearish' if analysis.pcr_ratio > 1.2 else 'Neutral'
        
        return f"""
================================================================
              SMART STRIKE ANALYSIS                            
================================================================
  Spot Price: Rs.{analysis.spot_price:,.2f}
  ATM Strike: {analysis.atm_strike}
  Trend: {trend}
  
  SUPPORT/RESISTANCE (from OI analysis):
  - Resistance: {', '.join(map(str, analysis.resistance_levels))}
  - Support: {', '.join(map(str, analysis.support_levels))}
  - PCR Ratio: {analysis.pcr_ratio:.2f} ({pcr_sentiment})
  
----------------------------------------------------------------
  STRIKE RECOMMENDATIONS:
  
  [CONSERVATIVE] Low Risk:
     Strike: {analysis.conservative_strike.strike} {analysis.conservative_strike.option_type}
     Type: {analysis.conservative_strike.moneyness}
     Delta: ~{analysis.conservative_strike.estimated_delta:.2f}
     Confidence: {analysis.conservative_strike.confidence:.0f}%
     {analysis.conservative_strike.rationale[:60]}...
  
  [MODERATE] Balanced:
     Strike: {analysis.moderate_strike.strike} {analysis.moderate_strike.option_type}
     Type: {analysis.moderate_strike.moneyness}
     Delta: ~{analysis.moderate_strike.estimated_delta:.2f}
     Confidence: {analysis.moderate_strike.confidence:.0f}%
     {analysis.moderate_strike.rationale[:60]}...
  
  [AGGRESSIVE] High Risk/Reward:
     Strike: {analysis.aggressive_strike.strike} {analysis.aggressive_strike.option_type}
     Type: {analysis.aggressive_strike.moneyness}
     Delta: ~{analysis.aggressive_strike.estimated_delta:.2f}
     Confidence: {analysis.aggressive_strike.confidence:.0f}%
     {analysis.aggressive_strike.rationale[:60]}...
  
================================================================
"""
    
    def format_stock_analysis(self, analysis: StockAnalysis) -> str:
        """Format stock analysis as readable report"""
        
        action_label = "[BUY]" if analysis.action == "BUY" else "[SELL]" if analysis.action == "SELL" else "[HOLD]"
        rsi_note = '(Oversold)' if analysis.rsi < 30 else '(Overbought)' if analysis.rsi > 70 else ''
        change_sign = '+' if analysis.change_percent > 0 else ''
        
        return f"""
================================================================
  {analysis.name} ({analysis.symbol})
================================================================
  Current Price: Rs.{analysis.current_price:,.2f} ({change_sign}{analysis.change_percent:.2f}%)
  Trend: {analysis.trend}
  
  TECHNICAL INDICATORS:
  - RSI(14): {analysis.rsi:.1f} {rsi_note}
  - SMA(20): Rs.{analysis.sma_20:,.2f}
  - SMA(50): Rs.{analysis.sma_50:,.2f}
  - SMA(200): Rs.{analysis.sma_200:,.2f}
  
  LEVELS:
  - Support: Rs.{analysis.support:,.2f}
  - Resistance: Rs.{analysis.resistance:,.2f}
  
  {action_label} RECOMMENDATION: {analysis.action}
  - Target: Rs.{analysis.target_price:,.2f}
  - Stop Loss: Rs.{analysis.stop_loss:,.2f}
  - Confidence: {analysis.confidence:.0f}%
  
  Rationale: {analysis.rationale}
================================================================
"""


# Global instance
market_research = MarketResearch()
