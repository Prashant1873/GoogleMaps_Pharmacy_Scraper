"""
Enhanced Reverse Doctor-to-Pharmacy Verification Testing Module
Randomly samples entries from the matching result sheet and performs a comprehensive multi-channel
reverse Google Maps verification to verify if the mapped pharmacy is physically near the doctor.

Verification Channels:
1. Google Places Nearby Search (Doctors, Clinics, Hospitals, Health Providers around Pharmacy)
2. Targeted Google Places TextSearch (Doctor Name Variations & Specialty Clinics near Pharmacy)
3. Establishment / Hospital / Healthcare Center Resolution (from Doctor's Address)
4. Physical Geocoding Proximity Verification (Walking Distance between Doctor Address & Pharmacy)
"""

import os
import re
import math
import time
import json
import sqlite3
import argparse
import logging
import glob
from difflib import SequenceMatcher
from typing import Dict, List, Any, Optional, Tuple
from urllib.parse import quote_plus
from datetime import datetime

import pandas as pd
import requests
from anyascii import anyascii

# ---------------------------------------------------------
# Environment & Configuration
# ---------------------------------------------------------
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

def load_api_key() -> str:
    """Loads API key from .env or environment variable."""
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
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join("logs", "verification_test.log"), encoding="utf-8")
    ]
)
logger = logging.getLogger("ReverseVerification")


# ---------------------------------------------------------
# SQLite Cache Manager
# ---------------------------------------------------------
class VerificationCacheManager:
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
                CREATE TABLE IF NOT EXISTS place_details_cache (
                    place_id TEXT PRIMARY KEY,
                    formatted_address TEXT,
                    vicinity TEXT,
                    postal_code TEXT,
                    city TEXT,
                    raw_json TEXT,
                    status TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reverse_doctor_search_cache (
                    cache_key TEXT PRIMARY KEY,
                    raw_json TEXT,
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

    def get_place_details(self, place_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT formatted_address, vicinity, postal_code, city, raw_json, status FROM place_details_cache WHERE place_id = ?", (place_id.strip(),))
            row = cur.fetchone()
            if row:
                return {
                    "formatted_address": row[0],
                    "vicinity": row[1],
                    "postal_code": row[2],
                    "city": row[3],
                    "raw_json": row[4],
                    "status": row[5]
                }
        return None

    def save_place_details(self, place_id: str, data: Dict[str, Any]):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO place_details_cache (place_id, formatted_address, vicinity, postal_code, city, raw_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (place_id.strip(), data.get("formatted_address"), data.get("vicinity"), data.get("postal_code"), data.get("city"), data.get("raw_json"), data.get("status")))
            conn.commit()

    def get_reverse_doctors(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT raw_json FROM reverse_doctor_search_cache WHERE cache_key = ?", (cache_key,))
            row = cur.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except Exception:
                    pass
        return None

    def save_reverse_doctors(self, cache_key: str, results: List[Dict[str, Any]], status: str):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO reverse_doctor_search_cache (cache_key, raw_json, status)
                VALUES (?, ?, ?)
            """, (cache_key, json.dumps(results), status))
            conn.commit()


# ---------------------------------------------------------
# Helper Functions (Geometry & Parsing)
# ---------------------------------------------------------
def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes approximate straight-line distance in meters between two coordinates."""
    R = 6371000  # Radius of earth in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def clean_text(text: str) -> str:
    """Normalizes whitespace and transliterates to standard ASCII."""
    if not text or pd.isna(text):
        return ""
    ascii_text = anyascii(str(text))
    ascii_text = re.sub(r'[\t\r\n\v\f]+', ' ', ascii_text)
    ascii_text = re.sub(r'\s+', ' ', ascii_text).strip()
    return ascii_text


def extract_place_id_from_url(url: str) -> Optional[str]:
    """Extracts query_place_id parameter from Google Maps search URL."""
    if not url or pd.isna(url):
        return None
    match = re.search(r'[?&]query_place_id=([A-Za-z0-9_\-]+)', str(url))
    if match:
        return match.group(1).strip()
    return None


def extract_establishment_from_address(address: str) -> Optional[str]:
    """Extracts hospital, clinic, plaza, or medical center name from doctor's address."""
    if not address or pd.isna(address):
        return None
    clean = clean_text(address)
    match = re.search(r'([A-Za-z0-9\s\.\-\']+(?:hospital|clinic|nursing home|health\s*care|maternity home|polyclinic|dispensary|centre|center|institute|plaza|complex|building|mahal|bhavan|towers?|chambers?))', clean, re.IGNORECASE)
    if match:
        found = match.group(1).strip()
        if len(found) >= 5:
            return found
    first_part = clean.split(',')[0].strip()
    if any(k in first_part.lower() for k in ["hospital", "clinic", "nursing", "centre", "center", "plaza", "complex", "mahal", "bhavan", "towers", "chambers"]):
        return first_part
    return None


MEDICAL_STOPWORDS = {
    "clinic", "clinics", "hospital", "hospitals", "nursing", "home", "polyclinic", "polyclinics",
    "dispensary", "dispensaries", "centre", "center", "centres", "centers", "care", "heart", "hridayam",
    "skin", "eye", "dental", "dento", "diagnostic", "diagnostics", "specialist", "specialists",
    "physician", "physicians", "surgeon", "surgeons", "md", "ms", "mbbs", "bams", "bhms", "dnb",
    "dgo", "dch", "dr", "doctor", "doctors", "prof", "professor", "consultant", "consultants",
    "consulting", "general", "health", "healthcare", "ortho", "orthopaedic", "ent", "paediatric",
    "pediatric", "child", "children", "maternity", "laser", "advanced", "best", "near", "me",
    "trust", "memorial", "super", "multi", "speciality", "diabetes", "diabetic", "chest", "cure",
    "institute", "foundation", "lifeline", "city", "prime", "wellness", "room", "rooms", "opd",
    "medicare", "med", "medical", "medicos", "research"
}


def remove_doctor_titles_and_credentials(name: str) -> str:
    """Removes common doctor titles, qualifications, and credentials for name comparison."""
    if not name:
        return ""
    text = clean_text(name).lower()
    text = re.sub(r'[\(\)\[\]\{\}"\'`]', ' ', text)

    prefixes = [
        r'\bdr\b\.?', r'\bdoctor\b', r'\bprof\b\.?', r'\bprofessor\b',
        r'\bvaidya\b', r'\bhakim\b', r'\bconsultant\b', r'\bphysician\b',
        r'\bsurgeon\b', r'\bspecialist\b'
    ]
    for p in prefixes:
        text = re.sub(p, ' ', text, flags=re.IGNORECASE)

    suffixes = [
        r'\bmbbs\b', r'\bmd\b', r'\bms\b', r'\bdnb\b', r'\bbams\b', r'\bbhms\b',
        r'\bbds\b', r'\bmch\b', r'\bdm\b', r'\bdgo\b', r'\bdch\b', r'\bfrcs\b',
        r'\bmrcp\b', r'\bdip\b', r'\bphd\b', r'\bfacog\b', r'\bfics\b', r'\bgeneral physician\b'
    ]
    for s in suffixes:
        text = re.sub(s, ' ', text, flags=re.IGNORECASE)

    institutions = [
        r'\bclinic\b', r'\bclinics\b', r'\bhospital\b', r'\bhospitals\b',
        r'\bnursing home\b', r'\bpolyclinic\b', r'\bdispensary\b',
        r'\bhealth care\b', r'\bhealthcare\b', r'\bcentre\b', r'\bcenter\b',
        r'\bmedical centre\b', r'\bmedical center\b', r'\bpoly clinic\b',
        r'\bconsulting room\b', r'\bconsulting rooms\b', r'\bopd\b', r'\btrust\b'
    ]
    for inst in institutions:
        text = re.sub(inst, ' ', text, flags=re.IGNORECASE)

    text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_name_tokens(name: str) -> List[str]:
    """Extracts non-trivial name tokens (minimum 2 characters)."""
    cleaned = remove_doctor_titles_and_credentials(name)
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    return tokens


def calculate_name_similarity(
    expected_name: str,
    candidate_name: str,
    establishment: Optional[str] = None
) -> Tuple[float, str]:
    """
    Evaluates multi-strategy similarity between expected doctor name and candidate name.
    """
    exp_cleaned = remove_doctor_titles_and_credentials(expected_name)
    cand_cleaned = remove_doctor_titles_and_credentials(candidate_name)

    if not exp_cleaned or not cand_cleaned:
        return 0.0, "NO_DATA"

    # 1. Exact normalized match
    if exp_cleaned == cand_cleaned:
        return 1.0, "EXACT_NORMALIZED"

    exp_tokens = extract_name_tokens(expected_name)
    cand_tokens = extract_name_tokens(candidate_name)
    exp_set = set(exp_tokens)
    cand_set = set(cand_tokens)

    if not exp_tokens or not cand_tokens:
        return 0.0, "NO_TOKENS"

    # 2. Complete Token Subset Match
    if exp_set.issubset(cand_set):
        return 0.95, "TOKEN_SUBSET_CONTAINED"

    # 3. Permutation match
    if exp_set == cand_set:
        return 0.98, "TOKEN_PERMUTATION"

    common_tokens = exp_set.intersection(cand_set)

    # 4. Multi-token overlap (>= 2 tokens)
    if len(common_tokens) >= 2:
        overlap_score = len(common_tokens) / max(len(exp_set), len(cand_set))
        return min(0.92, 0.75 + (overlap_score * 0.20)), f"TOKEN_OVERLAP_{len(common_tokens)}_WORDS"

    # 5. Fuzzy string ratio (Levenshtein)
    seq_ratio = SequenceMatcher(None, exp_cleaned, cand_cleaned).ratio()
    if seq_ratio >= 0.80:
        return round(seq_ratio, 3), "FUZZY_STRING_SIMILARITY"

    # 6. Single distinctive token match (e.g. "Chavan Clinic" or "Dr. Oak Hospital")
    if len(common_tokens) == 1:
        matched_tok = list(common_tokens)[0]
        unmatched_cand_words = [
            w for w in cand_cleaned.split()
            if w != matched_tok and len(w) >= 3 and w not in MEDICAL_STOPWORDS
        ]
        cand_lower = candidate_name.lower()
        has_dr_title = bool(re.search(r'\b(dr|doctor|prof)\b', cand_lower))

        # Avoid conflicting doctor personal names
        if not (unmatched_cand_words and has_dr_title) and len(matched_tok) >= 3:
            is_facility = any(w in cand_lower for w in ["clinic", "hospital", "dispensary", "polyclinic", "centre", "center", "care", "heart", "dr", "doctor"])
            is_surname = (matched_tok == exp_tokens[-1])
            is_first_name = (matched_tok == exp_tokens[0])

            if is_facility and (is_surname or is_first_name or len(exp_tokens) == 1):
                return 0.85, f"FACILITY_NAME_MATCH_{matched_tok}"

    # 7. Establishment / Hospital name matching from address
    if establishment:
        est_cleaned = remove_doctor_titles_and_credentials(establishment)
        if est_cleaned and len(est_cleaned) >= 4:
            est_tokens = set(extract_name_tokens(establishment))
            if est_tokens and est_tokens.issubset(cand_set):
                return 0.90, f"ESTABLISHMENT_MATCH_{est_cleaned[:15]}"
            est_ratio = SequenceMatcher(None, est_cleaned, cand_cleaned).ratio()
            if est_ratio >= 0.75:
                return round(est_ratio, 3), "ESTABLISHMENT_SIMILARITY"

    return round(seq_ratio, 3), "LOW_SIMILARITY"


# ---------------------------------------------------------
# Google Maps Multi-Channel Reverse Search Engine
# ---------------------------------------------------------
class DoctorReverseSearchEngine:
    def __init__(self, api_key: str, cache: VerificationCacheManager):
        self.api_key = api_key
        self.cache = cache
        self.session = requests.Session()

    def get_place_details_location(self, place_id: str) -> Optional[Dict[str, Any]]:
        """Queries Google Place Details API or cache for exact coordinates."""
        place_id = str(place_id).strip()
        if not place_id:
            return None

        cached = self.cache.get_place_details(place_id)
        if cached and cached.get("raw_json"):
            try:
                p_json = json.loads(cached["raw_json"])
                if "geometry" in p_json and "location" in p_json["geometry"]:
                    loc = p_json["geometry"]["location"]
                    return {
                        "lat": loc["lat"],
                        "lng": loc["lng"],
                        "formatted_address": cached.get("formatted_address") or "",
                        "source": "PLACE_DETAILS_CACHE"
                    }
            except Exception:
                pass

        url = "https://maps.googleapis.com/maps/api/place/details/json"
        params = {
            "place_id": place_id,
            "fields": "name,formatted_address,vicinity,geometry",
            "language": "en",
            "key": self.api_key
        }
        try:
            res = self.session.get(url, params=params, timeout=15).json()
            if res.get("status") == "OK" and res.get("result"):
                result = res["result"]
                loc = result.get("geometry", {}).get("location")
                if loc:
                    details_data = {
                        "formatted_address": result.get("formatted_address", ""),
                        "vicinity": result.get("vicinity", ""),
                        "postal_code": "",
                        "city": "",
                        "raw_json": json.dumps(result),
                        "status": "OK"
                    }
                    self.cache.save_place_details(place_id, details_data)
                    return {
                        "lat": loc["lat"],
                        "lng": loc["lng"],
                        "formatted_address": result.get("formatted_address", ""),
                        "source": "LIVE_PLACE_DETAILS_API"
                    }
        except Exception as e:
            logger.error(f"Place Details API error for place_id={place_id}: {e}")

        return None

    def geocode_pharmacy(
        self,
        pharm_name: str,
        pharm_address: str,
        pharm_city: str = "",
        pharm_pincode: str = "",
        place_id: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Resolves accurate lat/lng coordinates for a pharmacy."""
        # 1. Place ID
        if place_id:
            loc = self.get_place_details_location(place_id)
            if loc:
                return loc

        # 2. Geocoding with full query
        query_parts = [clean_text(p) for p in [pharm_name, pharm_address, pharm_city, pharm_pincode, "India"] if p and clean_text(p)]
        query = ", ".join(query_parts)

        cached_geo = self.cache.get_geocode(query)
        if cached_geo and cached_geo.get("lat") is not None:
            return {
                "lat": cached_geo["lat"],
                "lng": cached_geo["lng"],
                "formatted_address": cached_geo.get("formatted_address", ""),
                "source": "GEOCODE_CACHE"
            }

        url = "https://maps.googleapis.com/maps/api/geocode/json"
        try:
            res = self.session.get(url, params={"address": query, "language": "en", "key": self.api_key}, timeout=15).json()
            if res.get("status") == "OK" and res.get("results"):
                top_res = res["results"][0]
                loc = top_res["geometry"]["location"]
                geo_data = {
                    "lat": loc["lat"],
                    "lng": loc["lng"],
                    "formatted_address": top_res.get("formatted_address", ""),
                    "status": "OK"
                }
                self.cache.save_geocode(query, geo_data)
                return {
                    "lat": loc["lat"],
                    "lng": loc["lng"],
                    "formatted_address": top_res.get("formatted_address", ""),
                    "source": "LIVE_GEOCODE_API"
                }
        except Exception as e:
            logger.error(f"Geocoding error for pharmacy '{query}': {e}")

        # Fallback: Address + City
        query_fallback = ", ".join([clean_text(p) for p in [pharm_address, pharm_city, "India"] if p and clean_text(p)])
        if query_fallback and query_fallback != query:
            cached_fallback = self.cache.get_geocode(query_fallback)
            if cached_fallback and cached_fallback.get("lat") is not None:
                return {
                    "lat": cached_fallback["lat"],
                    "lng": cached_fallback["lng"],
                    "formatted_address": cached_fallback.get("formatted_address", ""),
                    "source": "GEOCODE_CACHE_FALLBACK"
                }

        return None

    def geocode_doctor_address(
        self,
        doc_name: str,
        doc_address: str,
        doc_city: str = "",
        doc_pincode: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Resolves doctor's clinic coordinates from doctor address."""
        candidates = [
            ", ".join([clean_text(p) for p in [doc_name, doc_address, doc_city, doc_pincode, "India"] if p and clean_text(p)]),
            ", ".join([clean_text(p) for p in [doc_address, doc_city, doc_pincode, "India"] if p and clean_text(p)]),
            ", ".join([clean_text(p) for p in [doc_address, "India"] if p and clean_text(p)])
        ]

        for q in candidates:
            cached = self.cache.get_geocode(q)
            if cached and cached.get("lat") is not None:
                return cached

            url = "https://maps.googleapis.com/maps/api/geocode/json"
            try:
                res = self.session.get(url, params={"address": q, "language": "en", "key": self.api_key}, timeout=15).json()
                if res.get("status") == "OK" and res.get("results"):
                    top_res = res["results"][0]
                    loc = top_res["geometry"]["location"]
                    geo_data = {
                        "lat": loc["lat"],
                        "lng": loc["lng"],
                        "formatted_address": top_res.get("formatted_address", ""),
                        "status": "OK"
                    }
                    self.cache.save_geocode(q, geo_data)
                    return geo_data
            except Exception as e:
                logger.error(f"Doctor address geocoding error for '{q}': {e}")

        return None

    def search_nearby_doctors(
        self,
        lat: float,
        lng: float,
        radius: int = 600
    ) -> List[Dict[str, Any]]:
        """Nearby search for doctors, clinics, and health providers around coordinates."""
        cache_key = f"rev_doc_{lat:.6f}_{lng:.6f}_{radius}"
        cached_results = self.cache.get_reverse_doctors(cache_key)
        if cached_results is not None:
            return cached_results

        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        doctor_candidates = []
        seen_place_ids = set()

        search_configs = [
            {"type": "doctor", "keyword": None},
            {"type": "health", "keyword": "doctor"},
            {"type": None, "keyword": "clinic doctor hospital"}
        ]

        for cfg in search_configs:
            params = {
                "location": f"{lat},{lng}",
                "radius": radius,
                "language": "en",
                "key": self.api_key
            }
            if cfg["type"]:
                params["type"] = cfg["type"]
            if cfg["keyword"]:
                params["keyword"] = cfg["keyword"]

            try:
                res = self.session.get(url, params=params, timeout=15).json()
                status = res.get("status")
                if status in ["OK", "ZERO_RESULTS"]:
                    for item in res.get("results", []):
                        pid = item.get("place_id") or item.get("name")
                        if pid and pid not in seen_place_ids:
                            seen_place_ids.add(pid)
                            loc = item.get("geometry", {}).get("location", {})
                            doctor_candidates.append({
                                "name": clean_text(item.get("name", "")),
                                "vicinity": clean_text(item.get("vicinity") or item.get("formatted_address") or ""),
                                "place_id": item.get("place_id", ""),
                                "types": item.get("types", []),
                                "lat": loc.get("lat"),
                                "lng": loc.get("lng"),
                                "rating": item.get("rating"),
                                "user_ratings_total": item.get("user_ratings_total")
                            })
            except Exception as e:
                logger.error(f"Reverse doctor search API error: {e}")

        self.cache.save_reverse_doctors(cache_key, doctor_candidates, "OK")
        return doctor_candidates

    def text_search_places(
        self,
        query: str,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        radius: int = 1000
    ) -> List[Dict[str, Any]]:
        """Text search for places/doctors centered at specific coordinates."""
        q_clean = clean_text(query)
        if not q_clean or q_clean.lower() in ["nan", "none"]:
            return []

        coord_key = f"_{lat:.4f}_{lng:.4f}_{radius}" if (lat and lng) else ""
        cache_key = f"textsearch_{q_clean}{coord_key}"
        cached = self.cache.get_reverse_doctors(cache_key)
        if cached is not None:
            return cached

        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params = {
            "query": q_clean,
            "language": "en",
            "key": self.api_key
        }
        if lat is not None and lng is not None:
            params["location"] = f"{lat},{lng}"
            params["radius"] = radius

        results = []
        try:
            res = self.session.get(url, params=params, timeout=15).json()
            if res.get("status") in ["OK", "ZERO_RESULTS"]:
                for item in res.get("results", []):
                    loc = item.get("geometry", {}).get("location", {})
                    results.append({
                        "name": clean_text(item.get("name", "")),
                        "formatted_address": clean_text(item.get("formatted_address", "")),
                        "place_id": item.get("place_id", ""),
                        "lat": loc.get("lat"),
                        "lng": loc.get("lng"),
                        "types": item.get("types", [])
                    })
        except Exception as e:
            logger.error(f"TextSearch API error for '{q_clean}': {e}")

        self.cache.save_reverse_doctors(cache_key, results, "OK")
        return results


# ---------------------------------------------------------
# Test Runner & Verifier
# ---------------------------------------------------------
def find_latest_result_file() -> Optional[str]:
    """Finds the most recent result Excel file in output/ or current workspace."""
    patterns = [
        os.path.join("output", "final_doctor_nearest_5_chemists_*.xlsx"),
        os.path.join("output", "final_doctor_nearest_*.xlsx"),
        os.path.join("output", "final_doctor_nearest_5_chemists.xlsx"),
        "final_doctor_nearest_5_chemists_*.xlsx",
        "final_doctor_nearest_*.xlsx",
        "final_doctor_nearest_5_chemists.xlsx"
    ]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))

    if not files:
        return None
    files = [f for f in files if "test_verification" not in f and "checkpoint" not in f]
    if not files:
        return None
    files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return files[0]


def run_reverse_verification_test(
    result_file: Optional[str] = None,
    sample_size: int = 20,
    seed: Optional[int] = 42,
    api_key: Optional[str] = None,
    match_threshold: float = 0.75,
    search_radius: int = 600,
    output_excel: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes multi-channel reverse verification test.
    """
    key = api_key or load_api_key()
    cache = VerificationCacheManager()
    engine = DoctorReverseSearchEngine(api_key=key, cache=cache)

    if not result_file or not os.path.exists(result_file):
        latest = find_latest_result_file()
        if not latest:
            raise FileNotFoundError("No result file specified and no 'final_doctor_nearest_*.xlsx' found.")
        result_file = latest

    logger.info("=================================================================")
    logger.info("   ENHANCED MULTI-CHANNEL DOCTOR-PHARMACY VERIFICATION TEST")
    logger.info(f"   Result File:     {os.path.abspath(result_file)}")
    logger.info(f"   Target Samples:  {sample_size}")
    logger.info(f"   Random Seed:     {seed if seed is not None else 'Random'}")
    logger.info(f"   Match Threshold: {match_threshold:.2f}")
    logger.info("=================================================================\n")

    # Read Result Sheet
    df_raw = None
    try:
        xl = pd.ExcelFile(result_file)
        sheet_to_use = xl.sheet_names[0]
        for s in xl.sheet_names:
            if "matches" in s.lower() or "chemist" in s.lower() or "pharmacies" in s.lower():
                sheet_to_use = s
                break
        df_raw = pd.read_excel(xl, sheet_name=sheet_to_use)
    except Exception:
        csv_fallback = result_file.rsplit(".", 1)[0] + "_matches.csv"
        df_raw = pd.read_csv(csv_fallback)

    # Standardize Column Names
    col_map = {}
    for col in df_raw.columns:
        c_l = col.strip().lower()
        if "doc" in c_l and "id" in c_l:
            col_map["doc_id"] = col
        elif "doctor" in c_l and "name" in c_l:
            col_map["doc_name"] = col
        elif "doctor" in c_l and "address" in c_l:
            col_map["doc_address"] = col
        elif "doctor" in c_l and "city" in c_l:
            col_map["doc_city"] = col
        elif "doctor" in c_l and "pin" in c_l:
            col_map["doc_pincode"] = col
        elif "iqvia" in c_l and "id" in c_l:
            col_map["iqvia_id"] = col
        elif "pharmacy" in c_l and "name" in c_l:
            col_map["pharm_name"] = col
        elif "pharmacy" in c_l and "address" in c_l and "no plus" not in c_l:
            col_map["pharm_address"] = col
        elif "pharmacy" in c_l and "city" in c_l:
            col_map["pharm_city"] = col
        elif "pharmacy" in c_l and "pincode" in c_l:
            col_map["pharm_pincode"] = col
        elif "distance" in c_l and "meter" in c_l:
            col_map["distance_meters"] = col
        elif "place" in c_l and "id" in c_l:
            col_map["place_id"] = col
        elif "url" in c_l or "maps" in c_l:
            col_map["url"] = col

    p_col = col_map.get("pharm_name", "Pharmacy Name")
    df_valid = df_raw[df_raw[p_col].notna() & (df_raw[p_col] != "N/A") & (df_raw[p_col] != "")].copy()
    actual_sample_size = min(sample_size, len(df_valid))
    df_sample = df_valid.sample(n=actual_sample_size, random_state=seed).copy().reset_index(drop=True)

    detailed_results = []
    success_count = 0
    failure_count = 0
    start_time = time.time()

    for idx, row in df_sample.iterrows():
        sample_num = idx + 1
        doc_id = str(row.get(col_map.get("doc_id", "Doc ID"), f"DOC_{sample_num}"))
        doc_name = clean_text(str(row.get(col_map.get("doc_name", "Doctor Name"), "")))
        doc_address = clean_text(str(row.get(col_map.get("doc_address", "Doctor Address"), "")))
        doc_city = clean_text(str(row.get(col_map.get("doc_city", "Doctor City"), "")))
        doc_pin = str(row.get(col_map.get("doc_pincode", "Doctor Pincode"), "")).replace(".0", "")

        iqvia_id = str(row.get(col_map.get("iqvia_id", "IQVIA ID"), "UNMATCHED"))
        pharm_name = clean_text(str(row.get(col_map.get("pharm_name", "Pharmacy Name"), "")))
        pharm_address = clean_text(str(row.get(col_map.get("pharm_address", "Pharmacy Address"), "")))
        pharm_city = clean_text(str(row.get(col_map.get("pharm_city", "Pharmacy City"), doc_city)))
        pharm_pin = str(row.get(col_map.get("pharm_pincode", "Pharmacy Pincode"), "")).replace(".0", "")
        recorded_dist = row.get(col_map.get("distance_meters", "Pharmacy Distance (meters)"), 300)

        place_id = str(row.get(col_map.get("place_id", "Place ID"), "")).strip()
        if not place_id or place_id.lower() == "nan":
            place_id = extract_place_id_from_url(row.get(col_map.get("url", "Google Maps URL"), "")) or ""

        establishment = extract_establishment_from_address(doc_address)

        try:
            dist_val = float(recorded_dist)
            dynamic_radius = int(max(search_radius, dist_val + 250))
        except Exception:
            dynamic_radius = search_radius

        logger.info(f"[{sample_num}/{actual_sample_size}] Checking Doctor '{doc_name}' ({doc_id}) <-> Pharmacy '{pharm_name}' [IQVIA: {iqvia_id}]...")

        # 1. Geocode Pharmacy Coordinates
        pharm_geo = engine.geocode_pharmacy(pharm_name, pharm_address, pharm_city, pharm_pin, place_id)
        if not pharm_geo or pharm_geo.get("lat") is None:
            failure_count += 1
            detailed_results.append({
                "Sample_#": sample_num,
                "Doc_ID": doc_id,
                "Expected_Doctor_Name": doc_name,
                "Expected_Doctor_Address": doc_address,
                "IQVIA_ID": iqvia_id,
                "Pharmacy_Name": pharm_name,
                "Pharmacy_Address": pharm_address,
                "Pharmacy_City": pharm_city,
                "Verification_Status": "FAIL",
                "Verified_Channel": "NONE",
                "Discovered_Entity": None,
                "Distance_From_Pharmacy_Meters": None,
                "Confidence_Score": 0.0,
                "Notes": "Could not geocode pharmacy location on Google Maps."
            })
            continue

        p_lat, p_lng = pharm_geo["lat"], pharm_geo["lng"]

        best_match = None
        best_score = 0.0
        best_type = "NO_MATCH"
        best_dist = None
        channel_used = "NONE"

        # Channel 1: Google Places Nearby Search
        nearby_doctors = engine.search_nearby_doctors(p_lat, p_lng, radius=dynamic_radius)
        for cand in nearby_doctors:
            score, m_type = calculate_name_similarity(doc_name, cand["name"], establishment=establishment)
            if cand.get("lat") is not None:
                d_m = int(haversine_distance(p_lat, p_lng, cand["lat"], cand["lng"]))
            else:
                d_m = None
            if score > best_score:
                best_score = score
                best_match = cand["name"]
                best_type = m_type
                best_dist = d_m
                channel_used = "PLACES_NEARBY_SEARCH"

        # Channel 2: Targeted TextSearch for Doctor Name & Specialty near Pharmacy
        if best_score < match_threshold:
            text_candidates = engine.text_search_places(f"Dr. {doc_name} {doc_city}", lat=p_lat, lng=p_lng, radius=1200)
            for cand in text_candidates:
                score, m_type = calculate_name_similarity(doc_name, cand["name"], establishment=establishment)
                if cand.get("lat") is not None:
                    d_m = int(haversine_distance(p_lat, p_lng, cand["lat"], cand["lng"]))
                else:
                    d_m = 9999
                if d_m <= max(dynamic_radius, 900) and score > best_score:
                    best_score = score
                    best_match = cand["name"]
                    best_type = m_type
                    best_dist = d_m
                    channel_used = "TARGETED_DOCTOR_TEXTSEARCH"

        # Channel 3: Hospital / Clinic Establishment from Doctor's Address
        if best_score < match_threshold and establishment:
            est_candidates = engine.text_search_places(f"{establishment} {doc_city}", lat=p_lat, lng=p_lng, radius=1200)
            for cand in est_candidates:
                score, m_type = calculate_name_similarity(doc_name, cand["name"], establishment=establishment)
                if cand.get("lat") is not None:
                    d_m = int(haversine_distance(p_lat, p_lng, cand["lat"], cand["lng"]))
                else:
                    d_m = 9999
                if d_m <= max(dynamic_radius, 900) and score > best_score:
                    best_score = score
                    best_match = cand["name"]
                    best_type = m_type
                    best_dist = d_m
                    channel_used = "HOSPITAL_ESTABLISHMENT_SEARCH"

        # Channel 4: Physical Address Geocoding Proximity Confirmation
        if best_score < match_threshold:
            doc_geo = engine.geocode_doctor_address(doc_name, doc_address, doc_city, doc_pin)
            if doc_geo and doc_geo.get("lat") is not None:
                phys_dist = int(haversine_distance(p_lat, p_lng, doc_geo["lat"], doc_geo["lng"]))
                # If doctor's physical clinic address is within walking proximity of pharmacy
                if phys_dist <= max(dynamic_radius, 500):
                    best_score = 0.88
                    best_match = doc_geo.get("formatted_address", doc_address)
                    best_type = f"DOCTOR_CLINIC_ADDRESS_AT_{phys_dist}m"
                    best_dist = phys_dist
                    channel_used = "PHYSICAL_ADDRESS_PROXIMITY"

        is_success = best_score >= match_threshold
        if is_success:
            success_count += 1
            logger.info(f"   [✓ SUCCESS] Verified via {channel_used}! Entity: '{best_match}' (Dist: {best_dist}m, Score: {best_score:.2f})")
            detailed_results.append({
                "Sample_#": sample_num,
                "Doc_ID": doc_id,
                "Expected_Doctor_Name": doc_name,
                "Expected_Doctor_Address": doc_address,
                "IQVIA_ID": iqvia_id,
                "Pharmacy_Name": pharm_name,
                "Pharmacy_Address": pharm_address,
                "Pharmacy_City": pharm_city,
                "Verification_Status": "SUCCESS",
                "Verified_Channel": channel_used,
                "Discovered_Entity": best_match,
                "Distance_From_Pharmacy_Meters": best_dist,
                "Confidence_Score": round(best_score, 2),
                "Notes": f"Confirmed proximity. Match: '{best_match}' ({best_type})"
            })
        else:
            failure_count += 1
            logger.info(f"   [x UNVERIFIED] Best candidate: '{best_match}' (Dist: {best_dist}m, Score: {best_score:.2f})")
            detailed_results.append({
                "Sample_#": sample_num,
                "Doc_ID": doc_id,
                "Expected_Doctor_Name": doc_name,
                "Expected_Doctor_Address": doc_address,
                "IQVIA_ID": iqvia_id,
                "Pharmacy_Name": pharm_name,
                "Pharmacy_Address": pharm_address,
                "Pharmacy_City": pharm_city,
                "Verification_Status": "FAIL",
                "Verified_Channel": channel_used,
                "Discovered_Entity": best_match,
                "Distance_From_Pharmacy_Meters": best_dist,
                "Confidence_Score": round(best_score, 2),
                "Notes": f"Could not verify doctor within {dynamic_radius}m radius."
            })

    elapsed_time = round(time.time() - start_time, 2)
    success_percentage = round((success_count / actual_sample_size) * 100.0, 2)

    summary_data = {
        "Test_Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Result_Source_File": os.path.abspath(result_file),
        "Total_Result_Rows": len(df_valid),
        "Sample_Size_Tested": actual_sample_size,
        "Successful_Verifications": success_count,
        "Failed_Verifications": failure_count,
        "Success_Percentage": success_percentage,
        "Execution_Time_Seconds": elapsed_time
    }

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not output_excel:
        output_excel = os.path.join("output", f"test_verification_report_{timestamp_str}.xlsx")

    out_dir = os.path.dirname(os.path.abspath(output_excel))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    df_details = pd.DataFrame(detailed_results)
    df_summary = pd.DataFrame([summary_data])

    try:
        with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
            df_summary.to_excel(writer, sheet_name="Verification_Summary", index=False)
            df_details.to_excel(writer, sheet_name="Sample_Audit_Log", index=False)
        logger.info(f"[✓] Excel Report saved: {os.path.abspath(output_excel)}")
    except Exception as e:
        logger.warning(f"Could not save Excel report: {e}")

    csv_output = output_excel.rsplit(".", 1)[0] + ".csv"
    try:
        df_details.to_csv(csv_output, index=False)
        logger.info(f"[✓] CSV Report saved: {os.path.abspath(csv_output)}")
    except Exception as e:
        logger.warning(f"Could not save CSV report: {e}")

    terminal_report = f"""
================================================================================
          ENHANCED DOCTOR-TO-PHARMACY REVERSE VERIFICATION SUMMARY
================================================================================
  Date & Time:              {summary_data['Test_Timestamp']}
  Result Sheet Source:      {os.path.basename(result_file)}
  Sample Size Tested:       {actual_sample_size}
  Execution Time:           {elapsed_time}s
--------------------------------------------------------------------------------
  SUCCESSFUL VERIFICATIONS: {success_count} / {actual_sample_size}
  FAILED VERIFICATIONS:     {failure_count} / {actual_sample_size}
--------------------------------------------------------------------------------
  >>> VERIFICATION SUCCESS RATE: {success_percentage:.2f}% <<<
--------------------------------------------------------------------------------
  Audit Excel Report:       {os.path.abspath(output_excel)}
  Audit CSV Log:            {os.path.abspath(csv_output)}
================================================================================
"""
    print(terminal_report)

    return {
        "summary": summary_data,
        "details_df": df_details,
        "excel_path": output_excel,
        "csv_path": csv_output,
        "success_percentage": success_percentage
    }


# ---------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enhanced Reverse Doctor-Pharmacy Verification Testing")
    parser.add_argument("--results", default=None, help="Path to result Excel/CSV file (defaults to latest)")
    parser.add_argument("--sample-size", type=int, default=20, help="Number of random entries to test (default: 20)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible sampling (default: 42)")
    parser.add_argument("--radius", type=int, default=600, help="Base search radius around pharmacy in meters (default: 600)")
    parser.add_argument("--threshold", type=float, default=0.75, help="Match confidence threshold (default: 0.75)")
    parser.add_argument("--output", default=None, help="Custom output path for report")
    parser.add_argument("--key", default="", help="Google Maps API Key (optional)")

    args = parser.parse_args()

    api_key = args.key.strip() or load_api_key()
    run_reverse_verification_test(
        result_file=args.results,
        sample_size=args.sample_size,
        seed=args.seed,
        api_key=api_key,
        match_threshold=args.threshold,
        search_radius=args.radius,
        output_excel=args.output
    )
