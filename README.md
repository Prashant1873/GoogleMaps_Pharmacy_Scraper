# Doctor-to-Pharmacy Proximity Finder & Matching Pipeline

A production-grade Python pipeline designed to match Healthcare Professionals (Doctors/Clinics/Hospitals) with their **Top 5 Nearest Retail Pharmacies/Chemists** using official Google Maps Platform APIs (Geocoding, Places, and Distance Matrix) and map them to the **IQVIA Chemist Master (`Chemist_HCM.xlsx`)** to maintain official **`IQVIA ID`**s.

Built with persistent caching, adaptive radius expansion, walking route calculations, checkpoint recovery, sub-millisecond SQLite master lookups, and automated cost telemetry.

---

## 🌟 Key Features

- **IQVIA Chemist Master Resolution**: Cross-references Google Places pharmacies against 145,557+ master retail chemist records (`Chemist_HCM.xlsx`) using sub-millisecond SQLite indexing to assign and maintain official `IQVIA ID`s, master chemist names, and addresses.
- **Accurate Geocoding**: Intelligently cleans and normalizes doctor clinic/hospital addresses, city, and pincodes to resolve precise geographic coordinates (Latitude / Longitude).
- **Multi-Tier Adaptive Proximity Search**: Uses Google Places API Nearby Search starting at a localized 300m walking radius, dynamically expanding in tiers (`300m -> 800m -> 1,500m -> 2,500m -> 3,500m -> 5,000m -> 7,500m -> 10,000m`) to guarantee up to 5 valid pharmacy candidates per doctor.
- **True Walking Distance & Duration**: Calculates realistic pedestrian walking route distances (meters) and transit times (minutes) using the Google Distance Matrix API (`mode=walking`).
- **Zero-Redundancy SQLite Cache**: Caches all API responses (Geocodes, Places searches, and Distance Matrix elements) and indexed IQVIA chemist records in a local SQLite database (`cache.db`). Re-runs execute in seconds with 0 additional API calls.
- **Cost-Optimized & Free-Tier Friendly**: Drastically cuts API billing overhead and generates an instant cost-estimation summary report.
- **Fail-Safe & Resumable**: Checkpoints intermediate geocoding records (`checkpoints/intermediate_geocoded_doctors.xlsx`) to avoid loss of progress during network disruptions.
- **Structured Multi-Format Exports**: Outputs dual-sheet formatted Excel files (`.xlsx`: `Doctor_Chemist_Matches` + `Doctor_Summary_Wide`) and CSV files (`.csv`) along with detailed execution telemetry (`summary_report.txt`).

---

## 📁 Repository Structure

```text
GOOGLEMAPS/
├── Chemist_HCM.xlsx                             # Master chemist dataset (145,557 records with IQVIA IDs)
├── data/                                        # Input datasets & templates
│   └── All_doctors.xlsx                         # Master doctor dataset / template
├── output/                                      # Generated reports & exports (git-ignored)
│   ├── final_doctor_nearest_5_chemists_*.xlsx   # Final matched Excel report (Long & Wide sheets)
│   ├── final_doctor_nearest_5_chemists_*.csv    # Final matched CSV report
│   ├── summary_report_*.txt                     # Telemetry, IQVIA resolution & cost estimation report
│   └── test_verification_report_*.xlsx/.csv     # Reverse verification audit logs
├── logs/                                        # Execution log files (git-ignored)
│   ├── pipeline_execution.log
│   ├── chemist_finder.log
│   └── verification_test.log
├── checkpoints/                                 # Intermediate geocoding checkpoints (git-ignored)
│   └── intermediate_geocoded_doctors_*.xlsx
├── run_pipeline.py                              # Main end-to-end matching pipeline
├── find_chemists.py                             # Core proximity & pharmacy search module
├── verify_reverse_matching.py                   # Reverse verification & audit script
├── test_api_key.py                              # Diagnostic & API key verification script
├── requirements.txt                             # Python dependencies
├── .env.example                                 # Environment variable template
├── .gitignore                                   # Git exclusion rules
└── README.md                                    # Project documentation
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

To process doctor records from the master Excel file:

```bash
# Runs pipeline on data/All_doctors.xlsx, resolves against Chemist_HCM.xlsx, and exports to output/
python run_pipeline.py
```

Or specify custom inputs, outputs, and chemist master:

```bash
python run_pipeline.py --input data/All_doctors.xlsx --chemist-master Chemist_HCM.xlsx --output output/final_doctor_nearest_5_chemists.xlsx
```

#### Available CLI Arguments:

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--input` | Path to the source Excel file containing doctor addresses | `data/All_doctors.xlsx` |
| `--chemist-master` | Path to master chemist Excel file containing IQVIA IDs | `Chemist_HCM.xlsx` |
| `--output` | Output filename for the final matched Excel report | `output/final_doctor_nearest_5_chemists.xlsx` |
| `--summary` | Output filename for telemetry summary report | `output/summary_report.txt` |
| `--radius` | Initial search radius in meters (walking distance) | `300` |
| `--max-radius` | Maximum search radius in meters to expand until target chemists found | `10000` |
| `--target-count`| Target number of nearest pharmacies to find per doctor | `5` |
| `--limit` | Optional limit on number of doctors to process | `None` (all) |
| `--verify` | Run reverse verification test on random sample after matching | `False` |
| `--verify-samples` | Number of samples for verification test | `200` |

---

### Running the Reverse Doctor-Pharmacy Verification Test

To independently test mapping accuracy, the verification module randomly samples entries from your latest result sheet in `output/`, locates each pharmacy on Google Maps, performs a reverse search for nearby healthcare providers, and verifies the mapped doctor's presence and IQVIA ID:

```bash
# Auto-detects the latest result in output/ and tests 200 random entries
python verify_reverse_matching.py --sample-size 200
```

---

## 📊 Pipeline Telemetry & Summary Report

Every pipeline run automatically records detailed performance metrics into `output/summary_report_*.txt` and `logs/pipeline_execution.log`, including:

- **Doctor Processing Statistics**: Total records processed and match completion rates.
- **IQVIA Chemist Master Breakdown**: Total pharmacies matched to official IQVIA IDs, exact match %, high confidence %, and newly discovered pharmacies.
- **Cache Hit vs. Fresh API Call Breakdown**: Quantified stats for Geocoding, Places, and Distance Matrix calls.
- **Estimated API Costs**: Real-time gross billable amount vs. Google Cloud Monthly Free Tier allowance.
- **Verification Audit Reports**: Detailed sample-by-sample verification logs (`output/test_verification_report_*.xlsx` and `.csv`) detailing match confidence scores, IQVIA IDs, match types, and success percentage.

---

## 🔒 Security & Best Practices

- Never commit your `.env` file containing actual Google Cloud API keys.
- Keep the local SQLite database (`cache.db`) protected to leverage cached queries across runs.



