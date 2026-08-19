"""
Google Maps Doctor-to-Pharmacy Matcher & Ranker
Finds the top 5 closest pharmacies/chemists near each doctor ranked by actual road/walking distance.
Includes SQLite caching, smart adaptive radius (300m -> fallback), checkpointing, and Excel export.
"""

import os
import re
import math
import time
import sqlite3
import argparse
import logging
import glob
from typing import Dict, List, Any, Optional, Tuple
from urllib.parse import quote_plus
from datetime import datetime

import pandas as pd
import requests
from anyascii import anyascii

# ---------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

def load_api_key() -> str:
    """Loads Google Maps API Key from environment or .env file."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key and os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GOOGLE_MAPS_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        raise ValueError("Google Maps API Key not found. Please set GOOGLE_MAPS_API_KEY in .env file or environment variable.")
    return api_key

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("chemist_finder.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("ChemistFinder")


# ---------------------------------------------------------
# SQLite Cache Manager (Prevents Duplicate API Charges)
# ---------------------------------------------------------
class CacheManager:
    def __init__(self, db_path: str = "cache.db"):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path, timeout=30.0)

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    query_text TEXT PRIMARY KEY,
                    lat REAL,
                    lng REAL,
                    formatted_address TEXT,
                    postal_code TEXT,
                    city TEXT,
                    status TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS places_cache (
                    cache_key TEXT PRIMARY KEY,
                    raw_json TEXT,
                    status TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS distance_cache (
                    cache_key TEXT PRIMARY KEY,
                    distance_meters INTEGER,
                    distance_text TEXT,
                    duration_seconds INTEGER,
                    duration_text TEXT,
                    status TEXT
                )
            """)
            conn.commit()

    def get_geocode(self, query: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT lat, lng, formatted_address, postal_code, city, status FROM geocode_cache WHERE query_text = ?", (query.strip(),))
            row = cur.fetchone()
            if row:
                return {
                    "lat": row[0],
                    "lng": row[1],
                    "formatted_address": row[2],
                    "postal_code": row[3],
                    "city": row[4],
                    "status": row[5]
                }
        return None

    def save_geocode(self, query: str, data: Dict[str, Any]):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO geocode_cache (query_text, lat, lng, formatted_address, postal_code, city, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (query.strip(), data.get("lat"), data.get("lng"), data.get("formatted_address"), data.get("postal_code"), data.get("city"), data.get("status")))
            conn.commit()

    def get_places(self, cache_key: str) -> Optional[str]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT raw_json FROM places_cache WHERE cache_key = ?", (cache_key,))
            row = cur.fetchone()
            return row[0] if row else None

    def save_places(self, cache_key: str, raw_json: str, status: str):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO places_cache (cache_key, raw_json, status)
                VALUES (?, ?, ?)
            """, (cache_key, raw_json, status))
            conn.commit()

    def get_distance(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT distance_meters, distance_text, duration_seconds, duration_text, status FROM distance_cache WHERE cache_key = ?", (cache_key,))
            row = cur.fetchone()
            if row:
                return {
                    "distance_meters": row[0],
                    "distance_text": row[1],
                    "duration_seconds": row[2],
                    "duration_text": row[3],
                    "status": row[4]
                }
        return None

    def save_distance(self, cache_key: str, data: Dict[str, Any]):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO distance_cache (cache_key, distance_meters, distance_text, duration_seconds, duration_text, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cache_key, data.get("distance_meters"), data.get("distance_text"), data.get("duration_seconds"), data.get("duration_text"), data.get("status")))
            conn.commit()


# ---------------------------------------------------------
# Helper Functions (Geometry & Parsing)
# ---------------------------------------------------------
def add_timestamp(filepath: str, timestamp_str: Optional[str] = None) -> str:
    """
    Appends a timestamp suffix to a file path before the extension.
    Example: 'final_doctor_nearest_5_chemists.xlsx' -> 'final_doctor_nearest_5_chemists_20260818_150336.xlsx'
    """
    if not timestamp_str:
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    dirname, filename = os.path.split(filepath)
    base, ext = os.path.splitext(filename)
    new_filename = f"{base}_{timestamp_str}{ext}"
    return os.path.join(dirname, new_filename) if dirname else new_filename


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes approximate straight-line distance in meters between two coordinates."""
    R = 6371000  # Radius of earth in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def extract_indian_pincode(text: str) -> Optional[str]:
    """Extracts a 6-digit Indian PIN code from address text."""
    if not text:
        return None
    matches = re.findall(r'\b[1-9][0-9]{5}\b', str(text))
    return matches[-1] if matches else None

def cleanup_old_results(base_dir: str = ".") -> List[str]:
    """
    Deletes all previous run result files and checkpoints before starting a new run.
    Cleans:
    - final_doctor_nearest_*.xlsx / final_doctor_nearest_*.csv
    - summary_report*.txt
    - checkpoints/*.xlsx / checkpoints/*.json
    """
    patterns = [
        os.path.join(base_dir, "final_doctor_nearest_*.xlsx"),
        os.path.join(base_dir, "final_doctor_nearest_*.csv"),
        os.path.join(base_dir, "summary_report*.txt"),
        os.path.join(base_dir, "checkpoints", "*.xlsx"),
        os.path.join(base_dir, "checkpoints", "*.json"),
    ]
    deleted_files = []
    for pattern in patterns:
        for filepath in glob.glob(pattern):
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
                    deleted_files.append(filepath)
            except Exception as e:
                logger.warning(f"Could not delete old file {filepath}: {e}")
    
    if deleted_files:
        logger.info(f"Cleaned up {len(deleted_files)} old result/checkpoint file(s): {[os.path.basename(f) for f in deleted_files]}")
    else:
        logger.info("No old result files found to clean.")
    return deleted_files


def extract_city(address: str, fallback_city: str = "") -> str:
    """
    Returns the exact city where the associated doctor is located.
    Falls back to cleaned doctor city or address parsing if doctor city is missing.
    """
    if fallback_city and str(fallback_city).strip() and str(fallback_city).strip().lower() not in ["nan", "none", ""]:
        return str(fallback_city).strip()
    if not address:
        return ""
    parts = [p.strip() for p in str(address).split(",") if p.strip()]
    if len(parts) >= 2:
        for part in reversed(parts):
            cleaned = re.sub(r'\b[1-9][0-9]{5}\b', '', part).strip()
            if cleaned and cleaned.lower() not in ["india", "maharashtra", "delhi", "karnataka", "tamil nadu", "west bengal", "uttar pradesh", "gujarat"]:
                return cleaned
    return ""


def clean_extracted_name(raw_name: str) -> str:
    """
    Cleans and standardizes extracted names:
    - Transliterates non-ASCII characters to standard English Latin text
    - Replaces pipe characters ('|', '||', '¦', '‖', etc.) with standard clean separators (' - ')
    - Removes unnecessary decorative/special symbols (like '•', '★', '®', '™', '~', '^', '_', '`', '#', etc.)
    - Preserves legitimate apostrophes (e.g., Mohan's, Dey's), asterisks ('*'), and alphanumeric text
    - Strips leading/trailing punctuation and redundant whitespace
    """
    if not raw_name:
        return ""
    
    # 1. Transliterate unicode to ASCII (handles Hindi, regional scripts, accented letters)
    text = anyascii(str(raw_name))
    
    # 2. Normalize whitespace, newlines, tabs, and common HTML entities
    text = re.sub(r'[\t\r\n\v\f]+', ' ', text)
    text = re.sub(r'&nbsp;|&quot;|&lt;|&gt;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    
    # 3. Normalize quotes and apostrophes (convert backtick, smart quotes to standard apostrophe)
    text = text.replace('`', "'").replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    
    # 4. Replace pipe / vertical bar variants with ' - '
    # Handles '|', '||', ' | ', '| - |', '¦', '‖', '∣', '│', '｜', etc.
    text = re.sub(r'\s*[|¦‖∣│｜]+\s*', ' - ', text)
    
    # 5. Remove unnecessary decorative/special symbols (preserving asterisk '*')
    # Bullets, stars, trademark, copyright, tildes, underscores, carets, section/degree symbols, hash
    text = re.sub(r'[•·●▪◆★☆✪~^_®™©°±§#]+', ' ', text)
    
    # 6. Remove quotes & backslashes, but preserve in-word apostrophes like Mohan's
    text = re.sub(r'["\\]', ' ', text)
    text = re.sub(r"(?<![a-zA-Z0-9])'|'(?![a-zA-Z0-9])", " ", text)
    
    # 7. Clean up duplicate or mismatched punctuation sequences
    text = re.sub(r'(\s*-\s*)+', ' - ', text)
    text = re.sub(r'(\s*,\s*)+', ', ', text)
    text = re.sub(r'\s*,\s*-\s*', ' - ', text)
    text = re.sub(r'\s*-\s*,\s*', ' - ', text)
    
    # 8. Normalize spaces
    text = re.sub(r'\s+', ' ', text).strip()
    
    # 9. Strip leading / trailing punctuation (hyphens, commas, colons, semicolons, slashes, pipes, dots)
    text = re.sub(r'^[\s\-:,;./|~]+', '', text)
    text = re.sub(r'[\s\-:,;./|~]+$', '', text)
    
    return text.strip()


def remove_plus_codes(address: str) -> str:
    """
    Removes Google Plus Codes (Open Location Codes, e.g. 'H9P7+WGM', 'VW86+P6P', '7XXR+5RM')
    from address strings while preserving clean street, landmark, area, city, and state information.
    """
    if not address or pd.isna(address):
        return ""
    text = str(address).strip()
    if text.lower() in ["nan", "none"]:
        return ""
    
    # 1. Remove standard word-bounded Open Location Codes (1 to 8 alphanumeric chars + '+' + 2 to 4 alphanumeric chars)
    # Examples: 'H9P7+WGM', 'VW86+P6P', '4WJP+7C9', '9F29F2F+9V', '7632FHJJ+VX', '2222+22'
    text = re.sub(r'\b[A-Za-z0-9]{1,8}\+[A-Za-z0-9]{2,4}\b', '', text)
    
    # 2. Remove uppercase plus codes attached to lowercase words (e.g. 'TelanganaG+94H' -> 'Telangana')
    text = re.sub(r'(?<=[a-z])[A-Z0-9]{1,4}\+[A-Z0-9]{2,4}\b', '', text)
    
    # 3. Clean up commas, hyphens, and whitespace leftovers
    text = re.sub(r'(\s*,\s*)+', ', ', text)
    text = re.sub(r'(\s*-\s*)+', ' - ', text)
    text = re.sub(r'^\s*[,;\-|\s]+', '', text)
    text = re.sub(r'[,;\-|\s]+$', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text


def extract_establishment_name(address: str) -> Optional[str]:
    """Extracts establishment / hospital / clinic / nursing home prefix from address if available."""
    if not address:
        return None
    match = re.match(
        r'^([^,]+?(?:hospital|nursing home|clinic|centre|center|health care|maternity home|polyclinic|dispensary|care|institute))(?=[,\s]|$)',
        address,
        re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    first_clause = address.split(',')[0].strip()
    if re.search(r'\b(hospital|nursing home|clinic|centre|center|polyclinic|dispensary|institute)\b', first_clause, re.IGNORECASE):
        return first_clause
    return None


def format_doctor_name_for_query(raw_name: str) -> str:
    """
    Normalizes and formats doctor name for Google Maps entity search.
    Ensures a clean 'Dr. ' prefix for individual practitioners while
    preventing duplicate prefixes (e.g. 'Dr. Dr.') and skipping institutions.
    """
    if not raw_name:
        return ""
    clean = clean_extracted_name(raw_name)
    if not clean or clean.lower() in ["nan", "none"]:
        return ""
    
    # If the name is an institution/clinic/hospital rather than an individual doctor, leave as is
    if re.search(r'\b(hospital|nursing home|clinic|centre|center|polyclinic|dispensary|trust|institute|lab|laboratory|diagnostics?)\b', clean, re.IGNORECASE):
        return clean

    # Strip any existing variations of doctor/prof prefix
    stripped = re.sub(r'^(dr\.?|doctor|prof\.?|professor)\s*', '', clean, flags=re.IGNORECASE).strip()
    if not stripped:
        return clean
    
    return f"Dr. {stripped}"


def extract_prominent_localities(address: str) -> List[str]:
    """Extracts known locality and city tokens from address to validate against geocode results."""
    tokens = [
        'bhiwandi', 'kalyan', 'dombivli', 'thane', 'mumbra', 'ulhasnagar', 'ambarnath', 'badlapur',
        'chembur', 'andheri', 'borivali', 'ghatkopar', 'kurla', 'mulund', 'dadar', 
        'bandra', 'malad', 'kandivali', 'goregaon', 'jogeshwari', 'santacruz', 'vile parle',
        'mira road', 'bhayandar', 'virar', 'vasai', 'kharghar', 'nerul', 'airoli', 'belapur',
        'panvel', 'colaba', 'worli', 'parel', 'byculla', 'mahim', 'sion', 'matunga', 'wadala',
        'dahisar', 'bhandup', 'kanjurmarg', 'vikhroli', 'vidyavihar', 'govandi', 'mankhurd',
        'shahdara', 'rohini', 'dwarka', 'saket', 'janakpuri', 'kamla nagar', 'karol bagh', 'lajpat nagar',
        'whitefield', 'indiranagar', 'hsr layout', 'koramangala', 'jayanagar', 'jp nagar', 'btm layout',
        'rajajinagar', 'malleshwaram', 'banashankari', 'hebbal', 'yelahanka', 'electronic city',
        'kothrud', 'hadapsar', 'wakad', 'hinjewadi', 'baner', 'aundh', 'viman nagar', 'kalyani nagar',
        'lucknow', 'kanpur', 'varanasi', 'prayagraj', 'allahabad', 'agra', 'kolkata', 'howrah',
        'delhi', 'noida', 'gurgaon', 'gurugram', 'ghaziabad', 'faridabad', 'patna', 'ranchi',
        'bengaluru', 'bangalore', 'chennai', 'hyderabad', 'secunderabad', 'ahmedabad', 'surat', 'vadodara',
        'pune', 'nagpur', 'nashik', 'aurangabad', 'kolhapur', 'solapur', 'amravati'
    ]
    addr_lower = address.lower()
    found = [t for t in tokens if re.search(r'\b' + re.escape(t) + r'\b', addr_lower)]
    return found


def locality_matches_result(expected_tokens: List[str], formatted_address: str) -> bool:
    """Verifies that formatted address returned by Google contains the expected locality if known."""
    if not expected_tokens:
        return True
    fmt_lower = formatted_address.lower()
    for token in expected_tokens:
        if token in fmt_lower:
            return True
    return False


def is_unwanted_pharmacy_entity(place_or_name) -> Tuple[bool, str]:
    """
    Identifies and filters non-pharmacy entities:
    - Clinics, Doctors, Polyclinics, Dispensaries, OPDs
    - Diagnostic centers, Pathology Labs, Blood collection centres, Imaging/Scans
    - Surgical suppliers & equipment
    - Enterprises, B2B Distributors, Wholesale Agencies, Trading, Corporate, Marketing, Agro, Chemicals
    - Banking, ATMs, Financial kiosks
    - Gyms, Fitness centers, Yoga studios, Wellness centers
    - Veterinary & Pet clinics/shops, Animal care
    - Homeopathy, Ayurveda, Unani, Siddha, Naturopathy, Herbal/Patanjali/Baidyanath
    - Dental clinics, Opticals, Eye care, Eyewear
    - Standalone Hospitals & Medical Colleges
    """
    if isinstance(place_or_name, dict):
        name = place_or_name.get("name", "").strip()
        types = set(place_or_name.get("types", []))
    else:
        name = str(place_or_name).strip()
        types = set()

    name = clean_extracted_name(name)
    name_lower = name.lower()

    # 0. Reject Empty / Punctuation-only / Garbage / Dummy Placeholders (minimum 5 valid characters required)
    alpha_letters = re.findall(r'[a-zA-Z0-9]', name)
    if len(alpha_letters) < 5:
        return True, f"Garbage: Too short (< 5 chars: '{name}')"

    if re.match(r'^(test|testing|dummy|unknown|null|none|n/a|na|nil|temp|sample|untitled|fake|invalid)$', name_lower):
        return True, "Garbage: Placeholder/Dummy"

    if re.match(r'^(test|dummy|fake|sample)\s+(pharmacy|medical|chemist|store|shop)$', name_lower):
        return True, "Garbage: Test/Fake Pharmacy"

    # 1. Place Types Filter (pure non-pharmacies)
    if types.intersection({"veterinary_care", "pet_store"}):
        return True, "Type: Veterinary/Pet"
    if "gym" in types:
        return True, "Type: Gym"
    if "dentist" in types and not any(k in name_lower for k in ["pharmacy", "chemist", "medical store", "medicos"]):
        return True, "Type: Dentist"

    # 2. Veterinary & Pet Clinics / Animal Care
    if re.search(r"\b(veterinary|veterinarian|vet|vets|pet|pets|pet care|pet clinic|pet shop|animal|animals|dog|cat|canine|puppy|aquarium|birds)\b", name_lower):
        return True, "Keyword: Veterinary/Pet"

    # 3. Gym, Fitness, Workout, Yoga & Wellness Centers
    if re.search(r"\b(gym|gymnasium|fitness|workout|crossfit|yoga|aerobics|wellness centre|wellness center)\b", name_lower):
        return True, "Keyword: Gym/Fitness/Yoga"

    # 4. Banking, ATM & Financial Services
    if re.search(r"\b(kiosk banking|banking|money transfer|insurance|loan|credit)\b", name_lower):
        return True, "Keyword: Banking/Finance"
    if re.search(r"^(bank of|state bank|canara bank|hdfc|icici|sbi|axis bank|punjab national bank|union bank|bank|atm)\b", name_lower):
        return True, "Keyword: Bank/ATM"

    # 5. Clinics, Polyclinics, Dispensaries, OPDs, Counselling & Consulting
    if re.search(r"\b(clinic|clinics|polyclinic|polyclinics|smartclinic|dispensary|dispensaries|counselling|consulting|consultant|consultants|consultancy|opd|sevadham|care clinic)\b", name_lower):
        return True, "Keyword: Clinic/Polyclinic/Dispensary"

    # 6. Doctors, Physicians, Surgeons & Individual Practices
    if re.search(r"\b(doctor|doctors|physician|physicians|surgeon|surgeons)\b", name_lower):
        return True, "Keyword: Doctor/Physician/Surgeon"
    if re.search(r"\b(prof\.|professor)\s*dr\b", name_lower):
        return True, "Keyword: Prof. Dr."
    if re.search(r"\b(dr\.\s*[a-z]|dr\s+[a-z])", name_lower):
        if any(b in name_lower for b in ["morepen", "tabscience", "dr.generics", "dr generics", "dr. pharmacy", "dr pharmacy", "dr. chemist", "dr chemist"]):
            pass
        elif re.search(r"\b(road|rd|nagar|colony|marg|salai|nedunsalai|rasta|gali|bazaar|bazar|market|street|st|circle|cross|layout|block|sector)\b", name_lower) and any(k in name_lower for k in ["medplus", "apollo pharmacy", "wellness forever", "zeelab", "pharmacy", "chemist", "medical store", "medicals"]):
            pass
        else:
            return True, "Keyword: Doctor Practice/Name"

    # 7. Diagnostic Centers, Pathology Labs, Blood Collection & Imaging
    if re.search(r"\b(diagnostic|diagnostics|laboratory|laboratories|lab|labs|pathology|pathlab|pathlabs|blood collection|blood bank|sample collection|imaging|scan|scans|ultrasound|x-ray|xray|mri|ecg|test centre|test center|path care)\b", name_lower):
        return True, "Keyword: Diagnostic/Lab/Blood Collection"

    # 8. Surgical Equipment & Supplies
    if re.search(r"\b(surgical|surgicals|surgery|surgico|surgi)\b", name_lower):
        return True, "Keyword: Surgical"

    # 9. Enterprises, Wholesale Agencies, Distributors, B2B, Industrial, Agro, Chemicals, Non-pharma Retail
    if re.search(r"\b(enterprise|enterprises|agency|agencies|distributor|distributors|wholesaler|wholesalers|supplier|suppliers|exports|imports|trader|traders|trading|corporation|associates|kiosk|telecom|recharge|xerox|printers|travels|courier|logistics|marketing|machinery|packaging|software|technologies|properties|realtors|jewellers|jewellery|garments|textiles|stationery|agro|chemicals|chemical|scientific|equipment)\b", name_lower):
        return True, "Keyword: Enterprise/Agency/Distributor/Wholesale"

    # 10. Homeopathy, Ayurveda, Unani, Siddha, Naturopathy & Herbal
    if re.search(r"\b(homeo|homoeo|homeopathy|homoeopathy|homeopathic|homoeopathic|ayurved|ayurveda|ayurvedic|ayur|unani|siddha|naturopathy|herbal|patanjali|baidyanath|dabur|himalaya store|hamdard|tibbi|zandu)\b", name_lower):
        return True, "Keyword: Homeopathy/Ayurveda/Alternative"

    # 11. Dental, Optical, Vision & Eyewear
    if re.search(r"\b(dental|dentist|dentistry|dento|tooth|teeth|opticals|optical|optician|opticians|spectacles|lens|eyewear|vision care|eye care|eye hospital|lenskart)\b", name_lower):
        return True, "Keyword: Dental/Optical"

    # 12. Nursing Staff at Home / Home Healthcare
    if re.search(r"\b(nursing staff|nursing care|home health care|home nursing|nursing service|attendant)\b", name_lower):
        return True, "Keyword: Nursing Staff / Home Care"

    # 13. Standalone Hospital / Medical College / Nursing Home
    if re.search(r"\b(hospital|hospitals|nursing home|maternity home|medical college)\b", name_lower):
        if not re.search(r"\b(pharmacy|chemist|medical|medicals|medicos|druggist|drug store|aushadhi)\b", name_lower):
            return True, "Keyword: Standalone Hospital/Nursing Home"
        if re.search(r"\b(best multi speciality hospital|super speciality hospital|medical college and hospital)\b", name_lower) and not re.search(r"\b(apollo pharmacy|wellness forever|medplus|chemist|pharmacy)\b", name_lower):
            return True, "Keyword: Multi Speciality Hospital"

    return False, ""


# ---------------------------------------------------------
# Google Maps API Client
# ---------------------------------------------------------
class GoogleMapsClient:
    def __init__(self, api_key: str, cache: CacheManager):
        self.api_key = api_key
        self.cache = cache
        self.session = requests.Session()

    def _query_single_geocode(self, query: str) -> Optional[Dict[str, Any]]:
        """Queries SQLite cache or Google Geocoding API for a single query candidate."""
        query = query.strip()
        if not query:
            return None

        # Check SQLite Cache
        cached = self.cache.get_geocode(query)
        if cached and cached.get("lat") is not None:
            return cached

        # Make Live API Call
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        try:
            res = self.session.get(url, params={"address": query, "language": "en", "key": self.api_key}, timeout=15).json()
            status = res.get("status")
            if status == "OK" and res.get("results"):
                result = res["results"][0]
                loc = result["geometry"]["location"]
                formatted = result.get("formatted_address", "")

                postal_code = None
                city_found = None
                for comp in result.get("address_components", []):
                    types = comp.get("types", [])
                    if "postal_code" in types:
                        postal_code = comp.get("long_name")
                    if "locality" in types or "administrative_area_level_2" in types:
                        if not city_found:
                            city_found = comp.get("long_name")

                geo_data = {
                    "lat": loc["lat"],
                    "lng": loc["lng"],
                    "formatted_address": formatted,
                    "postal_code": postal_code or extract_indian_pincode(formatted) or "",
                    "city": city_found or "",
                    "status": "OK"
                }
                self.cache.save_geocode(query, geo_data)
                return geo_data
            elif status == "ZERO_RESULTS":
                self.cache.save_geocode(query, {"status": "ZERO_RESULTS"})
            else:
                logger.warning(f"Geocoding API status '{status}' for: {query}")
        except Exception as e:
            logger.error(f"Geocoding error for '{query}': {e}")
            time.sleep(0.5)

        return None

    def geocode_doctor(self, doc_name: str, address: str, pincode: str, city: str) -> Optional[Dict[str, Any]]:
        """
        Robust Dual-Anchor Geocoding System with Spatial Drift Guard.
        """
        clean_name = clean_extracted_name(doc_name)
        clean_addr = clean_extracted_name(address)
        clean_city = clean_extracted_name(city)
        clean_pin = str(pincode).strip().replace(".0", "")
        if clean_city.lower() in ["nan", "none"]:
            clean_city = ""
        if clean_pin.lower() in ["nan", "none"]:
            clean_pin = ""

        expected_localities = extract_prominent_localities(clean_addr)
        establishment = extract_establishment_name(clean_addr)

        # Step 1: Establish Physical Address Anchor
        addr_candidates = []
        p_full = [p for p in [clean_addr, clean_city, clean_pin, "India"] if p]
        addr_candidates.append(", ".join(p_full))

        if clean_pin:
            p_pin = [p for p in [clean_addr, clean_pin, "India"] if p]
            addr_candidates.append(", ".join(p_pin))

        if clean_city:
            p_city = [p for p in [clean_addr, clean_city, "India"] if p]
            addr_candidates.append(", ".join(p_city))

        addr_candidates.append(f"{clean_addr}, India")

        if establishment:
            p_est = [p for p in [establishment, clean_city, clean_pin, "India"] if p]
            addr_candidates.append(", ".join(p_est))

        geo_anchor = None
        for q in addr_candidates:
            res = self._query_single_geocode(q)
            if res and res.get("lat") is not None:
                if expected_localities:
                    if locality_matches_result(expected_localities, res.get("formatted_address", "")):
                        geo_anchor = res
                        break
                else:
                    geo_anchor = res
                    break

        if not geo_anchor:
            for q in addr_candidates:
                res = self._query_single_geocode(q)
                if res and res.get("lat") is not None:
                    geo_anchor = res
                    break

        # Step 2: Formulate Doctor Entity Query
        geo_entity = None
        query_doc_name = format_doctor_name_for_query(clean_name)
        if query_doc_name:
            entity_candidates = []
            if establishment:
                p_ne = [p for p in [query_doc_name, establishment, clean_city, clean_pin, "India"] if p]
                entity_candidates.append(", ".join(p_ne))
            p_na = [p for p in [query_doc_name, clean_addr, clean_city, clean_pin, "India"] if p]
            entity_candidates.append(", ".join(p_na))

            # Fallback to un-prefixed name if different
            if clean_name and clean_name != query_doc_name:
                if establishment:
                    p_ne_raw = [p for p in [clean_name, establishment, clean_city, clean_pin, "India"] if p]
                    entity_candidates.append(", ".join(p_ne_raw))
                p_na_raw = [p for p in [clean_name, clean_addr, clean_city, clean_pin, "India"] if p]
                entity_candidates.append(", ".join(p_na_raw))

            for q in entity_candidates:
                res = self._query_single_geocode(q)
                if res and res.get("lat") is not None:
                    geo_entity = res
                    break

        # Step 3: Spatial Drift Check
        if geo_anchor and geo_entity:
            dist = haversine_distance(geo_anchor["lat"], geo_anchor["lng"], geo_entity["lat"], geo_entity["lng"])
            entity_locality_ok = locality_matches_result(expected_localities, geo_entity.get("formatted_address", ""))

            if dist <= 350 and entity_locality_ok:
                return geo_entity
            else:
                logger.info(f"   [DRIFT GUARD] Doctor '{clean_name}' query drifted {dist:.1f}m. Enforcing address anchor: '{geo_anchor.get('formatted_address')}'.")
                return geo_anchor
        elif geo_anchor:
            return geo_anchor
        elif geo_entity:
            return geo_entity

        return None


    def search_nearby_pharmacies(self, lat: float, lng: float, radius: int = 300) -> List[Dict[str, Any]]:
        """Searches for pharmacies/chemists around coordinates using Places API."""
        cache_key = f"places_{lat:.6f}_{lng:.6f}_{radius}"
        cached_json = self.cache.get_places(cache_key)
        
        if cached_json:
            import json
            try:
                return json.loads(cached_json)
            except Exception:
                pass

        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lng}",
            "radius": radius,
            "type": "pharmacy",
            "language": "en",
            "key": self.api_key
        }

        try:
            res = self.session.get(url, params=params, timeout=15).json()
            status = res.get("status")
            if status in ["OK", "ZERO_RESULTS"]:
                results = res.get("results", [])
                import json
                self.cache.save_places(cache_key, json.dumps(results), status)
                return results
            else:
                logger.warning(f"Places API status '{status}' at ({lat}, {lng}), radius={radius}")
                return []
        except Exception as e:
            logger.error(f"Places API error at ({lat}, {lng}): {e}")
            return []

    def get_road_distances(
        self, origin_lat: float, origin_lng: float, destinations: List[Dict[str, Any]], mode: str = "walking"
    ) -> List[Dict[str, Any]]:
        """Computes road/walking distances from origin to list of destinations via Distance Matrix API."""
        if not destinations:
            return []

        results = []
        uncached_indices = []
        uncached_dest_coords = []

        # 1. Check SQLite distance cache first
        for i, dest in enumerate(destinations):
            d_lat = dest["geometry"]["location"]["lat"]
            d_lng = dest["geometry"]["location"]["lng"]
            cache_key = f"dist_{origin_lat:.6f}_{origin_lng:.6f}_to_{d_lat:.6f}_{d_lng:.6f}_{mode}"
            cached = self.cache.get_distance(cache_key)

            if cached and cached.get("distance_meters") is not None:
                dest_copy = dict(dest)
                dest_copy["road_distance_meters"] = cached["distance_meters"]
                dest_copy["road_distance_text"] = cached["distance_text"]
                dest_copy["duration_seconds"] = cached["duration_seconds"]
                dest_copy["duration_text"] = cached["duration_text"]
                results.append(dest_copy)
            else:
                dest_copy = dict(dest)
                results.append(dest_copy)
                uncached_indices.append(i)
                uncached_dest_coords.append(f"{d_lat},{d_lng}")

        # 2. Call Distance Matrix API for uncached elements in batches of up to 25
        if uncached_dest_coords:
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            batch_size = 25
            for b in range(0, len(uncached_dest_coords), batch_size):
                b_coords = uncached_dest_coords[b : b + batch_size]
                b_indices = uncached_indices[b : b + batch_size]

                params = {
                    "origins": f"{origin_lat},{origin_lng}",
                    "destinations": "|".join(b_coords),
                    "mode": mode,
                    "language": "en",
                    "key": self.api_key
                }

                try:
                    res = self.session.get(url, params=params, timeout=15).json()
                    if res.get("status") == "OK" and res.get("rows"):
                        elements = res["rows"][0].get("elements", [])
                        for el_idx, el in enumerate(elements):
                            real_idx = b_indices[el_idx]
                            d_lat = destinations[real_idx]["geometry"]["location"]["lat"]
                            d_lng = destinations[real_idx]["geometry"]["location"]["lng"]
                            cache_key = f"dist_{origin_lat:.6f}_{origin_lng:.6f}_to_{d_lat:.6f}_{d_lng:.6f}_{mode}"

                            if el.get("status") == "OK":
                                dist_val = el["distance"]["value"]
                                dist_txt = el["distance"]["text"]
                                dur_val = el["duration"]["value"]
                                dur_txt = el["duration"]["text"]

                                results[real_idx]["road_distance_meters"] = dist_val
                                results[real_idx]["road_distance_text"] = dist_txt
                                results[real_idx]["duration_seconds"] = dur_val
                                results[real_idx]["duration_text"] = dur_txt

                                self.cache.save_distance(cache_key, {
                                    "distance_meters": dist_val,
                                    "distance_text": dist_txt,
                                    "duration_seconds": dur_val,
                                    "duration_text": dur_txt,
                                    "status": "OK"
                                })
                            else:
                                # Fallback to Haversine if distance matrix fails for element
                                h_dist = int(haversine_distance(origin_lat, origin_lng, d_lat, d_lng))
                                results[real_idx]["road_distance_meters"] = h_dist
                                results[real_idx]["road_distance_text"] = f"{h_dist} m (est)"
                                results[real_idx]["duration_seconds"] = int(h_dist / 1.4)  # ~1.4 m/s walking
                                results[real_idx]["duration_text"] = f"{max(1, round(h_dist / 84))} mins"
                    else:
                        logger.warning(f"Distance Matrix API response: {res.get('status')}")
                except Exception as e:
                    logger.error(f"Distance Matrix API error: {e}")

        # Ensure all elements have at least haversine fallback if road distance is missing
        for r in results:
            if "road_distance_meters" not in r:
                d_lat = r["geometry"]["location"]["lat"]
                d_lng = r["geometry"]["location"]["lng"]
                h_dist = int(haversine_distance(origin_lat, origin_lng, d_lat, d_lng))
                r["road_distance_meters"] = h_dist
                r["road_distance_text"] = f"{h_dist} m"
                r["duration_seconds"] = int(h_dist / 1.4)
                r["duration_text"] = f"{max(1, round(h_dist / 84))} mins"

        return results


def get_adaptive_radius_steps(base_radius: int = 300, max_radius: int = 10000) -> List[int]:
    """Generates an expanding ladder of search radius thresholds up to max_radius."""
    tiers = [300, 800, 1500, 2500, 3500, 5000, 7500, 10000]
    steps = [base_radius]
    for t in tiers:
        if t > base_radius and t <= max_radius:
            steps.append(t)
    if max_radius not in steps and max_radius > base_radius:
        steps.append(max_radius)
    return sorted(list(set(steps)))


# ---------------------------------------------------------
# Core Matching Engine with Adaptive Search
# ---------------------------------------------------------
def find_top_pharmacies_for_doctor(
    doc_row: pd.Series,
    client: GoogleMapsClient,
    base_radius: int = 300,
    max_radius: int = 10000,
    target_count: int = 5,
    mode: str = "walking"
) -> List[Dict[str, Any]]:
    """Finds and ranks top pharmacies for a single doctor using multi-tier smart adaptive radius."""
    doc_id = str(doc_row.get("Doc ID", ""))
    doc_name = clean_extracted_name(str(doc_row.get("DOCTOR NAME", "")))
    doc_address = clean_extracted_name(str(doc_row.get("Address", "")))
    doc_pincode = str(doc_row.get("Pincode", "")).replace(".0", "")
    doc_city = clean_extracted_name(str(doc_row.get("Doctor City", "") or doc_row.get("City", "") or doc_row.get("CITY", "") or "")).strip()
    if doc_city.lower() in ["nan", "none"]:
        doc_city = ""

    # 1. Geocode Doctor
    geo = client.geocode_doctor(doc_name, doc_address, doc_pincode, doc_city)
    if not geo or geo.get("lat") is None:
        logger.warning(f"Failed to geocode Doctor {doc_id}: {doc_name} ({doc_address})")
        return []

    origin_lat, origin_lng = geo["lat"], geo["lng"]

    # 2. Multi-Tier Smart Adaptive Search (expands until target_count chemists are found)
    radius_steps = get_adaptive_radius_steps(base_radius=base_radius, max_radius=max_radius)
    candidates: Dict[str, Dict[str, Any]] = {}

    for current_radius in radius_steps:
        raw_places = client.search_nearby_pharmacies(origin_lat, origin_lng, radius=current_radius)
        for place in raw_places:
            # Filter non-pharmacy entities (clinics, doctors, labs, diagnostics, surgicals, enterprises, banking, gyms, pets, ayurveda, etc.)
            is_unwanted, reason = is_unwanted_pharmacy_entity(place)
            if is_unwanted:
                continue

            p_id = place.get("place_id") or place.get("name")
            if p_id and p_id not in candidates:
                candidates[p_id] = place

        if len(candidates) >= target_count:
            break

    if not candidates:
        logger.info(f"No pharmacies found for Doctor {doc_id} within {max_radius}m.")
        return []

    # 3. Pre-rank candidates by Haversine distance and take candidate pool for Distance Matrix
    candidate_list = list(candidates.values())
    for c in candidate_list:
        c_loc = c["geometry"]["location"]
        c["_haversine_dist"] = haversine_distance(origin_lat, origin_lng, c_loc["lat"], c_loc["lng"])

    candidate_list.sort(key=lambda x: x["_haversine_dist"])
    pool_size = max(10, target_count * 2)
    top_candidates = candidate_list[:pool_size]  # Take top candidates to compute road distance

    # 4. Compute Real Road / Walking Distance
    ranked_candidates = client.get_road_distances(origin_lat, origin_lng, top_candidates, mode=mode)

    # 5. Sort strictly by Road Distance (meters) Ascending
    ranked_candidates.sort(key=lambda x: x.get("road_distance_meters", 999999))
    top_matched = ranked_candidates[:target_count]

    # 6. Format Structured Records
    matched_records = []
    for rank_idx, pharm in enumerate(top_matched, 1):
        pharm_name = clean_extracted_name(pharm.get("name", "Unknown Pharmacy"))
        pharm_address = clean_extracted_name(pharm.get("vicinity") or pharm.get("formatted_address", ""))
        pharm_address_no_plus = remove_plus_codes(pharm_address)
        
        # Pincode extraction
        p_pincode = extract_indian_pincode(pharm_address) or doc_pincode
        p_city = extract_city(pharm_address, fallback_city=doc_city)

        dist_meters = pharm.get("road_distance_meters", 0)
        dist_km = round(dist_meters / 1000.0, 3)
        travel_time = pharm.get("duration_text", "")
        place_id = pharm.get("place_id", "")
        gmaps_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(pharm_name)}&query_place_id={place_id}" if place_id else f"https://maps.google.com/?q={quote_plus(pharm_name + ' ' + pharm_address)}"

        record = {
            "Doc ID": doc_id,
            "Doctor Name": doc_name,
            "Doctor Address": doc_address,
            "Doctor Pincode": doc_pincode,
            "Doctor City": doc_city,
            "Doctor Lat": origin_lat,
            "Doctor Lng": origin_lng,
            "Pharmacy Rank": rank_idx,
            "Pharmacy Name": pharm_name,
            "Pharmacy Address": pharm_address,
            "Pharmacy Address (No Plus Code)": pharm_address_no_plus,
            "Pharmacy Pincode": p_pincode,
            "Pharmacy City": p_city,
            "Pharmacy Distance (meters)": dist_meters,
            "Pharmacy Distance (km)": dist_km,
            "Travel Time": travel_time,
            "Google Maps URL": gmaps_url,
            "Place ID": place_id
        }
        matched_records.append(record)

    return matched_records


# ---------------------------------------------------------
# Batch Processor & Checkpointing
# ---------------------------------------------------------
def process_doctor_file(
    input_file: str,
    output_excel: str = "final_doctor_nearest_5_chemists.xlsx",
    api_key: str = "",
    limit: Optional[int] = None,
    base_radius: int = 300,
    max_radius: int = 10000,
    target_count: int = 5,
    mode: str = "walking",
    use_timestamp: bool = True
):
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if use_timestamp:
        output_excel = add_timestamp(output_excel, run_timestamp)
        checkpoint_file = add_timestamp("checkpoints/checkpoint_data.json", run_timestamp)
    else:
        checkpoint_file = "checkpoints/checkpoint_data.json"

    # Clean up previous run result files and checkpoints before starting a new run
    cleanup_old_results(base_dir=os.path.dirname(os.path.abspath(output_excel)) or ".")

    logger.info(f"Starting Doctor-Pharmacy Matching Pipeline...")
    logger.info(f"Input File: {input_file}")
    logger.info(f"Output File: {output_excel}")
    logger.info(f"Search Strategy: Smart Adaptive (Base: {base_radius}m -> Max: {max_radius}m, Target: {target_count}, Mode: {mode})")

    # Read Input
    df_doctors = pd.read_excel(input_file)
    total_docs = len(df_doctors)
    if limit and limit > 0:
        df_doctors = df_doctors.head(limit)
        total_docs = len(df_doctors)
        logger.info(f"Running for first {total_docs} doctors as requested by limit.")

    cache = CacheManager()
    client = GoogleMapsClient(api_key=api_key, cache=cache)

    all_matched_records = []
    os.makedirs("checkpoints", exist_ok=True)

    start_time = time.time()

    for idx, (_, doc_row) in enumerate(df_doctors.iterrows(), 1):
        doc_id = doc_row.get("Doc ID", f"DOC_{idx}")
        doc_name = doc_row.get("DOCTOR NAME", "Unknown")

        logger.info(f"[{idx}/{total_docs}] Processing Doctor: {doc_id} - {doc_name}...")
        
        matches = find_top_pharmacies_for_doctor(
            doc_row=doc_row,
            client=client,
            base_radius=base_radius,
            max_radius=max_radius,
            target_count=target_count,
            mode=mode
        )
        
        if matches:
            all_matched_records.extend(matches)
            logger.info(f"   -> Found {len(matches)} pharmacies (Closest: {matches[0]['Pharmacy Name']} at {matches[0]['Pharmacy Distance (meters)']}m)")
        else:
            logger.warning(f"   -> No pharmacies matched for {doc_id}")

        # Checkpoint every 50 records
        if idx % 50 == 0:
            logger.info(f"--- Checkpoint: Processed {idx}/{total_docs} doctors. Total pharmacy rows: {len(all_matched_records)} ---")

    # ---------------------------------------------------------
    # Generate Output Reports (Long Format & Wide Format)
    # ---------------------------------------------------------
    df_long = pd.DataFrame(all_matched_records)
    
    # Build Wide Summary Table (1 row per doctor)
    wide_rows = []
    for _, doc_row in df_doctors.iterrows():
        doc_id = doc_row.get("Doc ID")
        doc_matches = [m for m in all_matched_records if m["Doc ID"] == doc_id]
        doc_city = clean_extracted_name(str(doc_row.get("Doctor City", "") or doc_row.get("City", "") or doc_row.get("CITY", "") or "")).strip()
        if doc_city.lower() in ["nan", "none"]:
            doc_city = ""
        
        row_dict = {
            "Doc ID": doc_id,
            "Doctor Name": clean_extracted_name(str(doc_row.get("DOCTOR NAME", ""))),
            "Doctor Address": clean_extracted_name(str(doc_row.get("Address", ""))),
            "Doctor Pincode": doc_row.get("Pincode"),
            "Doctor City": doc_city,
            "Pharmacies Found": len(doc_matches)
        }
        
        for rank in range(1, target_count + 1):
            if rank <= len(doc_matches):
                m = doc_matches[rank - 1]
                row_dict[f"Pharmacy_{rank}_Name"] = m["Pharmacy Name"]
                row_dict[f"Pharmacy_{rank}_Address"] = m["Pharmacy Address"]
                row_dict[f"Pharmacy_{rank}_Address_No_Plus_Code"] = m.get("Pharmacy Address (No Plus Code)", remove_plus_codes(m.get("Pharmacy Address", "")))
                row_dict[f"Pharmacy_{rank}_Pincode"] = m["Pharmacy Pincode"]
                row_dict[f"Pharmacy_{rank}_City"] = m["Pharmacy City"]
                row_dict[f"Pharmacy_{rank}_Distance_meters"] = m["Pharmacy Distance (meters)"]
                row_dict[f"Pharmacy_{rank}_Distance_km"] = m["Pharmacy Distance (km)"]
                row_dict[f"Pharmacy_{rank}_Travel_Time"] = m["Travel Time"]
                row_dict[f"Pharmacy_{rank}_URL"] = m["Google Maps URL"]
            else:
                row_dict[f"Pharmacy_{rank}_Name"] = "N/A"
                row_dict[f"Pharmacy_{rank}_Address"] = "N/A"
                row_dict[f"Pharmacy_{rank}_Address_No_Plus_Code"] = "N/A"
                row_dict[f"Pharmacy_{rank}_Pincode"] = "N/A"
                row_dict[f"Pharmacy_{rank}_City"] = "N/A"
                row_dict[f"Pharmacy_{rank}_Distance_meters"] = "N/A"
                row_dict[f"Pharmacy_{rank}_Distance_km"] = "N/A"
                row_dict[f"Pharmacy_{rank}_Travel_Time"] = "N/A"
                row_dict[f"Pharmacy_{rank}_URL"] = "N/A"
                
        wide_rows.append(row_dict)

    df_wide = pd.DataFrame(wide_rows)

    # Save to Excel with 2 Formatted Sheets
    saved_excel_path = output_excel
    try:
        with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
            df_long.to_excel(writer, sheet_name="Doctor_Chemist_Matches", index=False)
            df_wide.to_excel(writer, sheet_name="Doctor_Summary_Wide", index=False)
        logger.info(f"[✓] Excel Output saved successfully: {os.path.abspath(output_excel)}")
    except PermissionError:
        fallback_excel = output_excel.rsplit(".", 1)[0] + "_cleaned.xlsx"
        logger.warning(f"File {output_excel} is currently locked (likely open in Excel). Saving to {fallback_excel} instead.")
        with pd.ExcelWriter(fallback_excel, engine="openpyxl") as writer:
            df_long.to_excel(writer, sheet_name="Doctor_Chemist_Matches", index=False)
            df_wide.to_excel(writer, sheet_name="Doctor_Summary_Wide", index=False)
        logger.info(f"[✓] Excel Output saved successfully to fallback: {os.path.abspath(fallback_excel)}")
        saved_excel_path = fallback_excel

    # Also export CSV
    csv_output = output_excel.rsplit(".", 1)[0] + "_matches.csv"
    try:
        df_long.to_csv(csv_output, index=False)
        logger.info(f"[✓] CSV Output saved successfully: {os.path.abspath(csv_output)}")
    except Exception as e:
        logger.warning(f"Could not save CSV file: {e}")

    elapsed = time.time() - start_time
    logger.info(f"\n=======================================================")
    logger.info(f"MATCHING COMPLETE IN {elapsed:.2f} seconds!")
    logger.info(f"Total Doctors Processed: {total_docs}")
    logger.info(f"Total Pharmacy Matches Generated: {len(df_long)}")
    logger.info(f"Excel Output: {os.path.abspath(saved_excel_path)}")
    logger.info(f"CSV Output: {os.path.abspath(csv_output)}")
    logger.info(f"=======================================================\n")


# ---------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find closest pharmacies per doctor using Google Maps API")
    parser.add_argument("--input", default="All_doctors.xlsx", help="Path to input Excel file")
    parser.add_argument("--output", default="final_doctor_nearest_5_chemists.xlsx", help="Path to output Excel file")
    parser.add_argument("--key", default="", help="Google Maps API Key (optional, defaults to .env)")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit for testing (e.g. --limit 5)")
    parser.add_argument("--radius", type=int, default=300, help="Initial search radius in meters (default: 300)")
    parser.add_argument("--max-radius", type=int, default=10000, help="Maximum search radius in meters (default: 10000)")
    parser.add_argument("--target-count", type=int, default=5, help="Target number of pharmacies per doctor (default: 5)")
    parser.add_argument("--mode", default="walking", choices=["walking", "driving"], help="Distance mode (default: walking)")
    parser.add_argument("--no-timestamp", action="store_true", help="Disable automatic timestamp suffix on output file names")

    args = parser.parse_args()

    api_key = args.key.strip() or load_api_key()
    process_doctor_file(
        input_file=args.input,
        output_excel=args.output,
        api_key=api_key,
        limit=args.limit,
        base_radius=args.radius,
        max_radius=args.max_radius,
        target_count=args.target_count,
        mode=args.mode,
        use_timestamp=not args.no_timestamp
    )
