import json
import os
import time
from pathlib import Path
from collections import defaultdict
import pycountry
import requests
import unicodedata
import re
import csv
from datetime import datetime
from typing import Optional, List
from plotly.offline.offline import get_plotlyjs
def _norm_name(s: str) -> str:
	"""
	Normaliza nombres para matching laxo:
	- uppercase
	- sin tildes
	- solo letras/numeros/espacios
	- colapsa espacios
	- AND como separador estable
	"""
	s = (s or "").strip().upper()
	s = s.replace("&", " AND ")
	s = unicodedata.normalize("NFKD", s)
	s = "".join(ch for ch in s if not unicodedata.combining(ch))
	s = re.sub(r"[^A-Z0-9 ]+", " ", s)
	s = re.sub(r"\s+", " ", s).strip()
	return s


def detect_numista_csv_delimiter(csv_path) -> str:
	"""Detect comma/semicolon/tab delimiters by checking parsed Numista headers.

	The previous raw character-count heuristic can fail for semicolon CSVs when
	text fields contain many commas. That makes the wishlist load as zero rows
	without crashing. This detector actually parses the header with each candidate
	delimiter and chooses the one that exposes the expected Numista columns.
	"""
	p = Path(csv_path)
	sample = p.read_text(encoding="utf-8-sig", errors="ignore")[:8192]
	expected = {
		"N# number (with link)",
		"Número N# (con enlace)",
		"Numero N# (con enlace)",
		"Quantity",
		"Cantidad",
		"Title",
		"Título",
		"Titulo",
	}
	best_delim = ","
	best_score = (-1, -1)
	for delim in (",", ";", "\t"):
		try:
			reader = csv.DictReader(sample.splitlines(), delimiter=delim)
			fields = [(f or "").lstrip("\ufeff").strip() for f in (reader.fieldnames or [])]
		except Exception:
			fields = []
		matched = sum(1 for f in fields if f in expected)
		score = (matched, len(fields))
		if score > best_score:
			best_score = score
			best_delim = delim
	return best_delim
def load_collection_from_numista_csv(csv_path: str) -> dict:
	"""
	Load Numista COLLECTION export CSV (downloaded from Numista page) and build the
	same structure the script expects from out/collection.json:
	  {"item_count": int, "item_type_count": int, "items": [...]}
	Required columns used:
	  - "N# number (with link)" -> type_id
	  - "Quantity"
	  - "Gregorian year" (preferred) or "Year"
	  - "Title" (optional stub)
	  - "Issuer" / "Country" (optional stub)
	"""
	p = Path(csv_path)
	if not p.exists():
		raise FileNotFoundError(f"Missing collection CSV: {csv_path}")
	def _pick(row: dict, *keys: str) -> str:
		for k in keys:
			if k in row and (row[k] or "").strip() != "":
				return (row[k] or "").strip()
		return ""
	# detect delimiter by parsed headers, not by raw character counts
	delim = detect_numista_csv_delimiter(p)
	items = []
	with p.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
		reader = csv.reader(f, delimiter=delim)
		try:
			headers = [str(h or "").strip() for h in next(reader)]
		except StopIteration:
			headers = []
		first_index = {}
		for i, h in enumerate(headers):
			if h not in first_index:
				first_index[h] = i
		grade_idx = -1
		for i, h in enumerate(headers):
			if h == "Grade" and i > 0 and headers[i - 1] == "For exchange":
				grade_idx = i
				break
		if grade_idx < 0:
			grade_idx = first_index.get("Grade", -1)
		def _cell(row, *keys):
			for k in keys:
				idx = first_index.get(k)
				if idx is not None and idx < len(row) and str(row[idx] or "").strip() != "":
					return str(row[idx] or "").strip()
			return ""
		for raw_row in reader:
			nfield = _cell(raw_row, "N# number (with link)", "Número N# (con enlace)", "Numero N# (con enlace)")
			m = re.search(r"(\d+)", nfield)
			if not m:
				continue
			type_id = int(m.group(1))
			qraw = _cell(raw_row, "Quantity", "Cantidad") or "1"
			try:
				qty = int(float(qraw))
			except Exception:
				qty = 1
			y_raw = _cell(raw_row, "Year", "Año")
			gy_raw = _cell(raw_row, "Gregorian year", "Año gregoriano")
			grade = ""
			if grade_idx >= 0 and grade_idx < len(raw_row):
				grade = str(raw_row[grade_idx] or "").strip()
			grade = grade or "Ungraded"
			year = None
			gyear = None
			try:
				if y_raw != "":
					year = int(float(y_raw))
			except Exception:
				year = None
			try:
				if gy_raw != "":
					gyear = int(float(gy_raw))
			except Exception:
				gyear = None
			year_for_filter = gyear if isinstance(gyear, int) else year
			title = _cell(raw_row, "Title", "Título", "Titulo")
			issuer = _cell(raw_row, "Issuer", "Country", "Emisor", "País", "Pais")
			item = {
				"type": {"id": type_id},
				"issue": {},
				"quantity": qty,
				"grade": grade,
			}
			if title:
				item["type"]["title"] = title
			if issuer:
				item["type"]["issuer"] = {"name": issuer}
			if isinstance(year_for_filter, int):
				item["issue"]["year"] = year_for_filter
			if isinstance(year, int):
				item["issue"]["raw_year"] = year
			if isinstance(gyear, int):
				item["issue"]["gregorian_year"] = gyear
			if not item["issue"]:
				item["issue"] = None
			items.append(item)
	total_items = sum(int(it.get("quantity", 1) or 1) for it in items)
	total_types = len({it["type"]["id"] for it in items if isinstance(it.get("type", {}).get("id"), int)})
	return {
		"item_count": total_items,
		"item_type_count": total_types,
		"items": items,
		"source_notes": {"source": "numista_collection_csv", "path": str(csv_path)},
	}
def load_type_ids_from_numista_csv(csv_path: str) -> list[int]:
	"""
	Load a Numista CSV export that contains a "N# number (with link)" column and
	return de-duplicated type_ids preserving file order. Used for wishlist CSV input.
	"""
	p = Path(csv_path)
	if not p.exists():
		raise FileNotFoundError(f"Missing Numista CSV: {csv_path}")
	def _pick(row: dict, *keys: str) -> str:
		for k in keys:
			if k in row and (row[k] or "").strip() != "":
				return (row[k] or "").strip()
		return ""
	# detect delimiter by parsed headers, not by raw character counts
	delim = detect_numista_csv_delimiter(p)
	type_ids = []
	seen = set()
	with p.open("r", encoding="utf-8", errors="ignore", newline="") as f:
		reader = csv.DictReader(f, delimiter=delim)
		for row in reader:
			nfield = _pick(row, "N# number (with link)", "Número N# (con enlace)", "Numero N# (con enlace)")
			m = re.search(r"(\d+)", nfield)
			if not m:
				continue
			type_id = int(m.group(1))
			if type_id in seen:
				continue
			seen.add(type_id)
			type_ids.append(type_id)
	return type_ids

def load_wishlist_issuer_codes(txt_path: str, issuers_json_path: str) -> tuple[list[str], list[str]]:
	"""
	Lee wishlist_issuers.txt (nombres, uno por línea) y lo mapea a issuer.code usando issuers_en.json.
	Retorna (codes, unmatched_names).
	"""
	if not Path(txt_path).exists():
		return ([], [])
	if not Path(issuers_json_path).exists():
		raise FileNotFoundError(f"Missing {issuers_json_path} (needed to map wishlist issuers)")
	wanted = []
	for line in Path(txt_path).read_text(encoding="utf-8", errors="ignore").splitlines():
		line = line.strip()
		if line:
			wanted.append(line)
	issuers_blob = json.loads(Path(issuers_json_path).read_text(encoding="utf-8", errors="ignore"))
	issuers = issuers_blob.get("issuers") or []
	by_norm = {}
	for it in issuers:
		name = it.get("name", "")
		code = it.get("code", "")
		if name and code:
			by_norm[_norm_name(name)] = code
	# aliases mínimos (por idioma/typos comunes en tu lista)
	aliases = {
		"ANTIGUA Y BARDUDA": "ANTIGUA AND BARBUDA",
		"BOSNIA Y HERZEGOVINA": "BOSNIA AND HERZEGOVINA",
		"GUINEA BISAU": "GUINEA BISSAU",
		"CONGO": "CONGO, REPUBLIC OF THE",
		"COMORO ISLAND": "COMOROS",
		"MICRONESIA": "MICRONESIA FEDERATED STATES OF",
		"MOLDAVIA": "MOLDOVA",
	}
	codes = []
	unmatched = []
	for raw in wanted:
		key = _norm_name(raw)
		if key in aliases:
			key = _norm_name(aliases[key])
		code = by_norm.get(key)
		if code:
			codes.append(code)
		else:
			unmatched.append(raw)
	# dedup preservando orden
	seen = set()
	codes2 = []
	for c in codes:
		if c not in seen:
			seen.add(c)
			codes2.append(c)
	return (codes2, unmatched)

def _parse_year_list_cell(value: str) -> list[int]:
	"""Parse a flexible year-list cell like '1975;1976;1978' or '1975, 1976'."""
	text = str(value or "").strip()
	if not text:
		return []
	years = []
	seen = set()
	for part in re.split(r"[;,|\s]+", text):
		part = part.strip()
		if not part:
			continue
		m = re.fullmatch(r"-?\d{1,4}", part)
		if not m:
			continue
		try:
			y = int(part)
		except Exception:
			continue
		if y not in seen:
			seen.add(y)
			years.append(y)
	return sorted(years)


def load_chile_date_runs_csv(csv_path: str) -> list[dict]:
	"""Load optional curated Chile date-run checklist.

	Expected columns:
	  type_id,title,currency,category,composition,year_range,expected_years,
	  excluded_years,source_url,source_method,notes
	Only type_id and expected_years are required. Extra fields are carried to the UI.
	"""
	p = Path(csv_path)
	if not p.exists():
		return []
	rows = []
	with p.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
		reader = csv.DictReader(f)
		for row in reader:
			m = re.search(r"\d+", str(row.get("type_id") or row.get("N#") or row.get("n") or ""))
			if not m:
				continue
			type_id = int(m.group(0))
			expected = _parse_year_list_cell(row.get("expected_years") or row.get("years") or "")
			if not expected:
				continue
			excluded = _parse_year_list_cell(row.get("excluded_years") or "")
			rows.append({
				"type_id": type_id,
				"title": str(row.get("title") or "").strip(),
				"currency": str(row.get("currency") or "").strip(),
				"category": str(row.get("category") or "").strip(),
				"composition": str(row.get("composition") or "").strip(),
				"year_range": str(row.get("year_range") or "").strip(),
				"expected_years": expected,
				"excluded_years": excluded,
				"source_url": str(row.get("source_url") or "").strip(),
				"image_url": str(row.get("image_url") or row.get("obv_url") or row.get("obv") or row.get("image") or "").strip(),
				"obv_url": str(row.get("obv_url") or row.get("obv") or row.get("image_url") or row.get("image") or "").strip(),
				"rev_url": str(row.get("rev_url") or row.get("rev") or "").strip(),
				"source_method": str(row.get("source_method") or "curated_csv").strip() or "curated_csv",
				"notes": str(row.get("notes") or "").strip(),
			})
	rows.sort(key=lambda r: ((r.get("currency") or ""), min(r.get("expected_years") or [9999]), r.get("title") or "", r.get("type_id") or 0))
	return rows

TOKEN_REQUESTS = 0  # how many times we requested an OAuth token
API_KEY = os.environ.get("NUMISTA_API_KEY", "").strip()
AUTO_FETCH_MISSING = os.environ.get("NUMISTA_AUTO_FETCH_MISSING", "").strip().lower() in ("1", "true", "yes", "y")
CLIENT_ID = "104851"
BASE_URL = "https://api.numista.com/v3"
MAX_TYPES = 2100  # prueba segura: solo 5 llamadas a /types/{id}
CACHE_DIR = Path("type_cache")
ISSUERS_CACHE = Path("issuers_cache")
FAILED_FLAG_ISSUERS = set()
COLLECTION_PATH = "out/collection.json"
COLLECTION_OUT_HTML = "out/collection_preview.html"
COLLECTION_CSV_PATH = "inputs/collection_public.csv"   # <-- put your collection export here
DEFAULT_COLLECTION_SOURCE = "csv"                   # "csv" (default) or "json"
REPLICAS_CSV_PATH = "inputs/replicas.csv"              # optional; extra items marked as category "replica"
ON_TRANSIT_CSV_PATH = "inputs/on_transit.csv"          # optional; extra items marked as category "on transit"
CRISTOBAL_COLLECTION_CSV_PATH = "out/cristobal_export.csv"
CRISTOBAL_COLLECTION_OUT_HTML = "out/cristobal_collection_preview.html"
CRISTOBAL_COLLECTION_TITLE = "Cristobal's Coin Collection"
WISHLIST_COLLECTION_CSV_PATH = "inputs/wishlist_public.csv"
WISHLIST_OUT_HTML = "out/wishlist_preview.html"
COMBINED_APP_HTML = "out/collection_wishlist_analytics.html"
PORTAL_SUMMARY_JSON = "out/summary.json"
CHILE_DATE_RUNS_CSV_PATH = "inputs/chile_date_runs.csv"  # optional curated Chile expected years, generated by build_chile_date_runs.py

# Header coin image: N#34945, Chile 20 Pesos gold.
HEADER_COIN_REVERSE_URL = "https://en.numista.com/catalogue/photos/chili/525-original.jpg"
HEADER_COIN_ICON_URL = "https://en.numista.com/catalogue/photos/chili/525-original.jpg"
WISHLIST_ISSUERS_CSV = "inputs/wishlist_issuers.csv"
WISHLIST_ISSUER_PLACEHOLDER_IMG = "https://en.numista.com/catalogue/no-obverse-coin-en.png"
HISTORICAL_FLAGS = {
	"China, People's Republic of": "Flag_of_China.svg",
	"Czechoslovakia": "Flag_of_Czechoslovakia.svg",
	"Democratic Republic of the Congo (1997-date)": "Flag_of_the_Democratic_Republic_of_the_Congo.svg",
	"German Democratic Republic": "Flag_of_East_Germany.svg",
	"Germany, Federal Republic of": "Flag_of_Germany.svg",
	"Netherlands Antilles": "Flag_of_the_Netherlands_Antilles.svg",
	"Soviet Union": "Flag_of_the_Soviet_Union.svg",
	"Swaziland, Kingdom of (1968-2018)": "Flag_of_Eswatini.svg",
	"Transnistria": "Flag_of_Transnistria_(state).svg",
	"Turkey": "Flag_of_Turkey.svg",
	"Western African States": "Flag_of_the_West_African_Economic_and_Monetary_Union.svg",
	"Yugoslavia": "Flag_of_Yugoslavia.svg",
	"Eastern Caribbean States": "Flag_of_Eastern_Caribean.png",
	"Frankfurt, Free imperial city of": "Flag_of_the_Free_City_of_Frankfurt.svg",
	"Papal States": "Flag_of_the_Papal_States_(pre_1808).svg",
	"Republic of the Congo (Léopoldville) (1960-1971)": "Flag_of_the_Democratic_Republic_of_the_Congo_(1966%E2%80%931971).svg",
	"Tyrol, County of": "Flag_of_Tirol_and_Upper_Austria.svg",
	"Austrian Empire": "Austria-Hungary-flag-1869-1914-naval-1786-1869-merchant.svg",
	"Republic of the Congo (Léopoldville)": "Flag_of_the_Democratic_Republic_of_the_Congo_(1966%E2%80%931971).svg",
}
NAME_TO_ISO2_OVERRIDE = {
	"England": "gb-eng",   # not ISO3166-1; FlagCDN supports this subdivision code sometimes, but not always
	"Curaçao": "cw",
}
CONTINENT_OVERRIDES = {
	"Jersey": "Europe",
	"Czechoslovakia": "Europe",
	"Yugoslavia": "Europe",
	"Timor-Leste": "Asia",
	"Turkey": "Asia",  # or Europe, but standard is Asia (transcontinental)
	"Vatican City": "Europe",
	"Papal States": "Europe",
	"Eastern Caribbean States": "North America",
	"Western African States": "Africa",
	"Transnistria": "Europe",
	"Republic of the Congo (Léopoldville)": "Africa",
	"Congo (Democratic Republic)": "Africa",
	"Congo, Democratic Republic of the": "Africa",
	"Cape Verde": "Africa",
	"Central African States": "Africa",   # agrupación regional
	"Comoros": "Africa",                  # a veces te llega como Comoro Islands › Comoros
	"Somaliland": "Africa",               # entidad no reconocida por ISO/pycountry
	"Comoro Islands": "Africa",
	"British Crown dependencies": "Europe"
}
EXTRA_SPANISH_ISSUER_ALIASES = {
	"British Crown dependencies": ["Dependencias de la Corona Britanica", "Dependencias de la Corona Británica"],
	"Central African States": ["Estados de Africa Central", "Estados de África Central"],
	"China, People's Republic of": ["China", "Republica Popular China", "República Popular China"],
	"Comoro Islands": ["Islas Comoras", "Comoras"],
	"Congo, Democratic Republic of the": ["Republica Democratica del Congo", "República Democrática del Congo"],
	"Czechoslovakia": ["Checoslovaquia"],
	"Eastern Caribbean States": ["Estados del Caribe Oriental"],
	"England": ["Inglaterra"],
	"European Union": ["Union Europea", "Unión Europea"],
	"Germany, Federal Republic of": ["Alemania Occidental", "Republica Federal de Alemania", "República Federal de Alemania"],
	"German Democratic Republic": ["Alemania Oriental", "Republica Democratica Alemana", "República Democrática Alemana"],
	"Great Britain": ["Gran Bretana", "Gran Bretaña"],
	"Isle of Man": ["Isla de Man"],
	"North Macedonia": ["Macedonia del Norte"],
	"Rome": ["Roma"],
	"Roman Empire": ["Imperio Romano"],
	"Scotland": ["Escocia"],
	"Somaliland": ["Somalilandia"],
	"Soviet Union": ["Union Sovietica", "Unión Soviética"],
	"United Kingdom": ["Reino Unido", "Gran Bretaña"],
	"United States": ["Estados Unidos", "EEUU", "EE UU", "Estados Unidos de America", "Estados Unidos de América"],
	"Vatican City": ["Ciudad del Vaticano", "Vaticano"],
	"Wales": ["Gales"],
	"Western African States": ["Estados de Africa Occidental", "Estados de África Occidental"],
	"Yugoslavia": ["Yugoslavia"]
}


def _spanish_country_name_from_iso3(iso3: str) -> str:
	"""Resolve a modern ISO-3 country to a Spanish display name when possible."""
	iso3 = (iso3 or "").strip().upper()
	if not iso3:
		return ""
	# Special non-ISO or disputed codes used by the map aliases.
	special = {
		"XKX": "Kosovo",
	}
	if iso3 in special:
		return special[iso3]
	alpha2 = ""
	try:
		alpha2 = pycountry.countries.get(alpha_3=iso3).alpha_2
	except Exception:
		alpha2 = ""
	if not alpha2:
		return ""
	try:
		from babel import Locale
		name = Locale('es').territories.get(alpha2)
		return (name or "").strip()
	except Exception:
		return ""


def spanish_search_terms_for_issuer(root_name: str, issuer_path: str = "") -> str:
	"""Return extra Spanish aliases for issuer/country search.

	Strategy:
	- derive a modern ISO-3 using the same country mapping logic as Analytics
	- translate that modern country to Spanish via Babel territory names
	- add manual aliases for historical/special issuers and path nodes
	This keeps search aligned with the map logic and avoids a brittle country-by-country table.
	"""
	terms = []
	seen = set()

	def add(value: str):
		value = (value or "").strip()
		if not value:
			return
		key = value.lower()
		if key in seen:
			return
		seen.add(key)
		terms.append(value)

	parts = [p.strip() for p in (issuer_path or "").split(" › ") if p.strip()]
	root_clean = (root_name or "").strip()

	# 1) Main modern-country alias in Spanish, based on the same ISO mapping as Analytics.
	iso3 = modern_country_iso3_from_issuer_root(root_clean)
	spanish_modern = _spanish_country_name_from_iso3(iso3)
	add(spanish_modern)
	if spanish_modern:
		# Common plain variants without accents are already matched by JS normalization,
		# but keeping the original form here helps exact-substring search too.
		pass

	# 2) Manual aliases for root and path nodes (historical / special issuers).
	for part in parts:
		for alias in EXTRA_SPANISH_ISSUER_ALIASES.get(part, []):
			add(alias)
	for alias in EXTRA_SPANISH_ISSUER_ALIASES.get(root_clean, []):
		add(alias)

	# 3) Spanish path composed from translated modern root + manual aliases for deeper nodes.
	path_parts_es = []
	for i, part in enumerate(parts):
		if i == 0 and spanish_modern:
			path_parts_es.append(spanish_modern)
			continue
		aliases = EXTRA_SPANISH_ISSUER_ALIASES.get(part) or []
		path_parts_es.append(aliases[0] if aliases else part)
	if path_parts_es:
		add(" ".join(path_parts_es))
		add(" › ".join(path_parts_es))

	return " ".join(terms)

def get_token(api_key: str, client_id: str) -> dict:
	global TOKEN_REQUESTS
	TOKEN_REQUESTS += 1
	# OAuth client_credentials, scope view_collection (según Swagger)
	r = requests.post(
		f"{BASE_URL}/oauth_token",
		data={
			"grant_type": "client_credentials",
			"client_id": client_id,
			"client_secret": api_key,
			"scope": "view_collection",
		},
		timeout=30,
	)
	r.raise_for_status()
	return r.json()
def _require_numista_api_key(api_key: str) -> str:
	if not api_key:
		raise RuntimeError("NUMISTA_API_KEY is required because an uncached Numista record must be downloaded.")
	return api_key

def get_type_cached(api_key: str, type_id: int, lang: str = "en") -> dict:
	CACHE_DIR.mkdir(parents=True, exist_ok=True)
	p = CACHE_DIR / f"{type_id}.json"
	if p.exists():
		return json.loads(p.read_text(encoding="utf-8"))
	api_key = _require_numista_api_key(api_key)
	r = requests.get(
		f"{BASE_URL}/types/{type_id}",
		headers={"Numista-API-Key": api_key},
		params={"lang": lang},
		timeout=30,
	)
	r.raise_for_status()
	data = r.json()
	p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
	return data
def get_issuers_cached(api_key: str, lang: str = "en") -> dict:
	"""
	One-time fetch of issuers tree (has code, name, parent{code,name}, flag).
	Cached locally to avoid repeated requests.
	"""
	ISSUERS_CACHE.mkdir(parents=True, exist_ok=True)
	p = ISSUERS_CACHE / f"issuers_{lang}.json"
	if p.exists():
		data = json.loads(p.read_text(encoding="utf-8"))
	else:
		api_key = _require_numista_api_key(api_key)
		r = requests.get(
			f"{BASE_URL}/issuers",
			headers={"Numista-API-Key": api_key},
			params={"lang": lang},
			timeout=60,
		)
		r.raise_for_status()
		data = r.json()
		p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
	issuers = data.get("issuers", [])
	by_code = {x.get("code"): x for x in issuers if x.get("code")}
	return by_code
def issuer_path_from_code(issuer_code: str, issuers_by_code: dict) -> str:
	"""
	Build 'United Kingdom › England' by following parent.code in /issuers list.
	"""
	if not issuer_code:
		return ""
	parts = []
	seen = set()
	cur_code = issuer_code
	while cur_code and cur_code not in seen:
		seen.add(cur_code)
		node = issuers_by_code.get(cur_code)
		if not node:
			break
		name = node.get("name") or ""
		name = strip_date_suffix(name)  # display-only (keeps your preference)
		if name:
			parts.append(name)
		parent = node.get("parent") or {}
		cur_code = parent.get("code")
	return " \u203A ".join(reversed(parts))
def flag_from_code(issuer_code: str, issuers_by_code: dict) -> str:
	node = issuers_by_code.get(issuer_code) if issuer_code else None
	if node and node.get("flag"):
		return node["flag"]
	return ""
def issuer_root_from_path(path: str) -> str:
	return path.split(" \u203A ", 1)[0].strip() if path else ""
def issuer_subpath_from_path(path: str) -> str:
	# everything after root, for stable ordering inside root
	parts = path.split(" \u203A ")
	return " \u203A ".join(parts[1:]).strip() if len(parts) > 1 else ""
#     # Builds: United Kingdom › British Overseas Territories › Bermuda
#         parts.append(cur["name"])
def strip_date_suffix(s: str) -> str:
	# remove trailing " (YYYY-date)" or "(YYYY-YYYY)" for display only
	return re.sub(r"\s*\(\d{4}[^)]*\)\s*$", "", s).strip()
def issuer_path(issuer: dict) -> str:
	# Use issuer.name only (matches Numista UI better than full_name)
	parts = []
	cur = issuer
	while isinstance(cur, dict) and cur.get("name"):
		parts.append(cur["name"])
		cur = cur.get("parent")
	# display without date suffixes
	parts = [strip_date_suffix(p) for p in reversed(parts) if p]
	return " \u203A ".join(parts)
def issuer_root_code_from_code(issuer_code: str, issuers_by_code: dict) -> str:
	if not issuer_code:
		return ""
	seen = set()
	cur = issuer_code
	last = issuer_code
	while cur and cur not in seen:
		seen.add(cur)
		last = cur
		node = issuers_by_code.get(cur) or {}
		parent = (node.get("parent") or {})
		cur = parent.get("code")
	return last
# --- Continent helpers (7-continent model) ---
_CONTINENT_MAP = {
	"AF": "Africa",
	"AN": "Antarctica",
	"AS": "Asia",
	"EU": "Europe",
	"NA": "North America",
	"OC": "Oceania",
	"SA": "South America",
}
def continent_from_iso2(iso2: str) -> str:
	"""Return continent name from ISO-3166 alpha-2 code (7-continent model)."""
	iso2 = (iso2 or "").strip().upper()
	if not iso2:
		return "Unknown"
	try:
		import pycountry_convert as pc  # optional dependency
		code = pc.country_alpha2_to_continent_code(iso2)
		return _CONTINENT_MAP.get(code, "Unknown")
	except Exception:
		# Fallback: limited mapping so the script still runs without extra deps.
		fallback = {
			"US": "North America", "CA": "North America", "MX": "North America",
			"BR": "South America", "AR": "South America", "CL": "South America", "CO": "South America", "PE": "South America", "UY": "South America", "VE": "South America", "EC": "South America",
			"GB": "Europe", "UK": "Europe", "FR": "Europe", "DE": "Europe", "ES": "Europe", "PT": "Europe", "IT": "Europe", "NL": "Europe", "BE": "Europe", "CH": "Europe", "AT": "Europe",
			"SE": "Europe", "NO": "Europe", "DK": "Europe", "FI": "Europe", "PL": "Europe", "GR": "Europe",
			"CN": "Asia", "JP": "Asia", "KR": "Asia", "IN": "Asia",
			"AU": "Oceania", "NZ": "Oceania",
			"ZA": "Africa", "EG": "Africa", "NG": "Africa",
		}
		return fallback.get(iso2, "Unknown")
# def continent_from_issuer_root(root_name: str) -> str:
#     """Infer continent from an issuer root display name using iso2_from_name()."""
#     return continent_from_iso2(iso2_from_name(root_name))
def continent_from_issuer_root(root_name: str) -> str:
	"""Infer continent from an issuer root display name."""
	root_clean = strip_date_suffix(root_name or "").strip()
	if not root_clean:
		return "Unknown"
	if root_clean in CONTINENT_OVERRIDES:
		return CONTINENT_OVERRIDES[root_clean]
	iso2 = iso2_from_name(root_clean)
	return continent_from_iso2(iso2)
def iso2_from_name(name: str) -> str:
	if not name:
		return ""
	override = NAME_TO_ISO2_OVERRIDE.get(name)
	if override:
		return override
	# strip date suffixes like " (1973-date)"
	name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
	# fix "Bahamas, The" -> "The Bahamas"
	if name.endswith(", The"):
		name = "The " + name[:-5].strip()
	try:
		return pycountry.countries.search_fuzzy(name)[0].alpha_2.lower()
	except Exception:
		return ""
def currency_sort_key(label: str):
	"""
	Sort by start year if label contains "(YYYY-...)", else put at end.
	"""
	m = re.search(r"\((\d{4})\s*[-–]", label)
	if m:
		return (0, int(m.group(1)), label.lower())
	return (1, 9999, label.lower())

def normalize_currency_label(label: str, issuer_root: str = "") -> str:
	"""Collapse known Numista currency label splits that should behave as one bucket."""
	label = (label or "").strip()
	issuer_root = strip_date_suffix(issuer_root or "").strip()
	if issuer_root == "United Kingdom" and label in {"Pound sterling (1158-1970)", "Pound sterling (1158-1971)"}:
		return "Pound sterling (1158-1971)"
	return label


def build_latest_currency_by_issuer(types_by_id: dict, issuers_by_code: dict) -> dict:
	"""
	Infer issuer_code -> 'latest' currency label from already-fetched types, and propagate to ancestors
	using issuers_by_code[parent.code]. No extra API calls.
	"""
	currencies = defaultdict(set)
	def ancestors(code: str):
		seen = set()
		cur = code
		while cur and cur not in seen:
			seen.add(cur)
			yield cur
			node = issuers_by_code.get(cur) or {}
			cur = ((node.get("parent") or {}).get("code")) or ""
	for td in (types_by_id or {}).values():
		issuer_code = ((td.get("issuer") or {}).get("code")) or ""
		if not issuer_code:
			continue
		issuer_root = issuer_root_from_path(issuer_path_from_code(issuer_code, issuers_by_code)) if issuer_code else ""
		currency = normalize_currency_label((((td.get("value") or {}).get("currency") or {}).get("full_name")) or "", issuer_root)
		if not currency:
			continue
		for a in ancestors(issuer_code):
			currencies[a].add(currency)
	latest = {}
	for code, curset in currencies.items():
		try:
			latest[code] = sorted(curset, key=currency_sort_key)[-1]
		except Exception:
			pass
	return latest
#             FAILED_FLAG_ISSUERS.add(name)
def wikimedia_flag_url(name: str) -> str:
	filename = HISTORICAL_FLAGS.get(name)
	if not filename:
		return ""
	# force a small raster version (prevents huge SVG display)
	return f"https://commons.wikimedia.org/wiki/Special:FilePath/{filename}?width=20"
def flag_url_from_issuer(issuer_obj: dict) -> str:
	name = (issuer_obj or {}).get("name", "")
	# 1) Try Wikimedia overrides first (historical/unions/special cases)
	url = wikimedia_flag_url(name)
	if url:
		return url
	# 2) Otherwise, fall back to your existing ISO/pycountry logic
	iso2 = iso2_from_name(name)  # your existing function
	if iso2:
		return f"https://flagcdn.com/w20/{iso2}.png"
	if name:
		FAILED_FLAG_ISSUERS.add(name)
	return ""
def flag_url_from_name(name: str) -> str:
	if not name:
		return ""
	url = wikimedia_flag_url(name)
	if url:
		return url
	iso2 = iso2_from_name(name)
	if iso2:
		return f"https://flagcdn.com/w20/{iso2}.png"
	return ""
def pick_km_or_y(type_details: dict, type_id: Optional[int] = None) -> str:
	if type_id is not None and type_id in KM_Y_OVERRIDE_BY_TYPE_ID:
		return KM_Y_OVERRIDE_BY_TYPE_ID[type_id]
	refs = type_details.get("references") or []
	if not isinstance(refs, list):
		return ""
	wanted = []
	for ref in refs:
		cat = (ref.get("catalogue") or {}).get("code")
		num = ref.get("number")
		if cat in {"KM", "Y"} and num:
			wanted.append(f"{cat}#{num}")
	# de-dup, preserve order
	seen = set()
	wanted = [x for x in wanted if not (x in seen or seen.add(x))]
	if not wanted:
		return ""
	if len(wanted) == 1:
		return wanted[0]
	# ONLY if more than one, show them both (same line formatting)
	return " / ".join(wanted)
def km_y_sort_key(km_y: str):
	"""
	Sort KM/Y references naturally for wishlist default order.
	Examples:
	- KM#75a -> (0, 75.0, 'a')
	- Y#45 -> (1, 45.0, '')
	- KM#2430 / KM#2603 -> first valid reference wins
	Unknown refs go to the end.
	"""
	raw = (km_y or "").upper().strip()
	if not raw:
		return (99, 1e30, "", raw)
	matches = re.findall(r'\b(KM|Y)\s*#?\s*([0-9]+(?:\.[0-9]+)?)([A-Z]*)\b', raw)
	if not matches:
		return (99, 1e30, "", raw)
	rank_map = {"KM": 0, "Y": 1}
	parsed = []
	for cat, num, suffix in matches:
		try:
			num_f = float(num)
		except Exception:
			num_f = 1e30
		parsed.append((rank_map.get(cat, 99), num_f, suffix or "", f"{cat}#{num}{suffix}"))
	parsed.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
	return parsed[0]

def _sort_number(value, default: float = 1e30) -> float:
	"""Return a finite float for sort keys, with blanks/invalid values at the end."""
	try:
		n = float(value)
		if n == n and n not in (float("inf"), float("-inf")):
			return n
	except Exception:
		pass
	return default


def _norm_sort_text(value: str) -> str:
	"""Normalize text for denomination-unit detection without changing display text."""
	s = str(value or "").lower()
	s = (s.replace("ø", "o").replace("æ", "ae").replace("œ", "oe")
	       .replace("å", "a").replace("ð", "d").replace("þ", "th")
	       .replace("ł", "l").replace("ß", "ss"))
	s = unicodedata.normalize("NFKD", s)
	s = "".join(ch for ch in s if not unicodedata.combining(ch))
	s = re.sub(r"[^a-z0-9]+", " ", s)
	return re.sub(r"\s+", " ", s).strip()


def _has_any_word(text: str, words: tuple[str, ...]) -> bool:
	return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


_MINOR_DENOMINATION_FACTORS = [
	(1000, ("mill", "mille", "mil", "mils", "millime", "millimes", "millieme", "milliemes", "fils")),
	(100, ("cent", "cents", "centime", "centimes", "centimo", "centimos", "centavo", "centavos", "centesimo", "centesimos", "centas", "centu", "penny", "pennies", "pence", "new pence", "ore", "oere", "grosz", "groszy", "kopeck", "kopecks", "kopek", "kopeks", "kopiyka", "kopiyok", "sen", "bani", "ban", "para", "paras")),
	(20, ("shilling", "shillings")),
]


def face_sort_value(numeric_value, label: str = "", title_full: str = "", currency: str = "") -> float:
	"""Face-value sort key used by both default HTML order and dropdown sorting.

	Numista numeric_value is usually already comparable, but some exports/API rows can
	carry the displayed minor unit as the number (for example 50 Pence vs 1 Pound).
	When the denomination text clearly names a smaller unit and the currency bucket is
	not already that smaller unit, normalize it to the major unit so 50 pence sorts
	before 1 pound inside the same grid.
	"""
	v = _sort_number(numeric_value)
	if v >= 1e29:
		return v
	label_norm = _norm_sort_text(" ".join([str(label or ""), str(title_full or "")]))
	currency_norm = _norm_sort_text(currency or "")
	if not label_norm:
		return v
	# If Numista already gave a fractional major-unit value, keep it.
	if abs(v) < 1:
		return v
	for factor, words in _MINOR_DENOMINATION_FACTORS:
		if _has_any_word(label_norm, words) and not _has_any_word(currency_norm, words):
			return v / factor
	return v


def year_sort_value(value) -> int:
	return value if isinstance(value, int) else 999999


def default_row_sort_key(r: dict):
	"""Shared default order for collection, wishlist, transit, and issuer wishes."""
	return (
		1 if r.get("is_issuer_only") else 0,
		(r.get("issuer_root") or "").lower(),
		(r.get("issuer_subpath") or "").lower(),
		currency_sort_key(r.get("currency", "")),
		face_sort_value(r.get("face_sort_value", r.get("numeric_value", 1e30)), r.get("label", ""), r.get("title_full", ""), r.get("currency", "")),
		year_sort_value(r.get("min_year")),
		year_sort_value(r.get("max_year")),
		km_y_sort_key(r.get("km_y", "")),
		(r.get("label") or r.get("title_full") or "").lower(),
	)

def shorten_composition(comp_text: str) -> str:
	"""Make composition text compact for display.
	- Drop anything in parentheses (typically percentage breakdowns)
	- For bimetallic patterns, simplify 'centre in X ring' -> '... and X'
	"""
	s = (comp_text or "").strip()
	if not s:
		return ""
	# Remove detailed breakdowns
	s = s.split("(", 1)[0].strip()
	# Normalize spacing
	s = re.sub(r"\s+", " ", s).strip()
	# Simplify common bimetallic phrasing
	s = re.sub(r"\b(centre|center)\s+in\s+", "and ", s, flags=re.IGNORECASE)
	s = re.sub(r"\bring\b", "", s, flags=re.IGNORECASE)
	s = re.sub(r"\s+", " ", s).strip()
	# Remove stray punctuation at end
	s = s.rstrip(" .;,-")
	return s
def html_escape(s: str) -> str:
	return (
		s.replace("&", "&amp;")
		 .replace("<", "&lt;")
		 .replace(">", "&gt;")
		 .replace('"', "&quot;")
		 .replace("'", "&#039;")
	)

def split_title_main_extra(label: str, title_full: str) -> tuple[str, str]:
	"""Split a displayed title into a main part and a smaller extra suffix.

	Rules:
	- Keep the full denomination/currency name in the main title.
	- Only move true modifiers/notes to the smaller suffix, typically parenthetical
	  qualifiers such as "(Large Coat of Arms)" or "(1st type; 2nd map)".
	- If Numista's face value text is shorter than the full title (for example
	  "1 Sol" vs "1 Sol de Oro"), keep connector phrases like "de Oro" in the
	  main title so search and display both match the real denomination.
	"""
	main = (label or "").strip()
	full = (title_full or "").strip()
	if not full:
		return main, ""
	if not main:
		return full, ""
	if full == main:
		return main, ""
	if not full.startswith(main):
		return main, ""

	remainder = full[len(main):].strip()
	if not remainder:
		return main, ""

	# If the remaining text starts with a lowercase connector phrase, keep that
	# phrase in the main title until a parenthetical note begins.
	# Example: "1 Sol" + "de Oro (Large Coat of Arms)" ->
	#          main="1 Sol de Oro", extra="(Large Coat of Arms)"
	if remainder and remainder[0].islower():
		paren_idx = remainder.find("(")
		if paren_idx != -1:
			connector = remainder[:paren_idx].strip()
			extra = remainder[paren_idx:].strip()
			if connector:
				return f"{main} {connector}".strip(), extra
		return full, ""

	return main, remainder

def compact_int_list(values: list[int], *, limit: int = 6) -> str:
	"""Compact a list of integer values for badge tooltips."""
	clean = sorted({v for v in values if isinstance(v, int)})
	if not clean:
		return ""
	if len(clean) <= limit:
		return ", ".join(str(v) for v in clean)
	return f"{clean[0]}–{clean[-1]}"


def duplicate_years_display_label(raw_years: list[int], greg_years: list[int], year_map: dict) -> str:
	"""Human-readable years for the duplicate-year badge tooltip.

	Prefer raw/display years when the same raw year is duplicated. If duplicate
	detection only happened on Gregorian fallback years, show those instead.
	"""
	raw_clean = sorted({y for y in (raw_years or []) if isinstance(y, int)})
	greg_clean = sorted({y for y in (greg_years or []) if isinstance(y, int)})
	if raw_clean:
		def fmt_year(y: int) -> str:
			gy = year_map.get(y, y) if isinstance(year_map, dict) else y
			return f"{y} ({gy})" if isinstance(gy, int) and gy != y else f"{y}"
		if len(raw_clean) <= 6:
			return ", ".join(fmt_year(y) for y in raw_clean)
		return f"{fmt_year(raw_clean[0])}–{fmt_year(raw_clean[-1])}"
	return compact_int_list(greg_clean)


def build_card_badges(r: dict, mode: str) -> str:
	"""Build semantic badges for collection/wishlist cards."""
	badges = []
	qty = 0
	try:
		qty = int(r.get("qty") or 0)
	except Exception:
		qty = 0
	category = (r.get("category") or "").strip().lower()
	if category == "exonumia":
		badges.append("<span class='badge badge-exo'>Exonumia</span>")
	if mode == "wishlist":
		badges.append("<span class='badge badge-wish'>Wishlist</span>")
		if r.get("is_issuer_only"):
			badges.append("<span class='badge badge-muted'>Any coin</span>")
	else:
		if r.get("is_replica"):
			badges.append("<span class='badge badge-alert'>Replica</span>")
		if r.get("is_on_transit"):
			badges.append("<span class='badge badge-transit'>On transit</span>")
		# Duplicate type: same Numista type has more than one owned item.
		if bool(r.get("duplicate_type")) or qty > 1:
			badges.append("<span class='badge badge-dup-type' title='Same type: more than one item'>Duplicate type</span>")
		# Duplicate year: same Numista type + same owned year has more than one item.
		if bool(r.get("duplicate_year")):
			dup_year_label = (r.get("duplicate_years_label") or "").strip()
			title = "Same type and same year" + (f": {dup_year_label}" if dup_year_label else "")
			badges.append(f"<span class='badge badge-dup-year' title='{html_escape(title)}'>Duplicate year</span>")
		if qty:
			badges.append(f"<span class='badge'>x{qty}</span>")
	return "".join(badges)
def issuer_root_name(issuer: dict) -> str:
	# Return the top-most ancestor name (RAW, not stripped)
	cur = issuer
	last = ""
	while isinstance(cur, dict) and cur.get("name"):
		last = cur["name"]
		cur = cur.get("parent")
	return last or ""
def parse_wishlist_type_ids(html_glob: str) -> list[int]:
	"""
	Extrae type_id desde filas <tr id="t####"> dentro de <table class="selected_wishlist">.
	Une todas las páginas (1..5), dedup y ordena.
	"""
	from bs4 import BeautifulSoup
	type_ids = []
	for fp in sorted(Path().glob(html_glob)):
		txt = fp.read_text(encoding="utf-8", errors="ignore")
		soup = BeautifulSoup(txt, "html.parser")
		table = soup.find("table", {"class": "selected_wishlist"})
		if not table:
			continue
		for tr in table.find_all("tr"):
			tid = tr.get("id", "")
			if tid.startswith("t") and tid[1:].isdigit():
				type_ids.append(int(tid[1:]))
	# dedup preserve order
	seen = set()
	out = []
	for x in type_ids:
		if x not in seen:
			seen.add(x)
			out.append(x)
	return out
def analytics_country_rows(rows: list[dict], *, include_qty: bool = True) -> list[dict]:
	acc = {}
	for r in rows:
		root = (r.get("issuer_root") or "Unknown").strip() or "Unknown"
		info = acc.setdefault(root, {"country": root, "types": 0, "qty": 0, "replicas": 0, "on_transit": 0, "issuer_only": 0, "duplicates": 0})
		if r.get("is_issuer_only"):
			info["issuer_only"] += 1
		else:
			info["types"] += 1
			qty = 0
			try:
				qty = int(r.get("qty") or 0)
			except Exception:
				qty = 0
			if include_qty:
				info["qty"] += qty
			info["duplicates"] += max(qty - 1, 0)
		if r.get("is_replica"):
			info["replicas"] += 1
		if r.get("is_on_transit"):
			info["on_transit"] += 1
	return sorted(acc.values(), key=lambda x: (-x["types"], -x["qty"], x["country"].lower()))
def _sort_dict_rows(counter_dict: dict, value_key: str = "count") -> list[dict]:
	return sorted(counter_dict.values(), key=lambda x: (-x.get(value_key, 0), str(x.get("label", "")).lower()))
def _bucket_numeric(value, buckets: list[tuple[float, str]], *, default_label: str = "Unknown") -> str:
	try:
		v = float(value)
	except Exception:
		return default_label
	if v < 0:
		return default_label
	for upper, label in buckets:
		if v <= upper:
			return label
	return buckets[-1][1] if buckets else default_label
def size_bucket_label(mm) -> str:
	return _bucket_numeric(mm, [
		(14.999, "< 15 mm"),
		(19.999, "15-19.9 mm"),
		(24.999, "20-24.9 mm"),
		(29.999, "25-29.9 mm"),
		(34.999, "30-34.9 mm"),
		(1e9, "35+ mm"),
	])
def weight_bucket_label(g) -> str:
	return _bucket_numeric(g, [
		(0.999, "< 1 g"),
		(2.499, "1-2.49 g"),
		(4.999, "2.5-4.99 g"),
		(9.999, "5-9.99 g"),
		(19.999, "10-19.99 g"),
		(1e9, "20+ g"),
	])
def normalize_analytics_type(r: dict) -> str:
	cat = (r.get("category") or "").strip()
	obj = (r.get("object_type") or "").strip()
	if cat:
		return cat
	if obj:
		return obj
	return "Unknown"
def analytics_group_counts(rows: list[dict], key_fn, *, qty_field: Optional[str] = None, skip_issuer_only: bool = True) -> list[dict]:
	acc = {}
	for r in rows:
		if skip_issuer_only and r.get("is_issuer_only"):
			continue
		label = key_fn(r) or "Unknown"
		info = acc.setdefault(label, {"label": label, "count": 0, "qty": 0})
		info["count"] += 1
		if qty_field:
			try:
				info["qty"] += int(r.get(qty_field) or 0)
			except Exception:
				pass
	return _sort_dict_rows(acc)
def analytics_century_rows(rows: list[dict]) -> list[dict]:
	acc = {}
	for r in rows:
		if r.get("is_issuer_only"):
			continue
		y = r.get("min_year")
		if not isinstance(y, int):
			label = "Unknown"
			sort = 999999
		else:
			century = ((y - 1) // 100) + 1 if y > 0 else 0
			if century <= 0:
				label = "Unknown"
				sort = 999999
			else:
				label = f"{century}th century"
				sort = century
		info = acc.setdefault(label, {"label": label, "count": 0, "qty": 0, "sort": sort})
		info["count"] += 1
		try:
			info["qty"] += int(r.get("qty") or 0)
		except Exception:
			pass
	return sorted(acc.values(), key=lambda x: (x["sort"], x["label"] if x["label"] == "Unknown" else ""))
KM_Y_OVERRIDE_BY_TYPE_ID = {
	2217: "KM#112.1-112.3",
	571: "KM#143.1, 143.2",
	1244: "KM#142.1, 142.2",
	1323: "KM#215.1-215.3",
	6022: "KM#12.1, 12.2",
	4030: "KM#777.1, 777.2",
	2752: "KM#853, 853a",
}

COUNTRY_ALIAS_TO_ISO3 = {
	"united kingdom": "GBR", "great britain": "GBR", "england": "GBR", "scotland": "GBR", "wales": "GBR",
	"united states": "USA", "united states of america": "USA", "usa": "USA",
	"czech republic": "CZE", "czechia": "CZE", "russia": "RUS", "ussr": None,
	"south korea": "KOR", "north korea": "PRK", "viet nam": "VNM", "vietnam": "VNM",
	"laos": "LAO", "moldova": "MDA", "bolivia": "BOL", "venezuela": "VEN",
	"iran": "IRN", "syria": "SYR", "tanzania": "TZA", "brunei": "BRN",
	"cape verde": "CPV", "cabo verde": "CPV", "myanmar": "MMR", "burma": "MMR",
	"palestine": "PSE", "kosovo": "XKX", "macedonia": "MKD", "north macedonia": "MKD",
	"eswatini": "SWZ", "swaziland": "SWZ", "taiwan": "TWN", "hong kong": "HKG",
	"macao": "MAC", "macau": "MAC", "curaçao": "CUW", "curacao": "CUW",
	"réunion": "REU", "reunion": "REU", "åland": "ALA", "aland": "ALA",
	"french polynesia": "PYF", "new caledonia": "NCL", "guadeloupe": "GLP", "martinique": "MTQ",
	"bermuda": "BMU", "cayman islands": "CYM", "falkland islands": "FLK", "falkland islands malvinas": "FLK", "greenland": "GRL", "faroe islands": "FRO", "guernsey": "GGY", "jersey": "JEY", "tokelau": "TKL",
	"isle of man": "IMN", "gibraltar": "GIB", "puerto rico": "PRI",
	"saint thomas and prince": "STP",   # solo si tienes variantes raras
	"sao tome and principe": "STP",
	"turkey": "TUR",
	"vatican city": "VAT",
	"comoro islands": "COM",
	"rome": "ITA",
	"transnistria": "MDA",
	"somaliland": "SOM",
	# Congo != Democratic Republic of the Congo. Keep both explicit so pycountry
	# fallback does not collapse or misread Numista issuer names.
	"congo": "COG",
	"congo republic of the": "COG",
	"congo, republic of the": "COG",
	"republic of the congo": "COG",
	"democratic republic of the congo": "COD",
	"democratic republic of the congo (1997-date)": "COD",
	"congo democratic republic": "COD",
	"congo democratic republic of the": "COD",
	"congo, democratic republic of the": "COD",
	"congo (democratic republic)": "COD",
	"republic of the congo leopoldville": "COD",
	"republic of the congo léopoldville": "COD",
}
def _normalize_country_name(s: str) -> str:
	s = unicodedata.normalize("NFD", (s or "").strip().lower())
	s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
	s = re.sub(r"\s+", " ", s)
	return s
def modern_country_iso3_from_issuer_root(name: str):
	raw = (name or "").strip()
	if not raw:
		return None
	norm = _normalize_country_name(raw)
	if norm in COUNTRY_ALIAS_TO_ISO3:
		return COUNTRY_ALIAS_TO_ISO3[norm]
	try:
		import pycountry
	except Exception:
		pycountry = None
	if pycountry is not None:
		try:
			c = pycountry.countries.lookup(raw)
			return getattr(c, "alpha_3", None)
		except Exception:
			pass
		for candidate in [raw.replace("&", "and"), raw.split("(")[0].strip(), raw.split(",")[0].strip()]:
			if not candidate:
				continue
			try:
				c = pycountry.countries.lookup(candidate)
				return getattr(c, "alpha_3", None)
			except Exception:
				pass
	return None
def modern_country_iso3_from_row(row: dict):
	"""Resolve a row to a modern ISO-3 country.

	Use the full Numista issuer path, not only the root. This prevents grouped
	issuers such as "British Crown dependencies › Isle of Man" from leaving
	Isle of Man as a false 0-item country in the analytics map/list.
	"""
	path = (row.get("issuer_path") or "").strip()
	parts = [p.strip() for p in path.split(" › ") if p.strip()]
	# Prefer the most specific issuer first, then walk back to the root.
	for part in reversed(parts):
		iso3 = modern_country_iso3_from_issuer_root(part)
		if iso3:
			return iso3
	root = (row.get("issuer_root") or "").strip()
	return modern_country_iso3_from_issuer_root(root)


# Issuers that are fantasy/non-real targets for this collector map.
# They are ignored rather than mapped to a modern country.
EXCLUDED_FANTASY_ISSUER_ROOTS = {
	"Tortuga Island",
}

def historical_issuer_group_from_row(row: dict):
	"""Return (historical_label, modern_iso3_members) for historical issuers that
	should appear as sub-counts under a modern country, rather than as direct items.
	"""
	root = (row.get("issuer_root") or "").strip()
	path = (row.get("issuer_path") or "").strip()
	path_norm = _normalize_country_name(path)
	if root == "Rome" and "roman empire" in path_norm:
		return ("Roman Empire", ["ITA"])
	if root == "Rome":
		return ("Rome", ["ITA"])
	return ("", [])


def analytics_modern_country_map_rows(collection_rows: list[dict], wishlist_rows: list[dict]) -> list[dict]:
	acc = {}
	unmapped = set()
	def _country_name(iso3: str) -> str:
		try:
			c = pycountry.countries.get(alpha_3=iso3)
			return getattr(c, "common_name", None) or getattr(c, "name", "") or iso3
		except Exception:
			return iso3
	def _new_country_info(iso3: str) -> dict:
		return {
			"iso3": iso3,
			"country": _country_name(iso3),
			"types": 0,
			"qty": 0,
			"wishlist": 0,
			"duplicates": 0,
			"issuer_roots": set(),
			"direct_types": 0,
			"direct_qty": 0,
			"historical_issuers": {},
			"group_currency": False,
			"currency_group": "",
		}

	def _add_iso(iso3: str, r: dict, source: str, root: str, group_name: str = "", historical_issuer: str = "") -> None:
		if not iso3:
			return
		info = acc.setdefault(iso3, _new_country_info(iso3))
		info["issuer_roots"].add(root)
		if group_name or GROUP_CURRENCY_BY_ISO3.get(iso3):
			info["group_currency"] = True
			info["currency_group"] = group_name or GROUP_CURRENCY_BY_ISO3.get(iso3, "")
		if source == "collection":
			if r.get("is_issuer_only"):
				return
			try:
				qty = int(r.get("qty") or 0)
			except Exception:
				qty = 0
			info["types"] += 1
			info["qty"] += qty
			info["duplicates"] += max(qty - 1, 0)
			if historical_issuer:
				hist = info["historical_issuers"].setdefault(historical_issuer, {"types": 0, "qty": 0, "duplicates": 0})
				hist["types"] += 1
				hist["qty"] += qty
				hist["duplicates"] += max(qty - 1, 0)
			else:
				info["direct_types"] += 1
				info["direct_qty"] += qty
		else:
			if r.get("is_issuer_only"):
				return
			info["wishlist"] += 1
	for rows, source in ((collection_rows, "collection"), (wishlist_rows, "wishlist")):
		for r in rows:
			root = (r.get("issuer_root") or "Unknown").strip() or "Unknown"
			if root in EXCLUDED_FANTASY_ISSUER_ROOTS:
				continue
			regional = REGIONAL_CURRENCY_ISSUER_GROUPS.get(root)
			if regional:
				group_name, members = regional
				for iso3 in members:
					_add_iso(iso3, r, source, root, group_name)
				continue
			path_hist_label, path_hist_members = historical_issuer_group_from_row(r)
			if path_hist_members:
				for iso3 in path_hist_members:
					_add_iso(iso3, r, source, root, historical_issuer=path_hist_label)
				continue
			historical_members = HISTORICAL_ISSUER_MODERN_GROUPS.get(root)
			if historical_members:
				for iso3 in historical_members:
					_add_iso(iso3, r, source, root, historical_issuer=root)
				continue
			iso3 = modern_country_iso3_from_row(r)
			if not iso3:
				unmapped.add(root)
				continue
			_add_iso(iso3, r, source, root)
	if unmapped:
		print("[MAP DEBUG] Issuer roots not mapped to ISO-3:")
		for root in sorted(unmapped):
			print(" -", root)
		print("[MAP DEBUG] Total unmapped:", len(unmapped))
		print()
	rows_out = []
	for info in acc.values():
		item = dict(info)
		item["issuer_roots"] = sorted(info.get("issuer_roots") or [])
		hist = info.get("historical_issuers") or {}
		item["historical_issuers"] = [
			{"issuer": k, "types": int(v.get("types") or 0), "qty": int(v.get("qty") or 0), "duplicates": int(v.get("duplicates") or 0)}
			for k, v in sorted(hist.items())
		]
		rows_out.append(item)
	return sorted(rows_out, key=lambda x: (-x["types"], -x["qty"], x["country"].lower()))


# Countries/territories shown as "no own currency / no own coin target" in Analytics.
# Collector-oriented and deliberately conservative: do not include places that
# issue their own named coins, NCLT, or clear post-1900 local coinage.
NO_OWN_CURRENCY_ISO3 = {
	# No separate coin target under the collector rules used for the map.
	# These are gray only when the mapped item count is 0.
	"ALA",  # Aland Islands
	"ASM",  # American Samoa
	"ATA",  # Antarctica
	"BES",  # Bonaire, Sint Eustatius and Saba
	"BLM",  # Saint Barthelemy
	"BVT",  # Bouvet Island
	"CCK",  # Cocos (Keeling) Islands - local tokens/exonumia only for this purpose
	"CXR",  # Christmas Island - fantasy issues only for this purpose
	"GUF",  # French Guiana - local coins too old for the chosen cutoff
	"GLP",  # Guadeloupe - local coins too old for the chosen cutoff
	"GUM",  # Guam
	"HMD",  # Heard Island and McDonald Islands
	"MAF",  # Saint Martin, French part
	"MNP",  # Northern Mariana Islands
	"MTQ",  # Martinique - local coins too old for the chosen cutoff
	"MYT",  # Mayotte
	"FSM",  # Micronesia, Federated States of - fantasy issues only for this purpose
	"NFK",  # Norfolk Island - no coin target under these rules
	"NIU",  # Niue - excluded by preference despite legal-tender/NCLT style issues
	"PRI",  # Puerto Rico
	"PSE",  # Palestine, State of - modern fantasy issues excluded; British Palestine separate
	"SJM",  # Svalbard and Jan Mayen - company/exonumia style target excluded
	"UMI",  # United States Minor Outlying Islands
	"VIR",  # U.S. Virgin Islands
	"XKX",  # Kosovo
	"ESH",  # Western Sahara - fantasy issues excluded
}

# Shared/group currency members. These are still valid map targets; when you own
# an item, the country keeps the numeric color and gets a distinct outline.
GROUP_CURRENCY_GROUPS = {
	"West African CFA franc": {"BEN", "BFA", "CIV", "GNB", "MLI", "NER", "SEN", "TGO"},
	"Central African CFA franc": {"CMR", "CAF", "TCD", "COG", "GNQ", "GAB"},
	"Eastern Caribbean dollar": {"AIA", "ATG", "DMA", "GRD", "MSR", "KNA", "LCA", "VCT"},
	"CFP franc": {"PYF", "NCL", "WLF"},
	"Caribbean guilder": {"CUW", "SXM"},
}
GROUP_CURRENCY_BY_ISO3 = {
	iso3: group_name
	for group_name, iso3s in GROUP_CURRENCY_GROUPS.items()
	for iso3 in iso3s
}
REGIONAL_CURRENCY_ISSUER_GROUPS = {
	"Western African States": ("West African CFA franc", GROUP_CURRENCY_GROUPS["West African CFA franc"]),
	"Central African States": ("Central African CFA franc", GROUP_CURRENCY_GROUPS["Central African CFA franc"]),
	"Eastern Caribbean States": ("Eastern Caribbean dollar", GROUP_CURRENCY_GROUPS["Eastern Caribbean dollar"]),
}

# Historical/multi-territory issuers expanded to modern ISO-3 map targets.
# These are not styled as shared-currency groups; they only prevent historical
# issuer rows from being dropped from the modern-country coverage map.
HISTORICAL_ISSUER_MODERN_GROUPS = {
	"British West Africa": {"GMB", "GHA", "NGA", "SLE"},
	"French West Africa": {"BEN", "BFA", "CIV", "GIN", "MLI", "MRT", "NER", "SEN"},
	"French India": {"IND"},
	"Rhodesia and Nyasaland": {"MWI", "ZMB", "ZWE"},
	"Czechoslovakia": {"CZE", "SVK"},
	"Yugoslavia": {"BIH", "HRV", "MKD", "MNE", "SRB", "SVN", "XKX"},
}

def analytics_all_modern_countries() -> list[dict]:
	"""Modern ISO countries/territories used to show 0-item rows in Analytics."""
	rows = []
	for c in getattr(pycountry, "countries", []):
		iso3 = getattr(c, "alpha_3", "")
		name = getattr(c, "common_name", None) or getattr(c, "name", "")
		if iso3 and name:
			group_name = GROUP_CURRENCY_BY_ISO3.get(iso3, "")
			rows.append({
				"iso3": iso3,
				"country": name,
				"no_own_currency": iso3 in NO_OWN_CURRENCY_ISO3,
				"group_currency": bool(group_name),
				"currency_group": group_name,
			})
	# Non-ISO user-facing map/list entries used elsewhere in the script.
	for iso3, name in (("XKX", "Kosovo"),):
		group_name = GROUP_CURRENCY_BY_ISO3.get(iso3, "")
		rows.append({
			"iso3": iso3,
			"country": name,
			"no_own_currency": iso3 in NO_OWN_CURRENCY_ISO3,
			"group_currency": bool(group_name),
			"currency_group": group_name,
		})
	return sorted(rows, key=lambda x: x["country"].lower())

def render_bar_rows(rows: list[dict], label_key: str = "label", value_key: str = "count", max_rows: int = 8, value_fmt=None) -> str:
	rows = rows[:max_rows]
	if not rows:
		return "<div class='miniEmpty'>No data</div>"
	max_v = max((r.get(value_key) or 0) for r in rows) or 1
	parts = []
	for r in rows:
		label = html_escape(str(r.get(label_key) or "Unknown"))
		val = r.get(value_key) or 0
		disp = value_fmt(val) if value_fmt else str(val)
		width = max(6, round((val / max_v) * 100)) if max_v else 6
		parts.append(
			f"<div class='barRow'><div class='barTop'><span class='barLabel'>{label}</span><span class='barValue'>{html_escape(str(disp))}</span></div><div class='barTrack'><div class='barFill' style='width:{width}%'></div></div></div>"
		)
	return "".join(parts)
def build_inline_plotly_loader() -> str:
	return "<script>" + get_plotlyjs() + "</script>"
def render_combined_app(collection_rows: list[dict], wishlist_rows: list[dict], out_html: str, collection_total_items: int, collection_total_types: int, wishlist_total_types: int, chile_date_runs: Optional[List[dict]] = None) -> None:
	def mode_markup(rows: list[dict], prefix: str, title: str, total_items: int, total_types: int, *, include_stored: bool, default_sort: str):
		sec_types = defaultdict(int)
		sec_items = defaultdict(int)
		mobile_sections = {}
		for r in rows:
			sec = r.get("issuer_path") or ""
			if not sec:
				continue
			if sec not in mobile_sections:
				mobile_sections[sec] = {
					"flag_url": r.get("flag_url", ""),
					"items": [],
				}
			mobile_sections[sec]["items"].append(r)
			if not r.get("is_issuer_only"):
				sec_types[sec] += 1
				try:
					sec_items[sec] += int(r.get("qty") or 0)
				except Exception:
					pass
		continents = sorted({(r.get('continent') or 'Unknown') for r in rows}, key=lambda s: s.lower())
		cont_map = {}
		for r in rows:
			c = (r.get('continent') or 'Unknown')
			ir = (r.get('issuer_root') or '').strip()
			if ir:
				cont_map.setdefault(c, set()).add(ir)
		cont_map = {k: sorted(list(v), key=lambda s: s.lower()) for k, v in cont_map.items()}
		unique_countries = len({(r.get('issuer_root') or '').strip() for r in rows if (r.get('issuer_root') or '').strip()})
		duplicates_count = sum(max(int(r.get('qty') or 0) - 1, 0) for r in rows if not r.get('is_issuer_only'))
		replicas_count = sum(1 for r in rows if r.get('is_replica'))
		on_transit_count = sum(1 for r in rows if r.get('is_on_transit'))
		issuer_only_count = sum(1 for r in rows if r.get('is_issuer_only'))
		html = []
		html.append(f"<section class='modeShell' id='{prefix}-mode' data-prefix='{prefix}'>")
		html.append(f"<div class='modeHeader compactModeHeader'><h2>{html_escape(title)}</h2><p class='summary'>Total items: {total_items} &nbsp;|&nbsp; Total types: {total_types}</p></div>")
		html.append(f"<div class='mobileToolbar'><button id='{prefix}-mobileFilterToggle' type='button'>Filters</button><button id='{prefix}-mobileClearBtn' type='button'>Clear</button><span class='mobileResults' id='{prefix}-mobileResults'>0 shown</span></div>")
		html.append(f"<div class='filters' id='{prefix}-filtersPanel'><div class='filtersGrid'>")
		continent_opts = "".join(
			f"<option value='{html_escape(c)}'>{html_escape(c)}</option>"
			for c in continents
		)
		html.append(f"<div class='fCell fCont'><label for='{prefix}-continentFilter'>Continent</label><select id='{prefix}-continentFilter'><option value='ALL'>All</option>{continent_opts}</select></div>")
		html.append(f"<div class='fCell fIssuerBox'><label>Countries</label><div id='{prefix}-issuerBox' class='issuerBox compactIssuerBox'></div></div>")
		html.append(f"<div class='fCell fCoin'><label class='labelWithHelp' for='{prefix}-coinSearch'>Search <span class='helpBubble' tabindex='0' aria-label='Search help'>?<span class='helpPopover'><b>Search help</b><br/>Use <code>&amp;</code> for AND, <code>,</code> or <code>|</code> for OR, <code>!</code> for NOT.<br/><br/>Examples:<br/><code>country:usa,country:uk</code><br/><code>year:1950-1970</code><br/><code>diameter:17-20</code><br/><code>grade:XF</code><br/><code>!replica</code></span></span></label><input id='{prefix}-coinSearch' type='text' placeholder='country:chile & year:1950-1970 · diameter:17-20 · !replica · grade:XF' /></div>")
		html.append(f"<div class='fCell fSort'><label for='{prefix}-sortSel'>Sort</label><select id='{prefix}-sortSel'>")
		if include_stored:
			html.append(f"<option value='none'{' selected' if default_sort=='none' else ''}>Default (Numista)</option>")
			html.append(f"<option value='face'{' selected' if default_sort=='face' else ''}>Face value</option><option value='type'{' selected' if default_sort=='type' else ''}>Type</option><option value='ref'{' selected' if default_sort=='ref' else ''}>Reference (KM/Y)</option><option value='date'{' selected' if default_sort=='date' else ''}>Date</option>")
		else:
			html.append(f"<option value='face'{' selected' if default_sort=='face' else ''}>Face value</option><option value='type'{' selected' if default_sort=='type' else ''}>Type</option><option value='ref'{' selected' if default_sort=='ref' else ''}>Reference (KM/Y)</option><option value='date'{' selected' if default_sort=='date' else ''}>Date</option>")
		html.append("</select></div>")
		if include_stored:
			obj_types = sorted({(r.get('object_type') or '').strip() for r in rows if (r.get('object_type') or '').strip()}, key=lambda s: s.lower())
			cats = sorted({(r.get('category') or '').strip() for r in rows if (r.get('category') or '').strip()}, key=lambda s: s.lower())
			obj_type_opts = "".join(
				f"<option value='{html_escape(o)}'>{html_escape(o)}</option>"
				for o in obj_types
			)
			cat_opts = "".join(
				f"<option value='{html_escape(c)}'>{html_escape(c)}</option>"
				for c in cats
			)
			html.append(f"<div class='fCell fObject'><label for='{prefix}-objTypeFilter'>Object</label><select id='{prefix}-objTypeFilter'><option value='ALL'>All</option>{obj_type_opts}</select></div>")
			html.append(f"<div class='fCell fCategory'><label for='{prefix}-catFilter'>Category</label><select id='{prefix}-catFilter'><option value='ALL'>All</option>{cat_opts}</select></div>")
		else:
			html.append(f"<div class='fCell fObject'><label for='{prefix}-objTypeFilter'>Object</label><select id='{prefix}-objTypeFilter'><option value='ALL'>All</option></select></div>")
			html.append(f"<div class='fCell fCategory'><label for='{prefix}-catFilter'>Category</label><select id='{prefix}-catFilter'><option value='ALL'>All</option></select></div>")
		html.append(f"<div class='fCell fCountrySearch'><label for='{prefix}-issuerSearch'>Country search</label><input id='{prefix}-issuerSearch' type='text' placeholder='Search country...' /></div>")
		html.append(f"<div class='fCell fClear'><button id='{prefix}-clearAll' type='button'>Clear</button></div>")
		html.append(f"<div class='fCell fView'><select id='{prefix}-viewModeSel' class='viewModeSel' title='View mode'><option value='grid'>Grid view</option><option value='list'>List view</option></select></div>")
		html.append(f"<div class='fCell fExport'><button id='{prefix}-exportCsv' class='exportBtn primarySoft' type='button'>Export CSV</button></div>")
		html.append(f"<div class='fRight'><div class='rightTop compactStats'><div class='metrics' id='{prefix}-filterMetrics'></div><div class='metrics' id='{prefix}-countryCount'></div></div><div class='rightChips'><div id='{prefix}-activeChips' class='chips'></div></div><div class='rightStats'><div id='{prefix}-liveStats' class='live-stats'></div></div></div>")
		html.append("</div></div>")
		html.append(f"<div id='{prefix}-stickyFilterBar' class='stickyFilterBar'></div>")
		html.append(f"<div id='{prefix}-emptyState' class='emptyState'><h3>No coins match the current filters</h3><p>Try clearing one or more filters, or broaden the text search.</p><button id='{prefix}-emptyClearBtn' type='button'>Clear filters</button></div>")

		current_section = None
		current_currency = None
		section_open = False
		currency_open = False
		for r in rows:
			section = r['issuer_path']
			section_flag = r.get('flag_url', '')
			if section != current_section:
				if currency_open:
					html.append("</div>")
					currency_open = False
				if section_open:
					html.append("</div>")
					section_open = False
				current_section = section
				current_currency = None
				types_n = sec_types.get(section, 0)
				items_n = sec_items.get(section, 0)
				count_html = f"<span class='secCount'>({items_n} items · {types_n} types)</span>" if (types_n or items_n) else ""
				header_inner = "<span class='chev'>▾</span>"
				if section_flag:
					header_inner += f"<img src='{html_escape(section_flag)}' style='width:20px;height:14px;object-fit:contain;vertical-align:middle;margin-right:8px'/>{html_escape(section)}{count_html}"
				else:
					header_inner += f"{html_escape(section)}{count_html}"
				sec_id = re.sub(r"[^a-zA-Z0-9]+", "_", section)[:80]
				html.append(f"<h2 class='sectionHeader desktopSectionHeader' data-sec='{html_escape(prefix + '_' + sec_id)}'>{header_inner}</h2><div class='sectionBody desktopSectionBody' data-secbody='{html_escape(prefix + '_' + sec_id)}'>")
				section_open = True
			if r['currency'] != current_currency:
				if currency_open:
					html.append("</div>")
					currency_open = False
				current_currency = r['currency']
				html.append(f"<h3>{html_escape(current_currency)}</h3><div class='grid'>")
				currency_open = True
			imgs=[]
			if r.get('obv'):
				imgs.append(f"<img src='{html_escape(r['obv'])}' alt='obverse' loading='lazy' decoding='async' />")
			if r.get('rev'):
				imgs.append(f"<img src='{html_escape(r['rev'])}' alt='reverse' loading='lazy' decoding='async' />")
			imgs_html=''.join(imgs)
			main_label=r.get('label','')
			ref_label=r.get('km_y','')
			title_main, title_extra = split_title_main_extra(main_label, r.get('title_full',''))
			title_line=' '.join(b for b in [main_label, ref_label] if b)
			badge_mode = 'wishlist' if prefix == 'wishlist' else 'collection'
			qty_badge = build_card_badges(r, badge_mode)
			stored_cb = ''
			years_greg_list = r.get('years_greg_list') or (r.get('years_list') or [])
			years_raw_list = r.get('years_raw_list') or []
			years_attr=','.join(str(y) for y in years_greg_list if isinstance(y,int))
			years_raw_attr=','.join(str(y) for y in years_raw_list if isinstance(y,int))
			miny=r.get('min_year') or ''
			maxy=r.get('max_year') or ''
			search_blob=' '.join([(title_line or ''),(r.get('title_full') or ''),(r.get('currency') or ''),(r.get('km_y') or ''),(r.get('year_str') or ''),(r.get('grade_str') or ''),' '.join(str(y) for y in years_raw_list if isinstance(y,int)),' '.join(str(y) for y in years_greg_list if isinstance(y,int)),(r.get('issuer_root') or ''),(r.get('issuer_path') or ''),(r.get('issuer_search_es') or ''),(r.get('composition') or ''),str(r.get('weight_g') or '')]).lower()
			objtype_attr = f"data-objtype='{html_escape(r.get('object_type',''))}' "
			cat_attr = f"data-category='{html_escape(r.get('category',''))}' "
			html.append(f"<div class='card' data-mode='{prefix}' data-continent='{html_escape(r.get('continent','Unknown'))}' data-issuerroot='{html_escape(r.get('issuer_root',''))}' {objtype_attr}{cat_attr}data-qty='{r.get('qty',0)}' data-facevalue='{html_escape(str(r.get('numeric_value','')))}' data-facesort='{html_escape(str(r.get('face_sort_value', r.get('numeric_value',''))))}' data-title='{html_escape(title_line)}' data-ref='{html_escape(r.get('km_y',''))}' data-typeid='{html_escape(str(r.get('type_id','')))}' data-issuerpath='{html_escape(r.get('issuer_path',''))}' data-currency='{html_escape(r.get('currency',''))}' data-yearstr='{html_escape(r.get('year_str',''))}' data-grade='{html_escape(r.get('grade_str',''))}' data-url='{html_escape(r.get('url',''))}' data-isissueronly='{1 if r.get('is_issuer_only') else 0}' data-duptype='{1 if r.get('duplicate_type') else 0}' data-dupyear='{1 if r.get('duplicate_year') else 0}' data-years='{html_escape(years_attr)}' data-yearsraw='{html_escape(years_raw_attr)}' data-minyear='{html_escape(str(miny))}' data-maxyear='{html_escape(str(maxy))}' data-sizemm='{html_escape(str(r.get('size_mm','')))}' data-weightg='{html_escape(str(r.get('weight_g','')))}' data-composition='{html_escape(str(r.get('composition','')))}' data-search='{html_escape(search_blob)}'>")
			html.append("<div class='cardTop'><div></div><div class='cardBadges'>" + qty_badge + stored_cb + "</div></div>")
			html.append(f"<div class='imgs'>{imgs_html}</div>")
			main_html = html_escape(title_main or main_label)
			if r.get('url'):
				main_html = f"<a href='{html_escape(r['url'])}' target='_blank' rel='noopener'>{main_html}</a>"
			extra_html = f"<span class='valueTitleExtra'>{html_escape(title_extra)}</span>" if title_extra else ""
			html.append("<div class='cardMain'><div class='valueLine'><p class='valueTitle'>" + main_html + extra_html + "</p><div class='refText'>" + html_escape(ref_label) + "</div></div>")
			if r.get('currency'):
				html.append(f"<p class='subTitle' title='{html_escape(title_line)}'>{html_escape(r.get('currency',''))}</p>")
			meta1=[]; meta2=[]
			if r.get('year_str'): meta1.append(f"Years: {html_escape(r['year_str'])}")
			if r.get('grade_str'): meta1.append(f"Grade: {html_escape(r['grade_str'])}")
			if r.get('size_mm') is not None:
				try: meta2.append(f"{float(r['size_mm']):g} mm")
				except Exception: pass
			if r.get('composition'): meta2.append(html_escape(r['composition']))
			if meta1: html.append("<p class='metaRow'>" + " · ".join(meta1) + "</p>")
			if meta2: html.append("<p class='metaRow'>" + " · ".join(meta2) + "</p>")
			html.append("</div></div>")
		if currency_open: html.append("</div>")
		if section_open: html.append("</div>")

		for section, payload in mobile_sections.items():
			types_n = sec_types.get(section, 0)
			items_n = sec_items.get(section, 0)
			count_html = f"<span class='secCount'>({items_n} items · {types_n} types)</span>" if (types_n or items_n) else ""
			header_inner = "<span class='chev'>▸</span>"
			if payload.get("flag_url"):
				header_inner += f"<img src='{html_escape(payload['flag_url'])}' style='width:20px;height:14px;object-fit:contain;vertical-align:middle;margin-right:8px'/>{html_escape(section)}{count_html}"
			else:
				header_inner += f"{html_escape(section)}{count_html}"
			sec_id = re.sub(r"[^a-zA-Z0-9]+", "_", section)[:80]
			html.append(f"<h2 class='sectionHeader mobileLazyHeader' data-sec='{html_escape(prefix + '_' + sec_id)}' data-sectionkey='{html_escape(section)}'>{header_inner}</h2><div class='sectionBody mobileLazyBody hidden' data-secbody='{html_escape(prefix + '_' + sec_id)}' data-sectionkey='{html_escape(section)}' data-rendered='0'></div>")

		html.append(f"<script>window.__NUMISTA_CONT_MAPS = window.__NUMISTA_CONT_MAPS || {{}}; window.__NUMISTA_CONT_MAPS['{prefix}'] = {json.dumps(cont_map)};</script>")
		html.append(f"<script>window.__NUMISTA_MOBILE_SECTION_DATA = window.__NUMISTA_MOBILE_SECTION_DATA || {{}}; window.__NUMISTA_MOBILE_SECTION_DATA['{prefix}'] = {json.dumps(mobile_sections, ensure_ascii=False)};</script>")
		html.append("</section>")
		return '\n'.join(html)

	analytics_rows_json = json.dumps(collection_rows)
	chile_date_runs_json = json.dumps(chile_date_runs or [])
	map_rows_json = json.dumps(analytics_modern_country_map_rows(collection_rows, []))
	all_countries_json = json.dumps(analytics_all_modern_countries())
	css = """
	:root { --bg:#f5f7fb; --surface:#ffffff; --surface-soft:#f9fbff; --text:#162033; --muted:#5b6474; --border:#d9e0ec; --accent:#2a52be; --accent-soft:#eef3ff; --shadow:0 10px 24px rgba(21,34,61,.08); }
	* { box-sizing: border-box; }
	html, body { max-width:100%; overflow-x:hidden; }
	body { font-family: Arial, sans-serif; margin: 20px; background: var(--bg); color: var(--text); }
	img, canvas, svg { max-width:100%; }
	a { color: var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
	.appHeader { display:flex; justify-content:space-between; gap:18px; align-items:center; margin-bottom:14px; max-width:100%; }
	.appHeaderLead{display:flex;align-items:center;gap:10px;min-width:0;} .portalHome{width:40px;height:40px;display:inline-grid;place-items:center;flex:0 0 auto;border:1px solid var(--border);border-radius:12px;background:#fff;color:var(--accent);box-shadow:0 3px 10px rgba(21,34,61,.10);text-decoration:none;transition:transform .15s ease,border-color .15s ease;} .portalHome:hover{transform:translateY(-1px);border-color:var(--accent);text-decoration:none;} .portalHome svg{width:20px;height:20px;}
	.compactAppHeader{align-items:center;} .brandTitle{display:flex;align-items:center;gap:10px;min-width:0;} .brandTitle h1{margin:0;font-size:31px;line-height:1.05;} .titleCoin{width:38px;height:38px;border-radius:999px;object-fit:cover;border:1px solid var(--border);box-shadow:0 3px 10px rgba(21,34,61,.13);background:#fff;} .appSub { color:var(--muted); margin:0; }
	.topTabs { display:flex; gap:8px; flex-wrap:wrap; margin:0; align-items:center; }
	.topTabs button { border:1px solid var(--border); background:#fff; border-radius:999px; padding:9px 14px; cursor:pointer; font-weight:bold; }
	.topTabs button.active { background:var(--accent); color:#fff; border-color:var(--accent); }
	.appPanel { display:none; max-width:100%; min-width:0; } .appPanel.active { display:block; }
	.summary { color: var(--muted); margin: 0 0 14px; }
	.analyticsStrip { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:12px; margin:0 0 16px; }
	.kpiCard { background: var(--surface); border: 1px solid var(--border); border-radius:14px; padding:12px 14px; box-shadow: var(--shadow); }
	.kpiLabel { font-size:12px; color:var(--muted); margin-bottom:6px; } .kpiValue { font-size:22px; font-weight:bold; } .kpiSub { font-size:12px; color:var(--muted); margin-top:4px; }
	h2 { border-bottom:2px solid var(--accent); padding-bottom:6px; margin-top:18px; cursor:pointer; user-select:none; }.modeHeader h2{margin:0;cursor:default;}.compactModeHeader{display:flex;align-items:baseline;gap:14px;margin:6px 0 8px;min-width:0;}.compactModeHeader .summary{margin:0;font-size:13px;}.compactAnalyticsHeader{display:flex;align-items:center;gap:16px;margin:8px 0 12px;border-bottom:2px solid var(--accent);padding-bottom:8px;}.compactAnalyticsHeader h2{border:0;padding:0;margin:0;}.compactAnalyticsHeader .analyticsFilterBar{margin:0 0 0 auto;}
	h2 .secCount { font-weight:normal; color:var(--muted); font-size:12px; margin-left:10px; } h2 .chev { font-weight:normal; color:#666; margin-right:8px; }
	h3 { margin:12px 0 10px; color:#1f3f94; } .sectionBody { margin-top:8px; }
	.grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(250px,1fr)); gap:16px; margin:12px 0 22px; max-width:100%; }
	.card { border:1px solid var(--border); border-radius:14px; padding:12px; background:var(--surface); box-shadow:var(--shadow); }
	.cardTop { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px; } .cardBadges { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }
	.imgs { display:flex; gap:10px; align-items:center; justify-content:center; margin-bottom:10px; } .imgs img { width:112px; border-radius:8px; border:1px solid #eef1f6; background:#fff; aspect-ratio:1/1; object-fit:contain; }
	.cardMain { display:flex; flex-direction:column; gap:6px; } .valueLine { display:flex; align-items:flex-start; justify-content:space-between; gap:8px; } .valueTitle { font-size:16px; line-height:1.2; font-weight:bold; margin:0; display:flex; flex-wrap:wrap; align-items:baseline; gap:6px; } .valueTitleExtra { font-size:.78em; font-weight:600; color:inherit; } .refText { font-size:12px; line-height:1.2; color:var(--muted); text-align:right; min-width:fit-content; } .subTitle { font-size:13px; line-height:1.3; color:var(--text); margin:0; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; } .metaRow { font-size:12px; color:var(--muted); margin:0; }
	.badge { display:inline-block; font-size:11px; padding:3px 8px; border-radius:999px; background:var(--accent-soft); color:var(--accent); } .badge.badge-muted{ background:#eef1f6; color:#465066; } .badge.badge-alert{ background:#fff1e7; color:#a24b00; } .badge.badge-dup-type{ background:#ffe9e9; color:#a00000; } .badge.badge-dup-year{ background:#fff4cc; color:#7a4b00; } .badge.badge-wish{ background:#eef3ff; color:#2a52be; } .badge.badge-exo{ background:#f1ecff; color:#5b3db8; } .badge.badge-transit{ background:#e8f7ef; color:#087443; }
	.filters { margin:6px 0 12px; background:var(--surface-soft); padding:8px 10px; border:1px solid var(--border); border-radius:14px; box-shadow:var(--shadow); } .filtersGrid{ display:grid; grid-template-columns: 0.9fr 1.25fr 1.65fr 0.95fr 0.95fr; grid-template-rows:auto auto auto; column-gap:10px; row-gap:6px; align-items:start; max-width:100%; } .filtersGrid > *{ min-width:0; }
	.fCell label{ display:block; font-size:12px; color:#333; margin-bottom:3px; } .fCell select, .fCell input[type=text]{ width:100%; box-sizing:border-box; padding:7px 9px; font-size:12px; border:1px solid var(--border); border-radius:10px; background:#fff; } .labelWithHelp{display:flex !important; align-items:center; gap:6px;} .helpBubble{position:relative; display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:999px; border:1px solid var(--border); background:#fff; color:var(--accent); font-size:11px; font-weight:bold; cursor:help; user-select:none;} .helpPopover{display:none; position:absolute; left:50%; top:22px; transform:translateX(-50%); width:260px; max-width:70vw; padding:10px 12px; border:1px solid var(--border); border-radius:12px; background:#fff; color:var(--text); box-shadow:0 10px 24px rgba(21,34,61,.16); z-index:99999; font-size:12px; line-height:1.35; font-weight:normal;} .helpPopover code{background:#f3f6fb; border:1px solid #e5ebf5; border-radius:5px; padding:1px 4px;} .helpBubble:hover .helpPopover, .helpBubble:focus .helpPopover{display:block;} .fCell button, .mobileToolbar button, .emptyState button { padding:8px 11px; font-size:12px; border-radius:10px; border:1px solid var(--border); background:#fff; cursor:pointer; } .exportBtn{margin-top:6px;width:100%;} .exportBtn.primarySoft{background:var(--accent-soft);color:var(--accent);font-weight:bold;}
	.fCont { grid-column:1; grid-row:1; } .fCountrySearch{ grid-column:1; grid-row:2; } .fIssuerBox{ grid-column:2; grid-row:1 / span 2; align-self:start; padding-top:0; box-sizing:border-box; } .fCoin{ grid-column:3; grid-row:1; } .fClear{ grid-column:3; grid-row:2; align-self:end; padding-top:0;} .fSort{ grid-column:4; grid-row:1; } .fView{ grid-column:4; grid-row:2; align-self:end; } .fObject{ grid-column:5; grid-row:1; } .fCategory{ grid-column:5; grid-row:2; } .fExport{ grid-column:5; grid-row:3; align-self:end; } .fRight{ display:none!important; }
	.rightTop{ font-size:12px; color:var(--muted); text-align:right; line-height:1.35; } .chips{ display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; } .chip{ display:inline-flex; align-items:center; gap:8px; padding:4px 10px; border-radius:999px; border:1px solid var(--border); background:#f6f8ff; font-size:12px; color:#223; } .chip button{ border:0; background:transparent; cursor:pointer; font-size:14px; line-height:1; padding:0; color:#445; } .live-stats{ font-size:12px; color:var(--muted); display:flex; gap:14px; align-items:center; justify-content:flex-end; } .live-stats b { color:var(--text); }
	.issuerBox{ border:1px solid var(--border); border-radius:10px; padding:5px 6px; width:100%; height:74px; overflow-y:auto; overflow-x:hidden; background:#fff; box-sizing:border-box; } .issuerItem{ display:flex; align-items:center; gap:6px; font-size:12px; margin:2px 0; } .issuerItem input{ margin:0; width:14px; height:14px; }
	.hidden{ display:none !important; } .emptyState{ display:none; margin:14px 0 20px; padding:20px; border:1px dashed #b8c4d9; border-radius:14px; background:#fff; text-align:center; color:var(--muted); } .emptyState h3{ margin:0 0 8px; color:var(--text); } .emptyState p{ margin:0 0 12px; } .mobileToolbar{ display:none; gap:8px; align-items:center; margin:0 0 12px; } .mobileToolbar .mobileResults{ margin-left:auto; font-size:12px; color:var(--muted); background:#fff; border:1px solid var(--border); border-radius:999px; padding:7px 10px; }
	.mobileLazyHeader, .mobileLazyBody{ display:none; }
	.analyticsFilterBar{display:flex;gap:8px;align-items:center;margin:0;}.analyticsFilterBar select{padding:7px 10px;border:1px solid var(--border);border-radius:10px;background:#fff;}.analyticsSectionTitle{margin:18px 0 10px;border:0;padding:0;cursor:default;font-size:18px;color:var(--text);} .analyticsSectionSub{display:none;} .analyticsGrid { display:grid; gap:16px; margin-bottom:16px; } .analyticsGrid2 { grid-template-columns:1.35fr .65fr; } .analyticsGrid3 { grid-template-columns:repeat(3, minmax(0, 1fr)); }.pieGrid{align-items:stretch;}.pieChart{height:240px;min-height:240px;} .geographyGrid .analyticsPanel{min-width:0;} .analyticsPanel { background:#fff; border:1px solid var(--border); border-radius:14px; padding:14px; box-shadow:var(--shadow); } .analyticsPanel h3{ margin-top:0; margin-bottom:12px; } table.analyticsTable { width:100%; border-collapse:collapse; font-size:13px; } .analyticsTable th,.analyticsTable td{ padding:8px 6px; border-bottom:1px solid #edf1f7; text-align:left; } .analyticsTable th:last-child,.analyticsTable td:last-child{ text-align:right; } .barList{ display:flex; flex-direction:column; gap:10px; } .barRow{ display:flex; flex-direction:column; gap:5px; } .barTop{ display:flex; justify-content:space-between; gap:10px; font-size:12px; } .barLabel{ color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .barValue{ color:var(--muted); min-width:36px; text-align:right; } .barTrack{ height:8px; background:#eef2f8; border-radius:999px; overflow:hidden; } .barFill{ height:100%; background:linear-gradient(90deg, #7aa2ff 0%, #4f7fe8 100%); border-radius:999px; } .miniEmpty{ font-size:12px; color:var(--muted); } .countrySubcount{ color:var(--muted); font-size:11px; line-height:1.35; margin-top:2px; } .mapToolbar{ display:flex; gap:8px; align-items:center; margin:0 0 10px; font-size:12px; color:var(--muted); } .mapToolbar select{ padding:6px 8px; border:1px solid var(--border); border-radius:10px; background:#fff; } .countryMap{ width:100%; min-height:410px; } .timelineChart{width:100%;height:280px;min-height:280px;} .timelinePanel{margin-bottom:16px;} .timelineHint{font-size:12px;color:var(--muted);margin:6px 0 0;} .mapNote{ display:none; } .zeroCountries{ margin-top:12px; font-size:13px; } .zeroCountries summary{ cursor:pointer; color:var(--text); font-weight:bold; } .zeroCountriesList{ columns:2; margin-top:8px; color:var(--muted); } .zeroCountriesList div{ break-inside:avoid; padding:2px 0; } .analyticsMapMobileNote{ display:none; font-size:12px; color:var(--muted); margin:0 0 10px; }

	/* v1_41 UX additions */
	.stickyFilterBar{display:flex;position:sticky;top:8px;z-index:450;margin:0 0 10px;padding:7px 10px;border:1px solid var(--border);border-radius:999px;background:rgba(255,255,255,.94);backdrop-filter:blur(8px);box-shadow:0 6px 16px rgba(21,34,61,.08);font-size:12px;color:var(--muted);align-items:center;gap:10px;justify-content:space-between;min-height:30px;}
	.stickyFilterBar.is-visible{display:flex;}.stickyFilterBar b{color:var(--text);}.stickyFilterBar button{border:1px solid var(--border);border-radius:999px;background:#fff;padding:4px 9px;font-size:11px;cursor:pointer;}.stickyFilterText{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}.stickyFilterMain{display:flex;align-items:center;gap:8px;min-width:0;flex:1;}.stickyFilterChips{display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0;}.stickyFilterChips .chip{padding:3px 8px;background:#f6f8ff;}.stickyFilterChips .chip button{border:0;background:transparent;padding:0;font-size:14px;line-height:1;}
	.viewModeSel{width:100%;box-sizing:border-box;padding:7px 9px;font-size:12px;border:1px solid var(--border);border-radius:10px;background:#fff;}
	.fView .viewModeSel{margin-top:0;} .fExport .exportBtn{width:100%;}
	.issuerGroup{margin:4px 0 7px;}.issuerGroupTitle{font-size:11px;color:var(--muted);font-weight:bold;margin:5px 0 3px;padding-top:4px;border-top:1px solid #edf1f7;}.issuerGroup:first-child .issuerGroupTitle{border-top:0;padding-top:0;}.issuerCount{margin-left:auto;color:var(--muted);font-size:11px;}.issuerItem{padding:1px 0;}.issuerItem span:first-of-type{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}.issuerItem input{flex:0 0 auto;}
	.analyticsNavRow{cursor:pointer;}.analyticsNavRow:hover td{background:#f7f9ff;}.clickHint{font-size:11px;color:var(--muted);margin:-4px 0 8px;}.pieChart,.countryMap,.timelineChart{cursor:default;}
	.dqGrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:14px;}.dqCard{background:#fff;border:1px solid var(--border);border-radius:14px;padding:12px 14px;box-shadow:var(--shadow);}.dqCard .kpiValue{font-size:24px;}.dqSection{margin:14px 0;}.dqRow{cursor:pointer;}.dqRow:hover td{background:#f7f9ff;}.dqSmall{font-size:12px;color:var(--muted);}
	.chileToolbar{display:flex;gap:8px;align-items:center;margin:8px 0 12px;}.chileToolbar input{max-width:280px;padding:8px 10px;border:1px solid var(--border);border-radius:10px;}.chileRunTable{table-layout:fixed;}.chileRunTable td,.chileRunTable th{vertical-align:top;}.chileRunTable th:nth-child(1),.chileRunTable td:nth-child(1){width:92px;}.chileRunTable th:nth-child(2),.chileRunTable td:nth-child(2){width:78px;}.chileRunTable th:nth-child(3),.chileRunTable td:nth-child(3){width:32%;}.chileRunTable th:nth-child(4),.chileRunTable td:nth-child(4){width:112px;white-space:nowrap;}.chileRunTable th:nth-child(5),.chileRunTable td:nth-child(5){width:23%;}.chileRunTable th:nth-child(6),.chileRunTable td:nth-child(6){width:13%;}.chileRunTable th:nth-child(7),.chileRunTable td:nth-child(7){width:116px;white-space:nowrap;text-align:right;}.chileThumbs{display:flex;gap:4px;align-items:center;justify-content:flex-start;width:86px;}.chileThumb{width:38px;height:38px;object-fit:contain;border-radius:6px;border:1px solid #eef1f6;background:#fff;display:block;flex:0 0 auto;}.chileRunGroup td{text-align:left!important;font-weight:600;color:var(--text);background:#fbfcff;}.missingYears{color:#a24b00;}.ownedYears{color:#087443;}.runOk{color:#087443;font-weight:bold;}.runPartial{color:#a24b00;font-weight:bold;}
.chileMobileRefImg{display:none;}
.chileMobileThumb{width:38px;height:38px;object-fit:contain;border-radius:6px;border:1px solid #eef1f6;background:#fff;margin-top:4px;display:block;}
@media (max-width: 700px){
  #panel-chile{overflow-x:hidden;}
  #panel-chile .analyticsPanel{padding:10px;overflow-x:hidden;}
  #panel-chile .summary{font-size:13px;line-height:1.25;}
  #panel-chile .chileKpis{grid-template-columns:1fr;gap:8px;}
  #panel-chile .dqCard{padding:10px 12px;}
  .chileToolbar{display:grid;grid-template-columns:1fr 120px;gap:8px;margin:10px 0;}
  .chileToolbar input,.chileToolbar select{width:100%;min-width:0;box-sizing:border-box;}
  .chileRunTable{display:block;width:100%;table-layout:auto;overflow-x:hidden;}
  .chileRunTable thead{display:none;}
  .chileRunTable tbody{display:block;width:100%;}
  .chileRunTable tr.chileRunGroup{display:block;width:100%;}
  .chileRunTable tr.chileRunGroup td{display:block;width:auto!important;padding:8px 10px;font-size:14px;}
  .chileRunTable tr.chileRunRow{display:grid;grid-template-columns:82px minmax(0,1fr) 92px;column-gap:10px;align-items:start;width:100%;box-sizing:border-box;padding:10px 0;border-bottom:1px solid #e8edf5;}
  .chileRunTable tr.chileRunRow td{display:block!important;width:auto!important;border:0!important;padding:0!important;min-width:0;box-sizing:border-box;font-size:14px;line-height:1.22;}
  .chileImageCell{grid-column:1;grid-row:1;}
  .chileTypeCell{grid-column:2;grid-row:1;font-weight:500;}
  .chileMissingCell{grid-column:3;grid-row:1;text-align:left;white-space:normal!important;overflow-wrap:anywhere;}
  .chileRefCell,.chileRangeCell,.chileOwnedCell,.chileStatusCell,.chileComposition{display:none!important;}
  .chileMobileRefImg{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:13px;line-height:1.1;}
  .chileThumbs{display:none!important;}
  .chileMobileThumb{width:42px;height:42px;}
  .chileMissingCell:empty::after{content:'—';color:#a24b00;}
  .chileMissingCell{font-weight:600;}
}

	.modeShell.view-list .grid{display:block;margin:8px 0 18px;}.modeShell.view-list .card{display:grid;grid-template-columns:86px minmax(0,1fr) auto;gap:10px;align-items:center;margin:7px 0;padding:8px 10px;border-radius:12px;}.modeShell.view-list .cardTop{grid-column:3;grid-row:1;margin:0;align-self:start;}.modeShell.view-list .cardTop>div:first-child{display:none;}.modeShell.view-list .imgs{grid-column:1;grid-row:1;margin:0;justify-content:flex-start;gap:4px;}.modeShell.view-list .imgs img{width:38px;height:38px;border-radius:6px;}.modeShell.view-list .cardMain{grid-column:2;grid-row:1;gap:3px;min-width:0;}.modeShell.view-list .valueTitle{font-size:14px;}.modeShell.view-list .subTitle,.modeShell.view-list .metaRow{font-size:11px;}.modeShell.view-list .refText{font-size:11px;}
	@media (max-width:700px){.stickyFilterBar{display:none!important;}.dqGrid{grid-template-columns:1fr;}.modeShell.view-list .card{display:block;}.modeShell.view-list .imgs{justify-content:center;margin-bottom:8px;}.modeShell.view-list .imgs img{width:calc(50% - 3px);height:auto;}}

	@media (hover:hover) and (pointer:fine) { .card { transition: transform .15s ease, box-shadow .15s ease; } .card:hover { transform: translateY(-3px); box-shadow: 0 8px 22px rgba(0,0,0,.10); } }
	@media (max-width:1100px){ .filtersGrid{ grid-template-columns:1.05fr 1.35fr 1.55fr 1.05fr 1.05fr; } .fCont{ grid-column:1; grid-row:1; } .fIssuerBox{ grid-column:2; grid-row:1 / span 2; } .fCoin{ grid-column:3; grid-row:1; } .fClear{ grid-column:3; grid-row:2; align-self:end; padding-top:0; } .fSort{ grid-column:4; grid-row:1; } .fView{ grid-column:4; grid-row:2; } .fObject{ grid-column:5; grid-row:1; } .fCountrySearch{ grid-column:1; grid-row:2; } .fCategory{ grid-column:5; grid-row:2; } .fExport{ grid-column:5; grid-row:3; } .fRight{ display:none!important; } .analyticsGrid, .analyticsGrid2, .analyticsGrid3 { grid-template-columns:1fr; } }
	@media (max-width:700px){ body{ margin:12px; padding-right:64px; } .compactAppHeader{align-items:flex-start;gap:10px;flex-wrap:wrap;} .brandTitle h1{font-size:25px;} .titleCoin{width:32px;height:32px;} .topTabs{width:100%;} .compactModeHeader{display:block;} .compactAnalyticsHeader{display:block;} .compactAnalyticsHeader .analyticsFilterBar{margin-top:8px;}  .analyticsStrip{ grid-template-columns:repeat(2, minmax(0,1fr)); } .mobileToolbar{ display:flex; } .filters{ display:none; } .filters.mobile-open{ display:block; } .desktopSectionHeader, .desktopSectionBody{ display:none !important; } .mobileLazyHeader, .mobileLazyBody{ display:block; } .filtersGrid{ grid-template-columns:repeat(2, minmax(0,1fr)); row-gap:8px; } .fCont{ grid-column:1; grid-row:1; } .fCoin{ grid-column:2; grid-row:1; } .fCountrySearch{ grid-column:1; grid-row:2; } .fYear{ grid-column:2; grid-row:2; } .fSort{ grid-column:1; grid-row:3; } .fObject{ grid-column:2; grid-row:3; } .fCategory{ grid-column:1; grid-row:4; } .fClear{ grid-column:2; grid-row:4; align-self:end; padding-top:18px; } .fView{ grid-column:1; grid-row:5; } .fExport{ grid-column:2; grid-row:5; } .fIssuerBox{ grid-column:1 / span 2; grid-row:6; padding-top:0; } .fRight{ grid-column:1 / span 2; grid-row:7; align-items:flex-start; } .rightTop, .chips, .live-stats{ justify-content:flex-start; text-align:left; } .rightTop{ width:100%; } .rightTop .metrics + .metrics{ margin-top:4px; } .grid{ grid-template-columns:repeat(2, minmax(0,1fr)); gap:10px; } .card{ padding:10px; } .imgs{ gap:6px; margin-bottom:8px; } .imgs img{ width:calc(50% - 3px); max-width:none; } .valueTitle{ font-size:14px; } .valueTitleExtra{ font-size:.8em; } .subTitle{ font-size:12px; } .refText, .metaRow{ font-size:11px; } .topTabs{ position:sticky; top:8px; background:rgba(245,247,251,.96); padding:8px 0; z-index:500; } .mobileNavDock{ display:flex; position:fixed; right:10px; bottom:18px; z-index:10000; flex-direction:column; gap:8px; align-items:stretch; } .mobileNavDock select{ width:44px; height:120px; writing-mode:vertical-rl; text-orientation:mixed; border-radius:14px; padding:8px 4px; } .mobileNavDock button{ width:44px; height:44px; border-radius:999px; } }
	"""
	html = []
	html.append("<!doctype html><html><head><meta charset='utf-8' /><meta name='viewport' content='width=device-width, initial-scale=1' />")
	html.append("<title>Javignacio Coin Collection</title><link rel='icon' href='" + HEADER_COIN_ICON_URL + "' />")
	html.append(f"<style>{css}</style>{build_inline_plotly_loader()}</head><body>")
	html.append("<div class='appHeader compactAppHeader'><div class='appHeaderLead'><a class='portalHome' href='https://javignacio.github.io/collections-page/' aria-label='Back to Collections portal'><svg viewBox='0 0 24 24' aria-hidden='true'><path d='M3 10.8 12 3l9 7.8v9.7a.5.5 0 0 1-.5.5H15v-6H9v6H3.5a.5.5 0 0 1-.5-.5v-9.7Z' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/></svg></a><div class='brandTitle'><h1>Javignacio Coin Collection</h1><img class='titleCoin' src='" + HEADER_COIN_REVERSE_URL + "' alt='1926 Chile 20 Pesos reverse' /></div></div><div class='topTabs' id='topTabs'><button class='active' data-panel='panel-collection' type='button'>Collection</button><button data-panel='panel-chile' type='button'>Chile coins</button><button data-panel='panel-wishlist' type='button'>Wishlist</button><button data-panel='panel-analytics' type='button'>Analytics</button><button data-panel='panel-data-quality' type='button'>Data Quality</button></div></div>")
	html.append("<section id='panel-collection' class='appPanel active'>" + mode_markup(collection_rows, 'collection', "Collection", collection_total_items, collection_total_types, include_stored=True, default_sort='none') + "</section>")
	html.append("<section id='panel-chile' class='appPanel'><div class='modeHeader compactAnalyticsHeader'><h2>Chile coins</h2></div><div id='chileRunsPanel' class='analyticsPanel'></div></section>")
	html.append("<section id='panel-wishlist' class='appPanel'>" + mode_markup(wishlist_rows, 'wishlist', "Wishlist", wishlist_total_types, wishlist_total_types, include_stored=False, default_sort='face') + "</section>")
	html.append("<section id='panel-analytics' class='appPanel'>")
	html.append("<div class='modeHeader compactAnalyticsHeader'><h2>Analytics</h2><div class='analyticsFilterBar'><label for='analyticsCategoryFilter'>Category</label><select id='analyticsCategoryFilter'><option value='all'>All</option><option value='coins'>Coins</option><option value='banknotes'>Bank notes</option><option value='replica'>Replica</option><option value='on_transit'>On transit</option><option value='exonumia'>Exonumia</option></select></div></div>")
	html.append("<div class='analyticsGrid analyticsGrid2 geographyGrid'>")
	html.append("<div class='analyticsPanel analyticsMapPanel'><h3>Modern-country map</h3><p class='clickHint'>Click a country to filter Collection.</p><div class='mapToolbar'><label for='mapMetric'>Metric</label><select id='mapMetric'><option value='types'>Types</option><option value='qty'>Qty</option></select></div><div id='countryMap' class='countryMap'></div><details class='zeroCountries'><summary id='zeroCountriesSummary'>Countries with 0 items</summary><div id='zeroCountriesList' class='zeroCountriesList'></div></details><details class='zeroCountries'><summary id='noOwnCurrencySummary'>Countries/territories without own currency</summary><div id='noOwnCurrencyList' class='zeroCountriesList'></div></details><details class='zeroCountries'><summary id='groupCurrencySummary'>Countries/territories using group/shared currency</summary><div id='groupCurrencyList' class='zeroCountriesList'></div></details><p class='analyticsMapMobileNote'>Map hidden on mobile.</p></div>")
	html.append("<div class='analyticsPanel'><h3>Top countries</h3><p class='clickHint'>Click a row to filter Collection.</p><table class='analyticsTable compactTable'><thead><tr><th>Country</th><th>Types</th><th>Qty</th></tr></thead><tbody id='analyticsTopCountriesBody'></tbody></table></div>")
	html.append("</div>")
	html.append("<div class='analyticsPanel timelinePanel'><h3>Collection timeline</h3><div class='mapToolbar'><label for='timelineBucket'>Group</label><select id='timelineBucket'><option value='year' selected>Year</option><option value='decade'>Decade</option></select><label for='timelineMetric'>Metric</label><select id='timelineMetric'><option value='qty' selected>Qty</option><option value='types'>Types</option></select></div><div id='collectionTimeline' class='timelineChart'></div><p class='timelineHint'>Click a bar to open Collection filtered by that year or decade.</p></div>")
	html.append("<div class='analyticsGrid analyticsGrid3 pieGrid'>")
	html.append("<div class='analyticsPanel'><h3>Grade</h3><p class='clickHint'>Click a segment to filter Collection.</p><div class='pieChart' id='analyticsGradePie'></div></div>")
	html.append("<div class='analyticsPanel'><h3>Object type</h3><p class='clickHint'>Click a segment to filter Collection.</p><div class='pieChart' id='analyticsObjectTypePie'></div></div>")
	html.append("<div class='analyticsPanel'><h3>Continent</h3><p class='clickHint'>Click a segment to filter Collection.</p><div class='pieChart' id='analyticsContinentPie'></div></div>")
	html.append("<div class='analyticsPanel'><h3>Century</h3><p class='clickHint'>Click a segment to filter Collection.</p><div class='pieChart' id='analyticsCenturyPie'></div></div>")
	html.append("<div class='analyticsPanel analyticsPhysicalPanel'><h3>Composition</h3><p class='clickHint'>Click a segment to filter Collection.</p><div class='pieChart' id='analyticsCompositionPie'></div></div>")
	html.append("<div class='analyticsPanel analyticsPhysicalPanel'><h3>Diameter</h3><p class='clickHint'>Click a segment to filter Collection.</p><div class='pieChart' id='analyticsSizePie'></div></div>")
	html.append("<div class='analyticsPanel analyticsPhysicalPanel'><h3>Weight</h3><p class='clickHint'>Click a segment to filter Collection.</p><div class='pieChart' id='analyticsWeightPie'></div></div>")
	html.append("</div>")
	html.append(f"<script>window.__NUMISTA_ANALYTICS_ROWS = {analytics_rows_json}; window.__NUMISTA_CHILE_DATE_RUNS = {chile_date_runs_json}; window.__NUMISTA_MAP_ROWS = {map_rows_json}; window.__NUMISTA_ALL_COUNTRIES = {all_countries_json};</script>")

	html.append("</section>")
	html.append("<section id='panel-data-quality' class='appPanel'><div class='modeHeader compactAnalyticsHeader'><h2>Data Quality</h2></div><div id='dataQualityPanel'></div></section>")
	html.append("<div class='mobileNavDock'><select id='mobileJumpIssuer'><option value=''>Jump</option></select><button id='backToTop'>↑</button></div>")
	html.append(r"""<script>
(function(){
  if (typeof CSS === 'undefined') { window.CSS = {}; }
  if (typeof CSS.escape !== 'function') { CSS.escape = function(s){ return String(s).replace(/[^a-zA-Z0-9_-]/g, '\$&'); }; }
  const tabs = Array.from(document.querySelectorAll('#topTabs button'));
  function openPanel(panelId){
	tabs.forEach(b => b.classList.toggle('active', b.dataset.panel === panelId));
	document.querySelectorAll('.appPanel').forEach(panel => panel.classList.toggle('active', panel.id === panelId));
	if (panelId === 'panel-analytics' && typeof renderCountryMap === 'function') {
	  setTimeout(() => renderAnalytics(), 0);
	}
	if (panelId === 'panel-data-quality' && typeof renderDataQuality === 'function') {
	  setTimeout(() => renderDataQuality(), 0);
	}
	if (panelId === 'panel-chile' && typeof renderChileRuns === 'function') {
	  setTimeout(() => renderChileRuns(), 0);
	}
	window.scrollTo({top:0, behavior:'smooth'});
	refreshMobileJump();
  }
  tabs.forEach(btn => btn.addEventListener('click', () => openPanel(btn.dataset.panel)));
  const backBtn = document.getElementById('backToTop');
  const mobileJumpEl = document.getElementById('mobileJumpIssuer');
  function refreshMobileJump(){
    if (!mobileJumpEl) return;
    const activePanel = document.querySelector('.appPanel.active');
    const headers = activePanel ? Array.from(activePanel.querySelectorAll('.sectionHeader:not(.hidden)')) : [];
    mobileJumpEl.innerHTML = "<option value=''>Jump</option>" + headers.map((h, i) => `<option value="${i}">${(h.textContent || '').replace(/[▾▸]/g,'').trim()}</option>`).join('');
    mobileJumpEl._headers = headers;
    mobileJumpEl.value = '';
  }
  window.addEventListener('scroll', () => { backBtn.style.display = window.scrollY > 400 ? 'block' : 'none'; });
  backBtn.addEventListener('click', () => window.scrollTo({top:0, behavior:'smooth'}));
  if (mobileJumpEl) mobileJumpEl.addEventListener('change', () => { const idx = parseInt(mobileJumpEl.value || '', 10); const headers = mobileJumpEl._headers || []; if (!Number.isNaN(idx) && headers[idx]) headers[idx].scrollIntoView({behavior:'smooth', block:'start'}); mobileJumpEl.value=''; });
  function debounce(fn, wait){ let t = null; return function(){ const args = arguments; clearTimeout(t); t = setTimeout(() => fn.apply(this, args), wait); }; }
  function normText(s){ return (s || '').toString().toLowerCase().replace(/[øØ]/g, 'o').replace(/[æÆ]/g, 'ae').replace(/[œŒ]/g, 'oe').replace(/[åÅ]/g, 'a').replace(/[ðÐ]/g, 'd').replace(/[þÞ]/g, 'th').replace(/[łŁ]/g, 'l').replace(/ß/g, 'ss').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9# ]+/g, ' ').replace(/\s+/g, ' ').trim(); }
  function fuzzyMatchNorm(queryNorm, haystackNorm){ if (!queryNorm) return true; return queryNorm.split(' ').every(tok => haystackNorm.includes(tok.replace(/^([a-z])\s*#?\s*(\d+)$/i, '$1#$2'))); }
  function parseYearFilter(input){ if (!input) return null; const parts = input.split(',').map(x=>x.trim()).filter(Boolean); const rules=[]; for (const p of parts){ if (p.includes('-') || p.includes('–')){ const seg=p.split(/[-–]/).map(x=>parseInt(x.trim(),10)); if (seg.length===2 && !isNaN(seg[0]) && !isNaN(seg[1])) rules.push({type:'range', min:Math.min(seg[0],seg[1]), max:Math.max(seg[0],seg[1])}); } else { const y=parseInt(p,10); if (!isNaN(y)) rules.push({type:'single', year:y}); } } return rules.length ? rules : null; }
  function parseDiameterFilter(input){ if (!input) return null; const parts=input.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean); const rules=[]; for (const p of parts){ if (p === 'unknown' || p === '?'){ rules.push({type:'unknown'}); continue; } let m=p.match(/^>=\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'min', value:parseFloat(m[1]), inclusive:true}); continue; } m=p.match(/^>\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'min', value:parseFloat(m[1]), inclusive:false}); continue; } m=p.match(/^<=\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'max', value:parseFloat(m[1]), inclusive:true}); continue; } m=p.match(/^<\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'max', value:parseFloat(m[1]), inclusive:false}); continue; } m=p.match(/^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$/); if (m){ const a=parseFloat(m[1]), b=parseFloat(m[2]); rules.push({type:'range', min:Math.min(a,b), max:Math.max(a,b)}); continue; } m=p.match(/^(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'exact', value:parseFloat(m[1])}); continue; } } return rules.length ? rules : null; }
  function diameterMatches(sizeValue, rules){ if (!rules) return true; const n=Number.parseFloat(sizeValue); const hasValue=Number.isFinite(n); return rules.some(rule => { if (rule.type === 'unknown') return !hasValue; if (!hasValue) return false; if (rule.type === 'exact') return Math.abs(n - rule.value) < 0.11; if (rule.type === 'range') return n >= rule.min && n <= rule.max; if (rule.type === 'min') return rule.inclusive ? n >= rule.value : n > rule.value; if (rule.type === 'max') return rule.inclusive ? n <= rule.value : n < rule.value; return false; }); }
	function parseDiameterFilter(input){ if (!input) return null; const parts=input.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean); const rules=[]; for (const p of parts){ if (p === 'unknown' || p === '?'){ rules.push({type:'unknown'}); continue; } let m=p.match(/^>=\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'min', value:parseFloat(m[1]), inclusive:true}); continue; } m=p.match(/^>\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'min', value:parseFloat(m[1]), inclusive:false}); continue; } m=p.match(/^<=\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'max', value:parseFloat(m[1]), inclusive:true}); continue; } m=p.match(/^<\s*(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'max', value:parseFloat(m[1]), inclusive:false}); continue; } m=p.match(/^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$/); if (m){ const a=parseFloat(m[1]), b=parseFloat(m[2]); rules.push({type:'range', min:Math.min(a,b), max:Math.max(a,b)}); continue; } m=p.match(/^(\d+(?:\.\d+)?)$/); if (m){ rules.push({type:'exact', value:parseFloat(m[1])}); continue; } } return rules.length ? rules : null; }
	function diameterMatches(sizeValue, rules){ if (!rules) return true; const n=Number.parseFloat(sizeValue); const hasValue=Number.isFinite(n); return rules.some(rule => { if (rule.type === 'unknown') return !hasValue; if (!hasValue) return false; if (rule.type === 'exact') return Math.abs(n - rule.value) < 0.11; if (rule.type === 'range') return n >= rule.min && n <= rule.max; if (rule.type === 'min') return rule.inclusive ? n >= rule.value : n > rule.value; if (rule.type === 'max') return rule.inclusive ? n <= rule.value : n < rule.value; return false; }); }
  function parseRefRaw(raw){ raw=(raw||'').toUpperCase().trim(); if (!raw) return {catRank:99, num:1e30, suffix:'', raw:''}; const matches=Array.from(raw.matchAll(/\b(KM|Y)\s*#?\s*([0-9]+(?:\.[0-9]+)?)([A-Z]*)\b/g)); if (!matches.length) return {catRank:99, num:1e30, suffix:'', raw}; const rankMap={KM:0,Y:1}; const parsed=matches.map(m => ({catRank:rankMap[m[1]] ?? 99, num:parseFloat(m[2]), suffix:(m[3]||''), raw:m[0]})); parsed.sort((a,b)=>a.catRank-b.catRank || a.num-b.num || a.suffix.localeCompare(b.suffix) || a.raw.localeCompare(b.raw)); return parsed[0]; }
  function parseSortNumber(value, fallback){ const n = Number.parseFloat(value ?? ''); return Number.isFinite(n) ? n : fallback; }
  function analyticsCategoryForRow(row){
    const category = (row.category || '').toString().toLowerCase();
    const objectType = (row.object_type || '').toString().toLowerCase();
    const label = (row.label || '').toString().toLowerCase();
    const joined = [category, objectType, label].join(' | ');
    if (row.is_on_transit || category === 'on transit') return 'on_transit';
    if (row.is_replica || category === 'replica') return 'replica';
    if (category === 'exonumia' || /token|medal|medalet|jeton|fare/.test(joined)) return 'exonumia';
    if (/banknote|bank note|paper money|note|billet|billete/.test(joined) || ((row.size_mm||0) >= 80 && !row.weight_g)) return 'banknotes';
    return 'coins';
  }
  function modernCountryIso3FromRow(row){
    const path = String((row && row.issuer_path) || '').trim();
    const parts = path ? path.split('›').map(p => p.trim()).filter(Boolean) : [];
    for (let i = parts.length - 1; i >= 0; i--){
      const iso3 = modernCountryIso3FromIssuerRoot(parts[i]);
      if (iso3) return iso3;
    }
    const root = String((row && row.issuer_root) || '').trim();
    return modernCountryIso3FromIssuerRoot(root);
  }
  function analyticsCountryRows(rows){
    const allCountries = Array.isArray(window.__NUMISTA_ALL_COUNTRIES) ? window.__NUMISTA_ALL_COUNTRIES : [];
    const countryByIso3 = new Map(allCountries.map(c => [String(c.iso3 || '').toUpperCase(), c]));
    const groupByIso3 = new Map(allCountries.filter(c => c.currency_group).map(c => [String(c.iso3 || '').toUpperCase(), String(c.currency_group || '')]));
    const regionalGroups = {
      'western african states': {name:'West African CFA franc', members:['BEN','BFA','CIV','GNB','MLI','NER','SEN','TGO']},
      'central african states': {name:'Central African CFA franc', members:['CMR','CAF','TCD','COG','GNQ','GAB']},
      'eastern caribbean states': {name:'Eastern Caribbean dollar', members:['AIA','ATG','DMA','GRD','MSR','KNA','LCA','VCT']},
    };
    const historicalGroups = {
      'british west africa': {name:'British West Africa', members:['GMB','GHA','NGA','SLE']},
      'french west africa': {name:'French West Africa', members:['BEN','BFA','CIV','GIN','MLI','MRT','NER','SEN']},
      'french india': {name:'French India', members:['IND']},
      'rhodesia and nyasaland': {name:'Rhodesia and Nyasaland', members:['MWI','ZMB','ZWE']},
      'roman empire': {name:'Roman Empire', members:['ITA']},
      'rome': {name:'Rome', members:['ITA']},
      'czechoslovakia': {name:'Czechoslovakia', members:['CZE','SVK']},
      'yugoslavia': {name:'Yugoslavia', members:['BIH','HRV','MKD','MNE','SRB','SVN','XKX']},
    };
    const excludedFantasyIssuers = new Set(['tortuga island']);
    function regionalGroupFromRow(row){
      const parts = [row && row.issuer_root, row && row.issuer_path].map(v => normText(v || '')).filter(Boolean);
      for (const key of Object.keys(regionalGroups)){
        if (parts.some(v => v.includes(key))) return regionalGroups[key];
      }
      return null;
    }
    function historicalGroupFromRow(row){
      const parts = [row && row.issuer_root, row && row.issuer_path].map(v => normText(v || '')).filter(Boolean);
      for (const key of Object.keys(historicalGroups)){
        if (parts.some(v => v.includes(key))) return historicalGroups[key];
      }
      return null;
    }
    const acc = new Map();
    function newCountryItem(iso3, countryInfo, fallbackName){
      return {iso3, country: countryInfo.country || fallbackName, types:0, qty:0, duplicates:0, issuer_roots:new Set(), direct_types:0, direct_qty:0, historical_issuers:new Map(), group_currency:false, currency_group:''};
    }
    function addIsoRow(iso3, r, opts){
      iso3 = String(iso3 || '').toUpperCase();
      if (!iso3) return;
      const root = (r.issuer_root || 'Unknown').trim() || 'Unknown';
      const countryInfo = countryByIso3.get(iso3) || {};
      if (!acc.has(iso3)) acc.set(iso3, newCountryItem(iso3, countryInfo, root));
      const item = acc.get(iso3);
      item.issuer_roots.add(root);
      const groupName = (opts && opts.currency_group) || groupByIso3.get(iso3) || '';
      const histName = (opts && opts.historical_issuer) || '';
      if (groupName){ item.group_currency = true; item.currency_group = groupName; }
      if (r.is_issuer_only) return;
      const qty = parseInt(r.qty || 0, 10) || 0;
      item.types += 1;
      item.qty += qty;
      item.duplicates += Math.max(qty - 1, 0);
      if (histName){
        if (!item.historical_issuers.has(histName)) item.historical_issuers.set(histName, {issuer:histName, types:0, qty:0, duplicates:0});
        const hist = item.historical_issuers.get(histName);
        hist.types += 1;
        hist.qty += qty;
        hist.duplicates += Math.max(qty - 1, 0);
      } else {
        item.direct_types += 1;
        item.direct_qty += qty;
      }
    }
    rows.forEach(r => {
      const rootNorm = normText((r && r.issuer_root) || '');
      if (excludedFantasyIssuers.has(rootNorm)) return;
      const group = regionalGroupFromRow(r);
      if (group){
        group.members.forEach(iso3 => addIsoRow(iso3, r, {currency_group:group.name}));
        return;
      }
      const histGroup = historicalGroupFromRow(r);
      if (histGroup){
        histGroup.members.forEach(iso3 => addIsoRow(iso3, r, {historical_issuer:histGroup.name}));
        return;
      }
      const iso3 = modernCountryIso3FromRow(r) || (r.modern_country_iso3 || '').toString().trim();
      addIsoRow(iso3, r, null);
    });
    return Array.from(acc.values()).map(x => ({
      ...x,
      issuer_roots:Array.from(x.issuer_roots).sort(),
      historical_issuers:Array.from(x.historical_issuers.values()).sort((a,b)=>String(a.issuer).localeCompare(String(b.issuer)))
    })).sort((a,b)=>(b.types-a.types)||(b.qty-a.qty)||String(a.country).localeCompare(String(b.country)));
  }
  function renderZeroCountryList(countryRows){
    const allCountries = Array.isArray(window.__NUMISTA_ALL_COUNTRIES) ? window.__NUMISTA_ALL_COUNTRIES : [];
    const rowByIso3 = new Map((countryRows || []).map(r => [String(r.iso3 || '').toUpperCase(), r]));
    const hasItems = iso3 => { const r = rowByIso3.get(String(iso3 || '').toUpperCase()); return !!r && (Number(r.types || 0) > 0 || Number(r.qty || 0) > 0); };
    const itemLabel = iso3 => { const r = rowByIso3.get(String(iso3 || '').toUpperCase()); return r ? `${Number(r.types || 0)} / ${Number(r.qty || 0)}` : '0'; };
    const esc = value => String(value || '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[s]));
    const noOwn = allCountries.filter(c => Boolean(c.no_own_currency));
    const groupCurrency = allCountries.filter(c => Boolean(c.group_currency));
    const missing = allCountries.filter(c => !Boolean(c.no_own_currency) && !Boolean(c.group_currency) && !hasItems(c.iso3));
    const summary = document.getElementById('zeroCountriesSummary');
    const list = document.getElementById('zeroCountriesList');
    const noOwnSummary = document.getElementById('noOwnCurrencySummary');
    const noOwnList = document.getElementById('noOwnCurrencyList');
    const groupSummary = document.getElementById('groupCurrencySummary');
    const groupList = document.getElementById('groupCurrencyList');
    if (summary) summary.textContent = `Countries with 0 items (${missing.length})`;
    if (list) list.innerHTML = missing.map(c => `<div>${esc(c.country)} <b>0</b></div>`).join('');
    if (noOwnSummary) noOwnSummary.textContent = `Countries/territories without own currency (${noOwn.length})`;
    if (noOwnList) noOwnList.innerHTML = noOwn.map(c => `<div>${esc(c.country)} <b>${itemLabel(c.iso3)}</b></div>`).join('');
    if (groupSummary) groupSummary.textContent = `Countries/territories using group/shared currency (${groupCurrency.length})`;
    if (groupList) groupList.innerHTML = groupCurrency.map(c => `<div>${esc(c.country)} <b>${itemLabel(c.iso3)}</b> <span style="color:var(--muted)">${esc(c.currency_group || '')}</span></div>`).join('');
  }
  function modernCountryIso3FromIssuerRoot(name){
    const aliases = {"united kingdom":"GBR","great britain":"GBR","england":"GBR","scotland":"GBR","wales":"GBR","united states":"USA","united states of america":"USA","usa":"USA","czech republic":"CZE","czechia":"CZE","russia":"RUS","south korea":"KOR","north korea":"PRK","viet nam":"VNM","vietnam":"VNM","laos":"LAO","moldova":"MDA","bolivia":"BOL","venezuela":"VEN","iran":"IRN","syria":"SYR","tanzania":"TZA","brunei":"BRN","cape verde":"CPV","cabo verde":"CPV","myanmar":"MMR","burma":"MMR","palestine":"PSE","kosovo":"XKX","macedonia":"MKD","north macedonia":"MKD","eswatini":"SWZ","swaziland":"SWZ","taiwan":"TWN","hong kong":"HKG","macao":"MAC","macau":"MAC","curaçao":"CUW","curacao":"CUW","réunion":"REU","reunion":"REU","åland":"ALA","aland":"ALA","french polynesia":"PYF","new caledonia":"NCL","guadeloupe":"GLP","martinique":"MTQ","bermuda":"BMU","cayman islands":"CYM","falkland islands":"FLK","falkland islands malvinas":"FLK","greenland":"GRL","faroe islands":"FRO","guernsey":"GGY","jersey":"JEY","tokelau":"TKL","isle of man":"IMN","gibraltar":"GIB","puerto rico":"PRI","saint thomas and prince":"STP","sao tome and principe":"STP","turkey":"TUR","vatican city":"VAT","comoro islands":"COM","rome":"ITA","transnistria":"MDA","somaliland":"SOM","congo":"COG","congo republic of the":"COG","republic of the congo":"COG","democratic republic of the congo":"COD","democratic republic of the congo 1997 date":"COD","congo democratic republic":"COD","congo democratic republic of the":"COD","congo democratic republic of the 1997 date":"COD","republic of the congo leopoldville":"COD"};
    const raw=(name||'').trim(); if(!raw) return null;
    const norm = normText(raw);
    return aliases[norm] || null;
  }
  function renderBarRowsSimple(rows, labelKey, valueKey, maxRows){
    const subset = (rows || []).slice(0, maxRows || 8);
    if (!subset.length) return "<div class='miniEmpty'>No data</div>";
    const maxV = Math.max(...subset.map(r => Number(r[valueKey] || 0)), 1);
    return subset.map(r => { const label = String(r[labelKey] || 'Unknown').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[s])); const val = Number(r[valueKey] || 0); const width = Math.max(6, Math.round((val / maxV) * 100)); return `<div class='barRow'><div class='barTop'><span class='barLabel'>${label}</span><span class='barValue'>${val}</span></div><div class='barTrack'><div class='barFill' style='width:${width}%'></div></div></div>`; }).join('');
  }
  function collapseSmallSlices(rows, labelKey, valueKey, maxSlices){
    const sorted = (rows || []).map(r => ({label:String(r[labelKey] || 'Unknown'), value:Number(r[valueKey] || 0)})).filter(r => r.value > 0).sort((a,b)=>b.value-a.value || a.label.localeCompare(b.label));
    const limit = Math.max(2, maxSlices || 8);
    if (sorted.length <= limit) return sorted;
    const head = sorted.slice(0, limit - 1);
    const other = sorted.slice(limit - 1).reduce((a,r)=>a+r.value,0);
    if (other > 0) head.push({label:'Other', value:other});
    return head;
  }
  function renderPieChart(id, rows, labelKey, valueKey, maxSlices){
    const el = document.getElementById(id);
    if (!el) return;
    const dataRows = collapseSmallSlices(rows, labelKey, valueKey, maxSlices);
    if (!dataRows.length || typeof Plotly === 'undefined'){ el.innerHTML = "<div class='miniEmpty'>No data</div>"; return; }
    el.__pieDataRows = dataRows;
    Plotly.react(el, [{
      type:'pie',
      labels:dataRows.map(r => r.label),
      values:dataRows.map(r => r.value),
      hole:0.44,
      sort:false,
      textinfo:'percent',
      hovertemplate:'%{label}<br>%{value}<br>%{percent}<extra></extra>',
      marker:{line:{color:'#fff', width:1}}
    }], {
      margin:{l:4,r:4,t:4,b:4},
      height:240,
      showlegend:true,
      legend:{orientation:'h', y:-0.05, x:0, font:{size:10}},
      paper_bgcolor:'rgba(0,0,0,0)',
      plot_bgcolor:'rgba(0,0,0,0)'
    }, {responsive:true, displayModeBar:false});
  }
  function groupCounts(rows, labelFn){ const acc = new Map(); rows.forEach(r => { if (r.is_issuer_only) return; const label = labelFn(r) || 'Unknown'; const qty = parseInt(r.qty || 0, 10) || 0; if (!acc.has(label)) acc.set(label, {label, count:0, qty:0}); const item = acc.get(label); item.count += 1; item.qty += qty; }); return Array.from(acc.values()).sort((a,b)=>(b.count-a.count)||(b.qty-a.qty)||String(a.label).localeCompare(String(b.label))); }
  function gradeCounts(rows){
    const acc = new Map();
    rows.forEach(r => {
      if (r.is_issuer_only) return;
      const gc = (r.grade_counts && typeof r.grade_counts === 'object') ? r.grade_counts : null;
      if (gc && Object.keys(gc).length){
        Object.entries(gc).forEach(([grade, rawQty]) => {
          const label = String(grade || '').trim() || 'Ungraded';
          const qty = parseInt(rawQty || 0, 10) || 0;
          if (!acc.has(label)) acc.set(label, {label, count:0, qty:0});
          const item = acc.get(label);
          item.count += 1;
          item.qty += qty;
        });
        return;
      }
      const fallback = String(r.grade_str || '').trim() || 'Ungraded';
      fallback.split(',').map(x => x.trim()).filter(Boolean).forEach(label => {
        const qty = parseInt(r.qty || 0, 10) || 0;
        if (!acc.has(label)) acc.set(label, {label, count:0, qty:0});
        const item = acc.get(label);
        item.count += 1;
        item.qty += qty;
      });
    });
    return Array.from(acc.values()).sort((a,b) => (b.qty-a.qty) || (b.count-a.count) || String(a.label).localeCompare(String(b.label)));
  }
  function sizeBucket(v){ const n = Number(v); if (!Number.isFinite(n) || n < 0) return 'Unknown'; if (n < 15) return '< 15 mm'; if (n < 20) return '15-19.9 mm'; if (n < 25) return '20-24.9 mm'; if (n < 30) return '25-29.9 mm'; if (n < 35) return '30-34.9 mm'; return '35+ mm'; }
  function weightBucket(v){ const n = Number(v); if (!Number.isFinite(n) || n < 0) return 'Unknown'; if (n < 1) return '< 1 g'; if (n < 2.5) return '1-2.49 g'; if (n < 5) return '2.5-4.99 g'; if (n < 10) return '5-9.99 g'; if (n < 20) return '10-19.99 g'; return '20+ g'; }
  function centuryLabel(r){ const y = parseInt(r.min_year, 10); if (!Number.isFinite(y) || y <= 0) return 'Unknown'; return `${Math.floor((y - 1) / 100) + 1}th century`; }
  function analyticsTableRow(prefix, r, mode){ const roots = JSON.stringify(r.issuer_roots || [r.country]); return `<tr class="analyticsNavRow" data-nav-prefix="collection" data-issuer-roots="${roots.replace(/&/g,'&amp;').replace(/"/g,'&quot;')}"><td>${r.country}</td><td>${mode==='dup'?r.duplicates:r.types}</td><td>${r.qty}</td>${mode==='top'?`<td>${r.duplicates}</td>`:''}</tr>`; }
  function timelineYearPairs(row){
    const out = [];
    const qtyMap = (row.year_qty_greg && typeof row.year_qty_greg === 'object') ? row.year_qty_greg : null;
    if (qtyMap && Object.keys(qtyMap).length){
      Object.entries(qtyMap).forEach(([rawYear, rawQty]) => {
        const y = parseInt(rawYear, 10);
        const q = parseInt(rawQty || 0, 10) || 0;
        if (Number.isFinite(y) && q > 0) out.push({year:y, qty:q});
      });
      return out;
    }
    const years = Array.isArray(row.years_greg_list) && row.years_greg_list.length ? row.years_greg_list : (Array.isArray(row.years_list) ? row.years_list : []);
    const cleanYears = Array.from(new Set(years.map(y => parseInt(y, 10)).filter(y => Number.isFinite(y))));
    if (cleanYears.length){
      const rowQty = parseInt(row.qty || 0, 10) || 1;
      cleanYears.forEach(y => out.push({year:y, qty: cleanYears.length === 1 ? rowQty : 1}));
      return out;
    }
    const minY = parseInt(row.min_year, 10);
    const maxY = parseInt(row.max_year, 10);
    if (Number.isFinite(minY) && Number.isFinite(maxY)){
      const rowQty = parseInt(row.qty || 0, 10) || 1;
      if (minY === maxY){
        out.push({year:minY, qty:rowQty});
      } else if (maxY > minY && (maxY - minY) <= 100){
        for (let y = minY; y <= maxY; y++) out.push({year:y, qty:1});
      }
    }
    return out;
  }
  function renderTimeline(rows){
    const el = document.getElementById('collectionTimeline');
    if (!el || typeof Plotly === 'undefined') return;
    const bucketEl = document.getElementById('timelineBucket');
    const metricEl = document.getElementById('timelineMetric');
    const bucket = bucketEl ? bucketEl.value : 'decade';
    const metric = metricEl ? metricEl.value : 'qty';
    const acc = new Map();
    rows.forEach(row => {
      if (row.is_issuer_only) return;
      const touched = new Set();
      timelineYearPairs(row).forEach(pair => {
        const y = pair.year;
        let start = y;
        let end = y;
        let label = String(y);
        if (bucket === 'decade'){
          start = Math.floor(y / 10) * 10;
          end = start + 9;
          label = `${start}s`;
        }
        const key = `${start}-${end}`;
        if (!acc.has(key)) acc.set(key, {label, start, end, qty:0, types:0});
        const item = acc.get(key);
        item.qty += pair.qty;
        if (!touched.has(key)) { item.types += 1; touched.add(key); }
      });
    });
    const dataRows = Array.from(acc.values()).sort((a,b) => a.start - b.start);
    el.__timelineRows = dataRows;
    if (!dataRows.length){ el.innerHTML = "<div class='miniEmpty'>No dated items</div>"; return; }
    const yVals = dataRows.map(r => metric === 'types' ? r.types : r.qty);
    const custom = dataRows.map(r => [r.start, r.end, r.qty, r.types, r.start === r.end ? `year:${r.start}` : `year:${r.start}-${r.end}`]);
    Plotly.react(el, [{
      type:'bar',
      x:dataRows.map(r => r.label),
      y:yVals,
      customdata:custom,
      marker:{line:{color:'#fff', width:0.6}},
      hovertemplate:'%{x}<br>Qty: %{customdata[2]}<br>Types: %{customdata[3]}<br><b>Click to filter</b><extra></extra>'
    }], {
      margin:{l:42,r:12,t:8,b:46},
      height:280,
      xaxis:{title:'', tickangle: bucket === 'year' ? -45 : 0, automargin:true},
      yaxis:{title: metric === 'types' ? 'Types' : 'Qty', rangemode:'tozero'},
      paper_bgcolor:'rgba(0,0,0,0)',
      plot_bgcolor:'rgba(0,0,0,0)'
    }, {responsive:true, displayModeBar:false});
    if (el.removeAllListeners) el.removeAllListeners('plotly_click');
    el.on('plotly_click', evt => {
      const point = evt && evt.points && evt.points[0];
      let start = null, end = null, query = '';
      if (point && point.customdata){
        const cd = Array.isArray(point.customdata) ? point.customdata : [point.customdata];
        start = parseInt(cd[0], 10);
        end = parseInt(cd[1], 10);
        query = String(cd[4] || '');
      }
      if ((!query || !Number.isFinite(start) || !Number.isFinite(end)) && point && Array.isArray(el.__timelineRows)){
        const idx = Number.isInteger(point.pointIndex) ? point.pointIndex : (Number.isInteger(point.pointNumber) ? point.pointNumber : -1);
        const row = idx >= 0 ? el.__timelineRows[idx] : null;
        if (row){
          start = row.start;
          end = row.end;
          query = row.start === row.end ? `year:${row.start}` : `year:${row.start}-${row.end}`;
        }
      }
      if ((!query || !Number.isFinite(start) || !Number.isFinite(end)) && point && point.x != null){
        const label = String(point.x || '').trim();
        const decadeMatch = label.match(/^(-?\d+)s$/);
        const yearMatch = label.match(/^-?\d+$/);
        if (decadeMatch){ start = parseInt(decadeMatch[1], 10); end = start + 9; query = `year:${start}-${end}`; }
        else if (yearMatch){ start = parseInt(label, 10); end = start; query = `year:${start}`; }
      }
      if (query && Number.isFinite(start) && Number.isFinite(end)) {
        if (typeof window.__NUMISTA_APPLY_TIMELINE_FILTER === 'function') window.__NUMISTA_APPLY_TIMELINE_FILTER(start, end, query);
        else navigateToMode('collection', [], query, {start, end});
      }
    });
  }

  function escAttr(value){ return String(value ?? '').replace(/[&<>"]/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[s])); }
  function centuryRangeFromLabel(label){
    const m = String(label || '').match(/^(\d+)(?:st|nd|rd|th) century$/i);
    if (!m) return null;
    const c = parseInt(m[1], 10);
    if (!Number.isFinite(c) || c < 1) return null;
    return {start:(c - 1) * 100 + 1, end:c * 100};
  }
  function queryFromAnalyticsSlice(kind, label){
    label = String(label || '').trim();
    if (!label || label === 'Other' || label === 'Unknown') return '';
    if (kind === 'grade') return `grade:${label}`;
    if (kind === 'object') return `object:${label}`;
    if (kind === 'continent') return `continent:${label}`;
    if (kind === 'composition') return `material:${label}`;
    if (kind === 'century') { const r = centuryRangeFromLabel(label); return r ? `year:${r.start}-${r.end}` : ''; }
    if (kind === 'size') {
      if (label === '< 15 mm') return 'diameter<15';
      if (label === '35+ mm') return 'diameter>=35';
      const m = label.match(/^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?) mm$/); if (m) return `diameter:${m[1]}-${m[2]}`;
    }
    if (kind === 'weight') {
      if (label === '< 1 g') return 'weight<1';
      if (label === '20+ g') return 'weight>=20';
      const m = label.match(/^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?) g$/); if (m) return `weight:${m[1]}-${m[2]}`;
    }
    return label;
  }
  function bindPieNav(id, kind){
    const el = document.getElementById(id);
    if (!el || !el.on) return;
    if (el.__pieNavKind === kind) return;
    el.__pieNavKind = kind;
    el.on('plotly_click', evt => {
      const point = evt && evt.points && evt.points[0];
      const label = point && (point.label || point.x);
      const query = queryFromAnalyticsSlice(kind, label);
      if (query) navigateToMode('collection', [], query);
    });
  }
  function rowUrl(r){ return r && r.url ? `<a href="${escAttr(r.url)}" target="_blank" rel="noopener">N#${escAttr(r.type_id || '')}</a>` : `N#${escAttr(r.type_id || '')}`; }
  function renderDataQuality(){
    const panel = document.getElementById('dataQualityPanel');
    if (!panel) return;
    const rows = (Array.isArray(window.__NUMISTA_ANALYTICS_ROWS) ? window.__NUMISTA_ANALYTICS_ROWS : []).filter(r => !r.is_issuer_only);
    const missingYear = rows.filter(r => !(Array.isArray(r.years_greg_list) && r.years_greg_list.length) && !(Number.isFinite(parseInt(r.min_year,10)) && Number.isFinite(parseInt(r.max_year,10))));
    const missingRef = rows.filter(r => !String(r.km_y || '').trim());
    const missingSize = rows.filter(r => !Number.isFinite(Number(r.size_mm)));
    const missingWeight = rows.filter(r => !Number.isFinite(Number(r.weight_g)));
    const missingMaterial = rows.filter(r => !String(r.composition || '').trim());
    const notOnMap = rows.filter(r => !String(r.modern_country_iso3 || '').trim());
    const dupType = rows.filter(r => r.duplicate_type || (parseInt(r.qty || 0, 10) || 0) > 1);
    const dupYear = rows.filter(r => r.duplicate_year);
    const blocks = [
      {name:'Missing year', rows:missingYear, query:'!year:1-9999'},
      {name:'Missing KM/Y', rows:missingRef, query:'!ref:KM'},
      {name:'Missing size', rows:missingSize, query:''},
      {name:'Missing weight', rows:missingWeight, query:''},
      {name:'Missing material', rows:missingMaterial, query:''},
      {name:'Not on map / excluded', rows:notOnMap, query:''},
      {name:'Duplicate type', rows:dupType, query:''},
      {name:'Duplicate year', rows:dupYear, query:''}
    ];
    function sampleTable(title, arr){
      const body = arr.slice(0, 80).map(r => `<tr class='dqRow' data-query='${escAttr(r.type_id ? 'ref:' + (r.km_y || r.type_id) : '')}'><td>${rowUrl(r)}</td><td>${escAttr(r.issuer_root || '')}</td><td>${escAttr(r.label || r.title_full || '')}</td><td>${escAttr(r.year_str || '')}</td></tr>`).join('');
      return `<div class='dqSection'><h3>${title} <span class='secCount'>(${arr.length})</span></h3><table class='analyticsTable'><thead><tr><th>Ref</th><th>Issuer</th><th>Type</th><th>Years</th></tr></thead><tbody>${body || `<tr><td colspan='4' class='dqSmall'>No issues found.</td></tr>`}</tbody></table></div>`;
    }
    panel.innerHTML = `<div class='dqGrid'>${blocks.slice(0,6).map(b => `<div class='dqCard dqRow' data-query='${escAttr(b.query)}'><div class='kpiLabel'>${b.name}</div><div class='kpiValue'>${b.rows.length}</div><div class='kpiSub'>Click for related collection search when available</div></div>`).join('')}</div>` +
      sampleTable('Missing / incomplete data', missingYear.concat(missingRef, missingSize, missingWeight, missingMaterial)) +
      sampleTable('Mapping and duplicates', notOnMap.concat(dupType, dupYear));
    panel.querySelectorAll('.dqRow').forEach(row => row.addEventListener('click', () => {
      const q = row.dataset.query || '';
      if (q) navigateToMode('collection', [], q);
    }));
  }
  function renderChileRuns(){
    const panel = document.getElementById('chileRunsPanel');
    if (!panel) return;
    const allRows = (Array.isArray(window.__NUMISTA_ANALYTICS_ROWS) ? window.__NUMISTA_ANALYTICS_ROWS : []).filter(r => !r.is_issuer_only);
    const chileRows = allRows.filter(r => String(r.issuer_root || '').toLowerCase() === 'chile');
    const byType = new Map(chileRows.map(r => [String(r.type_id || ''), r]));
    const collectionOrder = new Map();
    chileRows.forEach((r, idx) => collectionOrder.set(String(r.type_id || ''), idx));
    const curated = Array.isArray(window.__NUMISTA_CHILE_DATE_RUNS) ? window.__NUMISTA_CHILE_DATE_RUNS : [];
    function cleanYearsFromRow(r){
      if (!r) return [];
      const source = (Array.isArray(r.years_greg_list) && r.years_greg_list.length ? r.years_greg_list : r.years_list) || [];
      return Array.from(new Set(source.map(y=>parseInt(y,10)).filter(y=>Number.isFinite(y)))).sort((a,b)=>a-b);
    }
    function compactYears(years){
      if (!years || !years.length) return '';
      const arr = Array.from(new Set(years.map(y=>parseInt(y,10)).filter(y=>Number.isFinite(y)))).sort((a,b)=>a-b);
      if (!arr.length) return '';
      const ranges=[]; let start=arr[0], prev=arr[0];
      for (let i=1;i<=arr.length;i++){
        const y=arr[i];
        if (y===prev+1){ prev=y; continue; }
        ranges.push(start===prev?String(start):`${start}-${prev}`);
        start=prev=y;
      }
      return ranges.join(', ');
    }
    function fallbackRuns(){
      return chileRows.filter(r => (parseInt(r.max_year || r.min_year || '0', 10) || 0) >= 1900).map(r => {
        const minY = parseInt(r.min_year,10), maxY=parseInt(r.max_year,10);
        const owned = cleanYearsFromRow(r);
        let expected=[];
        if (Number.isFinite(minY) && Number.isFinite(maxY) && maxY>=minY && (maxY-minY)<=140){
          for(let y=minY;y<=maxY;y++) expected.push(y);
        }
        return {type_id:r.type_id, title:r.label || r.title_full || '', currency:r.currency || '', composition:r.composition || '', year_range:r.year_str || '', expected_years:expected, excluded_years:[], source_url:r.url || '', image_url:r.obv || '', obv_url:r.obv || '', rev_url:r.rev || '', source_method:'range_fallback', notes:'Generated from broad type range; may include non-minted years.'};
      });
    }
    const sourceRuns = curated.length ? curated : fallbackRuns();
    const runs = sourceRuns.map(cr => {
      const ownedRow = byType.get(String(cr.type_id || '')) || null;
      const owned = cleanYearsFromRow(ownedRow);
      const expected = Array.from(new Set((cr.expected_years || []).map(y=>parseInt(y,10)).filter(y=>Number.isFinite(y)))).sort((a,b)=>a-b);
      const expectedSet = new Set(expected);
      const ownedSet = new Set(owned);
      const missing = expected.filter(y => !ownedSet.has(y));
      const extraOwned = owned.filter(y => !expectedSet.has(y));
      const minExpected = expected.length ? expected[0] : 9999;
      return {curated:cr, ownedRow, owned, expected, missing, extraOwned, minExpected};
    }).sort((a,b)=> {
      const ac = String((a.ownedRow && a.ownedRow.currency) || a.curated.currency || '');
      const bc = String((b.ownedRow && b.ownedRow.currency) || b.curated.currency || '');
      const acMin = a.minExpected, bcMin = b.minExpected;
      if (ac !== bc && acMin !== bcMin) return acMin - bcMin;
      if (ac !== bc) return ac.localeCompare(bc);
      const ao = collectionOrder.has(String(a.curated.type_id || '')) ? collectionOrder.get(String(a.curated.type_id || '')) : 1e9;
      const bo = collectionOrder.has(String(b.curated.type_id || '')) ? collectionOrder.get(String(b.curated.type_id || '')) : 1e9;
      if (ao !== bo) return ao - bo;
      const ar = a.ownedRow || {}, br = b.ownedRow || {};
      const af = parseFloat(ar.face_sort_value ?? ar.numeric_value ?? 1e30);
      const bf = parseFloat(br.face_sort_value ?? br.numeric_value ?? 1e30);
      if (Number.isFinite(af) && Number.isFinite(bf) && af !== bf) return af-bf;
      return (a.minExpected - b.minExpected) || String(a.curated.title||'').localeCompare(String(b.curated.title||''));
    });
    const totalExpected = runs.reduce((a,r)=>a+r.expected.length,0);
    const totalOwnedExpected = runs.reduce((a,r)=>a+r.expected.filter(y=>r.owned.includes(y)).length,0);
    const totalMissing = runs.reduce((a,r)=>a+r.missing.length,0);
    const coverage = totalExpected ? Math.round(totalOwnedExpected / totalExpected * 1000) / 10 : 0;
    const usingCurated = curated.length > 0;
    let body = '';
    let lastCurrency = null;
    runs.forEach(r => {
      const cr = r.curated;
      const ownedRow = r.ownedRow;
      const currency = (ownedRow && ownedRow.currency) || cr.currency || 'Unknown currency';
      if (currency !== lastCurrency){
        const groupCount = runs.filter(x => (((x.ownedRow && x.ownedRow.currency) || x.curated.currency || 'Unknown currency') === currency)).length;
        body += `<tr class='chileRunGroup' data-group='${escAttr(currency)}'><td colspan='7'>${escAttr(currency)} <span class='secCount'>(${groupCount} types)</span></td></tr>`;
        lastCurrency = currency;
      }
      const sourceUrl = cr.source_url || (ownedRow && ownedRow.url) || '';
      const ref = sourceUrl ? `<a href="${escAttr(sourceUrl)}" target="_blank" rel="noopener">N#${escAttr(cr.type_id || '')}</a>` : `N#${escAttr(cr.type_id || '')}`;
      const obvUrl = (ownedRow && ownedRow.obv) || cr.obv_url || cr.image_url || '';
      const revUrl = (ownedRow && ownedRow.rev) || cr.rev_url || '';
      const imgParts = [];
      if (obvUrl) imgParts.push(`<img class='chileThumb' src='${escAttr(obvUrl)}' alt='obverse' loading='lazy' decoding='async' />`);
      if (revUrl && revUrl !== obvUrl) imgParts.push(`<img class='chileThumb' src='${escAttr(revUrl)}' alt='reverse' loading='lazy' decoding='async' />`);
      const img = imgParts.length ? `<div class='chileThumbs'>${imgParts.join('')}</div>` : '';
      const mobileImgUrl = revUrl || obvUrl || '';
      const mobileImg = mobileImgUrl ? `<img class='chileMobileThumb' src='${escAttr(mobileImgUrl)}' alt='coin' loading='lazy' decoding='async' />` : '';
      const mobileRefImg = `<div class='chileMobileRefImg'>${ref}${mobileImg}</div>`;
      const status = !r.expected.length ? `<span class='dqSmall'>No expected years</span>` : (r.missing.length ? `<span class='runPartial'>${r.missing.length} missing</span>` : `<span class='runOk'>Complete</span>`);
      const q = ownedRow ? (ownedRow.km_y ? `ref:${ownedRow.km_y}` : `country:chile & ${cr.type_id}`) : `country:chile`;
      body += `<tr class='dqRow chileRunRow' data-query='${escAttr(q)}'><td class='chileImageCell'>${mobileRefImg}${img}</td><td class='chileRefCell'>${ref}</td><td class='chileTypeCell'>${escAttr(cr.title || (ownedRow && (ownedRow.label || ownedRow.title_full)) || '')}<div class='dqSmall chileComposition'>${escAttr(cr.composition || (ownedRow && ownedRow.composition) || '')}</div></td><td class='chileRangeCell'>${escAttr(cr.year_range || '')}</td><td class='ownedYears chileOwnedCell'>${escAttr(compactYears(r.owned) || '—')}</td><td class='missingYears chileMissingCell'>${escAttr(compactYears(r.missing) || '—')}</td><td class='chileStatusCell'>${status}</td></tr>`;
    });
    const sourceText = usingCurated ? `Chile date-table checklist: ${runs.length} Chile coin types, ${totalExpected} expected non-proof date entries.` : `No chile_date_runs.csv loaded. Using rough range fallback; this can include non-minted years.`;
    panel.innerHTML = `<p class='summary'>${sourceText}</p><div class='dqGrid chileKpis'><div class='dqCard'><div class='kpiLabel'>Coverage</div><div class='kpiValue'>${coverage}%</div><div class='kpiSub'>${totalOwnedExpected} / ${totalExpected} expected dates owned</div></div><div class='dqCard'><div class='kpiLabel'>Missing dates</div><div class='kpiValue'>${totalMissing}</div><div class='kpiSub'>Non-proof expected years not owned</div></div></div><div class='chileToolbar'><input id='chileRunSearch' type='text' placeholder='Filter Chile runs...' /><select id='chileRunStatus'><option value='all'>All</option><option value='missing'>Missing only</option><option value='complete'>Complete only</option><option value='owned'>Owned type only</option></select></div><table class='analyticsTable chileRunTable'><thead><tr><th></th><th>Ref</th><th>Type</th><th>Range</th><th>Owned dates</th><th>Missing dates</th><th>Status</th></tr></thead><tbody id='chileRunBody'>${body}</tbody></table>`;
    function applyChileRunFilter(){
      const q = normText((document.getElementById('chileRunSearch') || {}).value || '');
      const status = (document.getElementById('chileRunStatus') || {}).value || 'all';
      let currentGroup = null;
      panel.querySelectorAll('#chileRunBody tr').forEach(tr => {
        if (tr.classList.contains('chileRunGroup')){ currentGroup = tr; tr.classList.add('hidden'); return; }
        const txt = normText(tr.textContent || '');
        const hasMissing = !tr.querySelector('.missingYears') || (tr.querySelector('.missingYears').textContent || '').trim() !== '—';
        const hasOwned = !tr.querySelector('.ownedYears') || (tr.querySelector('.ownedYears').textContent || '').trim() !== '—';
        let ok = !q || txt.includes(q);
        if (status === 'missing') ok = ok && hasMissing;
        if (status === 'complete') ok = ok && !hasMissing;
        if (status === 'owned') ok = ok && hasOwned;
        tr.classList.toggle('hidden', !ok);
        if (ok && currentGroup) currentGroup.classList.remove('hidden');
      });
    }
    const input = document.getElementById('chileRunSearch');
    const statusSel = document.getElementById('chileRunStatus');
    if (input) input.addEventListener('input', applyChileRunFilter);
    if (statusSel) statusSel.addEventListener('change', applyChileRunFilter);
    applyChileRunFilter();
    panel.querySelectorAll('.chileRunRow').forEach(row => row.addEventListener('click', () => navigateToMode('collection', ['Chile'], row.dataset.query || 'country:chile')));
  }
  function renderAnalytics(){
    const allRows = Array.isArray(window.__NUMISTA_ANALYTICS_ROWS) ? window.__NUMISTA_ANALYTICS_ROWS : [];
    const filterEl = document.getElementById('analyticsCategoryFilter');
    const selected = filterEl ? filterEl.value : 'all';
    const rows = selected === 'all' ? allRows.slice() : allRows.filter(r => analyticsCategoryForRow(r) === selected);
    const qty = rows.filter(r => !r.is_issuer_only).reduce((a,r)=>a+(parseInt(r.qty||0,10)||0),0);
    const types = rows.filter(r => !r.is_issuer_only).length;
    const countries = new Set(rows.map(r => (r.issuer_root||'').trim()).filter(Boolean)).size;
    const duplicates = rows.filter(r => !r.is_issuer_only).reduce((a,r)=>a+Math.max((parseInt(r.qty||0,10)||0)-1,0),0);
    const countryRows = analyticsCountryRows(rows);
    renderZeroCountryList(countryRows);
    const topBody = document.getElementById('analyticsTopCountriesBody');
    function countryBreakdownHtml(r){
      const hist = Array.isArray(r.historical_issuers) ? r.historical_issuers : [];
      if (!hist.length) return '';
      const esc = value => String(value || '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[s]));
      const direct = Number(r.direct_qty || 0) || 0;
      const lines = [];
      if (direct > 0) lines.push(`<div class="countrySubcount">Modern/current: ${direct} items</div>`);
      hist.forEach(h => lines.push(`<div class="countrySubcount">${esc(h.issuer)}: ${Number(h.qty || 0)} items</div>`));
      return lines.join('');
    }
    if (topBody) topBody.innerHTML = countryRows.slice(0,14).map(r => `<tr class="analyticsNavRow" data-nav-prefix="collection" data-issuer-roots="${JSON.stringify(r.issuer_roots).replace(/&/g,'&amp;').replace(/"/g,'&quot;')}"><td>${r.country}${countryBreakdownHtml(r)}</td><td>${r.types}</td><td>${r.qty}</td></tr>`).join('');
    const objectRows = groupCounts(rows, r => r.object_type || 'Unknown');
    const gradeRows = gradeCounts(rows);
    const continentRows = groupCounts(rows, r => r.continent || 'Unknown');
    const compRows = groupCounts(rows, r => r.composition || 'Unknown');
    const centRows = groupCounts(rows, centuryLabel);
    const sizeRows = groupCounts(rows, r => sizeBucket(r.size_mm));
    const weightRows = groupCounts(rows, r => weightBucket(r.weight_g));
    renderPieChart('analyticsObjectTypePie', objectRows, 'label', 'count', 8);
    renderPieChart('analyticsGradePie', gradeRows, 'label', 'qty', 9);
    renderPieChart('analyticsContinentPie', continentRows, 'label', 'count', 7);
    renderPieChart('analyticsCompositionPie', compRows, 'label', 'count', 8);
    renderPieChart('analyticsCenturyPie', centRows, 'label', 'count', 8);
    renderPieChart('analyticsSizePie', sizeRows, 'label', 'count', 8);
    renderPieChart('analyticsWeightPie', weightRows, 'label', 'count', 8);
    renderTimeline(rows);
    document.querySelectorAll('.analyticsPhysicalPanel').forEach(el => el.classList.toggle('hidden', selected === 'banknotes'));
    bindAnalyticsNavRows();
    renderCountryMap(countryRows);
  }
  function bindAnalyticsNavRows(){ document.querySelectorAll('#panel-analytics .analyticsNavRow').forEach(row => { row.style.cursor='pointer'; row.title='Open filtered view'; if (row.dataset.boundNav==='1') return; row.dataset.boundNav='1'; row.addEventListener('click', () => { let issuerRoots=[]; try { issuerRoots = JSON.parse(row.dataset.issuerRoots || '[]'); } catch(e) {} navigateToMode('collection', issuerRoots); }); }); }
  function renderCountryMap(rowsArg){
	const ownedRows = Array.isArray(rowsArg) ? rowsArg : (Array.isArray(window.__NUMISTA_MAP_ROWS) ? window.__NUMISTA_MAP_ROWS : []);
	const allCountries = Array.isArray(window.__NUMISTA_ALL_COUNTRIES) ? window.__NUMISTA_ALL_COUNTRIES : [];
	const mapEl = document.getElementById('countryMap');
	const metricEl = document.getElementById('mapMetric');
	if (!mapEl || typeof Plotly === 'undefined') return;
	if (window.innerWidth <= 700) { mapEl.innerHTML=''; return; }
	const metric = (metricEl && metricEl.value) || 'types';
	const metricLabels = {types:'Types', qty:'Qty', duplicates:'Duplicates'};
	const noOwnIso3 = new Set(allCountries.filter(c => Boolean(c.no_own_currency)).map(c => String(c.iso3 || '').toUpperCase()).filter(Boolean));
	const groupIso3 = new Set(allCountries.filter(c => Boolean(c.group_currency)).map(c => String(c.iso3 || '').toUpperCase()).filter(Boolean));
	const groupNameByIso3 = new Map(allCountries.filter(c => c.currency_group).map(c => [String(c.iso3 || '').toUpperCase(), String(c.currency_group || '')]));
	const byIso3 = new Map();
	allCountries.forEach(c => {
		const iso3 = String(c.iso3 || '').toUpperCase();
		if (!iso3) return;
		byIso3.set(iso3, {
			iso3,
			country:c.country || iso3,
			types:0,
			qty:0,
			duplicates:0,
			issuer_roots:[],
			direct_types:0,
			direct_qty:0,
			historical_issuers:[],
			no_own_currency:Boolean(c.no_own_currency),
			group_currency:Boolean(c.group_currency),
			currency_group:String(c.currency_group || '')
		});
	});
	ownedRows.forEach(r => {
		const iso3 = String(r.iso3 || '').toUpperCase();
		if (!iso3) return;
		byIso3.set(iso3, {
			...r,
			iso3,
			country:r.country || iso3,
			types:Number(r.types || 0),
			qty:Number(r.qty || 0),
			duplicates:Number(r.duplicates || 0),
			issuer_roots:Array.isArray(r.issuer_roots) ? r.issuer_roots : [],
			direct_types:Number(r.direct_types || 0),
			direct_qty:Number(r.direct_qty || 0),
			historical_issuers:Array.isArray(r.historical_issuers) ? r.historical_issuers : [],
			no_own_currency:noOwnIso3.has(iso3) || Boolean(r.no_own_currency),
			group_currency:groupIso3.has(iso3) || Boolean(r.group_currency),
			currency_group:String(r.currency_group || groupNameByIso3.get(iso3) || '')
		});
	});
	const rows = Array.from(byIso3.values());
	if (!rows.length) { mapEl.innerHTML = '<div class="miniEmpty">No mappable countries</div>'; return; }
	function hasItems(row){ return Number(row.types || 0) > 0 || Number(row.qty || 0) > 0; }
	function isShared(row){ return Boolean(row.group_currency); }
	function isNoOwn(row){ return Boolean(row.no_own_currency); }
	const baseRows = rows.filter(r => (!hasItems(r) && !isNoOwn(r) && !isShared(r)) || (hasItems(r) && !isShared(r) && !isNoOwn(r)));
	const sharedOwnedRows = rows.filter(r => hasItems(r) && isShared(r));
	const noOwnOwnedRows = rows.filter(r => hasItems(r) && isNoOwn(r) && !isShared(r));
	const sharedZeroRows = rows.filter(r => !hasItems(r) && isShared(r));
	const noOwnZeroRows = rows.filter(r => !hasItems(r) && isNoOwn(r) && !isShared(r));
	const numericRows = baseRows.concat(sharedOwnedRows, noOwnOwnedRows);
	const zmax = Math.max(1, ...numericRows.map(r => Number(r[metric] || 0)));
	const metricColorscale = [[0.0,'#d9dee8'],[0.0001,'#d9dee8'],[0.0002,'#1a9850'],[0.5,'#fee08b'],[1.0,'#d73027']];
	function historicalHoverLines(row){
		const hist = Array.isArray(row.historical_issuers) ? row.historical_issuers : [];
		const lines = [];
		const directQty = Number(row.direct_qty || 0);
		const directTypes = Number(row.direct_types || 0);
		if (directQty > 0 || directTypes > 0) lines.push(`Modern/current: ${directQty} items / ${directTypes} types`);
		hist.forEach(h => lines.push(`${h.issuer}: ${Number(h.qty || 0)} items / ${Number(h.types || 0)} types`));
		return lines.length ? '<br><br>' + lines.join('<br>') : '';
	}
	function customRows(items){
		return items.map(r => [r.country || '', Number(r.types || 0), Number(r.qty || 0), Number(r.duplicates || 0), Array.isArray(r.issuer_roots) ? r.issuer_roots : [], String(r.currency_group || ''), historicalHoverLines(r)]);
	}
	function numericTrace(items, opts){
		return {
			type:'choropleth',
			locationmode:'ISO-3',
			locations:items.map(r => r.iso3),
			z:items.map(r => Number(r[metric] || 0)),
			zmin:0,
			zmax,
			customdata:customRows(items),
			colorscale:metricColorscale,
			showscale:opts.showscale,
			marker:{line:{color:opts.lineColor || '#ffffff', width:opts.lineWidth || 0.6}},
			colorbar:opts.showscale ? {title: metricLabels[metric] || metric, thickness:12, len:0.62, y:0.5, yanchor:'middle', tickfont:{size:10}} : undefined,
			hovertemplate:opts.hovertemplate
		};
	}
	function flatTrace(items, color, hoverLabel){
		return {
			type:'choropleth',
			locationmode:'ISO-3',
			locations:items.map(r => r.iso3),
			z:items.map(() => 1),
			zmin:0,
			zmax:1,
			customdata:customRows(items),
			colorscale:[[0,color],[1,color]],
			showscale:false,
			marker:{line:{color:'#ffffff', width:0.6}},
			hovertemplate:`<b>%{customdata[0]}</b><br>${hoverLabel}<br>Types: %{customdata[1]}<br>Qty: %{customdata[2]}<br>Duplicates: %{customdata[3]}%{customdata[6]}<extra></extra>`
		};
	}
	const data = [];
	if (baseRows.length) data.push(numericTrace(baseRows, {showscale:true, hovertemplate:'<b>%{customdata[0]}</b><br>Types: %{customdata[1]}<br>Qty: %{customdata[2]}<br>Duplicates: %{customdata[3]}%{customdata[6]}<extra></extra>'}));
	if (sharedOwnedRows.length) data.push(numericTrace(sharedOwnedRows, {showscale:false, lineColor:'#4c1d95', lineWidth:1.6, hovertemplate:'<b>%{customdata[0]}</b><br>Shared/group currency: %{customdata[5]}<br>Types: %{customdata[1]}<br>Qty: %{customdata[2]}<br>Duplicates: %{customdata[3]}%{customdata[6]}<extra></extra>'}));
	if (noOwnOwnedRows.length) data.push(numericTrace(noOwnOwnedRows, {showscale:false, lineColor:'#374151', lineWidth:1.4, hovertemplate:'<b>%{customdata[0]}</b><br>No own currency/coin target, but you have items<br>Types: %{customdata[1]}<br>Qty: %{customdata[2]}<br>Duplicates: %{customdata[3]}%{customdata[6]}<extra></extra>'}));
	if (sharedZeroRows.length) data.push(flatTrace(sharedZeroRows, '#8b95a7', 'Shared/group currency, 0 items'));
	if (noOwnZeroRows.length) data.push(flatTrace(noOwnZeroRows, '#6b7280', 'No own currency/coin target, 0 items'));
	const layout = {margin:{l:0,r:0,t:0,b:0}, paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)', geo:{scope:'world', projection:{type:'equirectangular'}, showframe:false, showcoastlines:true, coastlinecolor:'#c9d4e6', showcountries:true, countrycolor:'#ffffff', bgcolor:'rgba(0,0,0,0)', landcolor:'#edf2fb', showland:true}};
	Plotly.react(mapEl, data, layout, {responsive:true, displayModeBar:false});
	mapEl.on('plotly_click', evt => { const point=evt&&evt.points&&evt.points[0]; const issuerRoots=point&&point.customdata&&Array.isArray(point.customdata[4])?point.customdata[4]:[]; if (issuerRoots.length) navigateToMode('collection', issuerRoots); });
  }
  const mapMetricEl = document.getElementById('mapMetric');
  if (mapMetricEl) mapMetricEl.addEventListener('change', renderAnalytics);
  const analyticsCategoryEl = document.getElementById('analyticsCategoryFilter');
  if (analyticsCategoryEl) analyticsCategoryEl.addEventListener('change', renderAnalytics);
  const timelineBucketEl = document.getElementById('timelineBucket');
  if (timelineBucketEl) timelineBucketEl.addEventListener('change', renderAnalytics);
  const timelineMetricEl = document.getElementById('timelineMetric');
  if (timelineMetricEl) timelineMetricEl.addEventListener('change', renderAnalytics);
  const modeControllers = {};
  function initMode(prefix){
	const contMap = (window.__NUMISTA_CONT_MAPS || {})[prefix] || {};
	const mobileSectionData = ((window.__NUMISTA_MOBILE_SECTION_DATA || {})[prefix]) || {};
	const contSel = document.getElementById(prefix + '-continentFilter');
	const metricsEl = document.getElementById(prefix + '-filterMetrics');
	const countryCountEl = document.getElementById(prefix + '-countryCount');
	const clearBtn = document.getElementById(prefix + '-clearAll');
	const exportCsvBtn = document.getElementById(prefix + '-exportCsv');
	const emptyClearBtn = document.getElementById(prefix + '-emptyClearBtn');
	const mobileClearBtn = document.getElementById(prefix + '-mobileClearBtn');
	const mobileFilterToggle = document.getElementById(prefix + '-mobileFilterToggle');
	const mobileResults = document.getElementById(prefix + '-mobileResults');
	const filtersPanel = document.getElementById(prefix + '-filtersPanel');
	const searchEl = document.getElementById(prefix + '-issuerSearch');
	const coinSearchEl = document.getElementById(prefix + '-coinSearch');
	const boxEl = document.getElementById(prefix + '-issuerBox');
	const objTypeSel = document.getElementById(prefix + '-objTypeFilter');
	const catSel = document.getElementById(prefix + '-catFilter');
	const chipsEl = document.getElementById(prefix + '-activeChips');
	const liveStatsEl = document.getElementById(prefix + '-liveStats');
	const sortSel = document.getElementById(prefix + '-sortSel');
	const savedViewSel = document.getElementById(prefix + '-savedViewSel');
	const viewModeSel = document.getElementById(prefix + '-viewModeSel');
	const stickyBarEl = document.getElementById(prefix + '-stickyFilterBar');
	const emptyStateEl = document.getElementById(prefix + '-emptyState');
	const visibleQtyEl = document.getElementById(prefix + '-kpiVisibleQty');
	const visibleTypesEl = document.getElementById(prefix + '-kpiVisibleTypes');
	const yearEl = document.getElementById(prefix + '-yearFilter');
	const sizeEl = document.getElementById(prefix + '-sizeFilter');
	const shell = document.getElementById(prefix + '-mode');
	const isMobile = window.matchMedia('(max-width: 700px)').matches;
	function uniqSorted(arr){ return Array.from(new Set(arr)).sort((a,b)=>String(a).localeCompare(String(b))); }
	function getSelectedIssuers(){ return Array.from(boxEl.querySelectorAll('input[type=checkbox]:checked')).map(cb => cb.value); }
	function renderIssuerCheckboxes(list, selectedSet){
		const stats = new Map();
		entries.forEach(e => { if (!e.issuerroot) return; if (!stats.has(e.issuerroot)) stats.set(e.issuerroot, {qty:0, types:0, continent:e.continent || 'Unknown'}); const st=stats.get(e.issuerroot); st.qty += e.qty || 0; if (!e.isIssuerOnly) st.types += 1; });
		const selected = list.filter(v => selectedSet.has(v));
		const rest = list.filter(v => !selectedSet.has(v));
		const byCont = new Map();
		rest.forEach(v => { const c = (stats.get(v) || {}).continent || 'Unknown'; if (!byCont.has(c)) byCont.set(c, []); byCont.get(c).push(v); });
		function itemHtml(v){ const st=stats.get(v) || {qty:0,types:0}; return `<label class="issuerItem" data-norm="${normText(v)}"><input type="checkbox" value="${v.replace(/"/g,'&quot;')}" ${selectedSet.has(v) ? 'checked' : ''} /> <span>${v}</span><span class="issuerCount">${st.types}/${st.qty}</span></label>`; }
		let html='';
		if (selected.length) html += `<div class="issuerGroup"><div class="issuerGroupTitle">Selected</div>${selected.map(itemHtml).join('')}</div>`;
		Array.from(byCont.keys()).sort((a,b)=>a.localeCompare(b)).forEach(c => { const arr=byCont.get(c).sort((a,b)=>a.localeCompare(b)); html += `<div class="issuerGroup"><div class="issuerGroupTitle">${c}</div>${arr.map(itemHtml).join('')}</div>`; });
		boxEl.innerHTML = html;
	}
	function getActiveFilterChips(state){ const chips=[]; if (state.cont && state.cont !== 'ALL') chips.push({k:'cont', t:'Continent: ' + state.cont}); if (state.countries.length) chips.push({k:'countries', t:`Countries: ${state.countries.length}`}); if (state.coinQ) chips.push({k:'coinQ', t:'Search: ' + state.coinQ}); if (state.objT && state.objT !== 'ALL') chips.push({k:'objT', t:'Object: ' + state.objT}); if (state.cat && state.cat !== 'ALL') chips.push({k:'cat', t:'Category: ' + state.cat}); if (state.year) chips.push({k:'year', t:'Year: ' + state.year}); if (state.size) chips.push({k:'size', t:'Diameter: ' + state.size}); return chips; }
	function renderChips(state){ if (chipsEl) chipsEl.innerHTML = ''; }
	function renderStickyBar(state, shownItems, shownTypes, visibleCards){
		if (!stickyBarEl) return;
		const chips = getActiveFilterChips(state);
		const chipsHtml = chips.map(ch => `<span class="chip" data-k="${ch.k}">${ch.t}<button type="button" data-action="chip" aria-label="Remove ${ch.t}">×</button></span>`).join('');
		stickyBarEl.classList.add('is-visible');
		stickyBarEl.innerHTML = `<div class="stickyFilterMain"><span class="stickyFilterText"><b>${prefix === 'wishlist' ? 'Wishlist' : 'Collection'}</b> · Visible <b>${visibleCards}</b> types / <b>${shownItems}</b> items</span><span class="stickyFilterChips">${chipsHtml}</span></div><button type="button" data-action="clear">Clear</button>`;
	}
	function setCountrySelection(names){ const wanted = new Set((names || []).map(String)); boxEl.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = wanted.has(cb.value); }); }
	function applySavedView(value){
		if (!value) return;
		forcedTimelineYearRange = null;
		contSel.value='ALL'; searchEl.value=''; coinSearchEl.value=''; objTypeSel.value='ALL'; catSel.value='ALL';
		if (value === 'chile-modern'){ coinSearchEl.value='country:chile & year>=1930'; updateIssuerList(); setCountrySelection(['Chile']); }
		else if (value === 'uk-modern'){ coinSearchEl.value='country:united kingdom & year>=1930'; updateIssuerList(); setCountrySelection(['United Kingdom']); }
		else if (value === 'on-transit'){ updateIssuerList(); if ([...catSel.options].some(o=>o.value==='on transit')) catSel.value='on transit'; else coinSearchEl.value='category:on_transit,on transit'; }
		else if (value === 'replicas'){ updateIssuerList(); if ([...catSel.options].some(o=>o.value==='replica')) catSel.value='replica'; else coinSearchEl.value='replica'; }
		else if (value === 'exonumia'){ updateIssuerList(); if ([...catSel.options].some(o=>o.value==='exonumia')) catSel.value='exonumia'; else coinSearchEl.value='category:exonumia'; }
		else if (value === 'ancient'){ coinSearchEl.value='year<=1500'; updateIssuerList(); }
		lastSortMode = null; applyFilters(); if (savedViewSel) savedViewSel.value='';
	}
	function clearOneFilter(k){ if (k==='cont'){ contSel.value='ALL'; updateIssuerList(); return; } if (k==='countries'){ boxEl.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked=false); return; } if (k==='coinQ'){ coinSearchEl.value=''; return; } if (k==='objT'){ objTypeSel.value='ALL'; return; } if (k==='cat'){ catSel.value='ALL'; return; } if (k==='year' && yearEl){ yearEl.value=''; return; } if (k==='size' && sizeEl){ sizeEl.value=''; } }

	function escHtml(value){
		return String(value ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[s]));
	}
	function buildMobileBadgeHtml(item){
		const badges = [];
		const qty = parseInt(item.qty || 0, 10) || 0;
		const category = String(item.category || '').trim().toLowerCase();
		if (category === 'exonumia') badges.push("<span class='badge badge-exo'>Exonumia</span>");
		if (prefix === 'wishlist'){
			badges.push("<span class='badge badge-wish'>Wishlist</span>");
			if (item.is_issuer_only) badges.push("<span class='badge badge-muted'>Any coin</span>");
		} else {
			if (item.is_replica) badges.push("<span class='badge badge-alert'>Replica</span>");
			if (item.is_on_transit) badges.push("<span class='badge badge-transit'>On transit</span>");
			const hasDuplicateType = Boolean(item.duplicate_type) || qty > 1;
			const hasDuplicateYear = Boolean(item.duplicate_year);
			if (hasDuplicateType) badges.push("<span class='badge badge-dup-type' title='Same type: more than one item'>Duplicate type</span>");
			if (hasDuplicateYear){
				const dupYearLabel = String(item.duplicate_years_label || '').trim();
				const title = 'Same type and same year' + (dupYearLabel ? `: ${dupYearLabel}` : '');
				badges.push(`<span class='badge badge-dup-year' title='${escHtml(title)}'>Duplicate year</span>`);
			}
			if (qty) badges.push(`<span class='badge'>x${qty}</span>`);
		}
		return badges.join('');
	}
	function buildMobileCardHtml(item){
		const imgs = [];
		if (item.obv) imgs.push(`<img src="${escHtml(item.obv)}" alt="obverse" loading="lazy" decoding="async" />`);
		if (item.rev) imgs.push(`<img src="${escHtml(item.rev)}" alt="reverse" loading="lazy" decoding="async" />`);
		const mainLabel = item.label || '';
		const refLabel = item.km_y || '';
		const titleMain = mainLabel || '';
		const titleExtra = item.title_full && item.title_full !== mainLabel && item.title_full.startsWith(mainLabel) ? item.title_full.slice(mainLabel.length).trim() : '';
		const titleLine = [mainLabel, refLabel].filter(Boolean).join(' ');
		const yearsGregList = Array.isArray(item.years_greg_list) ? item.years_greg_list : (Array.isArray(item.years_list) ? item.years_list : []);
		const yearsRawList = Array.isArray(item.years_raw_list) ? item.years_raw_list : [];
		const yearsAttr = yearsGregList.filter(v => Number.isInteger(v)).join(',');
		const yearsRawAttr = yearsRawList.filter(v => Number.isInteger(v)).join(',');
		const searchBlob = [titleLine, item.title_full || '', item.currency || '', item.km_y || '', item.year_str || '', item.grade_str || '', yearsRawList.join(' '), yearsGregList.join(' '), item.issuer_root || '', item.issuer_path || '', item.issuer_search_es || '', item.composition || '', item.weight_g || ''].join(' ').toLowerCase();
		const mainHtml = item.url ? `<a href="${escHtml(item.url)}" target="_blank" rel="noopener">${escHtml(titleMain)}</a>` : escHtml(titleMain);
		const extraHtml = titleExtra ? `<span class="valueTitleExtra">${escHtml(titleExtra)}</span>` : '';
		const meta1 = [];
		const meta2 = [];
		if (item.year_str) meta1.push(`Years: ${escHtml(item.year_str)}`);
		if (item.grade_str) meta1.push(`Grade: ${escHtml(item.grade_str)}`);
		if (item.size_mm !== null && item.size_mm !== undefined && item.size_mm !== ''){
			const num = Number(item.size_mm);
			if (Number.isFinite(num)) meta2.push(`${num} mm`);
		}
		if (item.composition) meta2.push(escHtml(item.composition));
		return `<div class='card' data-mode='${prefix}' data-continent='${escHtml(item.continent || 'Unknown')}' data-issuerroot='${escHtml(item.issuer_root || '')}' data-objtype='${escHtml(item.object_type || '')}' data-category='${escHtml(item.category || '')}' data-qty='${escHtml(item.qty || 0)}' data-facevalue='${escHtml(item.numeric_value || '')}' data-facesort='${escHtml(item.face_sort_value ?? item.numeric_value ?? '')}' data-title='${escHtml(titleLine)}' data-ref='${escHtml(item.km_y || '')}' data-typeid='${escHtml(item.type_id || '')}' data-issuerpath='${escHtml(item.issuer_path || '')}' data-currency='${escHtml(item.currency || '')}' data-yearstr='${escHtml(item.year_str || '')}' data-grade='${escHtml(item.grade_str || '')}' data-url='${escHtml(item.url || '')}' data-isissueronly='${item.is_issuer_only ? '1' : '0'}' data-duptype='${item.duplicate_type ? '1' : '0'}' data-dupyear='${item.duplicate_year ? '1' : '0'}' data-years='${escHtml(yearsAttr)}' data-yearsraw='${escHtml(yearsRawAttr)}' data-minyear='${escHtml(item.min_year || '')}' data-maxyear='${escHtml(item.max_year || '')}' data-sizemm='${escHtml(item.size_mm ?? '')}' data-weightg='${escHtml(item.weight_g ?? '')}' data-composition='${escHtml(item.composition ?? '')}' data-search='${escHtml(searchBlob)}'><div class='cardTop'><div></div><div class='cardBadges'>${buildMobileBadgeHtml(item)}</div></div><div class='imgs'>${imgs.join('')}</div><div class='cardMain'><div class='valueLine'><p class='valueTitle'>${mainHtml}${extraHtml}</p><div class='refText'>${escHtml(refLabel)}</div></div>${item.currency ? `<p class='subTitle' title='${escHtml(titleLine)}'>${escHtml(item.currency)}</p>` : ''}${meta1.length ? `<p class='metaRow'>${meta1.join(' · ')}</p>` : ''}${meta2.length ? `<p class='metaRow'>${meta2.join(' · ')}</p>` : ''}</div></div>`;
	}
	function mobileItemYears(item){
		const greg = Array.isArray(item.years_greg_list) ? item.years_greg_list : (Array.isArray(item.years_list) ? item.years_list : []);
		const raw = Array.isArray(item.years_raw_list) ? item.years_raw_list : [];
		const seen = new Set();
		return greg.concat(raw).map(v => parseInt(v, 10)).filter(v => !isNaN(v) && !seen.has(v) && (seen.add(v), true));
	}
	function mobileItemMinYear(item){
		const v = parseInt(item.min_year, 10);
		return Number.isFinite(v) ? v : 999999;
	}
	function mobileItemMaxYear(item){
		const v = parseInt(item.max_year, 10);
		return Number.isFinite(v) ? v : 999999;
	}
	function compareMobileItems(a,b,mode){
		const aFace = parseSortNumber(a.face_sort_value ?? a.numeric_value, 1e30);
		const bFace = parseSortNumber(b.face_sort_value ?? b.numeric_value, 1e30);
		const aMin = mobileItemMinYear(a), bMin = mobileItemMinYear(b);
		const aMax = mobileItemMaxYear(a), bMax = mobileItemMaxYear(b);
		const aRef = parseRefRaw(a.km_y || ''), bRef = parseRefRaw(b.km_y || '');
		const aTitle = String(a.title_full || a.label || '').toLowerCase();
		const bTitle = String(b.title_full || b.label || '').toLowerCase();
		const aOrig = parseInt(a._orig || 0, 10) || 0;
		const bOrig = parseInt(b._orig || 0, 10) || 0;
		if (mode==='face') return aFace-bFace || aMin-bMin || aMax-bMax || aRef.catRank-bRef.catRank || aRef.num-bRef.num || aRef.suffix.localeCompare(bRef.suffix) || aOrig-bOrig;
		if (mode==='date') return aMin-bMin || aMax-bMax || aFace-bFace || aOrig-bOrig;
		if (mode==='ref') return aRef.catRank-bRef.catRank || aRef.num-bRef.num || aRef.suffix.localeCompare(bRef.suffix) || aOrig-bOrig;
		if (mode==='type') return aTitle.localeCompare(bTitle) || aOrig-bOrig;
		return aOrig-bOrig;
	}
	function sortMobileItems(items, mode){
		return [...items].sort((a,b) => compareMobileItems(a,b,mode || 'none'));
	}
	function mobileItemMatches(item, filters){
		const itemCont = item.continent || 'Unknown';
		const itemIssuer = item.issuer_root || '';
		if (filters.cont !== 'ALL' && itemCont !== filters.cont) return false;
		if (filters.selectedSet.size && !filters.selectedSet.has(itemIssuer)) return false;
		const yearsGreg = Array.isArray(item.years_greg_list) ? item.years_greg_list : (Array.isArray(item.years_list) ? item.years_list : []);
		const yearsRaw = Array.isArray(item.years_raw_list) ? item.years_raw_list : [];
		const haystack = normText([item.label || '', item.title_full || '', item.currency || '', item.km_y || '', item.year_str || '', item.grade_str || '', yearsRaw.join(' '), yearsGreg.join(' '), (item.issuer_root || ''), (item.issuer_path || ''), (item.issuer_search_es || '')].join(' '));
		if (!fuzzyMatchNorm(filters.coinQNorm, haystack)) return false;
		const itemObj = item.object_type || '';
		const itemCat = item.category || '';
		if (filters.objT !== 'ALL' && filters.objT && itemObj !== filters.objT) return false;
		if (filters.cat !== 'ALL' && filters.cat && itemCat !== filters.cat) return false;
		if (filters.sizeRules && !diameterMatches(item.size_mm, filters.sizeRules)) return false;
		if (filters.yearRules){
			const years = mobileItemYears(item);
			const hasExactYears = years.length > 0;
			const minYear = mobileItemMinYear(item);
			const maxYear = mobileItemMaxYear(item);
			const okYear = filters.yearRules.some(rule => {
				if (rule.type === 'single'){
					if (years.includes(rule.year)) return true;
					return !hasExactYears && minYear !== 999999 && maxYear !== 999999 && rule.year >= minYear && rule.year <= maxYear;
				}
				if (years.some(y => y >= rule.min && y <= rule.max)) return true;
				return !hasExactYears && minYear !== 999999 && maxYear !== 999999 && !(rule.max < minYear || rule.min > maxYear);
			});
			if (!okYear) return false;
		}
		return true;
	}
	function renderMobileSectionBody(body, secKey, itemsOverride){
		if (!isMobile || !body) return;
		const payload = mobileSectionData[secKey] || {items: []};
		const bodyFilteredItems = Array.isArray(body.__filteredItems) ? body.__filteredItems : null;
		const baseItems = Array.isArray(itemsOverride) ? itemsOverride : (bodyFilteredItems || (Array.isArray(payload.items) ? payload.items : []));
		const mode = sortSel ? (sortSel.value || 'none') : 'none';
		const byCurrency = new Map();
		const currencyOrder = [];
		baseItems.forEach(item => {
			const currency = item.currency || 'Unknown currency';
			if (!byCurrency.has(currency)) {
				byCurrency.set(currency, []);
				currencyOrder.push(currency);
			}
			byCurrency.get(currency).push(item);
		});
		const chunks = [];
		currencyOrder.forEach(currency => {
			const sectionItems = sortMobileItems(byCurrency.get(currency) || [], mode);
			if (!sectionItems.length) return;
			chunks.push(`<h3>${escHtml(currency)}</h3><div class="grid">`);
			sectionItems.forEach(item => chunks.push(buildMobileCardHtml(item)));
			chunks.push("</div>");
		});
		body.innerHTML = chunks.join('');
		body.dataset.rendered = '1';
	}

	const desktopHeaders = Array.from(shell.querySelectorAll('.desktopSectionHeader'));
	const desktopBodies = Array.from(shell.querySelectorAll('.desktopSectionBody'));
	if (isMobile){
		desktopHeaders.forEach(el => el.classList.add('hidden'));
		desktopBodies.forEach(el => el.classList.add('hidden'));
		const mobileHeaders = Array.from(shell.querySelectorAll('.mobileLazyHeader'));
		mobileHeaders.forEach(h2 => {
			const body = shell.querySelector(`.mobileLazyBody[data-secbody="${CSS.escape(h2.dataset.sec)}"]`);
			if (!body) return;
			body.classList.add('hidden');
			const chev = h2.querySelector('.chev');
			if (chev) chev.textContent = '▸';
			h2.addEventListener('click', () => {
				const itemsForRender = Array.isArray(body.__filteredItems) ? body.__filteredItems : undefined;
				renderMobileSectionBody(body, body.dataset.sectionkey || h2.dataset.sectionkey || h2.dataset.sec, itemsForRender);
				const isHidden = body.classList.toggle('hidden');
				const icon = h2.querySelector('.chev');
				if (icon) icon.textContent = isHidden ? '▸' : '▾';
				if (!isHidden) requestAnimationFrame(() => { if (typeof refreshMobileJump === 'function') refreshMobileJump(); });
			});
		});
	} else {
		Array.from(shell.querySelectorAll('.mobileLazyHeader, .mobileLazyBody')).forEach(el => el.classList.add('hidden'));
	}

	const grids = Array.from(shell.querySelectorAll('.grid'));
	grids.forEach(grid => Array.from(grid.querySelectorAll('.card')).forEach((c, idx) => { if (!c.dataset.orig) c.dataset.orig = String(idx); }));
	const sections = Array.from(shell.querySelectorAll('.desktopSectionBody')).map(body => ({
  	body,
  	header: shell.querySelector(`.desktopSectionHeader[data-sec="${CSS.escape(body.dataset.secbody)}"]`)
	}));
	const mobileHeaders = Array.from(shell.querySelectorAll('.mobileLazyHeader'));
	mobileHeaders.forEach((header, idx) => {
		const secKey = header.dataset.sectionkey || '';
		const payload = mobileSectionData[secKey] || {items: []};
		const body = shell.querySelector(`.mobileLazyBody[data-secbody="${CSS.escape(header.dataset.sec)}"]`);
		if (body) body.__allItems = Array.isArray(payload.items) ? payload.items.map((item, itemIdx) => ({...item, _orig:itemIdx})) : [];
	});
	const entries = Array.from(shell.querySelectorAll('.card')).map(card => { const qty=parseInt(card.dataset.qty || '0', 10) || 0; const yearsGreg=(card.dataset.years || '').split(',').map(x=>parseInt(x,10)).filter(x=>!isNaN(x)); const yearsRaw=(card.dataset.yearsraw || '').split(',').map(x=>parseInt(x,10)).filter(x=>!isNaN(x)); const seenYears=new Set(); const years=yearsGreg.concat(yearsRaw).filter(y => !seenYears.has(y) && (seenYears.add(y), true)); const hasExactYears=years.length > 0; const minYear=parseInt(card.dataset.minyear || '',10); const maxYear=parseInt(card.dataset.maxyear || '',10); return { el:card, grid:card.closest('.grid'), haystackNorm:normText(card.dataset.search || ''), continent:card.dataset.continent || 'Unknown', issuerroot:card.dataset.issuerroot || '', qty, isIssuerOnly:card.dataset.isissueronly==='1', objType:card.dataset.objtype || '', category:card.dataset.category || '', faceValue:parseSortNumber(card.dataset.facesort || card.dataset.facevalue, 1e30), minYear:Number.isFinite(minYear) ? minYear : 999999, maxYear:Number.isFinite(maxYear) ? maxYear : 999999, years, hasExactYears, sizeMm:Number.parseFloat(card.dataset.sizemm || ''), weightG:Number.parseFloat(card.dataset.weightg || ''), composition:card.dataset.composition || '', refKey:parseRefRaw(card.dataset.ref || ''), titleSort:(card.dataset.title || '').toLowerCase(), orig:parseInt(card.dataset.orig || '0', 10) || 0, visible:true }; });
	const gridEntries = new Map(grids.map(grid => [grid, entries.filter(e => e.grid === grid)]));
	let lastSortMode = null; let forcedTimelineYearRange = null; const sortCache = new Map();
	function compareEntries(a,b,mode){ if (mode==='face') return a.faceValue-b.faceValue || a.minYear-b.minYear || a.maxYear-b.maxYear || a.refKey.catRank-b.refKey.catRank || a.refKey.num-b.refKey.num || a.refKey.suffix.localeCompare(b.refKey.suffix) || a.orig-b.orig; if (mode==='date') return a.minYear-b.minYear || a.maxYear-b.maxYear || a.faceValue-b.faceValue || a.orig-b.orig; if (mode==='ref') return a.refKey.catRank-b.refKey.catRank || a.refKey.num-b.refKey.num || a.refKey.suffix.localeCompare(b.refKey.suffix) || a.orig-b.orig; if (mode==='type') return a.titleSort.localeCompare(b.titleSort) || a.orig-b.orig; return a.orig-b.orig; }
	function applyGridOrder(mode){ if (lastSortMode === mode) return; grids.forEach(grid => { const cacheKey = grid.dataset.sortcachekey || (grid.dataset.sortcachekey = Math.random().toString(36).slice(2)); const mapKey = `${cacheKey}:${mode}`; let ordered = sortCache.get(mapKey); if (!ordered){ ordered = [...(gridEntries.get(grid) || [])].sort((a,b)=>compareEntries(a,b,mode)); sortCache.set(mapKey, ordered); } const frag=document.createDocumentFragment(); ordered.forEach(entry => frag.appendChild(entry.el)); grid.appendChild(frag); }); lastSortMode = mode; }
	function updateIssuerList(){ const cont=contSel.value; const selectedSet=new Set(getSelectedIssuers()); const all = uniqSorted(Object.values(contMap).reduce((acc,v)=>acc.concat(v), [])); const list = (cont === 'ALL') ? all : uniqSorted(contMap[cont] || []); renderIssuerCheckboxes(list, selectedSet); applyFilters(); }
	function resetAllFilters(){ forcedTimelineYearRange = null; contSel.value='ALL'; searchEl.value=''; coinSearchEl.value=''; objTypeSel.value='ALL'; catSel.value='ALL'; if (yearEl) yearEl.value=''; if (sizeEl) sizeEl.value=''; const defaultSort = sortSel.dataset.default || sortSel.value || 'none'; sortSel.value = defaultSort; renderIssuerCheckboxes(uniqSorted(Object.values(contMap).reduce((acc,v)=>acc.concat(v), [])), new Set()); lastSortMode = null; applyFilters(); }
	function csvEscape(value){
		const s = String(value ?? '');
		return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
	}
	function downloadCsv(filename, rows){
		const csv = rows.map(row => row.map(csvEscape).join(',')).join('\r\n');
		const blob = new Blob(['\ufeff' + csv], {type:'text/csv;charset=utf-8;'});
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		document.body.appendChild(a);
		a.click();
		a.remove();
		URL.revokeObjectURL(url);
	}
	function exportYearsFromDataset(ds){
		// Export exact stored years, not the compact display label like "1950-1970".
		// Prefer Gregorian years because they are the normalized/searchable year field.
		const greg = String(ds.years || '').split(',').map(x => parseInt(x, 10)).filter(x => !isNaN(x));
		const raw = String(ds.yearsraw || '').split(',').map(x => parseInt(x, 10)).filter(x => !isNaN(x));
		const base = greg.length ? greg : raw;
		return Array.from(new Set(base)).sort((a,b) => a-b).join('; ');
	}
	function exportYearsFromItem(item){
		const greg = Array.isArray(item.years_greg_list) ? item.years_greg_list : (Array.isArray(item.years_list) ? item.years_list : []);
		const raw = Array.isArray(item.years_raw_list) ? item.years_raw_list : [];
		const base = greg.length ? greg : raw;
		return Array.from(new Set(base.filter(v => Number.isInteger(v)))).sort((a,b) => a-b).join('; ');
	}
	function rowFromDataset(ds){
		return [
			prefix,
			ds.typeid || '',
			ds.issuerroot || '',
			ds.issuerpath || '',
			ds.currency || '',
			ds.title || '',
			ds.ref || '',
			exportYearsFromDataset(ds),
			ds.grade || '',
			ds.qty || '',
			ds.objtype || '',
			ds.category || '',
			ds.url || ''
		];
	}
	function rowFromItem(item){
		return [
			prefix,
			item.type_id || '',
			item.issuer_root || '',
			item.issuer_path || '',
			item.currency || '',
			[item.label || '', item.km_y || ''].filter(Boolean).join(' '),
			item.km_y || '',
			exportYearsFromItem(item),
			item.grade_str || '',
			item.qty || '',
			item.object_type || '',
			item.category || '',
			item.url || ''
		];
	}
	function exportCurrentCsv(){
		const header = ['mode','type_id','issuer_root','issuer_path','currency','title','reference','years','grade','quantity','object_type','category','url'];
		let dataRows = [];
		if (isMobile){
			mobileHeaders.forEach(headerEl => {
				const body = shell.querySelector(`.mobileLazyBody[data-secbody="${CSS.escape(headerEl.dataset.sec)}"]`);
				if (!body || headerEl.classList.contains('hidden')) return;
				const items = Array.isArray(body.__filteredItems) ? body.__filteredItems : [];
				items.forEach(item => dataRows.push(rowFromItem(item)));
			});
		} else {
			dataRows = entries.filter(entry => entry.visible).map(entry => rowFromDataset(entry.el.dataset));
		}
		const stamp = new Date().toISOString().slice(0,10);
		downloadCsv(`numista_${prefix}_selected_${stamp}.csv`, [header, ...dataRows]);
	}


	function stripOuterParens(expr){
		expr = String(expr || '').trim();
		while (expr.startsWith('(') && expr.endsWith(')')){
			let depth = 0, ok = true, quote = '';
			for (let i=0; i<expr.length; i++){
				const ch = expr[i];
				if (quote){ if (ch === quote) quote = ''; continue; }
				if (ch === '"' || ch === "'"){ quote = ch; continue; }
				if (ch === '(') depth++;
				else if (ch === ')'){
					depth--;
					if (depth === 0 && i < expr.length - 1){ ok = false; break; }
				}
			}
			if (!ok) break;
			expr = expr.slice(1, -1).trim();
		}
		return expr;
	}
	function splitTopLevel(expr, ops){
		expr = String(expr || '');
		const out = [];
		let depth = 0, quote = '', start = 0;
		function isBoundary(i){ return i < 0 || i >= expr.length || /[^a-zA-Z0-9_]/.test(expr[i]); }
		for (let i=0; i<expr.length; i++){
			const ch = expr[i];
			if (quote){ if (ch === quote) quote = ''; continue; }
			if (ch === '"' || ch === "'"){ quote = ch; continue; }
			if (ch === '('){ depth++; continue; }
			if (ch === ')'){ depth = Math.max(0, depth - 1); continue; }
			if (depth !== 0) continue;
			let matched = null, len = 0;
			for (const op of ops){
				if (op === ',' || op === '|' || op === '&'){
					if (ch === op){ matched = op; len = 1; break; }
				} else {
					const part = expr.slice(i, i + op.length);
					if (part.toUpperCase() === op && isBoundary(i - 1) && isBoundary(i + op.length)){
						matched = op; len = op.length; break;
					}
				}
			}
			if (matched){
				const piece = expr.slice(start, i).trim();
				if (piece) out.push(piece);
				i += len - 1;
				start = i + 1;
			}
		}
		const last = expr.slice(start).trim();
		if (last) out.push(last);
		return out;
	}
	function yearMatchesTarget(target, value){
		const rules = parseYearFilter(String(value || '').trim());
		if (!rules) return false;
		const years = Array.isArray(target.years) ? target.years : [];
		const hasExact = years.length > 0;
		const minYear = Number.isFinite(target.minYear) ? target.minYear : 999999;
		const maxYear = Number.isFinite(target.maxYear) ? target.maxYear : 999999;
		return rules.some(rule => {
			if (rule.type === 'single'){
				if (years.includes(rule.year)) return true;
				return !hasExact && minYear !== 999999 && maxYear !== 999999 && rule.year >= minYear && rule.year <= maxYear;
			}
			if (years.some(y => y >= rule.min && y <= rule.max)) return true;
			return !hasExact && minYear !== 999999 && maxYear !== 999999 && !(rule.max < minYear || rule.min > maxYear);
		});
	}
	function diameterMatchesTarget(target, value){
		const rules = parseDiameterFilter(String(value || '').trim());
		return diameterMatches(target.diameter, rules);
	}
	function targetText(target, field){
		const all = [target.search, target.country, target.issuer, target.title, target.name, target.ref, target.currency, target.grade, target.category, target.object, target.composition, target.continent, String(target.weight || '')].join(' ');
		const fields = {
			all,
			country: target.country,
			issuer: target.issuer,
			issuerpath: target.issuer,
			title: target.title,
			name: target.name || target.title,
			currency: target.currency,
			ref: target.ref,
			reference: target.ref,
			grade: target.grade,
			category: target.category,
			object: target.object,
			type: target.object,
			continent: target.continent,
			material: target.composition,
			composition: target.composition,
			weight: String(target.weight || '')
		};
		return fields[field] ?? all;
	}
	function termMatchesAdvanced(term, target){
		term = stripOuterParens(String(term || '').trim());
		if (!term) return true;
		if ((term.startsWith('"') && term.endsWith('"')) || (term.startsWith("'") && term.endsWith("'"))) term = term.slice(1, -1);
		let m = term.match(/^(year|date|diameter|size|weight)\s*(>=|<=|>|<|=)\s*(.+)$/i);
		if (m){
			const field = m[1].toLowerCase();
			const op = m[2], raw = m[3].trim();
			if (field === 'year' || field === 'date'){
				const n = parseInt(raw, 10);
				if (!Number.isFinite(n)) return false;
				if (op === '=' || op === '==') return yearMatchesTarget(target, String(n));
				const years = (Array.isArray(target.years) && target.years.length) ? target.years : [target.minYear, target.maxYear].filter(y => Number.isFinite(y) && y !== 999999);
				if (!years.length) return false;
				if (op === '>=') return years.some(y => y >= n);
				if (op === '>') return years.some(y => y > n);
				if (op === '<=') return years.some(y => y <= n);
				if (op === '<') return years.some(y => y < n);
			}
			const d = Number.parseFloat(field === 'weight' ? target.weight : target.diameter);
			const n = Number.parseFloat(raw);
			if (!Number.isFinite(d) || !Number.isFinite(n)) return false;
			if (op === '=' || op === '==') return Math.abs(d - n) < 0.11;
			if (op === '>=') return d >= n;
			if (op === '>') return d > n;
			if (op === '<=') return d <= n;
			if (op === '<') return d < n;
		}
		m = term.match(/^([a-zA-Z_]+)\s*:\s*(.+)$/);
		if (m){
			const field = m[1].toLowerCase();
			const value = m[2].trim();
			if (field === 'year' || field === 'date') return yearMatchesTarget(target, value);
			if (field === 'diameter' || field === 'size') return diameterMatchesTarget(target, value);
			if (field === 'weight') return diameterMatches(target.weight, parseDiameterFilter(value));
			return fuzzyMatchNorm(normText(value), normText(targetText(target, field)));
		}
		if (/^-?\d{1,4}\s*[-–]\s*-?\d{1,4}$/.test(term)) return yearMatchesTarget(target, term);
		return fuzzyMatchNorm(normText(term), normText(targetText(target, 'all')));
	}
	function advancedQueryMatches(query, target){
		let expr = stripOuterParens(String(query || '').trim());
		if (!expr) return true;
		const andParts = splitTopLevel(expr, ['AND', '&']);
		if (andParts.length > 1) return andParts.every(part => advancedQueryMatches(part, target));
		const orParts = splitTopLevel(expr, ['OR', ',', '|']);
		if (orParts.length > 1) return orParts.some(part => advancedQueryMatches(part, target));
		let neg = false;
		while (true){
			expr = expr.trim();
			if (expr.startsWith('!')){ neg = !neg; expr = expr.slice(1); continue; }
			if (/^NOT\b/i.test(expr)){ neg = !neg; expr = expr.replace(/^NOT\b/i, ''); continue; }
			break;
		}
		const result = termMatchesAdvanced(expr, target);
		return neg ? !result : result;
	}
	function countrySearchAliases(value){
		const n = normText(value || '');
		const extra = [];
		if (n.includes('united states')) extra.push('usa us america united states of america');
		if (n.includes('united kingdom')) extra.push('uk great britain britain england scotland wales northern ireland');
		return [value || '', ...extra].join(' ');
	}

	function searchTargetFromEntry(entry){
		const ds = entry.el.dataset || {};
		return {
			search: ds.search || '',
			country: countrySearchAliases([ds.issuerroot || '', ds.issuerpath || ''].join(' ')),
			issuer: ds.issuerpath || ds.issuerroot || '',
			title: ds.title || '',
			name: ds.title || '',
			ref: ds.ref || '',
			currency: ds.currency || '',
			grade: ds.grade || '',
			category: ds.category || '',
			object: ds.objtype || '',
			years: entry.years || [],
			minYear: entry.minYear,
			maxYear: entry.maxYear,
			diameter: entry.sizeMm,
			weight: entry.weightG,
			composition: entry.composition || '',
			continent: entry.continent || ''
		};
	}
	function searchTargetFromMobileItem(item){
		return {
			search: [item.label || '', item.title_full || '', item.currency || '', item.km_y || '', item.year_str || '', item.grade_str || '', item.issuer_root || '', item.issuer_path || '', item.category || '', item.object_type || '', item.composition || '', item.weight_g || ''].join(' '),
			country: countrySearchAliases([item.issuer_root || '', item.issuer_path || ''].join(' ')),
			issuer: item.issuer_path || item.issuer_root || '',
			title: [item.label || '', item.title_full || ''].join(' '),
			name: [item.label || '', item.title_full || ''].join(' '),
			ref: item.km_y || '',
			currency: item.currency || '',
			grade: item.grade_str || '',
			category: item.category || '',
			object: item.object_type || '',
			years: mobileItemYears(item),
			minYear: mobileItemMinYear(item),
			maxYear: mobileItemMaxYear(item),
			diameter: Number.parseFloat(item.size_mm || ''),
			weight: Number.parseFloat(item.weight_g || ''),
			composition: item.composition || '',
			continent: item.continent || ''
		};
	}

	function matchesForcedTimelineRange(years, hasExactYears, minYear, maxYear){
	  if (!forcedTimelineYearRange) return true;
	  const start = forcedTimelineYearRange.start;
	  const end = forcedTimelineYearRange.end;
	  if (!Number.isFinite(start) || !Number.isFinite(end)) return true;
	  const exactYears = Array.isArray(years) ? years : [];
	  if (exactYears.some(y => y >= start && y <= end)) return true;
	  return !hasExactYears && minYear !== 999999 && maxYear !== 999999 && !(end < minYear || start > maxYear);
	}

	function setSelectedIssuers(issuerRoots){
	  const wanted = new Set((Array.isArray(issuerRoots) ? issuerRoots : []).filter(Boolean));
	  const allIssuers = uniqSorted(Object.values(contMap).reduce((acc,v)=>acc.concat(v), []));
	  let forcedCont = 'ALL';
	  if (wanted.size){
		const matching = Object.entries(contMap).filter(([_, issuers]) => issuers.some(v => wanted.has(v))).map(([cont]) => cont);
		if (matching.length === 1) forcedCont = matching[0];
	  }
	  contSel.value = forcedCont;
	  const visibleList = (forcedCont === 'ALL') ? allIssuers : uniqSorted(contMap[forcedCont] || []);
	  renderIssuerCheckboxes(visibleList, wanted);
	  searchEl.value = '';
	  applyFilters();
	}
	function setSearchQuery(query){
	  forcedTimelineYearRange = null;
	  coinSearchEl.value = query || '';
	  applyFilters();
	}
	function setTimelineYearRange(start, end, query){
	  const sYear = parseInt(start, 10);
	  const eYear = parseInt(end, 10);
	  forcedTimelineYearRange = {start: sYear, end: eYear};
	  coinSearchEl.value = query || (sYear === eYear ? `year:${sYear}` : `year:${sYear}-${eYear}`);
	  applyFilters();
	  requestAnimationFrame(applyFilters);
	  setTimeout(applyFilters, 60);
	}

	function applyFilters(){
		const cont = contSel.value;
		const selected = getSelectedIssuers();
		const selectedSet = new Set(selected);
		const q = normText(searchEl.value || '');
		const queryText = coinSearchEl.value || '';
		const objT = objTypeSel.value;
		const cat = catSel.value;
		boxEl.querySelectorAll('.issuerItem').forEach(lbl => {
			const name = lbl.dataset.norm || normText(lbl.textContent || '');
			lbl.style.display = (!q || name.includes(q)) ? '' : 'none';
		});
		const totalAvailable = Array.from(boxEl.querySelectorAll('input[type=checkbox]')).filter(cb => cb.parentElement.style.display !== 'none').length;
		countryCountEl.textContent = `Countries: ${selected.length} selected / ${totalAvailable} available`;
		let shownItems = 0, shownTypes = 0, shownIssuerOnly = 0, visibleCards = 0;
		if (isMobile){
			const filters = { cont, selectedSet, objT, cat };
			mobileHeaders.forEach(header => {
				const secKey = header.dataset.sectionkey || '';
				const body = shell.querySelector(`.mobileLazyBody[data-secbody="${CSS.escape(header.dataset.sec)}"]`);
				if (!body) return;
				const allItems = Array.isArray(body.__allItems) ? body.__allItems : [];
				const filteredItems = allItems.filter(item => {
					const years = mobileItemYears(item);
					const minYear = mobileItemMinYear(item);
					const maxYear = mobileItemMaxYear(item);
					return mobileItemMatches(item, filters) && advancedQueryMatches(queryText, searchTargetFromMobileItem(item)) && matchesForcedTimelineRange(years, years.length > 0, minYear, maxYear);
				});
				body.__filteredItems = filteredItems;
				const hasVisible = filteredItems.length > 0;
				header.classList.toggle('hidden', !hasVisible);
				if (!hasVisible){
					body.classList.add('hidden'); body.innerHTML = ''; body.dataset.rendered = '0';
					const icon = header.querySelector('.chev'); if (icon) icon.textContent = '▸';
					return;
				}
				filteredItems.forEach(item => {
					visibleCards += 1;
					if (item.is_issuer_only) shownIssuerOnly += 1;
					else { shownTypes += 1; shownItems += (parseInt(item.qty || 0, 10) || 0); }
				});
				if (!body.classList.contains('hidden') || body.dataset.rendered === '1') renderMobileSectionBody(body, secKey, filteredItems);
			});
			emptyStateEl.style.display = visibleCards === 0 ? 'block' : 'none';
			metricsEl.textContent = shownIssuerOnly > 0 ? `Showing: ${shownItems} items | ${shownTypes} types | ${shownIssuerOnly} issuer-wishes` : `Showing: ${shownItems} items | ${shownTypes} types`;
			liveStatsEl.innerHTML = `<span>Visible <b>${visibleCards}</b></span><span>Qty <b>${shownItems}</b></span>`;
			mobileResults.textContent = `${visibleCards} shown`;
			if (visibleQtyEl) visibleQtyEl.textContent = String(shownItems);
			if (visibleTypesEl) visibleTypesEl.textContent = String(shownTypes);
			renderChips({ cont, countries:selected, coinQ:queryText.trim(), objT, cat });
			return;
		}
		entries.forEach(entry => {
			const okCont = (cont === 'ALL' || entry.continent === cont);
			const okIssuer = (selectedSet.size === 0 || selectedSet.has(entry.issuerroot));
			const okSearch = advancedQueryMatches(queryText, searchTargetFromEntry(entry));
			const okObj = (objT === 'ALL' || !objT || entry.objType === objT);
			const okCat = (cat === 'ALL' || !cat || entry.category === cat);
			const okTimelineYear = matchesForcedTimelineRange(entry.years, entry.hasExactYears, entry.minYear, entry.maxYear);
			const visible = okCont && okIssuer && okSearch && okObj && okCat && okTimelineYear;
			entry.visible = visible;
			entry.el.classList.toggle('hidden', !visible);
			if (visible){
				visibleCards += 1;
				if (entry.isIssuerOnly) shownIssuerOnly += 1;
				else { shownTypes += 1; shownItems += entry.qty; }
			}
		});
		applyGridOrder(sortSel.value || 'none');
		grids.forEach(grid => {
			const anyVisible = (gridEntries.get(grid) || []).some(entry => entry.visible);
			grid.classList.toggle('hidden', !anyVisible);
			const h3 = grid.previousElementSibling;
			if (h3 && h3.tagName === 'H3') h3.classList.toggle('hidden', !anyVisible);
		});
		sections.forEach(sec => {
			const anyVisible = sec.body.querySelector('.card:not(.hidden)');
			sec.body.classList.toggle('hidden', !anyVisible);
			if (sec.header) sec.header.classList.toggle('hidden', !anyVisible);
		});
		emptyStateEl.style.display = visibleCards === 0 ? 'block' : 'none';
		metricsEl.textContent = shownIssuerOnly > 0 ? `Showing: ${shownItems} items | ${shownTypes} types | ${shownIssuerOnly} issuer-wishes` : `Showing: ${shownItems} items | ${shownTypes} types`;
		liveStatsEl.innerHTML = `<span>Visible <b>${visibleCards}</b></span><span>Qty <b>${shownItems}</b></span>`;
		mobileResults.textContent = `${visibleCards} shown`;
		if (visibleQtyEl) visibleQtyEl.textContent = String(shownItems);
		if (visibleTypesEl) visibleTypesEl.textContent = String(shownTypes);
		renderChips({ cont, countries:selected, coinQ:queryText.trim(), objT, cat });
	renderStickyBar({ cont, countries:selected, coinQ:queryText.trim(), objT, cat }, shownItems, shownTypes, visibleCards);
	}

	const debouncedApply = debounce(applyFilters, 160);
	contSel.addEventListener('change', updateIssuerList);
	searchEl.addEventListener('input', debouncedApply);
	boxEl.addEventListener('change', applyFilters);
	objTypeSel.addEventListener('change', applyFilters);
	catSel.addEventListener('change', applyFilters);
	sortSel.dataset.default = sortSel.value || 'none';
	sortSel.addEventListener('change', () => { lastSortMode = null; applyFilters(); });
	coinSearchEl.addEventListener('input', () => { forcedTimelineYearRange = null; debouncedApply(); });
	if (yearEl) yearEl.addEventListener('input', debouncedApply);
	if (sizeEl) sizeEl.addEventListener('input', debouncedApply);
	clearBtn.addEventListener('click', resetAllFilters);
	if (exportCsvBtn) exportCsvBtn.addEventListener('click', exportCurrentCsv);
	if (savedViewSel) savedViewSel.addEventListener('change', () => applySavedView(savedViewSel.value));
	if (viewModeSel) viewModeSel.addEventListener('change', () => { shell.classList.toggle('view-list', viewModeSel.value === 'list'); });
	if (stickyBarEl) stickyBarEl.addEventListener('click', e => { const target = e.target; if (!target) return; if (target.dataset.action === 'clear') { resetAllFilters(); return; } if (target.dataset.action === 'chip') { const chip = target.closest('.chip'); if (chip && chip.dataset.k) { clearOneFilter(chip.dataset.k); applyFilters(); } } });
	emptyClearBtn.addEventListener('click', resetAllFilters);
	mobileClearBtn.addEventListener('click', resetAllFilters);
	mobileFilterToggle.addEventListener('click', () => filtersPanel.classList.toggle('mobile-open'));
	chipsEl.addEventListener('click', (e) => { const btn=e.target.closest('button'); if (!btn) return; const chip=btn.closest('.chip'); if (!chip) return; clearOneFilter(chip.dataset.k || ''); applyFilters(); });

	if (!isMobile){
		Array.from(shell.querySelectorAll('.desktopSectionHeader')).forEach(h2 => { h2.addEventListener('click', () => { const id = h2.dataset.sec; const body = shell.querySelector(`.desktopSectionBody[data-secbody="${CSS.escape(id)}"]`); if (!body) return; const isHidden = body.classList.toggle('hidden'); const chev = h2.querySelector('.chev'); if (chev) chev.textContent = isHidden ? '▸' : '▾'; }); });
	}

	modeControllers[prefix] = { resetAllFilters, setSelectedIssuers, setSearchQuery, setTimelineYearRange, applyFilters };
	if (prefix === 'collection'){
	  function applyTimelineRangeOnly(start, end, query){
		const sYear = parseInt(start, 10);
		const eYear = parseInt(end, 10);
		if (!Number.isFinite(sYear) || !Number.isFinite(eYear)) return;
		forcedTimelineYearRange = {start:sYear, end:eYear};
		contSel.value = 'ALL';
		searchEl.value = '';
		coinSearchEl.value = query || (sYear === eYear ? `year:${sYear}` : `year:${sYear}-${eYear}`);
		objTypeSel.value = 'ALL';
		catSel.value = 'ALL';
		if (yearEl) yearEl.value = '';
		if (sizeEl) sizeEl.value = '';
		const defaultSort = sortSel.dataset.default || sortSel.value || 'none';
		sortSel.value = defaultSort;
		lastSortMode = null;
		const allIssuers = uniqSorted(Object.values(contMap).reduce((acc,v)=>acc.concat(v), []));
		renderIssuerCheckboxes(allIssuers, new Set());

		let shownItems = 0, shownTypes = 0, shownIssuerOnly = 0, visibleCards = 0;
		entries.forEach(entry => {
		  const years = Array.isArray(entry.years) ? entry.years : [];
		  const hasExact = years.length > 0;
		  const visible = matchesForcedTimelineRange(years, hasExact, entry.minYear, entry.maxYear);
		  entry.visible = visible;
		  entry.el.classList.toggle('hidden', !visible);
		  if (visible){
			visibleCards += 1;
			if (entry.isIssuerOnly) shownIssuerOnly += 1;
			else { shownTypes += 1; shownItems += entry.qty; }
		  }
		});
		applyGridOrder(sortSel.value || 'none');
		grids.forEach(grid => {
		  const anyVisible = (gridEntries.get(grid) || []).some(entry => entry.visible);
		  grid.classList.toggle('hidden', !anyVisible);
		  const h3 = grid.previousElementSibling;
		  if (h3 && h3.tagName === 'H3') h3.classList.toggle('hidden', !anyVisible);
		});
		sections.forEach(sec => {
		  const anyVisible = sec.body.querySelector('.card:not(.hidden)');
		  sec.body.classList.toggle('hidden', !anyVisible);
		  if (sec.header) sec.header.classList.toggle('hidden', !anyVisible);
		});
		emptyStateEl.style.display = visibleCards === 0 ? 'block' : 'none';
		metricsEl.textContent = shownIssuerOnly > 0 ? `Showing: ${shownItems} items | ${shownTypes} types | ${shownIssuerOnly} issuer-wishes` : `Showing: ${shownItems} items | ${shownTypes} types`;
		liveStatsEl.innerHTML = `<span>Visible <b>${visibleCards}</b></span><span>Qty <b>${shownItems}</b></span>`;
		mobileResults.textContent = `${visibleCards} shown`;
		if (visibleQtyEl) visibleQtyEl.textContent = String(shownItems);
		if (visibleTypesEl) visibleTypesEl.textContent = String(shownTypes);
		renderChips({ cont:'ALL', countries:[], coinQ:coinSearchEl.value, objT:'ALL', cat:'ALL' });
		renderStickyBar({ cont:'ALL', countries:[], coinQ:coinSearchEl.value, objT:'ALL', cat:'ALL' }, shownItems, shownTypes, visibleCards);
		refreshMobileJump();
	  }
	  window.__NUMISTA_APPLY_TIMELINE_FILTER = function(start, end, query){
		openPanel('panel-collection');
		applyTimelineRangeOnly(start, end, query);
		setTimeout(() => applyTimelineRangeOnly(start, end, query), 80);
		setTimeout(() => applyTimelineRangeOnly(start, end, query), 220);
	  };
	}
	updateIssuerList(); requestAnimationFrame(() => { applyFilters(); refreshMobileJump(); });
  }
  function navigateToMode(prefix, issuerRoots, searchQuery, timelineRange){
	const panelId = prefix === 'wishlist' ? 'panel-wishlist' : 'panel-collection';
	openPanel(panelId);
	const controller = modeControllers[prefix];
	if (!controller) return;
	controller.resetAllFilters();
	controller.setSelectedIssuers(issuerRoots);
	if (timelineRange && controller.setTimelineYearRange){
	  controller.setTimelineYearRange(timelineRange.start, timelineRange.end, searchQuery);
	} else if (searchQuery && controller.setSearchQuery) {
	  controller.setSearchQuery(searchQuery);
	}
  }
  document.querySelectorAll('.analyticsNavRow').forEach(row => {
	row.style.cursor = 'pointer';
	row.title = 'Open filtered view';
	row.addEventListener('click', () => {
	  let issuerRoots = [];
	  try { issuerRoots = JSON.parse(row.dataset.issuerRoots || '[]'); } catch (e) {}
	  navigateToMode(row.dataset.navPrefix || 'collection', issuerRoots);
	});
  });
  try { initMode('collection'); } catch (e) { console.error('Failed to initialize collection filters', e); }
  try { initMode('wishlist'); } catch (e) { console.error('Failed to initialize wishlist filters', e); }
  setTimeout(() => renderAnalytics(), 0);
})();
</script>""")
	html.append("</body></html>")
	Path(out_html).parent.mkdir(parents=True, exist_ok=True)
	Path(out_html).write_text('\n'.join(html), encoding='utf-8')
def render_html(rows: list[dict], title: str, out_html: str, total_items: int, total_types: int,
				include_stored: bool = False, include_filters: bool = False) -> None:
	"""Render the gallery. If include_filters=True, adds continent/country filters and (collection only) year filter."""
	css = """
	:root {
	  --bg: #f5f7fb;
	  --surface: #ffffff;
	  --surface-soft: #f9fbff;
	  --text: #162033;
	  --muted: #5b6474;
	  --border: #d9e0ec;
	  --accent: #2a52be;
	  --accent-soft: #eef3ff;
	  --shadow: 0 10px 24px rgba(21, 34, 61, 0.08);
	}
	body { font-family: Arial, sans-serif; margin: 20px; background: var(--bg); color: var(--text); }
	h1 { margin-bottom: 6px; }
	.summary { color: var(--muted); margin: 0 0 14px; }
	.analyticsStrip { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 0 0 16px; }
	.kpiCard { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 12px 14px; box-shadow: var(--shadow); }
	.kpiLabel { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
	.kpiValue { font-size: 22px; font-weight: bold; }
	.kpiSub { font-size: 12px; color: var(--muted); margin-top: 4px; }
	h2 { border-bottom: 2px solid var(--accent); padding-bottom: 6px; margin-top: 26px; cursor: pointer; user-select: none; }
	h2 .secCount { font-weight: normal; color: var(--muted); font-size: 12px; margin-left: 10px; }
	h2 .chev { font-weight: normal; color: #666; margin-right: 8px; }
	h3 { margin: 12px 0 10px; color: #1f3f94; }
	.sectionBody { margin-top: 8px; }
	.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 16px; margin: 12px 0 22px; }
	.card { border: 1px solid var(--border); border-radius: 14px; padding: 12px; background: var(--surface); box-shadow: var(--shadow); }
	.cardTop { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom: 8px; }
	.cardBadges { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }
	.imgs { display: flex; gap: 10px; align-items: center; justify-content: center; margin-bottom: 10px; }
	.imgs img { width: 112px; height: auto; border-radius: 8px; border: 1px solid #eef1f6; background:#fff; aspect-ratio: 1 / 1; object-fit: contain; }
	.cardMain { display:flex; flex-direction:column; gap:6px; }
	.valueLine { display:flex; align-items:flex-start; justify-content:space-between; gap:8px; }
	.valueTitle { font-size: 16px; line-height: 1.2; font-weight: bold; margin:0; display:flex; flex-wrap:wrap; align-items:baseline; gap:6px; } .valueTitleExtra { font-size:.78em; font-weight:600; color:inherit; }
	.refText { font-size: 12px; line-height:1.2; color: var(--muted); text-align:right; min-width: fit-content; }
	.subTitle { font-size: 13px; line-height: 1.3; color: var(--text); margin:0; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
	.metaRow { font-size: 12px; color: var(--muted); margin: 0; }
	.badge { display: inline-block; font-size: 11px; padding: 3px 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); }
	.badge.badge-muted { background:#eef1f6; color:#465066; }
	.badge.badge-alert { background:#fff1e7; color:#a24b00; }
	.badge.badge-dup-type { background:#ffe9e9; color:#a00000; }
	.badge.badge-dup-year { background:#fff4cc; color:#7a4b00; }
	.badge.badge-wish { background:#eef3ff; color:#2a52be; }
	.badge.badge-exo { background:#f1ecff; color:#5b3db8; }
	.badge.badge-transit { background:#e8f7ef; color:#087443; }
	a { color: var(--accent); text-decoration: none; }
	a:hover { text-decoration: underline; }
	.filters { margin: 8px 0 12px; position: sticky; top: 0; background: var(--surface-soft); padding: 10px; z-index: 2000; border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); }
	.filtersGrid{ display:grid; grid-template-columns: 1.05fr 1.35fr 1.55fr 1.05fr 1.05fr 1.25fr; grid-template-rows: auto auto auto; column-gap: 10px; row-gap: 6px; align-items: start; }
	.fCell label { display:block; font-size: 12px; color:#333; margin-bottom: 4px; }
	.fCell select, .fCell input[type=text] { width: 100%; box-sizing: border-box; padding: 7px 9px; font-size: 12px; border:1px solid var(--border); border-radius:10px; background:#fff; }
	.fCell button, .mobileToolbar button, .emptyState button { padding: 8px 11px; font-size: 12px; border-radius: 10px; border:1px solid var(--border); background:#fff; cursor:pointer; }
	.fCont { grid-column: 1; grid-row: 1; }
	.fCountrySearch { grid-column: 1; grid-row: 2; }
	.fIssuerBox { grid-column: 2; grid-row: 1 / span 2; align-self: start; padding-top: 18px; box-sizing: border-box; }
	.fCoin { grid-column: 3; grid-row: 1; }
	.fYear { grid-column: 3; grid-row: 2; }
	.fClear { grid-column: 4; grid-row: 2; align-self: start; padding-top: 18px; }
	.fSort { grid-column: 4; grid-row: 1; }
	.fObject { grid-column: 5; grid-row: 1; }
	.fCategory { grid-column: 5; grid-row: 2; }
	.fRight { grid-column: 6; grid-row: 1 / span 3; display:flex; flex-direction:column; gap: 8px; align-items:flex-end; }
	.rightTop { font-size:12px; color:var(--muted); text-align:right; line-height:1.35; }
	.chips { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
	.chip { display:inline-flex; align-items:center; gap:8px; padding:4px 10px; border-radius:999px; border:1px solid var(--border); background:#f6f8ff; font-size:12px; color:#223; }
	.chip button { border:0; background:transparent; cursor:pointer; font-size:14px; line-height:1; padding:0; color:#445; }
	.live-stats { font-size:12px; color:var(--muted); display:flex; gap:14px; align-items:center; justify-content:flex-end; }
	.live-stats b { color: var(--text); }
	.issuerBox { border: 1px solid var(--border); border-radius: 10px; padding: 6px; width: 100%; height: 96px; overflow-y: auto; overflow-x: hidden; background: #fff; box-sizing: border-box; }
	.issuerItem { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 2px 0; }
	.issuerItem input { margin: 0; width: 14px; height: 14px; }
	.hidden { display: none !important; }
	.emptyState { display:none; margin: 14px 0 20px; padding: 20px; border:1px dashed #b8c4d9; border-radius: 14px; background:#fff; text-align:center; color:var(--muted); }
	.emptyState h3 { margin:0 0 8px; color: var(--text); }
	.emptyState p { margin:0 0 12px; }
	.mobileToolbar { display:none; gap:8px; align-items:center; margin: 0 0 12px; }
	.mobileToolbar .mobileResults { margin-left:auto; font-size:12px; color:var(--muted); background:#fff; border:1px solid var(--border); border-radius:999px; padding:7px 10px; }
	.mobileNavDock{ display:none; }
	.mobileNavDock select,.mobileNavDock button{ border:1px solid var(--border); background:#fff; box-shadow: var(--shadow); }
	.rightTop .metrics { display:block; }
	#backToTop { position: fixed; bottom: 18px; right: 18px; padding: 10px 12px; border-radius: 999px; border: 1px solid #ccc; background: white; font-size: 14px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.15); display: none; z-index: 9999; }
	#backToTop:hover { background: #f2f2f2; }
	@media (max-width: 1100px){
		.filtersGrid{ grid-template-columns: 1.05fr 1.35fr 1.55fr 1.05fr 1.05fr; }
		.filtersGrid > div { min-width: 0; }
		.fCont{ grid-column: 1; grid-row: 1; }
		.fIssuerBox{ grid-column: 2; grid-row: 1 / span 2; }
		.fCoin{ grid-column: 3; grid-row: 1; }
		.fSort{ grid-column: 4; grid-row: 1; }
		.fObject{ grid-column: 5; grid-row: 1; }
		.fCountrySearch{ grid-column: 1; grid-row: 2; }
		.fYear{ grid-column: 3; grid-row: 2; }
		.fClear{ grid-column: 4; grid-row: 2; align-self: end; padding-top: 0; }
		.fCategory{ grid-column: 5; grid-row: 2; }
		.fRight{ grid-column: 1 / span 5; grid-row: 3; }
	}
	@media (hover:hover) and (pointer:fine) { .card { transition: transform 0.15s ease, box-shadow 0.15s ease; } .card:hover { transform: translateY(-3px); box-shadow: 0 8px 22px rgba(0,0,0,0.10); } }
	@media (max-width: 700px) {
	  body { margin: 12px; }
	  .analyticsStrip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
	  .mobileToolbar { display:flex; }
	  .filters { display:none; position: static; }
	  body { padding-right: 64px; }
	  .filters.mobile-open { display:block; }
	  .filtersGrid { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 8px; }
	  .fCont{ grid-column: 1; grid-row: 1; }
	  .fCoin{ grid-column: 2; grid-row: 1; }
	  .fCountrySearch{ grid-column: 1; grid-row: 2; }
	  .fYear{ grid-column: 2; grid-row: 2; }
	  .fSort{ grid-column: 1; grid-row: 3; }
	  .fObject{ grid-column: 2; grid-row: 3; }
	  .fCategory{ grid-column: 1; grid-row: 4; }
	  .fClear{ grid-column: 2; grid-row: 4; align-self:end; padding-top: 18px; }
	  .fIssuerBox{ grid-column: 1 / span 2; grid-row: 5; padding-top: 0; }
	  .fRight{ grid-column: 1 / span 2; grid-row: 6; align-items:flex-start; }
	  .rightTop, .chips, .live-stats { justify-content:flex-start; text-align:left; }
	  .rightTop{ width:100%; }
	  .rightTop .metrics + .metrics{ margin-top:4px; }
	  .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
	  .card { padding: 10px; }
	  .imgs { gap: 6px; margin-bottom: 8px; }
	  .imgs img { width: calc(50% - 3px); max-width:none; }
	  .valueTitle { font-size: 14px; }
	  .valueTitleExtra { font-size: .8em; }
	  .subTitle { font-size: 12px; }
	  .refText, .metaRow { font-size: 11px; }
	}
	"""
	# Precompute per-section counts (only meaningful for collection, but harmless for wishlist)
	sec_types = defaultdict(int)
	sec_items = defaultdict(int)
	for r in rows:
		sec = r.get("issuer_path") or ""
		if not sec:
			continue
		is_issuer_only = bool(r.get("is_issuer_only"))
		if not is_issuer_only:
			sec_types[sec] += 1
			try:
				sec_items[sec] += int(r.get("qty") or 0)
			except Exception:
				pass
	html: list[str] = []
	html.append("<!doctype html><html><head><meta charset='utf-8' />")
	html.append("<meta name='viewport' content='width=device-width, initial-scale=1' />")
	html.append(f"<title>{html_escape(title)}</title>")
	html.append(f"<style>{css}</style><script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script></head><body>")
	html.append(f"<h1>{html_escape(title)}</h1>")
	html.append(f"<p class='summary'>Total items: {total_items} &nbsp;|&nbsp; Total types: {total_types}</p>")
	unique_countries = len({(r.get('issuer_root') or '').strip() for r in rows if (r.get('issuer_root') or '').strip()})
	duplicates_count = sum(max(int(r.get('qty') or 0) - 1, 0) for r in rows if not r.get('is_issuer_only'))
	replicas_count = sum(1 for r in rows if r.get('is_replica'))
	on_transit_count = sum(1 for r in rows if r.get('is_on_transit'))
	issuer_only_count = sum(1 for r in rows if r.get('is_issuer_only'))
	html.append("<div class='analyticsStrip' id='analyticsStrip'>")
	html.append(f"<div class='kpiCard'><div class='kpiLabel'>Countries</div><div class='kpiValue' id='kpiCountries'>{unique_countries}</div><div class='kpiSub'>issuer roots</div></div>")
	html.append(f"<div class='kpiCard'><div class='kpiLabel'>Duplicates</div><div class='kpiValue' id='kpiDuplicates'>{duplicates_count}</div><div class='kpiSub'>extra pieces above 1</div></div>")
	html.append(f"<div class='kpiCard'><div class='kpiLabel'>Replicas</div><div class='kpiValue' id='kpiReplicas'>{replicas_count}</div><div class='kpiSub'>marked replica rows</div></div>")
	html.append(f"<div class='kpiCard'><div class='kpiLabel'>On transit</div><div class='kpiValue' id='kpiOnTransit'>{on_transit_count}</div><div class='kpiSub'>bought, not yet stored</div></div>")
	html.append(f"<div class='kpiCard'><div class='kpiLabel'>Issuer wishes</div><div class='kpiValue' id='kpiIssuerOnly'>{issuer_only_count}</div><div class='kpiSub'>wishlist placeholders</div></div>")
	html.append("<div class='kpiCard'><div class='kpiLabel'>Visible types</div><div class='kpiValue' id='kpiVisibleTypes'>0</div><div class='kpiSub'>after filters</div></div>")
	html.append("<div class='kpiCard'><div class='kpiLabel'>Visible qty</div><div class='kpiValue' id='kpiVisibleQty'>0</div><div class='kpiSub'>after filters</div></div>")
	html.append("</div>")
	if include_filters:
		continents = sorted({(r.get('continent') or 'Unknown') for r in rows}, key=lambda s: s.lower())
		cont_map = {}
		for r in rows:
			c = (r.get('continent') or 'Unknown')
			ir = (r.get('issuer_root') or '').strip()
			if not ir:
				continue
			cont_map.setdefault(c, set()).add(ir)
		cont_map = {k: sorted(list(v), key=lambda s: s.lower()) for k, v in cont_map.items()}

		html.append("<div class='mobileToolbar'><button id='mobileFilterToggle' type='button'>Filters</button><button id='mobileClearBtn' type='button'>Clear</button><span class='mobileResults' id='mobileResults'>0 shown</span></div>")
		html.append("<div class='filters' id='filtersPanel'>")
		html.append("<div class='filtersGrid'>")
		# Row 1 / Col 1: Continent
		continent_opts = "".join(
			f"<option value='{html_escape(c)}'>{html_escape(c)}</option>"
			for c in continents
		)
		html.append("<div class='fCell fCont'>")
		html.append("<label for='continentFilter'>Continent</label>")
		html.append("<select id='continentFilter'><option value='ALL'>All</option>" +
					continent_opts +
					"</select>")
		html.append("</div>")
		# Col 2 (spans rows 1-2): Country checkboxes
		html.append("<div class='fCell fIssuerBox'>")
		html.append("<div id='issuerBox' class='issuerBox'></div>")
		html.append("</div>")
		# Row 1 / Col 3: Coin search
		html.append("<div class='fCell fCoin'>")
		html.append("<label class='labelWithHelp' for='coinSearch'>Search <span class='helpBubble' tabindex='0' aria-label='Search help'>?<span class='helpPopover'><b>Search help</b><br/>Use <code>&amp;</code> for AND, <code>,</code> or <code>|</code> for OR, <code>!</code> for NOT.<br/><br/>Examples:<br/><code>country:usa,country:uk</code><br/><code>year:1950-1970</code><br/><code>diameter:17-20</code><br/><code>grade:XF</code><br/><code>!replica</code></span></span></label>")
		html.append("<input id='coinSearch' type='text' placeholder='country:chile & year:1950-1970 · diameter:17-20 · !replica · grade:XF' />")
		html.append("</div>")
		# Row 1 / Col 4: Sort
		if include_stored:
			html.append("<div class='fCell fSort'>")
			html.append("<label for='sortSel'>Sort</label>")
			html.append(
				"<select id='sortSel'>"
				"<option value='none' selected>Default (Numista)</option>"
				"<option value='face'>Face value</option>"
				"<option value='type'>Type</option>"
				"<option value='ref'>Reference (KM/Y)</option>"
				"<option value='date'>Date</option>"
				"</select>"
			)
			html.append("</div>")
		# Row 1 / Col 5: Object
		if include_stored:
			obj_types = sorted(
				{(r.get("object_type") or "").strip() for r in rows if (r.get("object_type") or "").strip()},
				key=lambda s: s.lower()
			)
			obj_type_opts = "".join(
				f"<option value='{html_escape(o)}'>{html_escape(o)}</option>"
				for o in obj_types
			)
			html.append("<div class='fCell fObject'>")
			html.append("<label for='objTypeFilter'>Object</label>")
			html.append("<select id='objTypeFilter'><option value='ALL'>All</option>" +
					obj_type_opts +
					"</select>")
			html.append("</div>")
		# Row 2 / Col 1: Country text search
		html.append("<div class='fCell fCountrySearch'>")
		html.append("<label for='issuerSearch'>Country search</label>")
		html.append("<input id='issuerSearch' type='text' placeholder='Search country...' />")
		html.append("</div>")
		# Year and diameter search are handled by the unified Search box.
		# Row 2 / Col 4: Clear (aligned with Year input)
		if include_stored:
			html.append("<div class='fCell fClear'>")
			html.append("<button id='clearAll' type='button'>Clear</button>")
			html.append("</div>")
		# Row 2 / Col 4: Clear (wishlist/simple)
		if not include_stored:
			html.append("<div class='fCell fClear'>")
			html.append("<button id='clearAll' type='button'>Clear</button>")
			html.append("</div>")
		# Row 2 / Col 5: Category
		if include_stored:
			cats = sorted(
				{(r.get("category") or "").strip() for r in rows if (r.get("category") or "").strip()},
				key=lambda s: s.lower()
			)
			cat_opts = "".join(
				f"<option value='{html_escape(c)}'>{html_escape(c)}</option>"
				for c in cats
			)
			html.append("<div class='fCell fCategory'>")
			html.append("<label for='catFilter'>Category</label>")
			html.append("<select id='catFilter'><option value='ALL'>All</option>" +
					cat_opts +
					"</select>")
			html.append("</div>")
		# Right panel (spans rows 1-3): summary + chips + live stats
		html.append("<div class='fRight'>")
		html.append("<div class='rightTop'>"
					"<span class='metrics' id='filterMetrics'></span>"
					"<span class='metrics' id='countryCount'></span>"
					"</div>")
		html.append("<div class='rightChips'><div id='activeChips' class='chips'></div></div>")
		if include_stored:
			html.append("<div class='rightStats'><div id='liveStats' class='live-stats'></div></div>")
		html.append("</div>")
		html.append("</div>")  # end filtersGrid
		html.append("</div>")  # end filters
		html.append("<script>const CONT_MAP = " + json.dumps(cont_map) + ";</script>")
		html.append("<div id='emptyState' class='emptyState'><h3>No coins match the current filters</h3><p>Try clearing one or more filters, or broaden the text search.</p><button id='emptyClearBtn' type='button'>Clear filters</button></div>")
	# Section/currency rendering with collapsible sections
	current_section = None
	current_currency = None
	section_open = False
	currency_open = False
	for r in rows:
		section = r["issuer_path"]
		section_flag = r.get("flag_url", "")
		if section != current_section:
			# close previous currency grid and section body
			if currency_open:
				html.append("</div>")  # close .grid
				currency_open = False
			if section_open:
				html.append("</div>")  # close .sectionBody
				section_open = False
			current_section = section
			current_currency = None
			# header with counts
			types_n = sec_types.get(section, 0)
			items_n = sec_items.get(section, 0)
			count_html = ""
			if types_n or items_n:
				count_html = f"<span class='secCount'>({items_n} items · {types_n} types)</span>"
			header_inner = f"<span class='chev'>▾</span>"
			if section_flag:
				header_inner += (
					f"<img src='{html_escape(section_flag)}' "
					f"style='width:20px;height:14px;object-fit:contain;vertical-align:middle;margin-right:8px'/>"
					f"{html_escape(section)}{count_html}"
				)
			else:
				header_inner += f"{html_escape(section)}{count_html}"
			sec_id = re.sub(r"[^a-zA-Z0-9]+", "_", section)[:80]
			html.append(f"<h2 class='sectionHeader' data-sec='{html_escape(sec_id)}'>{header_inner}</h2>")
			html.append(f"<div class='sectionBody' data-secbody='{html_escape(sec_id)}'>")
			section_open = True
		# Currency header
		if r["currency"] != current_currency:
			if currency_open:
				html.append("</div>")  # close previous .grid
				currency_open = False
			current_currency = r["currency"]
			html.append(f"<h3>{html_escape(current_currency)}</h3>")
			html.append("<div class='grid'>")
			currency_open = True
		# Card
		imgs = []
		if r.get("obv"):
			imgs.append(f"<img src='{html_escape(r['obv'])}' alt='obverse' loading='lazy' />")
		if r.get("rev"):
			imgs.append(f"<img src='{html_escape(r['rev'])}' alt='reverse' loading='lazy' />")
		imgs_html = "".join(imgs) if imgs else ""
		main_label = r.get("label","")
		ref_label = r.get("km_y","")
		title_main, title_extra = split_title_main_extra(main_label, r.get("title_full", ""))
		title_line = " ".join(b for b in [main_label, ref_label] if b)
		badge_mode = 'collection' if include_stored else 'wishlist'
		qty_badge = build_card_badges(r, badge_mode)
		stored_cb = ""
		# year metadata attributes (collection only)
		years_greg_list = r.get("years_greg_list") or (r.get("years_list") or [])
		years_raw_list = r.get("years_raw_list") or []
		years_attr = ",".join(str(y) for y in years_greg_list if isinstance(y, int))
		years_raw_attr = ",".join(str(y) for y in years_raw_list if isinstance(y, int))
		miny = r.get("min_year") or ""
		maxy = r.get("max_year") or ""
		search_blob = " ".join([
			(title_line or ""),
			(r.get("title_full") or ""),
			(r.get("currency") or ""),
			(r.get("km_y") or ""),
			(r.get("year_str") or ""),
			(r.get("grade_str") or ""),
			" ".join(str(y) for y in years_raw_list if isinstance(y,int)),
			" ".join(str(y) for y in years_greg_list if isinstance(y,int)),
			(r.get("issuer_root") or ""),
			(r.get("issuer_path") or ""),
			(r.get("issuer_search_es") or ""),
		]).lower()
		objtype_attr = f"data-objtype='{html_escape(r.get('object_type',''))}' " if include_stored else ""
		cat_attr = f"data-category='{html_escape(r.get('category',''))}' " if include_stored else ""

		html.append(
			f"<div class='card' "
			f"data-continent='{html_escape(r.get('continent','Unknown'))}' "
			f"data-issuerroot='{html_escape(r.get('issuer_root',''))}' "
			f"{objtype_attr}"
			f"{cat_attr}"
			f"data-qty='{r.get('qty',0)}' "
			f"data-facevalue='{html_escape(str(r.get('numeric_value','')))}' "
			f"data-facesort='{html_escape(str(r.get('face_sort_value', r.get('numeric_value',''))))}' "
			f"data-title='{html_escape(title_line)}' "
			f"data-ref='{html_escape(r.get('km_y',''))}' "
			f"data-isissueronly='{1 if r.get('is_issuer_only') else 0}' "
			f"data-duptype='{1 if r.get('duplicate_type') else 0}' "
			f"data-dupyear='{1 if r.get('duplicate_year') else 0}' "
			f"data-years='{html_escape(years_attr)}' "
			f"data-yearsraw='{html_escape(years_raw_attr)}' "
			f"data-minyear='{html_escape(str(miny))}' "
			f"data-maxyear='{html_escape(str(maxy))}' "
			f"data-search='{html_escape(search_blob)}'>"
		)
		html.append("<div class='cardTop'><div></div><div class='cardBadges'>" + qty_badge + stored_cb + "</div></div>")
		html.append(f"<div class='imgs'>{imgs_html}</div>")
		main_html = html_escape(title_main or main_label)
		if r.get('url'):
			main_html = f"<a href='{html_escape(r['url'])}' target='_blank' rel='noopener'>{main_html}</a>"
		extra_html = f"<span class='valueTitleExtra'>{html_escape(title_extra)}</span>" if title_extra else ""
		html.append("<div class='cardMain'>")
		html.append("<div class='valueLine'>" + f"<p class='valueTitle'>{main_html}{extra_html}</p>" + f"<div class='refText'>{html_escape(ref_label)}</div>" + "</div>")
		if r.get('currency'):
			html.append(f"<p class='subTitle' title='{html_escape(title_line)}'>{html_escape(r.get('currency',''))}</p>")
		meta1 = []
		meta2 = []
		if r.get('year_str'):
			meta1.append(f"Years: {html_escape(r['year_str'])}")
		if r.get('grade_str'):
			meta1.append(f"Grade: {html_escape(r['grade_str'])}")
		if r.get('size_mm') is not None:
			try:
				mm = float(r['size_mm'])
				meta2.append(f"{mm:g} mm")
			except Exception:
				pass
		if r.get('composition'):
			meta2.append(html_escape(r['composition']))
		if meta1:
			html.append("<p class='metaRow'>" + " · ".join(meta1) + "</p>")
		if meta2:
			html.append("<p class='metaRow'>" + " · ".join(meta2) + "</p>")
		html.append("</div>")
		html.append("</div>")  # close card
	# close open containers
	if currency_open:
		html.append("</div>")
	if section_open:
		html.append("</div>")
	# Back to top button
	html.append("<div class='mobileNavDock'><select id='mobileJumpIssuer'><option value=''>Jump</option></select><button id='backToTop'>↑</button></div>")
	# Filters + collapse + back-to-top JS
	scripts = []
	# Collapse / expand per country
	scripts.append(r"""
<script>
(function(){
  if (typeof CSS === 'undefined') { window.CSS = {}; }
  if (typeof CSS.escape !== 'function') {
	CSS.escape = function(s){ return String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&'); };
  }
  const headers = Array.from(document.querySelectorAll('.sectionHeader'));
  headers.forEach(h2 => {
	h2.addEventListener('click', () => {
	  const id = h2.dataset.sec;
	  const body = document.querySelector(`.sectionBody[data-secbody="${CSS.escape(id)}"]`);
	  if (!body) return;
	  const isHidden = body.classList.toggle('hidden');
	  const chev = h2.querySelector('.chev');
	  if (chev) chev.textContent = isHidden ? '▸' : '▾';
	});
  });
  const backBtn = document.getElementById('backToTop');
  const mobileJumpEl = document.getElementById('mobileJumpIssuer');
  function refreshMobileJump(){
	if (!mobileJumpEl) return;
	const headers = Array.from(document.querySelectorAll('.sectionHeader:not(.hidden)'));
	mobileJumpEl.innerHTML = "<option value=''>Jump</option>" + headers.map((h, i) => `<option value="${i}">${(h.textContent || '').replace(/[▾▸]/g,'').trim()}</option>`).join('');
	mobileJumpEl._headers = headers;
	mobileJumpEl.value = '';
  }
  window.addEventListener('scroll', () => {
	if (window.scrollY > 400) backBtn.style.display = 'block';
	else backBtn.style.display = 'none';
  });
  backBtn.addEventListener('click', () => window.scrollTo({top:0, behavior:'smooth'}));
  if (mobileJumpEl) mobileJumpEl.addEventListener('change', () => { const idx=parseInt(mobileJumpEl.value || '',10); const headers=mobileJumpEl._headers || []; if (!Number.isNaN(idx) && headers[idx]) headers[idx].scrollIntoView({behavior:'smooth', block:'start'}); mobileJumpEl.value=''; });
  refreshMobileJump();
})();
</script>
""")
	if include_filters:
		scripts.append(r"""
<script>
(function(){
  if (typeof CSS === 'undefined') { window.CSS = {}; }
  if (typeof CSS.escape !== 'function') {
	CSS.escape = function(s){ return String(s).replace(/[^a-zA-Z0-9_-]/g, '\$&'); };
  }
  const contSel = document.getElementById('continentFilter');
  const metricsEl = document.getElementById('filterMetrics');
  const countryCountEl = document.getElementById('countryCount');
  const clearBtn = document.getElementById('clearAll');
  const emptyClearBtn = document.getElementById('emptyClearBtn');
  const mobileClearBtn = document.getElementById('mobileClearBtn');
  const mobileFilterToggle = document.getElementById('mobileFilterToggle');
  const mobileResults = document.getElementById('mobileResults');
  const filtersPanel = document.getElementById('filtersPanel');
  const searchEl = document.getElementById('issuerSearch');
  const coinSearchEl = document.getElementById('coinSearch');
  const boxEl = document.getElementById('issuerBox');
  const objTypeSel = document.getElementById('objTypeFilter');
  const catSel = document.getElementById('catFilter');
  const chipsEl = document.getElementById('activeChips');
  const liveStatsEl = document.getElementById('liveStats');
  const sortSel = document.getElementById('sortSel');
  const emptyStateEl = document.getElementById('emptyState');
  const visibleQtyEl = document.getElementById('kpiVisibleQty');
  const visibleTypesEl = document.getElementById('kpiVisibleTypes');
  const yearEl = document.getElementById('yearFilter');
  const sizeEl = document.getElementById('sizeFilter');
  function debounce(fn, wait){ let t = null; return function(){ const args = arguments; clearTimeout(t); t = setTimeout(() => fn.apply(this, args), wait); }; }
  function isBlank(v){ return !v || !String(v).trim(); }
  function normText(s){ return (s || '').toString().toLowerCase().replace(/[øØ]/g, 'o').replace(/[æÆ]/g, 'ae').replace(/[œŒ]/g, 'oe').replace(/[åÅ]/g, 'a').replace(/[ðÐ]/g, 'd').replace(/[þÞ]/g, 'th').replace(/[łŁ]/g, 'l').replace(/ß/g, 'ss').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9# ]+/g, ' ').replace(/\s+/g, ' ').trim(); }
  function fuzzyMatchNorm(queryNorm, haystackNorm){ if (!queryNorm) return true; const q = queryNorm.split(' '); return q.every(tok => haystackNorm.includes(tok.replace(/^([a-z])\s*#?\s*(\d+)$/i, '$1#$2'))); }
  function parseYearFilter(input){ if (!input) return null; const parts = input.split(',').map(x=>x.trim()).filter(Boolean); const rules=[]; for (const p of parts){ if (p.includes('-') || p.includes('–')){ const seg=p.split(/[-–]/).map(x=>x.trim()); if (seg.length===2){ const a=parseInt(seg[0],10), b=parseInt(seg[1],10); if (!isNaN(a) && !isNaN(b)) rules.push({type:'range', min:Math.min(a,b), max:Math.max(a,b)}); } } else { const y=parseInt(p,10); if (!isNaN(y)) rules.push({type:'single', year:y}); } } return rules.length ? rules : null; }
  function parseRefRaw(raw){ const txt=(raw || '').toUpperCase().trim(); if (!txt) return {catRank:99,num:1e30,suffix:'',raw:''}; const matches=Array.from(txt.matchAll(/\b(KM|Y)\s*#?\s*([0-9]+(?:\.[0-9]+)?)([A-Z]*)\b/g)); if (!matches.length) return {catRank:99,num:1e30,suffix:'',raw:txt}; const rankMap={KM:0,Y:1}; const parsed=matches.map(m=>({catRank:rankMap[m[1]] ?? 99, num:parseFloat(m[2]), suffix:m[3] || '', raw:m[0]})); parsed.sort((a,b)=>a.catRank-b.catRank || a.num-b.num || a.suffix.localeCompare(b.suffix) || a.raw.localeCompare(b.raw)); return parsed[0]; }
  function parseSortNumber(value, fallback){ const n = Number.parseFloat(value ?? ''); return Number.isFinite(n) ? n : fallback; }
  function uniqSorted(arr){ return Array.from(new Set(arr)).sort((a,b)=>a.localeCompare(b, undefined, {sensitivity:'base'})); }
  function getSelectedIssuers(){ return Array.from(document.querySelectorAll('#issuerBox input[type=checkbox]:checked')).map(cb => cb.value); }
  function renderIssuerCheckboxes(list, selectedSet){ boxEl.innerHTML=''; list.forEach(name => { const safe=btoa(unescape(encodeURIComponent(name))).replace(/=+$/,''); const checked = selectedSet && selectedSet.has(name) ? ' checked' : ''; boxEl.insertAdjacentHTML('beforeend', `<label class="issuerItem" data-norm="${normText(name)}"><input type="checkbox" value="${name}" id="iss_${safe}"${checked}/> ${name}</label>`); }); }
  function renderChips(state){ if (!chipsEl) return; const chips=[]; if (state.cont && state.cont !== 'ALL') chips.push({k:'cont',label:`Continent: ${state.cont}`}); if (state.countries && state.countries.length) chips.push({k:'countries',label:`Countries: ${state.countries.length}`}); if (!isBlank(state.coinQ)) chips.push({k:'coinQ',label:`Search: ${state.coinQ}`}); if (state.objT && state.objT !== 'ALL') chips.push({k:'objT',label:`Object: ${state.objT}`}); if (state.cat && state.cat !== 'ALL') chips.push({k:'cat',label:`Category: ${state.cat}`}); if (yearEl && !isBlank(yearEl.value)) chips.push({k:'year',label:`Years: ${yearEl.value.trim()}`}); if (sizeEl && !isBlank(sizeEl.value)) chips.push({k:'size',label:`Diameter: ${sizeEl.value.trim()}`}); chipsEl.innerHTML = chips.map(ch => `<span class="chip" data-k="${ch.k}">${ch.label}<button type="button" aria-label="Remove">×</button></span>`).join(''); }
  function clearOneFilter(k){ if (k==='cont'){ contSel.value='ALL'; updateIssuerList(); return; } if (k==='countries'){ document.querySelectorAll('#issuerBox input[type=checkbox]').forEach(cb => cb.checked=false); return; } if (k==='coinQ' && coinSearchEl){ coinSearchEl.value=''; return; } if (k==='objT' && objTypeSel){ objTypeSel.value='ALL'; return; } if (k==='cat' && catSel){ catSel.value='ALL'; return; } if (k==='year' && yearEl){ yearEl.value=''; return; } if (k==='size' && sizeEl){ sizeEl.value=''; } }
  function ensureOrigOrder(){ document.querySelectorAll('.grid').forEach(grid => Array.from(grid.querySelectorAll('.card')).forEach((c, idx) => { if (!c.dataset.orig) c.dataset.orig = String(idx); })); }
  ensureOrigOrder();
  const grids = Array.from(document.querySelectorAll('.grid'));
  const sections = Array.from(document.querySelectorAll('.sectionBody')).map(body => ({ body, header: document.querySelector(`.sectionHeader[data-sec="${CSS.escape(body.dataset.secbody)}"]`) }));
  const entries = Array.from(document.querySelectorAll('.card')).map(card => { const qty=parseInt(card.dataset.qty || '0', 10) || 0; const yearsGreg=(card.dataset.years || '').split(',').map(x=>parseInt(x,10)).filter(x=>!isNaN(x)); const yearsRaw=(card.dataset.yearsraw || '').split(',').map(x=>parseInt(x,10)).filter(x=>!isNaN(x)); const seenYears=new Set(); const years=yearsGreg.concat(yearsRaw).filter(y => !seenYears.has(y) && (seenYears.add(y), true)); const hasExactYears=years.length > 0; const minYear=parseInt(card.dataset.minyear || '', 10); const maxYear=parseInt(card.dataset.maxyear || '', 10); return { el:card, grid:card.closest('.grid'), haystackNorm:normText(card.dataset.search || ''), continent:card.dataset.continent || 'Unknown', issuerroot:card.dataset.issuerroot || '', qty, isIssuerOnly:card.dataset.isissueronly === '1', objType:card.dataset.objtype || '', category:card.dataset.category || '', faceValue:parseSortNumber(card.dataset.facesort || card.dataset.facevalue, 1e30), minYear:Number.isFinite(minYear) ? minYear : 999999, maxYear:Number.isFinite(maxYear) ? maxYear : 999999, years, hasExactYears, sizeMm:Number.parseFloat(card.dataset.sizemm || ''), refKey:parseRefRaw(card.dataset.ref || ''), titleSort:(card.dataset.title || '').toLowerCase(), orig:parseInt(card.dataset.orig || '0', 10) || 0, visible:true }; });
  const gridEntries = new Map(grids.map(grid => [grid, entries.filter(e => e.grid === grid)]));
  let lastSortMode = null;
  const sortCache = new Map();
  function compareEntries(a,b,mode){ if (mode==='face') return a.faceValue-b.faceValue || a.minYear-b.minYear || a.maxYear-b.maxYear || a.refKey.catRank-b.refKey.catRank || a.refKey.num-b.refKey.num || a.refKey.suffix.localeCompare(b.refKey.suffix) || a.orig-b.orig; if (mode==='date') return a.minYear-b.minYear || a.maxYear-b.maxYear || a.orig-b.orig; if (mode==='ref') return a.refKey.catRank-b.refKey.catRank || a.refKey.num-b.refKey.num || a.refKey.suffix.localeCompare(b.refKey.suffix) || a.orig-b.orig; if (mode==='type') return a.titleSort.localeCompare(b.titleSort) || a.orig-b.orig; return a.orig-b.orig; }
  function applyGridOrder(mode){ if (lastSortMode === mode) return; grids.forEach(grid => { const cacheKey = grid.dataset.sortcachekey || (grid.dataset.sortcachekey = Math.random().toString(36).slice(2)); const mapKey=`${cacheKey}:${mode}`; let ordered=sortCache.get(mapKey); if (!ordered){ ordered=[...(gridEntries.get(grid) || [])].sort((a,b)=>compareEntries(a,b,mode)); sortCache.set(mapKey, ordered); } const frag=document.createDocumentFragment(); ordered.forEach(entry => frag.appendChild(entry.el)); grid.appendChild(frag); }); lastSortMode=mode; }
  function updateIssuerList(){ const cont=contSel.value; const selectedSet=new Set(getSelectedIssuers()); const list=(cont === 'ALL') ? uniqSorted(Object.values(CONT_MAP).reduce((acc,v)=>acc.concat(v), [])) : uniqSorted(CONT_MAP[cont] || []); renderIssuerCheckboxes(list, selectedSet); applyFilters(); }
  function resetAllFilters(){ contSel.value='ALL'; searchEl.value=''; if (coinSearchEl) coinSearchEl.value=''; if (objTypeSel) objTypeSel.value='ALL'; if (sortSel) sortSel.value='none'; if (catSel) catSel.value='ALL'; if (yearEl) yearEl.value=''; if (sizeEl) sizeEl.value=''; renderIssuerCheckboxes(uniqSorted(Object.values(CONT_MAP).reduce((acc,v)=>acc.concat(v), [])), new Set()); lastSortMode=null; applyFilters(); }
  function applyFilters(){ const cont=contSel.value; const selected=getSelectedIssuers(); const selectedSet=new Set(selected); const q=normText(searchEl.value || ''); const coinQNorm=normText(coinSearchEl ? coinSearchEl.value || '' : ''); const objT=objTypeSel ? objTypeSel.value : 'ALL'; const cat=catSel ? catSel.value : 'ALL'; const yearRules=yearEl ? parseYearFilter(yearEl.value) : null; const sizeRules=sizeEl ? parseDiameterFilter(sizeEl.value) : null; document.querySelectorAll('.issuerItem').forEach(lbl => { const name = lbl.dataset.norm || normText(lbl.textContent || ''); lbl.style.display = (!q || name.includes(q)) ? '' : 'none'; }); const totalAvailable=Array.from(document.querySelectorAll('#issuerBox input[type=checkbox]')).filter(cb => cb.parentElement.style.display !== 'none').length; if (countryCountEl) countryCountEl.textContent = `Countries: ${selected.length} selected / ${totalAvailable} available`; let shownItems=0, shownTypes=0, shownIssuerOnly=0, visibleCards=0; entries.forEach(entry => { const okCont=(cont==='ALL' || entry.continent===cont); const okIssuer=(selectedSet.size===0 || selectedSet.has(entry.issuerroot)); const okCoin=fuzzyMatchNorm(coinQNorm, entry.haystackNorm); const okObj=(objT==='ALL' || entry.objType===objT); const okCat=(cat==='ALL' || entry.category===cat); const okSize=diameterMatches(entry.sizeMm, sizeRules); let okYear=true; if (yearRules){ okYear = yearRules.some(rule => { if (rule.type==='single'){ if (entry.years.includes(rule.year)) return true; return !entry.hasExactYears && entry.minYear !== 999999 && entry.maxYear !== 999999 && rule.year >= entry.minYear && rule.year <= entry.maxYear; } if (entry.years.some(y => y >= rule.min && y <= rule.max)) return true; return !entry.hasExactYears && entry.minYear !== 999999 && entry.maxYear !== 999999 && !(rule.max < entry.minYear || rule.min > entry.maxYear); }); } const visible = okCont && okIssuer && okCoin && okObj && okCat && okSize && okYear; entry.visible = visible; entry.el.classList.toggle('hidden', !visible); if (visible){ visibleCards += 1; if (entry.isIssuerOnly) shownIssuerOnly += 1; else { shownTypes += 1; shownItems += entry.qty; } } }); applyGridOrder(sortSel ? (sortSel.value || 'none') : 'none'); grids.forEach(grid => { const anyVisible=(gridEntries.get(grid) || []).some(entry => entry.visible); grid.classList.toggle('hidden', !anyVisible); const h3=grid.previousElementSibling; if (h3 && h3.tagName === 'H3') h3.classList.toggle('hidden', !anyVisible); }); sections.forEach(sec => { const anyVisible=sec.body.querySelector('.card:not(.hidden)'); sec.body.classList.toggle('hidden', !anyVisible); if (sec.header) sec.header.classList.toggle('hidden', !anyVisible); }); if (emptyStateEl) emptyStateEl.style.display = visibleCards === 0 ? 'block' : 'none'; if (metricsEl){ let msg=`Showing: ${shownItems} items | ${shownTypes} types`; if (shownIssuerOnly > 0) msg += ` | ${shownIssuerOnly} issuer-wishes`; metricsEl.textContent=msg; }
  if (typeof refreshMobileJump === 'function') refreshMobileJump(); if (liveStatsEl) liveStatsEl.innerHTML = `<span>Visible <b>${visibleCards}</b></span><span>Qty <b>${shownItems}</b></span>`; if (mobileResults) mobileResults.textContent = `${visibleCards} shown`; if (visibleQtyEl) visibleQtyEl.textContent = String(shownItems); if (visibleTypesEl) visibleTypesEl.textContent = String(shownTypes); renderChips({ cont, countries:selected, coinQ:coinSearchEl ? coinSearchEl.value.trim() : '', objT, cat, size:sizeEl ? sizeEl.value.trim() : '' }); }
  const debouncedApply = debounce(applyFilters, 160);
  if (contSel) contSel.addEventListener('change', updateIssuerList);
  if (searchEl) searchEl.addEventListener('input', debouncedApply);
  if (boxEl) boxEl.addEventListener('change', applyFilters);
  if (objTypeSel) objTypeSel.addEventListener('change', applyFilters);
  if (catSel) catSel.addEventListener('change', applyFilters);
  if (sortSel) sortSel.addEventListener('change', () => { lastSortMode = null; applyFilters(); });
  if (coinSearchEl) coinSearchEl.addEventListener('input', debouncedApply);
  if (yearEl) yearEl.addEventListener('input', debouncedApply);
  if (sizeEl) sizeEl.addEventListener('input', debouncedApply);
  if (clearBtn) clearBtn.addEventListener('click', resetAllFilters);
  if (emptyClearBtn) emptyClearBtn.addEventListener('click', resetAllFilters);
  if (mobileClearBtn) mobileClearBtn.addEventListener('click', resetAllFilters);
  if (mobileFilterToggle && filtersPanel) mobileFilterToggle.addEventListener('click', () => filtersPanel.classList.toggle('mobile-open'));
  if (chipsEl) chipsEl.addEventListener('click', (e) => { const btn=e.target.closest('button'); if (!btn) return; const chip=btn.closest('.chip'); if (!chip) return; clearOneFilter(chip.dataset.k || ''); applyFilters(); });
  updateIssuerList();
  requestAnimationFrame(() => applyFilters());
})();
</script>
""")
	html.extend(scripts)
	html.append("</body></html>")
	Path(out_html).parent.mkdir(parents=True, exist_ok=True)
	Path(out_html).write_text("\n".join(html), encoding="utf-8")
def read_replicas_csv(path: str) -> list[int]:
	"""
	Reads optional CSV files for manually tracked extra coins, such as replicas
	or on-transit purchases.

	Accepted header pairs include:
	  - coin;type_id          (legacy replicas.csv)
	  - name;item_number      (new on_transit.csv)

	The first column is only a human-readable label. The second column is the
	Numista type/item number. The reader also accepts common aliases such as
	`type`, `id`, and Numista's `N# number (with link)` export field.
	Returns a de-duplicated list of type_id ints, preserving file order.
	Accepts ';' or ',' as delimiter.
	"""
	p = Path(path)
	if not p.exists():
		return []
	# detect delimiter quickly from header line
	first = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
	delim = ';' if first and ';' in first[0] else ','
	type_ids: list[int] = []
	seen = set()
	with p.open("r", encoding="utf-8", errors="ignore", newline="") as f:
		reader = csv.DictReader(f, delimiter=delim)
		for row in reader:
			if not row:
				continue
			tid_raw = (
				row.get("item_number")
				or row.get("type_id")
				or row.get("type")
				or row.get("id")
				or row.get("N# number (with link)")
				or row.get("Número N# (con enlace)")
				or row.get("Numero N# (con enlace)")
				or ""
			).strip()
			m = re.search(r"\d+", tid_raw)
			if not m:
				continue
			tid = int(m.group(0))
			if tid in seen:
				continue
			seen.add(tid)
			type_ids.append(tid)
	return type_ids


def read_on_transit_csv(path: str) -> list[int]:
	"""Reads on-transit CSV files with the same parser as replicas.csv."""
	return read_replicas_csv(path)
def read_wishlist_issuers_csv(path: str) -> list[dict]:
	"""
	Reads CSV file: issuer_name;type_id
	Returns list of dicts: {"issuer_name": str, "type_id": int}
	Ignores blank lines and malformed rows.
	"""
	p = Path(path)
	if not p.exists():
		return []
	rows = []
	with p.open("r", encoding="utf-8", newline="") as f:
		reader = csv.reader(f, delimiter=";")
		for row in reader:
			if not row or len(row) < 2:
				continue
			issuer_name = row[0].strip()
			if not issuer_name:
				continue
			try:
				type_id = int(row[1].strip())
			except Exception:
				continue
			rows.append({
				"issuer_name": issuer_name,
				"type_id": type_id,
			})
	return rows
def _dq_safe_float(value):
	try:
		if value is None or value == "":
			return None
		return float(value)
	except Exception:
		return None
def _dq_percent(part: int, total: int) -> str:
	if not total:
		return "0.0%"
	return f"{(part / total) * 100:.1f}%"
def _dq_example_line(row: dict, detail: str = "", *, full: bool = False) -> str:
	type_id = row.get("type_id")
	issuer = row.get("issuer_path") or row.get("issuer_root") or "Unknown issuer"
	label = row.get("label") or "<blank>"
	base = f"type_id={type_id} | {issuer} | {label}"
	if full:
		year_str = (row.get("year_str") or "").strip()
		category = (row.get("category") or "").strip() or "<blank>"
		object_type = (row.get("object_type") or "").strip() or "<blank>"
		url = (row.get("url") or "").strip() or "<blank>"
		base += f" | year={year_str or '<blank>'} | category={category} | object_type={object_type} | url={url}"
	if detail:
		base += f" | {detail}"
	return base
def _dq_print_block(title: str, examples: list[str], total_rows: int) -> None:
	count = len(examples)
	print(f"[DATA QUALITY] {title}: {count} / {total_rows} ({_dq_percent(count, total_rows)})")
	for line in examples[:10]:
		print(f" - {line}")
	print()
def _dq_is_placeholder_row(row: dict) -> bool:
	return bool(row.get("is_issuer_only"))
def _dq_is_banknote_like(row: dict, size: Optional[float] = None, weight: Optional[float] = None) -> bool:
	category = (row.get("category") or "").strip().lower()
	object_type = (row.get("object_type") or "").strip().lower()
	label = (row.get("label") or "").strip().lower()
	joined = " | ".join([category, object_type, label])
	keywords = (
		"banknote", "bank note", "paper money", "papel moneda", "note", "notes",
		"billet", "billete", "paper"
	)
	if any(k in joined for k in keywords):
		return True
	if size is not None and size >= 80 and (weight is None or weight <= 0):
		return True
	return False
def _dq_has_suspicious_kmy(km_y: str) -> bool:
	text = (km_y or "").strip().upper()
	if not text:
		return False
	if not (text.startswith("KM#") or text.startswith("Y#")):
		return True
	return re.search(r"[^A-Z0-9#\s,./\-–]", text) is not None

def _dq_should_ignore_missing_kmy(row: dict) -> bool:
	if bool(row.get("is_replica")):
		return True
	category = (row.get("category") or "").strip().lower()
	object_type = (row.get("object_type") or "").strip().lower()
	issuer = (row.get("issuer_path") or row.get("issuer_root") or "").strip().lower()
	label = (row.get("label") or "").strip().lower()
	joined = " | ".join([category, object_type, issuer, label])
	ignore_keywords = (
		"token", "medal", "medalet", "jeton", "exonumia", "fare"
	)
	return any(k in joined for k in ignore_keywords)
def _dq_is_special_missing_kmy_case(row: dict) -> bool:
	category = (row.get("category") or "").strip().lower()
	object_type = (row.get("object_type") or "").strip().lower()
	issuer = (row.get("issuer_path") or row.get("issuer_root") or "").strip().lower()
	label = (row.get("label") or "").strip().lower()
	joined = " | ".join([category, object_type, issuer, label])
	special_keywords = (
		"roman empire", "roman", "byzantine", "ancient",
		"greek", "nummus", "follis", "denarius", "sestertius"
	)
	return any(k in joined for k in special_keywords)
def audit_data_quality(rows: list[dict], dataset_name: str, type_details_by_id: Optional[dict] = None) -> None:
	rows = [r for r in rows if isinstance(r, dict)]
	total_rows = len(rows)
	print(f"\n[DATA QUALITY] Dataset: {dataset_name} | rows={total_rows}")

	if not total_rows:
		print("[DATA QUALITY] No rows to audit.\n")

		return
	current_year = 2026
	missing_size = []
	outlier_size = []
	missing_weight = []
	outlier_weight = []
	missing_km_y_standard = []
	missing_km_y_special = []
	weird_km_y = []
	weird_label = []
	missing_object_type = []
	missing_category = []
	replica_category_mismatch = []
	on_transit_category_mismatch = []
	exonumia_object_type_mismatch = []
	year_issues = []
	placeholder_labels = {"unknown", "n/a", "na", "-", "?", "none"}
	for row in rows:
		label = (row.get("label") or "").strip()
		label_lower = label.lower()
		size = _dq_safe_float(row.get("size_mm"))
		weight = _dq_safe_float(row.get("weight_g"))
		km_y = (row.get("km_y") or "").strip()
		category = (row.get("category") or "").strip()
		object_type = (row.get("object_type") or "").strip()
		is_replica = bool(row.get("is_replica"))
		is_on_transit = bool(row.get("is_on_transit"))
		is_placeholder = _dq_is_placeholder_row(row)
		is_banknote_like = _dq_is_banknote_like(row, size=size, weight=weight)
		if (not is_placeholder) and (not is_replica) and (not is_banknote_like):
			if size is None or size <= 0:
				missing_size.append(_dq_example_line(row, f"size={row.get('size_mm')!r}"))
			elif size < 5 or size > 60:
				outlier_size.append(_dq_example_line(row, f"size={size:g} mm"))
			if weight is None or weight <= 0:
				missing_weight.append(_dq_example_line(row, f"weight={row.get('weight_g')!r}"))
			elif weight < 0.1 or weight > 100:
				outlier_weight.append(_dq_example_line(row, f"weight={weight:g} g"))
		if (not is_placeholder) and (not is_banknote_like):
			if not km_y:
				if _dq_should_ignore_missing_kmy(row):
					pass
				elif not _dq_is_special_missing_kmy_case(row):
					example = _dq_example_line(row, "ref=<blank>", full=True)
					missing_km_y_standard.append(example)
				else:
					example = _dq_example_line(row, "ref=<blank>")
					missing_km_y_special.append(example)
			elif _dq_has_suspicious_kmy(km_y):
				weird_km_y.append(_dq_example_line(row, f"ref={km_y!r}"))
		label_has_alnum = any(ch.isalnum() for ch in label)
		label_has_bad_replacement = "�" in label or "??" in label or "  " in label
		if (not label or len(label) < 2 or len(label) > 80 or not label_has_alnum or label_has_bad_replacement or label_lower in placeholder_labels or label.isdigit()):
			weird_label.append(_dq_example_line(row, f"label={label!r}"))
		if not object_type and not is_placeholder:
			missing_object_type.append(_dq_example_line(row, "object_type=<blank>"))
		if not category and not is_placeholder:
			missing_category.append(_dq_example_line(row, "category=<blank>"))
		if is_replica and category.lower() != "replica":
			replica_category_mismatch.append(_dq_example_line(row, f"is_replica=True | category={category!r}"))
		if (not is_replica) and category.lower() == "replica":
			replica_category_mismatch.append(_dq_example_line(row, f"is_replica=False | category={category!r}"))
		if is_on_transit and category.lower() != "on transit":
			on_transit_category_mismatch.append(_dq_example_line(row, f"is_on_transit=True | category={category!r}"))
		if (not is_on_transit) and category.lower() == "on transit":
			on_transit_category_mismatch.append(_dq_example_line(row, f"is_on_transit=False | category={category!r}"))
		if category.lower() == "exonumia" and object_type.lower() == "coin":
			exonumia_object_type_mismatch.append(_dq_example_line(row, f"category={category!r} | object_type={object_type!r}"))
		min_year = row.get("min_year")
		max_year = row.get("max_year")
		year_str = (row.get("year_str") or "").strip()
		if isinstance(min_year, int) and isinstance(max_year, int):
			if min_year > max_year:
				year_issues.append(_dq_example_line(row, f"min_year={min_year} > max_year={max_year}"))
			if min_year < -1000 or max_year > current_year + 5:
				year_issues.append(_dq_example_line(row, f"min_year={min_year} | max_year={max_year}"))
			if not year_str:
				year_issues.append(_dq_example_line(row, f"year_str=<blank> | min_year={min_year} | max_year={max_year}"))
		elif year_str and not is_placeholder:
			year_issues.append(_dq_example_line(row, f"year_str={year_str!r} | min/max missing"))
	blocks = [
		("Missing size", missing_size),
		("Outlier size", outlier_size),
		("Missing weight", missing_weight),
		("Outlier weight", outlier_weight),
		("Missing KM/Y - standard coins", missing_km_y_standard),
		("Missing KM/Y - special cases", missing_km_y_special),
		("Weird KM/Y format", weird_km_y),
		("Weird label", weird_label),
		("Missing object_type", missing_object_type),
		("Missing category", missing_category),
		("Replica/category mismatch", replica_category_mismatch),
		("On transit/category mismatch", on_transit_category_mismatch),
		("Exonumia/object_type mismatch", exonumia_object_type_mismatch),
		("Year issues", year_issues),
	]
	for title, items in blocks:
		_dq_print_block(title, items, total_rows)
	if dataset_name == "collection" and missing_km_y_standard and isinstance(type_details_by_id, dict):
		print("[REF DEBUG] Raw references for collection standard coins missing KM/Y:")
		for line in missing_km_y_standard[:10]:
			m = re.search(r"type_id=(\d+)", line)
			if not m:
				continue
			tid = int(m.group(1))
			refs = (type_details_by_id.get(tid) or {}).get("references") or []
			print(f" - type_id={tid}")
			try:
				print(json.dumps(refs, ensure_ascii=False, indent=2))
			except Exception:
				print(repr(refs))
			print()
def write_portal_summary(
	out_path: str,
	collection_total_items: int,
	collection_total_types: int,
	wishlist_total_types: int,
) -> None:
	"""Write the small public metadata file consumed by the collections homepage.

	The full coin collection remains independent. The portal only reads this file
	to display current metrics and link to the published Numista page.
	"""
	summary = {
		"schema_version": 1,
		"id": "numista",
		"title": "Coin Collection",
		"description": "Coins, banknotes, tokens and other numismatic items.",
		# Resolved relative to summary.json by the future portal.
		"url": "./",
		"updated": datetime.now().astimezone().isoformat(timespec="seconds"),
		"metrics": [
			{"label": "Items", "value": int(collection_total_items)},
			{"label": "Types", "value": int(collection_total_types)},
			{"label": "Wishlist", "value": int(wishlist_total_types)},
		],
		"data": {
			"collection_items": int(collection_total_items),
			"collection_types": int(collection_total_types),
			"wishlist_types": int(wishlist_total_types),
		},
	}
	out = Path(out_path)
	out.parent.mkdir(parents=True, exist_ok=True)
	out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	print(f"Wrote portal summary: {out}")


def main(args):
	# 0) Cargar colección local (sin requests)
	# 0) Cargar colección local (sin requests)
	if args.collection_source == "csv":
		collection = load_collection_from_numista_csv(args.collection_csv)
	else:
		with open(args.collection_json, "r", encoding="utf-8") as f:
			collection = json.load(f)
	# Build fast lookup: type_ids that are already in the collection
	COLLECTED_TYPE_IDS = set()
	for it in (collection.get("items") or []):
		try:
			tid = int((it.get("type") or {}).get("id"))
			COLLECTED_TYPE_IDS.add(tid)
		except Exception:
			pass
	# Optional: load manually tracked extra items and append them to the collection view.
	# Replicas remain collection-only extras and do NOT affect wishlist de-dup.
	# On-transit coins are already bought, so they are also excluded from the wishlist view below.
	replica_type_ids_requested = read_replicas_csv(REPLICAS_CSV_PATH)
	on_transit_type_ids_requested = read_on_transit_csv(ON_TRANSIT_CSV_PATH)
	replica_type_ids = set()
	on_transit_type_ids = set()
	already_visible_type_ids = set(COLLECTED_TYPE_IDS)

	def _append_extra_type_ids(type_ids_to_add: list[int], target_set: set[int]) -> int:
		added = 0
		for tid in type_ids_to_add:
			# If the type is already in the real collection, don't duplicate it.
			if tid in already_visible_type_ids:
				continue
			(collection.setdefault("items", [])).append({
				"type": {"id": tid},
				"issue": None,
				"quantity": 1,
			})
			already_visible_type_ids.add(tid)
			target_set.add(tid)
			added += 1
		return added

	added_extra_items = 0
	added_extra_items += _append_extra_type_ids(replica_type_ids_requested, replica_type_ids)
	added_extra_items += _append_extra_type_ids(on_transit_type_ids_requested, on_transit_type_ids)
	if added_extra_items:
		# Keep header totals consistent
		try:
			collection["item_count"] = int(collection.get("item_count") or 0) + added_extra_items
		except Exception:
			pass
		try:
			collection["item_type_count"] = int(collection.get("item_type_count") or 0) + added_extra_items
		except Exception:
			pass
	# Optional: write the standard JSON structure from CSV and exit
	if args.write_collection_json_from_csv:
		outp = Path(args.collection_json)
		outp.parent.mkdir(parents=True, exist_ok=True)
		outp.write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")
		print(f"Wrote collection JSON: {outp}")
		return

	items = collection.get("items", [])
	if not isinstance(items, list):
		raise RuntimeError("collection.json no tiene 'items' como lista")
	# 1) Agrupar por type_id: quantity total + años (si existen)
	# agg = defaultdict(lambda: {"quantity": 0, "years": set(), "type_stub": None})
	agg = defaultdict(lambda: {
	"quantity": 0,
	"years_raw": set(),      # issue.year/raw_year (como viene/display)
	"years_greg": set(),     # issue.gregorian_year (para filtrar)
	"year_qty_raw": defaultdict(int),   # raw/display year -> quantity for duplicate-year badges
	"year_qty_greg": defaultdict(int),  # gregorian/filter year -> quantity for duplicate-year badges
	"year_map": {},          # raw -> greg (solo cuando difieren o para lookup)
	"grades": defaultdict(int),
	"type_stub": None
	})
	for item in items:
		t = item.get("type") or {}
		type_id = t.get("id")
		if not isinstance(type_id, int):
			continue
		qty = item.get("quantity", 1)
		try:
			qty = int(qty)
		except Exception:
			qty = 1
		agg[type_id]["quantity"] += qty
		grade = str(item.get("grade") or "").strip() or "Ungraded"
		agg[type_id]["grades"][grade] += qty
		agg[type_id]["type_stub"] = t  # title/issuer básicos vienen aquí
		issue = item.get("issue") or {}
		year = issue.get("year")
		raw_year = issue.get("raw_year")
		gyear = issue.get("gregorian_year")
		# CSV input stores both raw_year and gregorian_year; JSON input may only have issue.year.
		year_for_raw = raw_year if isinstance(raw_year, int) else year
		year_for_greg = gyear if isinstance(gyear, int) else (year if isinstance(year, int) else year_for_raw)
		if isinstance(year_for_raw, int):
			agg[type_id]["years_raw"].add(year_for_raw)
			agg[type_id]["year_qty_raw"][year_for_raw] += qty
		# Guardar gregorian year (si no viene, cae al issue.year, y luego al raw_year).
		if isinstance(year_for_greg, int):
			agg[type_id]["years_greg"].add(year_for_greg)
			agg[type_id]["year_qty_greg"][year_for_greg] += qty
		# Mapa raw->greg (para display)
		if isinstance(year_for_raw, int) and isinstance(gyear, int):
			agg[type_id]["year_map"][year_for_raw] = gyear

		# issue = item.get("issue") or {}
		# year = issue.get("year")
		# if isinstance(year, int):
		# 	agg[type_id]["years"].add(year)
	# Totales header (según tu JSON ya vienen, pero si faltan los calculo)
	total_items = collection.get("item_count")
	total_types = collection.get("item_type_count")
	if not isinstance(total_items, int):
		total_items = sum(v["quantity"] for v in agg.values())
	if not isinstance(total_types, int):
		total_types = len(agg)
	# 2) Token (1 request) — necesario para endpoints personales,
	# pero para /types/{id} NO hace falta, igual lo dejamos listo para el futuro.
	# 3) Elegir SOLO N tipos para probar (N = MAX_TYPES)
	type_ids = sorted(agg.keys())[:MAX_TYPES]
	# 4) Fetch de types (N requests) con cache
	types = {}
	missing = [tid for tid in type_ids if not (CACHE_DIR / f"{tid}.json").exists()]
	use_quota = False
	if missing:
		print(f"[collection] Missing {len(missing)} types in cache.")
		preview = ", ".join(str(x) for x in missing[:12])
		print(f"[collection] First missing type_ids: {preview}" + (" ..." if len(missing) > 12 else ""))
		if AUTO_FETCH_MISSING:
			print("[collection] Auto-fetch enabled; fetching missing /types/{id} records.")
			use_quota = True
		else:
			ans = input("[collection] Use quota to fetch missing /types/{id}? [y/N] ").strip().lower()
			use_quota = ans in ("y", "yes")
	for tid in type_ids:
		was_cached = (CACHE_DIR / f"{tid}.json").exists()
		if was_cached or use_quota:
			types[tid] = get_type_cached(API_KEY, tid, lang="en")
			if (not was_cached) and use_quota:
				# TOKEN_REQUESTS+=1
				time.sleep(0.2)  # only sleep when we actually fetched
		else:
			# No cache and user said no quota: keep empty dict (will fall back to type_stub)
			types[tid] = {}
	issuers_by_code = get_issuers_cached(API_KEY, lang="en")
	wishlist_type_ids_raw = load_type_ids_from_numista_csv(WISHLIST_COLLECTION_CSV_PATH)
	wishlist_excluded_type_ids = set(COLLECTED_TYPE_IDS) | set(on_transit_type_ids_requested)
	wishlist_type_ids = [tid for tid in wishlist_type_ids_raw if tid not in wishlist_excluded_type_ids]
	print(f"[wishlist] CSV: {WISHLIST_COLLECTION_CSV_PATH} | loaded={len(wishlist_type_ids_raw)} | excluded already owned/on-transit={len(wishlist_type_ids_raw) - len(wishlist_type_ids)} | remaining={len(wishlist_type_ids)}")
	# Fetch wishlist types separately (cached) so we don't mix them into collection rows/counts
	types_wishlist = {}
	for tid in wishlist_type_ids:
		was_cached = (CACHE_DIR / f"{tid}.json").exists()
		types_wishlist[tid] = get_type_cached(API_KEY, tid, lang="en")
		if not was_cached:
			time.sleep(0.2)
	# 5) Preparar filas con orden Numista:
	# issuer alfabético; dentro por numeric_value
	rows = []
	for tid in type_ids:
		td = types.get(tid) or {}
		issuer_obj = td.get("issuer") or {}
		issuer_code = issuer_obj.get("code")
		root_code = issuer_root_code_from_code(issuer_code, issuers_by_code)
		root_flag_url = flag_from_code(root_code, issuers_by_code)
		issuer = issuer_path_from_code(issuer_code, issuers_by_code) or strip_date_suffix(issuer_obj.get("name", "")) or "Unknown issuer"
		issuer_root = issuer_root_from_path(issuer)
		issuer_subpath = issuer_subpath_from_path(issuer)
		flag_url = flag_from_code(issuer_code, issuers_by_code)
		if not flag_url:
			flag_url = flag_url_from_issuer(issuer_obj)
		value = td.get("value") or {}
		numeric_value = value.get("numeric_value")
		try:
			numeric_value = float(numeric_value)
		except Exception:
			numeric_value = 1e18
		currency = normalize_currency_label((value.get("currency") or {}).get("full_name") or (value.get("currency") or {}).get("name") or "", issuer_root)
		# Keep the original Numista type title for collection cards, matching wishlist.
		# value.text is only the denomination/currency text and can look like an English translation.
		value_text = value.get("text") or ""
		display_title = td.get("title") or value_text or ""
		obv = (td.get("obverse") or {}).get("thumbnail") or ""
		rev = (td.get("reverse") or {}).get("thumbnail") or ""
		km_y = pick_km_or_y(td, tid)
		size_mm = td.get('size')
		weight_g = td.get('weight')
		comp = shorten_composition((td.get('composition') or {}).get('text') or '')
		# years = sorted(agg[tid]["years"])
		# if years:
		# 	if len(years) <= 6:
		# 		year_str = ", ".join(str(y) for y in years)
		# 	else:
		# 		year_str = f"{years[0]}–{years[-1]}"
		# else:
		# 	# fallback a min/max del type :contentReference[oaicite:7]{index=7}
		# 	min_y = td.get("min_year")
		# 	max_y = td.get("max_year")
		# 	if isinstance(min_y, int) and isinstance(max_y, int):
		# 		year_str = f"{min_y}–{max_y}" if min_y != max_y else f"{min_y}"
		# 	else:
		# 		year_str = ""
		years_raw = sorted(agg[tid]["years_raw"])
		years_greg = sorted(agg[tid]["years_greg"])
		year_map = agg[tid]["year_map"] or {}
		def fmt_year(y: int) -> str:
			gy = year_map.get(y, y)
			return f"{y} ({gy})" if isinstance(gy, int) and gy != y else f"{y}"
		if years_raw:
			# Display: raw (gregorian) cuando difiere
			if len(years_raw) <= 6:
				year_str = ", ".join(fmt_year(y) for y in years_raw)
			else:
				raw_min, raw_max = years_raw[0], years_raw[-1]
				g_min = year_map.get(raw_min, years_greg[0] if years_greg else raw_min)
				g_max = year_map.get(raw_max, years_greg[-1] if years_greg else raw_max)
				if isinstance(g_min, int) and isinstance(g_max, int) and (g_min != raw_min or g_max != raw_max):
					year_str = f"{raw_min}–{raw_max} ({g_min}–{g_max})"
				else:
					year_str = f"{raw_min}–{raw_max}"
		else:
			# fallback a min/max del type (ya son gregorianos típicamente)
			min_y = td.get("min_year")
			max_y = td.get("max_year")
			if isinstance(min_y, int) and isinstance(max_y, int):
				year_str = f"{min_y}–{max_y}" if min_y != max_y else f"{min_y}"
			else:
				year_str = ""

		qty = agg[tid]["quantity"]
		# Search/sort years use the actual available years in the collection when present.
		# If a row was added without per-issue years (for example some transit/replica rows),
		# keep the Numista type range as a fallback. Display still uses the compact year_str above.
		available_min_year = years_greg[0] if years_greg else td.get("min_year")
		available_max_year = years_greg[-1] if years_greg else td.get("max_year")
		duplicate_years_raw = sorted(y for y, count in (agg[tid].get("year_qty_raw") or {}).items() if isinstance(y, int) and int(count or 0) > 1)
		duplicate_years_greg = sorted(y for y, count in (agg[tid].get("year_qty_greg") or {}).items() if isinstance(y, int) and int(count or 0) > 1)
		duplicate_years_label = duplicate_years_display_label(duplicate_years_raw, duplicate_years_greg, year_map)
		grade_counts = agg[tid].get("grades") or {}
		grades_sorted = sorted(grade_counts.keys(), key=lambda g: (str(g).lower() == "ungraded", str(g).lower()))
		grade_str = ", ".join(grades_sorted)
		obj_type = (td.get("object_type") or {}).get("name") or ""
		is_replica = tid in replica_type_ids
		is_on_transit = tid in on_transit_type_ids
		if is_replica:
			cat = "replica"
		elif is_on_transit:
			cat = "on transit"
		else:
			cat = td.get("category") or ""
		issuer_search_es = spanish_search_terms_for_issuer(issuer_root, issuer)
		rows.append({
			"issuer_path": issuer,
			"issuer_root_raw": issuer_root_name(issuer_obj) or issuer,
			"issuer_root_display": strip_date_suffix(issuer_root_name(issuer_obj) or issuer),
			"issuer": issuer,
			"issuer_root": issuer_root,
			"issuer_subpath": issuer_subpath,
			"issuer_search_es": issuer_search_es,
			"category": cat,
			"is_replica": is_replica,
			"is_on_transit": is_on_transit,
			"object_type": obj_type,
			"continent": continent_from_issuer_root(issuer_root),
			"modern_country_iso3": modern_country_iso3_from_row({"issuer_path": issuer, "issuer_root": issuer_root}),
			"flag_url": flag_url,
			"root_flag_url": root_flag_url,
			"currency": currency,
			"numeric_value": numeric_value,
			"face_sort_value": face_sort_value(numeric_value, value_text, display_title, currency),
			"label": display_title,
			"title_full": display_title,
			"year_str": year_str,
			"grade_str": grade_str,
			"grade_counts": dict(grade_counts),
			# "years_list": years,
			"years_list": years_greg,
			"years_raw_list": years_raw,
			"years_greg_list": years_greg,
			"year_qty_greg": {str(int(y)): int(c or 0) for y, c in (agg[tid].get("year_qty_greg") or {}).items() if isinstance(y, int)},
			"duplicate_type": qty > 1,
			"duplicate_year": bool(duplicate_years_greg or duplicate_years_raw),
			"duplicate_years_raw_list": duplicate_years_raw,
			"duplicate_years_greg_list": duplicate_years_greg,
			"duplicate_years_label": duplicate_years_label,
			"min_year": available_min_year,
			"max_year": available_max_year,
			"type_min_year": td.get("min_year"),
			"type_max_year": td.get("max_year"),
			"size_mm": size_mm,
			"weight_g": weight_g,
			"composition": comp,
			"km_y": km_y,
			"qty": qty,
			"url": td.get("url") or "#",
			"obv": obv,
			"rev": rev,
			"type_id": tid,
		})
	# Shared 9-level default order; on-transit rows are normal collection rows here.
	rows.sort(key=default_row_sort_key)
	rows_wishlist = []
	issuer_codes_in_wishlist = set()
	for tid in wishlist_type_ids:
		td = types_wishlist.get(tid) or {}
		issuer_obj = td.get("issuer") or {}
		issuer_code = issuer_obj.get('code') or ''
		issuer_codes_in_wishlist.add(issuer_code)
		issuer = issuer_path_from_code(issuer_code, issuers_by_code) or strip_date_suffix(issuer_obj.get("name") or "") or "Unknown issuer"
		flag_url = flag_from_code(issuer_code, issuers_by_code) or ""
		issuer_root = issuer_root_from_path(issuer)
		issuer_subpath = issuer_subpath_from_path(issuer)
		size_mm = td.get('size')
		weight_g = td.get('weight')
		comp = shorten_composition((td.get('composition') or {}).get('text') or '')
		currency = normalize_currency_label(((td.get('value') or {}).get('currency') or {}).get('full_name') or 'Unknown currency', issuer_root)
		value = td.get('value') or {}
		numeric_value = value.get('numeric_value')
		try:
			numeric_value = float(numeric_value)
		except Exception:
			numeric_value = 999999.0
		label = (td.get('title') or '')
		# year_str from type range
		min_y = td.get("min_year")
		max_y = td.get("max_year")
		if isinstance(min_y, int) and isinstance(max_y, int):
			year_str = f"{min_y}–{max_y}" if min_y != max_y else f"{min_y}"
		else:
			year_str = ""
		issuer_search_es = spanish_search_terms_for_issuer(issuer_root, issuer)
		rows_wishlist.append({
			'issuer_path': issuer,
			'issuer_root': issuer_root,
			'issuer_subpath': issuer_subpath,
			'issuer_search_es': issuer_search_es,
			'continent': continent_from_issuer_root(issuer_root),
			'flag_url': flag_url,
			'currency': currency,
			'numeric_value': numeric_value,
			'face_sort_value': face_sort_value(numeric_value, label, td.get('title') or label or '', currency),
			'label': label,
			'title_full': (td.get('title') or label or ''),
			'size_mm': size_mm,
			'weight_g': weight_g,
			'composition': comp,
			"km_y": pick_km_or_y(td, tid),
			"qty": 1,
			"year_str": year_str,
			"min_year": min_y,
			"max_year": max_y,
			"obv": (td.get("obverse") or {}).get("thumbnail") or (td.get("obverse") or {}).get("picture") or "",
			"rev": (td.get("reverse") or {}).get("thumbnail") or (td.get("reverse") or {}).get("picture") or "",
			"url": td.get("url") or f"https://en.numista.com/catalogue/pieces{tid}.html",
			"type_id": tid,
		})
	# Añadir "wishlist por issuer" desde CSV (issuer_name;type_id) usando type_id SOLO para obtener currency + issuer_code
	wishlist_issuer_specs = read_wishlist_issuers_csv(WISHLIST_ISSUERS_CSV)
	for spec in wishlist_issuer_specs:
		tid = spec["type_id"]
		# fetch/cache this type only to extract currency + issuer_code
		td = types.get(tid)
		if not td:
			td = get_type_cached(API_KEY, tid, lang="en")
			types[tid] = td
		issuer_obj = td.get("issuer") or {}
		issuer_code = issuer_obj.get("code") or ""
		issuer_path = issuer_path_from_code(issuer_code, issuers_by_code) \
			or strip_date_suffix(issuer_obj.get("name") or "") \
			or spec["issuer_name"]
		currency = normalize_currency_label(((td.get("value") or {}).get("currency") or {}).get("full_name") or "Unknown currency", issuer_root)
		flag_url = flag_from_code(issuer_code, issuers_by_code) or ""
		issuer_root = issuer_root_from_path(issuer_path)
		issuer_subpath = issuer_subpath_from_path(issuer_path)
		issuer_search_es = spanish_search_terms_for_issuer(issuer_root, issuer_path)
		rows_wishlist.append({
			"issuer_path": issuer_path,
			"issuer_root": issuer_root,
			"issuer_subpath": issuer_subpath,
			"issuer_search_es": issuer_search_es,
			"continent": continent_from_issuer_root(issuer_root),
			"modern_country_iso3": modern_country_iso3_from_row({"issuer_path": issuer_path, "issuer_root": issuer_root}),
			"flag_url": flag_url,
			"currency": currency,
			"numeric_value": 999999.0,
			"face_sort_value": 999999.0,
			"label": "Any coin",
			"title_full": "Any coin",
			"size_mm": None,
			"weight_g": None,
			"composition": "",
			"km_y": "",
			"qty": 1,
			"year_str": "",
			"min_year": None,
			"max_year": None,
			"obv": WISHLIST_ISSUER_PLACEHOLDER_IMG,
			"rev": "",
			"url": f"https://en.numista.com/catalogue/index.php?issuer={issuer_code}" if issuer_code else "https://en.numista.com/",
			"type_id": tid,
			"is_issuer_only": True,
		})
	# colección
	render_html(
		rows=rows,
		title="Javignacio's Coin Collection",
		out_html=COLLECTION_OUT_HTML,
		total_items=total_items,
		total_types=total_types,
		include_stored=True,
	)
	# optional filtered version
	render_html(
		rows=rows,
		title="Javignacio's Coin Collection",
		out_html=COLLECTION_OUT_HTML.replace(".html","_filters.html"),
		total_items=total_items,
		total_types=total_types,
		include_stored=True,
		include_filters=True,
	)
	rows_wishlist.sort(key=default_row_sort_key)
	wishlist_coin_types = sum(1 for r in rows_wishlist if not r.get("is_issuer_only"))
	chile_date_runs = load_chile_date_runs_csv(args.chile_date_runs_csv)
	if chile_date_runs:
		print(f"Loaded Chile date-run checklist: {len(chile_date_runs)} types from {args.chile_date_runs_csv}")
	else:
		print(f"No Chile date-run checklist found at {args.chile_date_runs_csv}; Chile tab will use range fallback.")
	audit_data_quality(rows, "collection", type_details_by_id=types)
	# wish list
	render_html(
		rows=rows_wishlist,
		title="Javignacio's Coin Wishlist",
		out_html=WISHLIST_OUT_HTML,
		total_items=wishlist_coin_types,
		total_types=wishlist_coin_types,
	)
	render_html(
		rows=rows_wishlist,
		title="Javignacio's Coin Wishlist",
		out_html=WISHLIST_OUT_HTML.replace(".html","_filters.html"),
		total_items=wishlist_coin_types,
		total_types=wishlist_coin_types,
		include_filters=True,
	)
	render_combined_app(
		collection_rows=rows,
		wishlist_rows=rows_wishlist,
		out_html=COMBINED_APP_HTML,
		collection_total_items=total_items,
		collection_total_types=total_types,
		wishlist_total_types=wishlist_coin_types,
		chile_date_runs=chile_date_runs,
	)
	write_portal_summary(
		out_path=args.summary_json,
		collection_total_items=total_items,
		collection_total_types=total_types,
		wishlist_total_types=wishlist_coin_types,
	)
	# -------------------------------
	# Cristobal collection from CSV
	# -------------------------------
	"""
	if Path("out/cristobal_export.csv").exists():
		cristobal_collection = load_collection_from_numista_csv("out/cristobal_export.csv")
		# reuse the same logic
		items_c = cristobal_collection.get("items", [])
		agg_c = defaultdict(lambda: {
			"quantity": 0,
			"years_raw": set(),
			"years_greg": set(),
			"year_map": {},
			"type_stub": None
		})
		for item in items_c:
			t = item.get("type") or {}
			type_id = t.get("id")
			if not isinstance(type_id, int):
				continue
			qty = item.get("quantity", 1)
			try:
				qty = int(qty)
			except:
				qty = 1
			agg_c[type_id]["quantity"] += qty
			agg_c[type_id]["type_stub"] = t
		type_ids_c = sorted(agg_c.keys())
		rows_c = []
		for tid in type_ids_c:
			td = get_type_cached(API_KEY, tid, lang="en")
			issuer_obj = td.get("issuer") or {}
			issuer_code = issuer_obj.get("code")
			issuer = (
				issuer_path_from_code(issuer_code, issuers_by_code)
				or strip_date_suffix(issuer_obj.get("name", ""))
				or "Unknown issuer"
			)
			issuer_root = issuer_root_from_path(issuer)
			issuer_subpath = issuer_subpath_from_path(issuer)
			flag_url = flag_from_code(issuer_code, issuers_by_code) or flag_url_from_issuer(issuer_obj)
			value = td.get("value") or {}
			currency = (value.get("currency") or {}).get("full_name") or ""
			numeric_value = value.get("numeric_value")
			try:
				numeric_value = float(numeric_value)
			except:
				numeric_value = 1e18
			rows_c.append({
				"issuer_path": issuer,
				"issuer_root": issuer_root,
				"issuer_subpath": issuer_subpath,
				"continent": continent_from_issuer_root(issuer_root),
				"flag_url": flag_url,
				"currency": currency,
				"numeric_value": numeric_value,
				"face_sort_value": face_sort_value(numeric_value, td.get("title") or "", td.get("title") or "", currency),
				"label": td.get("title") or "",
				"year_str": "",
				"size_mm": td.get("size"),
				"composition": shorten_composition((td.get("composition") or {}).get("text") or ""),
				"km_y": pick_km_or_y(td, tid),
				"qty": agg_c[tid]["quantity"],
				"obv": (td.get("obverse") or {}).get("thumbnail") or "",
				"rev": (td.get("reverse") or {}).get("thumbnail") or "",
				"url": td.get("url") or "#",
				"type_id": tid,
			})
		rows_c.sort(key=default_row_sort_key)
		total_items_c = sum(r["qty"] for r in rows_c)
		total_types_c = len(rows_c)
		render_html(
			rows=rows_c,
			title="Cristobal's Coin Collection",
			out_html="out/cristobal_collection_preview.html",
			total_items=total_items_c,
			total_types=total_types_c,
		)
		render_html(
			rows=rows_c,
			title="Cristobal's Coin Collection",
			out_html="out/cristobal_collection_preview_filters.html",
			total_items=total_items_c,
			total_types=total_types_c,
			include_filters=True
		)
	"""
	# print(f"OAuth token requests used in this run: {TOKEN_REQUESTS}")
def _parse_args():
	import argparse
	ap = argparse.ArgumentParser()
	ap.add_argument("--collection-source", choices=["csv", "json"], default=DEFAULT_COLLECTION_SOURCE,
					help="Collection input source (default: csv)")
	ap.add_argument("--collection-csv", default=COLLECTION_CSV_PATH,
					help="Path to Numista collection export CSV")
	ap.add_argument("--collection-json", default=COLLECTION_PATH,
					help="Path to Numista API collection JSON (the structure this script uses)")
	ap.add_argument("--write-collection-json-from-csv", action="store_true",
					help="Build out/collection.json from the collection CSV and exit")
	ap.add_argument("--chile-date-runs-csv", default=CHILE_DATE_RUNS_CSV_PATH,
					help="Optional curated Chile date-run checklist generated from Numista date tables")
	ap.add_argument("--summary-json", default=PORTAL_SUMMARY_JSON,
					help="Public summary JSON consumed by the collections homepage")
	return ap.parse_args()
if __name__ == "__main__":
	args = _parse_args()
	main(args)
