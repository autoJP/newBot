#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
process_nmap_ips_for_pt.py

Process Nmap XML for a *single* DefectDojo Product Type:
 - list all products in that product_type
 - collect web IP:port targets from /tmp/nmap_<product_id>.xml (or --xml-dir)
 - skip ports in EXCLUDE_PORTS (default 80,443)
 - create new Product entries only for IP:port targets that do not already exist in that product_type
 - set internet_accessible = true for created products
 - build a dedicated targets artifact file with strict markers/parser:
     - main domain (canonical, without www), one line
     - each internet_accessible non-IP product (canonical host without www)
     - each IP:port target as <proto>://IP:port, <product_type.name>

The PT_STATE_JSON block is intentionally NOT touched here. State-machine workflows
remain the only owners of PT_STATE_JSON_START/PT_STATE_JSON_END in product_type.description.

Suitable for use from n8n via Execute Command with JSON summary on stdout.
"""

import os
import sys
import json
import argparse
import re
from typing import List, Tuple, Set, Dict
import requests
import xml.etree.ElementTree as ET
from ipaddress import ip_address
from urllib.parse import urlsplit

API_TIMEOUT = 30
TARGET_ARTIFACT_BLOCK_START = "PT_TARGET_LIST_START"
TARGET_ARTIFACT_BLOCK_END = "PT_TARGET_LIST_END"
TARGET_LINE_RE = re.compile(r"^(https?://[^,\s]+),\s+(.+)$")
PT_STATE_OWNER = "WF_Dojo_Master.json"


# ---------- CLI / ENV ----------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Process Nmap XML for a single DefectDojo product_type (IP-only products) and build targets artifact"
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("DOJO_BASE_URL", "http://localhost:8080/api/v2"),
        help="DefectDojo API base URL, e.g. http://host:8080/api/v2",
    )
    p.add_argument(
        "--api-token",
        default=os.environ.get("DOJO_API_TOKEN"),
        help="DefectDojo API token (or set DOJO_API_TOKEN env)",
    )
    p.add_argument(
        "--product-type-id",
        "--pt-id",
        dest="product_type_id",
        type=int,
        required=True,
        help="ID of product_type to process (required)",
    )
    p.add_argument(
        "--xml-dir",
        default=os.environ.get("NMAP_XML_DIR", "/tmp"),
        help="Directory containing nmap_<product_id>.xml files (default /tmp)",
    )
    p.add_argument(
        "--exclude-ports",
        default=os.environ.get("EXCLUDE_PORTS", "80,443"),
        help="Comma-separated ports to exclude for IP:port products (default 80,443)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write to the API; only simulate actions",
    )
    p.add_argument(
        "--targets-artifact-dir",
        default=os.environ.get("PT_TARGETS_ARTIFACT_DIR", "/tmp"),
        help="Directory for PT targets artifacts (default /tmp)",
    )
    return p


parser = build_arg_parser()
args = parser.parse_args()

DOJO_BASE = args.base_url.rstrip("/")
API_TOKEN = args.api_token
XML_DIR = args.xml_dir
EXCLUDE_PORTS: Set[int] = {int(p.strip()) for p in args.exclude_ports.split(",") if p.strip().isdigit()}
DRY_RUN = args.dry_run
PT_ID = args.product_type_id
TARGETS_ARTIFACT_DIR = args.targets_artifact_dir

if not API_TOKEN:
    print("ERROR: DOJO_API_TOKEN not set and --api-token not provided", file=sys.stderr)
    sys.exit(2)

HEADERS = {
    "Authorization": f"Token {API_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------- HTTP helpers ----------

def safe_get(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, headers=HEADERS, params=params, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def safe_post(url: str, payload: dict) -> dict:
    if DRY_RUN:
        return {"_dry_run": True, "url": url, "payload": payload}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def safe_patch(url: str, payload: dict) -> dict:
    if DRY_RUN:
        return {"_dry_run": True, "url": url, "payload": payload}
    r = requests.patch(url, headers=HEADERS, json=payload, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def api_get(path: str, params: dict | None = None) -> dict:
    return safe_get(DOJO_BASE + path, params=params)


def api_post(path: str, payload: dict) -> dict:
    return safe_post(DOJO_BASE + path, payload)


def api_patch(path: str, payload: dict) -> dict:
    return safe_patch(DOJO_BASE + path, payload)


# ---------- Helpers ----------

def looks_like_ip(s: str) -> bool:
    try:
        ip_address(s)
        return True
    except Exception:
        return False


def strip_www(host: str) -> str:
    host = (host or "").strip()
    if host.lower().startswith("www."):
        return host[4:]
    return host




def extract_host_from_product_name(value: str) -> str:
    """Return normalized host part from product/PT name, removing scheme/path/userinfo/port."""
    raw = (value or "").strip()
    if not raw:
        return ""

    candidate = raw
    lowered = raw.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        parsed = urlsplit(raw)
        candidate = (parsed.hostname or "").strip()
    else:
        candidate = raw.split("/", 1)[0].strip()
        if "@" in candidate:
            candidate = candidate.rsplit("@", 1)[1].strip()
        if candidate.startswith("[") and "]" in candidate:
            candidate = candidate[1:candidate.find("]")].strip()
        elif candidate.count(":") == 1:
            host_part, port_part = candidate.split(":", 1)
            if port_part.isdigit():
                candidate = host_part

    return candidate.strip().strip(".").lower()

def parse_nmap_xml_for_ips(filename: str, exclude_ports: Set[int]) -> List[Tuple[str, int, str]]:
    """
    Parse nmap xml and return list of (ip, port, proto) for open web-like services.
    Skip ports in exclude_ports.
    """
    if not os.path.exists(filename):
        return []
    try:
        tree = ET.parse(filename)
    except ET.ParseError:
        return []
    root = tree.getroot()
    result: List[Tuple[str, int, str]] = []

    for host in root.findall("host"):
        ip_addr = None
        for addr_el in host.findall("address"):
            addr = addr_el.get("addr")
            if addr:
                ip_addr = addr
                break
        if not ip_addr:
            continue

        ports_el = host.find("ports")
        if ports_el is None:
            continue

        for port_el in ports_el.findall("port"):
            state = port_el.find("state")
            if state is None or state.get("state") != "open":
                continue
            try:
                portnum = int(port_el.get("portid", "0"))
            except ValueError:
                continue
            if portnum in exclude_ports:
                continue
            svc = port_el.find("service")
            svc_name = (svc.get("name") if svc is not None else "") or ""
            svc_tunnel = (svc.get("tunnel") if svc is not None else "") or ""
            # heuristic: web-like service if name has http, or tunnel=ssl, or known web-ish ports
            if "http" in svc_name.lower() or "ssl" in svc_tunnel.lower() or portnum in {8000, 8008, 8080, 8443, 8888, 9000, 9443}:
                proto = "https" if ("ssl" in svc_tunnel.lower() or portnum in (8443, 9443)) else "http"
                result.append((ip_addr, portnum, proto))

    return result


def render_targets_artifact(target_lines: List[str]) -> str:
    body = "\n".join(target_lines)
    return f"{TARGET_ARTIFACT_BLOCK_START}\n{body}\n{TARGET_ARTIFACT_BLOCK_END}\n"


def parse_targets_artifact(content: str) -> List[str]:
    if not isinstance(content, str):
        raise ValueError("artifact content must be a string")
    pattern = re.compile(
        rf"^\s*{re.escape(TARGET_ARTIFACT_BLOCK_START)}\n(.*?)\n{re.escape(TARGET_ARTIFACT_BLOCK_END)}\s*$",
        re.DOTALL,
    )
    match = pattern.match(content)
    if not match:
        raise ValueError("artifact markers are missing or malformed")

    lines = [ln.strip() for ln in match.group(1).splitlines() if ln.strip()]
    for line in lines:
        if not TARGET_LINE_RE.match(line):
            raise ValueError(f"invalid target line format: {line}")
    return lines


def write_targets_artifact(pt_id: int, lines: List[str]) -> str:
    os.makedirs(TARGETS_ARTIFACT_DIR, exist_ok=True)
    artifact_path = os.path.join(TARGETS_ARTIFACT_DIR, f"pt_targets_{pt_id}.txt")
    content = render_targets_artifact(lines)
    parse_targets_artifact(content)  # strict self-check before write
    with open(artifact_path, "w", encoding="utf-8") as f:
        f.write(content)
    return artifact_path


# ---------- Core processing for single product_type ----------

def process_single_product_type(pt_id: int) -> dict:
    summary: Dict[str, object] = {
        "product_type_id": pt_id,
        "pt_state_owner": PT_STATE_OWNER,
        "xml_dir": XML_DIR,
        "targets_artifact_dir": TARGETS_ARTIFACT_DIR,
        "exclude_ports": sorted(list(EXCLUDE_PORTS)),
        "dry_run": DRY_RUN,
        "created_ip_products": [],
        "updated_description": False,
        "targets_artifact_path": None,
    }

    # 1) Load product_type
    pt = api_get(f"/product_types/{pt_id}/")
    pt_name = pt.get("name", f"pt_{pt_id}")
    summary["product_type_name"] = pt_name

    # 2) Load all products in this product_type
    products: List[dict] = []
    limit = 100
    offset = 0
    while True:
        params = {"prod_type": pt_id, "limit": limit, "offset": offset}
        data = api_get("/products/", params=params)
        results = data.get("results", [])
        if not results:
            break
        products.extend(results)
        if not data.get("next"):
            break
        offset += limit

    summary["products_count"] = len(products)

    # 3) Build initial sets
    existing_product_names: Set[str] = set()
    domain_hosts: Set[str] = set()  # canonical (without www) hosts for domain targets

    for p in products:
        pname = p.get("name", "")
        existing_product_names.add(pname)

        if p.get("internet_accessible"):
            host_part = extract_host_from_product_name(pname)
            if host_part and not looks_like_ip(host_part):
                canonical = strip_www(host_part)
                domain_hosts.add(canonical)

    # 4) Collect IP:port candidates from all XMLs of products in this PT
    candidates: Dict[str, str] = {}  # prod_name -> proto
    for p in products:
        pid = p.get("id")
        if not pid:
            continue
        xml_path = os.path.join(XML_DIR, f"nmap_{pid}.xml")
        if not os.path.exists(xml_path):
            continue
        targets = parse_nmap_xml_for_ips(xml_path, EXCLUDE_PORTS)
        for ip_addr, portnum, proto in targets:
            prod_name = f"{ip_addr}:{portnum}"
            candidates[prod_name] = proto  # last one wins

    # 5) Create missing products and collect IP target lines
    created_products: List[str] = []
    ip_lines_set: Set[str] = set()

    for prod_name, proto in sorted(candidates.items()):
        ip_lines_set.add(f"{proto}://{prod_name}, {pt_name}")
        if prod_name in existing_product_names:
            continue
        payload = {
            "name": prod_name,
            "prod_type": pt_id,
            "description": f"Auto-created from nmap XML in {XML_DIR}",
            "internet_accessible": True,
        }
        try:
            created = api_post("/products/", payload)
            created_name = created.get("name", prod_name)
            created_products.append(created_name)
            existing_product_names.add(created_name)
        except requests.HTTPError as e:
            created_products.append(f"{prod_name} (error: {e})")

    summary["created_ip_products"] = created_products
    summary["created_ip_products_count"] = len(created_products)

    # 6) Build description lines: main domain + domains (canonical, without www) + IP:ports
    lines: List[str] = []

    # main PT name: if not IP, add only https:// form, canonicalized for host, but label = pt_name
    pt_host = extract_host_from_product_name(pt_name)
    if pt_host and not looks_like_ip(pt_host):
        canonical_pt = strip_www(pt_host)
        lines.append(f"https://{canonical_pt}, {pt_name}")

    # products with internet_accessible and non-IP names (canonical hosts, no www)
    for host in sorted(domain_hosts):
        lines.append(f"https://{host}, {pt_name}")

    # IP:port lines
    for line in sorted(ip_lines_set):
        lines.append(line)

    # dedupe preserving order
    seen: Set[str] = set()
    unique_lines: List[str] = []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            unique_lines.append(line)

    # 7) Build separate target-list artifact and never patch PT description/state block here
    try:
        artifact_path = write_targets_artifact(pt_id, unique_lines)
        summary["targets_artifact_path"] = artifact_path
        summary["targets_artifact_count"] = len(unique_lines)
    except Exception as e:
        summary["error"] = f"failed_write_targets_artifact: {e}"

    return summary


def main() -> None:
    try:
        result = process_single_product_type(PT_ID)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
