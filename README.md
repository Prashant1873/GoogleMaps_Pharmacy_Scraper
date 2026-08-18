# Doctor-to-Pharmacy Proximity Finder & Matching Pipeline

A production-grade Python pipeline designed to match Healthcare Professionals (Doctors/Clinics/Hospitals) with their **Top 5 Nearest Retail Pharmacies/Chemists** using official Google Maps Platform APIs (Geocoding, Places, and Distance Matrix).

Built with persistent caching, adaptive radius expansion, walking route calculations, checkpoint recovery, and automated cost telemetry.

---

## 🌟 Key Features

- **Accurate Geocoding**: Intelligently cleans and normalizes doctor clinic/hospital addresses, city, and pincodes to resolve precise geographic coordinates (Latitude / Longitude).
- **Adaptive Walking Proximity Search**: Uses Google Places API Nearby Search starting at a localized 300m walking radius, dynamically expanding (up to 3,000m) to guarantee up to 5 valid pharmacy candidates per doctor.
- **True Walking Distance & Duration**: Calculates realistic pedestrian walking route distances (meters) and transit times (minutes) using the Google Distance Matrix API (`mode=walking`).
- **Zero-Redundancy SQLite Cache**: Caches all API responses (Geocodes, Places searches, and Distance Matrix elements) in a local SQLite database (`cache.db`). Re-runs execute in seconds with 0 additional API calls.
- **Cost-Optimized & Free-Tier Friendly**: Drastically cuts API billing overhead and generates an instant cost-estimation summary report.
- **Fail-Safe & Resumable**: Checkpoints intermediate geocoding records (`checkpoints/intermediate_geocoded_doctors.xlsx`) to avoid loss of progress during network disruptions.
- **Structured Multi-Format Exports**: Outputs cleanly formatted Excel files (`.xlsx`) and CSV files (`.csv`) along with detailed execution telemetry (`summary_report.txt`).

---

## 📁 Repository Structure

```text
├── run_pipeline.py                              # Main end-to-end matching pipeline
├── find_chemists.py                             # Core proximity & pharmacy search module
├── test_api_key.py                              # Verification script for Google Maps APIs
├── summary_report.txt                           # Sample telemetry and execution report
├── requirements.txt                             # Python dependencies
├── .env.example                                 # Environment variable template
├── .gitignore                                   # Git exclusion rules
├── All_doctors.xlsx                             # Input dataset template
├── final_doctor_nearest_5_chemists.xlsx         # Final matched Excel report
└── final_doctor_nearest_5_chemists_matches.csv  # Final matched CSV report
```

---

## 🚀 Getting Started

### 1. Prerequisites

- Python 3.9+
- A Google Cloud Project with the following APIs enabled:
  - **Geocoding API**
  - **Places API**
  - **Distance Matrix API**

### 2. Installation

Clone this repository and install required packages:

```bash
git clone https://github.com/Prashant1873/GoogleMaps_Pharmacy_Scraper.git
cd GoogleMaps_Pharmacy_Scraper

# (Optional) Create and activate a virtual environment
python -m venv venv
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration

Copy the example configuration file:

```bash
cp .env.example .env
```

Open `.env` and insert your Google Maps API key:

```env
GOOGLE_MAPS_API_KEY=your_actual_google_maps_api_key_here
```

### 4. Verify API Credentials

Run the test suite to verify that your API key is valid and all 3 required APIs are active:

```bash
python test_api_key.py
```

---

## 💻 Usage

### Running the Full Matching Pipeline

To process doctor records from an Excel file:

```bash
python run_pipeline.py --input All_doctors.xlsx --output final_doctor_nearest_5_chemists.xlsx
```

#### Available CLI Arguments:

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--input` | Path to the source Excel file containing doctor addresses | `All_doctors.xlsx` |
| `--output` | Output filename for the final matched Excel report | `final_doctor_nearest_5_chemists.xlsx` |
| `--radius` | Initial search radius in meters (walking distance) | `300` |
| `--limit` | Target number of nearest pharmacies to find per doctor | `5` |
| `--no-cache` | Disable SQLite caching and force fresh API queries | `False` |

---

## 📊 Pipeline Telemetry & Summary Report

Every pipeline run automatically records detailed performance metrics into `summary_report.txt` and `pipeline_execution.log`, including:

- **Doctor Processing Statistics**: Total records processed and match completion rates.
- **Cache Hit vs. Fresh API Call Breakdown**: Quantified stats for Geocoding, Places, and Distance Matrix calls.
- **Estimated API Costs**: Real-time gross billable amount vs. Google Cloud Monthly Free Tier allowance.

---

## 🔒 Security & Best Practices

- Never commit your `.env` file containing actual Google Cloud API keys.
- Keep the local SQLite database (`cache.db`) protected to leverage cached queries across runs.
