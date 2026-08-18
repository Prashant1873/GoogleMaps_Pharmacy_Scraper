import os
import requests

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

def load_api_key() -> str:
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

API_KEY = load_api_key()

def test_geocoding():
    print("Testing Geocoding API...")
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": "Bhiwandi, Mumbai, 421301", "key": API_KEY}
    res = requests.get(url, params=params).json()
    status = res.get("status")
    print(f"Geocoding Status: {status}")
    if status == "OK":
        loc = res["results"][0]["geometry"]["location"]
        print(f"-> Success! Lat: {loc['lat']}, Lng: {loc['lng']}")
        return loc
    else:
        print(f"-> Error/Response: {res}")
        return None

def test_places(loc):
    if not loc:
        return
    print("\nTesting Places API (Nearby Search)...")
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{loc['lat']},{loc['lng']}",
        "radius": 300,
        "type": "pharmacy",
        "key": API_KEY
    }
    res = requests.get(url, params=params).json()
    status = res.get("status")
    print(f"Places API Status: {status}")
    if status in ["OK", "ZERO_RESULTS"]:
        results = res.get("results", [])
        print(f"-> Success! Found {len(results)} pharmacies within 300m.")
        for r in results[:3]:
            print(f"   * {r.get('name')} - {r.get('vicinity')}")
        return results
    else:
        print(f"-> Error/Response: {res}")
        return None

def test_distance_matrix(origin, destinations):
    if not destinations:
        return
    print("\nTesting Distance Matrix API...")
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    dest_str = "|".join([f"{d['geometry']['location']['lat']},{d['geometry']['location']['lng']}" for d in destinations[:3]])
    params = {
        "origins": f"{origin['lat']},{origin['lng']}",
        "destinations": dest_str,
        "mode": "walking",
        "key": API_KEY
    }
    res = requests.get(url, params=params).json()
    status = res.get("status")
    print(f"Distance Matrix API Status: {status}")
    if status == "OK":
        elements = res["rows"][0]["elements"]
        print(f"-> Success! Got {len(elements)} distance elements.")
        for i, el in enumerate(elements):
            if el.get("status") == "OK":
                print(f"   * Destination {i+1}: {el['distance']['text']} ({el['duration']['text']})")
    else:
        print(f"-> Error/Response: {res}")

if __name__ == "__main__":
    loc = test_geocoding()
    if loc:
        places = test_places(loc)
        if places:
            test_distance_matrix(loc, places)
