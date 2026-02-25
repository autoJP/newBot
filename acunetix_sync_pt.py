#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import traceback
from urllib.parse import urljoin, urlsplit
from typing import Any, Dict, List, Optional
import re
from ipaddress import ip_address

import requests
import urllib3

# Самоподписанный TLS на Acunetix – живём с verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# -------------------- helpers --------------------


def make_session(verify: bool) -> requests.Session:
    s = requests.Session()
    s.verify = verify
    return s


# -------------------- Dojo --------------------


def dojo_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def dojo_get_product_type(
    s: requests.Session,
    base_url: str,
    token: str,
    pt_id: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/product_types/{pt_id}/"
    r = s.get(url, headers=dojo_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def dojo_get_products_for_pt(
    s: requests.Session,
    base_url: str,
    token: str,
    pt_id: int,
) -> List[Dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/products/?prod_type={pt_id}&limit=200&offset=0"
    products: List[Dict[str, Any]] = []

    while url:
        r = s.get(url, headers=dojo_headers(token), timeout=60)
        r.raise_for_status()
        data = r.json()
        products.extend(data.get("results", []))

        next_url = data.get("next")
        if not next_url:
            break
        url = next_url if str(next_url).startswith("http") else urljoin(f"{base_url.rstrip('/')}/", str(next_url).lstrip("/"))

    return products


def looks_like_ip(value: str) -> bool:
    try:
        ip_address(str(value or '').strip())
        return True
    except Exception:
        return False


def normalize_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(val, (int, float)):
        return bool(val)
    return False




def normalize_product_name(raw_name: Any) -> str:
    """Normalize Product.name to host/ip[:port] or legacy scheme://host/ip[:port]."""
    name = str(raw_name or "").strip()
    if not name:
        return ""

    lowered = name.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        parsed = urlsplit(name)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return ""
        if parsed.port:
            return f"{scheme}://{host}:{parsed.port}"
        return f"{scheme}://{host}"

    base = name.split("/", 1)[0].strip()
    if "@" in base:
        base = base.rsplit("@", 1)[1].strip()
    return base.lower()


def product_name_to_target_url(name: str) -> str:
    """Build Acunetix target URL from normalized Product.name."""
    value = (name or "").strip().lower()
    if not value:
        return ""

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlsplit(value)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return ""
        if parsed.port:
            return f"{parsed.scheme}://{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}"

    host_for_ip = value
    if value.startswith("[") and "]" in value:
        host_for_ip = value[1:value.find("]")].strip()
    elif value.count(":") == 1:
        host_part, port_part = value.split(":", 1)
        if port_part.isdigit():
            host_for_ip = host_part

    has_port = ":" in value and " " not in value
    if looks_like_ip(host_for_ip) and has_port:
        if value.startswith("[") and "]" in value and value[value.find("]") + 1:value.find("]") + 2] != ":":
            has_port = False

    if looks_like_ip(host_for_ip) and has_port:
        port = None
        if value.startswith("[") and "]" in value:
            suffix = value[value.find("]") + 1:]
            if suffix.startswith(":") and suffix[1:].isdigit():
                port = int(suffix[1:])
        elif value.count(":") == 1:
            _, port_part = value.split(":", 1)
            if port_part.isdigit():
                port = int(port_part)

        scheme = "https" if port in (8443, 9443) else "http"
        return f"{scheme}://{value}"

    bare = value[4:] if value.startswith("www.") else value
    return f"https://{bare}"

def build_targets_from_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Берём только internet_accessible=true, строим URL для Acunetix и сохраняем связку product_id -> url.
    • основной контракт Product.name: host/ip[:port] без протокола
    • legacy: если схема уже задана (http/https), сохраняем её после нормализации
    • IP:port -> https://IP:port для TLS-признаков/портов (минимум 8443, 9443), иначе http://IP:port
    • домен -> https://домен (без дубликатов www.)
    """
    seen = set()
    targets: List[Dict[str, Any]] = []

    for prod in products:
        if not normalize_bool(prod.get("internet_accessible")):
            continue

        product_id = prod.get("id")
        try:
            product_id = int(product_id)
        except Exception:
            continue

        normalized_name = normalize_product_name(prod.get("name"))
        if not normalized_name:
            continue

        url = product_name_to_target_url(normalized_name)
        if not url:
            continue

        if url in seen:
            continue
        seen.add(url)
        targets.append({"product_id": product_id, "url": url, "product_name": normalized_name})

    return targets


# -------------------- Acunetix --------------------


def acu_headers(token: str) -> Dict[str, str]:
    return {
        "X-Auth": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def acu_list_groups(
    s: requests.Session,
    base_url: str,
    token: str,
) -> List[Dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v1/target_groups?limit=100&c=0"
    groups: List[Dict[str, Any]] = []

    while url:
        r = s.get(url, headers=acu_headers(token), timeout=30)
        r.raise_for_status()
        data = r.json()
        groups.extend(data.get("groups", []))

        pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
        next_cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
        if next_cursor is None:
            break
        url = f"{base_url.rstrip('/')}/api/v1/target_groups?limit=100&c={next_cursor}"

    return groups


def acu_find_group_by_name(groups: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for g in groups:
        if g.get("name") == name:
            return g
    return None


def acu_create_group(
    s: requests.Session,
    base_url: str,
    token: str,
    name: str,
    desc: str,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/target_groups"
    payload = {"name": name, "description": desc}
    r = s.post(url, headers=acu_headers(token), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def acu_list_targets(
    s: requests.Session,
    base_url: str,
    token: str,
) -> List[Dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v1/targets?l=100"
    targets: List[Dict[str, Any]] = []

    while url:
        r = s.get(url, headers=acu_headers(token), timeout=45)
        r.raise_for_status()
        data = r.json()
        targets.extend(data.get("targets", []))

        pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
        next_cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
        if next_cursor is None:
            break
        url = f"{base_url.rstrip('/')}/api/v1/targets?l=100&c={next_cursor}"

    return targets


def normalize_target_address(value: Any) -> str:
    return re.sub(r"/+$", "", str(value or "").strip().lower())


def resolve_target_mapping(
    submitted_targets: List[Dict[str, Any]],
    add_result: Dict[str, Any],
    all_targets: List[Dict[str, Any]],
) -> Dict[str, str]:
    url_to_target: Dict[str, str] = {}

    response = add_result.get("response") if isinstance(add_result, dict) else {}
    response_targets = response.get("targets", []) if isinstance(response, dict) else []
    if isinstance(response_targets, list):
        for t in response_targets:
            if not isinstance(t, dict):
                continue
            target_id = str(t.get("target_id") or "").strip()
            if not target_id:
                continue
            for raw_addr in [t.get("address"), t.get("addressValue"), t.get("target")]:
                norm = normalize_target_address(raw_addr)
                if norm:
                    url_to_target[norm] = target_id

    for t in all_targets:
        if not isinstance(t, dict):
            continue
        target_id = str(t.get("target_id") or "").strip()
        if not target_id:
            continue
        for raw_addr in [t.get("address"), t.get("target")]:
            norm = normalize_target_address(raw_addr)
            if norm:
                url_to_target[norm] = target_id

    mapping: Dict[str, str] = {}
    for row in submitted_targets:
        if not isinstance(row, dict):
            continue
        pid = row.get("product_id")
        norm = normalize_target_address(row.get("url"))
        if pid is None or not norm:
            continue
        tid = url_to_target.get(norm)
        if tid:
            mapping[str(pid)] = tid

    return mapping


def save_product_target_mapping(
    mapping: Dict[str, str],
    pt_id: int,
    acu_base_url: str,
    output_path: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "updated_at": None,
        "version": 1,
        "items": {},
    }
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                payload["updated_at"] = old.get("updated_at")
                old_items = old.get("items")
                if isinstance(old_items, dict):
                    payload["items"] = old_items
        except Exception:
            pass

    items: Dict[str, Any] = payload.get("items", {})
    for product_id, target_id in mapping.items():
        items[str(product_id)] = {
            "target_id": str(target_id),
            "product_type_id": int(pt_id),
            "acu_base_url": acu_base_url.rstrip("/"),
        }

    from datetime import datetime, timezone
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload["items"] = items

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    return {
        "path": output_path,
        "saved": len(mapping),
        "total_items": len(items),
    }


def acu_targets_add(
    s: requests.Session,
    base_url: str,
    token: str,
    group_id: str,
    pt_name: str,
    urls: List[str],
) -> Dict[str, Any]:
    """
    POST /api/v1/targets/add
    body:
    {
      "targets": [
        {"addressValue": "...", "address": "...", "description": "<pt_name>", "web_asset_id": ""}
      ],
      "groups": ["<group_id>"]
    }
    """
    url = f"{base_url.rstrip('/')}/api/v1/targets/add"
    targets = [
        {
            "addressValue": u,
            "address": u,
            "description": pt_name,
            "web_asset_id": "",
        }
        for u in urls
    ]
    payload = {"targets": targets, "groups": [group_id]}
    r = s.post(url, headers=acu_headers(token), json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text}
    return {"status": r.status_code, "response": body}


# -------------------- main --------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dojo-base-url", required=True)
    ap.add_argument("--dojo-api-token", required=True)
    ap.add_argument("--product-type-id", "--pt-id", dest="pt_id", type=int, required=True)
    ap.add_argument("--acu-base-url", "--acu-endpoint", dest="acu_base_url")
    ap.add_argument("--acu-api-token", "--acu-token", dest="acu_api_token")
    ap.add_argument("--acu-node-name", default="")
    ap.add_argument("--acu-node-json", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mapping-output", default="")
    args = ap.parse_args()

    acu_base_url = (args.acu_base_url or "").strip()
    acu_api_token = (args.acu_api_token or "").strip()
    acu_node_name = (args.acu_node_name or "").strip()

    if args.acu_node_json:
        try:
            node = json.loads(args.acu_node_json)
            if isinstance(node, dict):
                acu_base_url = acu_base_url or str(node.get("endpoint") or "").strip()
                acu_api_token = acu_api_token or str(node.get("token") or "").strip()
                acu_node_name = acu_node_name or str(node.get("name") or "").strip()
        except Exception as e:
            raise RuntimeError(f"invalid --acu-node-json: {e}")

    acu_base_url = (
        acu_base_url
        or (os.environ.get("ACUNETIX_BASE_URL") or "").strip()
        or (os.environ.get("ACU_BASE_URL") or "").strip()
    )
    acu_api_token = (
        acu_api_token
        or (os.environ.get("ACUNETIX_API_KEY") or "").strip()
        or (os.environ.get("ACU_API_TOKEN") or "").strip()
    )

    if not acu_base_url:
        raise RuntimeError("Acunetix endpoint is required: pass --acu-endpoint/--acu-base-url or --acu-node-json")
    if not acu_api_token:
        raise RuntimeError("Acunetix token is required: pass --acu-token/--acu-api-token or --acu-node-json")

    mapping_output = (
        (args.mapping_output or "").strip()
        or (os.environ.get("ACUNETIX_TARGET_MAPPING_FILE") or "").strip()
        or "/tmp/acunetix_dojo_target_mapping.json"
    )

    debug: Dict[str, Any] = {
        "pt_id": args.pt_id,
        "dojo_base_url": args.dojo_base_url,
        "acu_base_url": acu_base_url,
        "acu_node_name": acu_node_name,
        "dry_run": bool(args.dry_run),
    }

    try:
        # ---- Dojo ----
        dojo_s = make_session(verify=True)
        pt = dojo_get_product_type(dojo_s, args.dojo_base_url, args.dojo_api_token, args.pt_id)
        pt_name = pt.get("name") or f"PT-{args.pt_id}"
        debug["pt_name"] = pt_name

        products = dojo_get_products_for_pt(dojo_s, args.dojo_base_url, args.dojo_api_token, args.pt_id)
        debug["dojo_products_total"] = len(products)

        prepared_targets = build_targets_from_products(products)
        urls = [str(x.get("url")) for x in prepared_targets]
        debug["targets_prepared"] = urls
        debug["targets_prepared_count"] = len(urls)

        if not urls:
            print(json.dumps({
                "ok": False,
                "reason": "no_internet_accessible_targets",
                "debug": debug,
            }, ensure_ascii=False))
            return

        # ---- Acunetix ----
        acu_s = make_session(verify=False)

        groups = acu_list_groups(acu_s, acu_base_url, acu_api_token)
        g = acu_find_group_by_name(groups, pt_name)

        if g:
            group_id = g.get("group_id")
            debug["acu_group_existing"] = g
        else:
            if args.dry_run:
                group_id = "dry-run-group-id"
                debug["acu_group_created"] = "dry_run_only"
            else:
                created = acu_create_group(
                    acu_s,
                    acu_base_url,
                    acu_api_token,
                    pt_name,
                    f"Dojo PT #{pt.get('id')} ({pt_name})",
                )
                group_id = created.get("group_id")
                debug["acu_group_created"] = created

        debug["acu_group_id"] = group_id

        if args.dry_run:
            debug["acu_add_payload_preview"] = {
                "targets": [
                    {
                        "addressValue": u,
                        "address": u,
                        "description": pt_name,
                        "web_asset_id": "",
                    }
                    for u in urls
                ],
                "groups": [group_id],
            }
            mapping = {str(x.get("product_id")): "dry-run-target-id" for x in prepared_targets if x.get("product_id") is not None}
            debug["dojo_to_target_mapping"] = mapping
            mapping_meta = save_product_target_mapping(mapping, args.pt_id, acu_base_url, mapping_output)
            debug["mapping_output"] = mapping_meta
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "target_mapping": mapping,
                "mapping_output": mapping_meta,
                "debug": debug,
            }, ensure_ascii=False))
            return

        add_result = acu_targets_add(
            acu_s,
            acu_base_url,
            acu_api_token,
            group_id,
            pt_name,
            urls,
        )
        debug["acu_add_result"] = add_result

        if add_result["status"] not in (200, 201):
            raise RuntimeError(f"/api/v1/targets/add returned {add_result['status']}: {add_result['response']}")

        all_targets = acu_list_targets(acu_s, acu_base_url, acu_api_token)
        mapping = resolve_target_mapping(prepared_targets, add_result, all_targets)
        debug["dojo_to_target_mapping"] = mapping
        debug["dojo_to_target_mapping_count"] = len(mapping)

        missing_ids = [str(x.get("product_id")) for x in prepared_targets if str(x.get("product_id")) not in mapping]
        if missing_ids:
            debug["mapping_missing_product_ids"] = missing_ids

        mapping_meta = save_product_target_mapping(mapping, args.pt_id, acu_base_url, mapping_output)
        debug["mapping_output"] = mapping_meta

        print(json.dumps({
            "ok": True,
            "product_type_id": args.pt_id,
            "product_type_name": pt_name,
            "group_id": group_id,
            "targets_count": len(urls),
            "target_mapping": mapping,
            "mapping_output": mapping_meta,
            "debug": debug,
        }, ensure_ascii=False))

    except Exception as e:
        debug["exception"] = str(e)
        debug["traceback"] = traceback.format_exc()
        print(json.dumps({
            "ok": False,
            "error": "unexpected_error",
            "details": str(e),
            "debug": debug,
        }, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
