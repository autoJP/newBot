#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, argparse, xml.etree.ElementTree as ET, requests, json

DOJO_BASE = os.getenv("DOJO_BASE_URL", "http://localhost:8080/api/v2").rstrip("/")
DOJO_API_TOKEN = os.getenv("DOJO_API_TOKEN", "").strip()
H = {"Authorization": f"Token {DOJO_API_TOKEN}", "Accept": "application/json", "Content-Type": "application/json"}

def host_is_up(xml_path: str) -> bool:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    for host in root.findall(".//host"):
        st = host.find("status")
        if st is not None and st.get("state"):
            return st.get("state").lower() == "up"
    return False

def get_product(pid: int) -> dict:
    r = requests.get(f"{DOJO_BASE}/products/{pid}/", headers=H, timeout=30)
    r.raise_for_status()
    return r.json()

def normalize_tags(tags):
    """Dojo может вернуть список строк или список объектов {'name': ...}"""
    if not tags:
        return []
    if isinstance(tags, list) and tags and isinstance(tags[0], dict):
        return [t.get("name") for t in tags if t.get("name")]
    return list(tags)

def patch_product(pid: int, internet_accessible: bool, tags: list):
    payload = {"internet_accessible": bool(internet_accessible), "tags": tags}
    r = requests.patch(f"{DOJO_BASE}/products/{pid}/", headers=H, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product-id", type=int, required=True)
    ap.add_argument("--xml", required=True)
    args = ap.parse_args()

    if not DOJO_API_TOKEN:
        print("ERROR: DOJO_API_TOKEN not set", file=sys.stderr)
        sys.exit(2)

    # Определяем живость
    up = host_is_up(args.xml)

    # Получаем текущие теги
    prod = get_product(args.product_id)
    cur_tags = set(normalize_tags(prod.get("tags")))

    # Убираем needs:nmap, не добавляем ничего нового
    if "needs:nmap" in cur_tags:
        cur_tags.remove("needs:nmap")

    new_tags = sorted(cur_tags)

    # Патчим только internet_accessible и очищенные теги
    patched = patch_product(args.product_id, up, new_tags)

    print(json.dumps({
        "ok": True,
        "product_id": args.product_id,
        "host_up": up,
        "internet_accessible": up,
        "tags_after": new_tags
    }, ensure_ascii=False))

if __name__ == "__main__":
    main()

