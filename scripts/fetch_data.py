"""
fetch_data.py — corre no GitHub Actions todas as semanas.
Vai buscar dados à API do Guia do Automóvel (apinode.netcar.pt),
faz merge com range_overrides.json (autonomias, variantes, links manuais),
e gera data.json para a app HTML consumir.

Suporta modelos com múltiplas variantes/motorizações (ex: Volvo EX60 P6/P10/P12),
cada uma com o seu próprio preço, autonomia e elegibilidade de IVA.
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── Configuração ───────────────────────────────────────────────────────────────
API_URL   = "https://apinode.netcar.pt/v1/classifieds/search"
HEADERS   = {
    "accept":       "application/json",
    "content-type": "application/json",
    "origin":       "https://www.guiadoautomovel.pt",
    "referer":      "https://www.guiadoautomovel.pt/",
    "user-agent":   "Mozilla/5.0 (compatible; GuiaAutoBot/1.0)",
}

LIMITE_IVA_EV   = 62_500
LIMITE_IVA_PHEV = 50_000
IVA_RATE        = 0.23

PHEV_FUELS = [
    "Híbrido (Plug-In)",
    "Gasolina / Híbrido Plug-in",
    "Híbrido Plug-In Gasóleo",
]


def api_search(fuel: str, max_results: int = 500) -> list:
    payload = json.dumps({"search": {"fuel": fuel}, "index": 0, "total": max_results}).encode()
    req = urllib.request.Request(API_URL, data=payload, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
            return data.get("available_cars", [])
    except urllib.error.URLError as e:
        print(f"  ⚠ Erro a obter fuel={fuel!r}: {e}")
        return []


def best_photo(car: dict) -> str:
    imgs = car.get("car_images") or []
    main = car.get("main_image") or {}
    for source in [imgs[0] if imgs else {}, main]:
        for size in ("large", "medium", "original"):
            url = source.get(size, "")
            if url:
                return url
    return ""


def build_catalogue(listings: list, tipo: str) -> dict:
    models: dict = {}
    for c in listings:
        maker = (c.get("maker_name") or "").strip()
        model = (c.get("car_model") or "").strip().lstrip()
        if not maker or not model:
            continue
        pvp = c.get("pvp") or c.get("promo_price") or 0
        if not pvp or pvp < 5_000:
            continue
        key = f"{maker}||{model}"
        photo = best_photo(c)
        hp    = c.get("engine_hp") or 0
        body  = c.get("body_type", "")
        existing = models.get(key)
        if not existing or pvp < existing["pvp"]:
            models[key] = {
                "Marca": maker, "Modelo": model, "Tipo": tipo,
                "pvp": pvp, "hp": hp, "carrocaria": body, "foto": photo,
            }
        else:
            if photo and not existing["foto"]:
                existing["foto"] = photo
            if hp and not existing["hp"]:
                existing["hp"] = hp
    return models


def limite_iva(tipo: str) -> float:
    return LIMITE_IVA_EV if tipo == "EV" else LIMITE_IVA_PHEV


def compute_iva_status(pvp_sem_iva: float, tipo: str, autonomia: int, max_aut_elegivel: int) -> str:
    if pvp_sem_iva <= limite_iva(tipo):
        return "Sim"
    if autonomia and max_aut_elegivel and autonomia > max_aut_elegivel:
        return "Autonomia"
    return "Não"


def summarize_iva(variant_statuses: list) -> str:
    statuses = set(variant_statuses)
    if statuses == {"Sim"}:
        return "Sim"
    if "Sim" in statuses:
        return "Parcial"
    if "Autonomia" in statuses:
        return "Autonomia"
    return "Não"


def build_entry_from_base(marca, modelo, tipo, pvp, hp, carrocaria, foto, ov) -> dict:
    variantes_ov = ov.get("variantes")

    if variantes_ov:
        variantes = []
        for v in variantes_ov:
            v_pvp = v["pvp"]
            v_pvp_sem_iva = round(v_pvp / (1 + IVA_RATE), 2)
            variantes.append({
                "nome":          v["nome"],
                "pvp":           v_pvp,
                "pvp_sem_iva":   v_pvp_sem_iva,
                "autonomia_km":  v.get("autonomia_km", 0) or 0,
                "hp":            v.get("hp", 0) or 0,
                "estimado":      bool(v.get("estimado", False)),
                "iva_dedutivel": "",
            })
        min_variant = min(variantes, key=lambda v: v["pvp"])
        max_aut     = max(v["autonomia_km"] for v in variantes)
        pvp_agr         = min_variant["pvp"]
        pvp_sem_iva_agr = min_variant["pvp_sem_iva"]
        hp_agr          = min_variant["hp"]
    else:
        variantes = None
        pvp_agr         = pvp
        pvp_sem_iva_agr = round(pvp / (1 + IVA_RATE), 2)
        max_aut         = ov.get("autonomia_km", 0) or 0
        hp_agr          = hp

    entry = {
        "Marca":                     marca,
        "Modelo":                    modelo,
        "Tipo":                      tipo,
        "Preço desde (€ PVP)":       pvp_agr,
        "Preço s/ IVA estimado (€)": pvp_sem_iva_agr,
        "Autonomia elétrica (km)":   max_aut,
        "Potência (cv)":             hp_agr,
        "Carroçaria":                carrocaria,
        "Foto":                      foto,
        "IVA dedutível empresas?":   "",
        "Representante PT":          ov.get("representante_pt", ""),
        "Fonte Guia":                ov.get("fonte_guia", ""),
        "Observações":               ov.get("observacoes", ""),
    }
    if variantes:
        entry["Variantes"] = variantes
    return entry


def main():
    root = Path(__file__).parent.parent

    overrides_path = root / "range_overrides.json"
    overrides = {}
    if overrides_path.exists():
        with open(overrides_path, encoding="utf-8") as f:
            overrides = json.load(f)
    print(f"Overrides carregados: {len(overrides)} entradas")

    print("\nA obter EVs...")
    ev_listings = api_search("Elétrico")
    print(f"  EV listings: {len(ev_listings)}")
    ev_models = build_catalogue(ev_listings, "EV")

    print("A obter PHEVs...")
    phev_listings: list = []
    for fuel in PHEV_FUELS:
        time.sleep(0.4)
        batch = api_search(fuel)
        print(f"  {fuel}: {len(batch)} listings")
        phev_listings.extend(batch)
    phev_models = build_catalogue(phev_listings, "PHEV")

    print(f"\nEV modelos únicos: {len(ev_models)}")
    print(f"PHEV modelos únicos: {len(phev_models)}")

    manual_path = root / "manual_models.json"
    manual_models_list = []
    if manual_path.exists():
        with open(manual_path, encoding="utf-8") as f:
            manual_models_list = json.load(f)
    print(f"Modelos manuais carregados: {len(manual_models_list)}")

    all_models = []
    api_keys_added: set = set()

    def merge(models_dict: dict):
        for key, m in models_dict.items():
            ov = overrides.get(key, {})
            entry = build_entry_from_base(
                m["Marca"], m["Modelo"], m["Tipo"],
                m["pvp"], m["hp"], m["carrocaria"], m["foto"], ov,
            )
            all_models.append(entry)
            api_keys_added.add(key)

    merge(ev_models)
    merge(phev_models)

    manual_added = 0
    for m in manual_models_list:
        key = f"{m['Marca']}||{m['Modelo']}"
        if key in api_keys_added:
            continue
        ov = overrides.get(key, {})
        pvp = m.get("Preço desde (€ PVP)", 0)
        entry = build_entry_from_base(
            m["Marca"], m["Modelo"], m["Tipo"],
            pvp, m.get("Potência (cv)", 0), m.get("Carroçaria", ""), m.get("Foto", ""), ov,
        )
        if not ov.get("variantes"):
            if not entry["Autonomia elétrica (km)"]:
                entry["Autonomia elétrica (km)"] = m.get("Autonomia elétrica (km)", 0)
            if not entry["Representante PT"]:
                entry["Representante PT"] = m.get("Representante PT", "")
            if not entry["Fonte Guia"]:
                entry["Fonte Guia"] = m.get("Fonte Guia", "")
            if not entry["Observações"]:
                entry["Observações"] = m.get("Observações", "")
        all_models.append(entry)
        manual_added += 1

    print(f"Modelos manuais adicionados: {manual_added}")

    def max_aut_elegivel_por_preco(tipo: str) -> int:
        candidatos = []
        for m in all_models:
            if m["Tipo"] != tipo:
                continue
            if m.get("Variantes"):
                for v in m["Variantes"]:
                    if v["pvp_sem_iva"] <= limite_iva(tipo) and v["autonomia_km"]:
                        candidatos.append(v["autonomia_km"])
            else:
                if m["Preço s/ IVA estimado (€)"] <= limite_iva(tipo) and m["Autonomia elétrica (km)"]:
                    candidatos.append(m["Autonomia elétrica (km)"])
        return max(candidatos) if candidatos else 0

    max_ev_aut   = max_aut_elegivel_por_preco("EV")
    max_phev_aut = max_aut_elegivel_por_preco("PHEV")
    print(f"\nMáx. autonomia elegível EV: {max_ev_aut} km")
    print(f"Máx. autonomia elegível PHEV: {max_phev_aut} km")

    for m in all_models:
        tipo    = m["Tipo"]
        max_aut = max_ev_aut if tipo == "EV" else max_phev_aut

        if m.get("Variantes"):
            statuses = []
            for v in m["Variantes"]:
                status = compute_iva_status(v["pvp_sem_iva"], tipo, v["autonomia_km"], max_aut)
                v["iva_dedutivel"] = status
                statuses.append(status)
            m["IVA dedutível empresas?"] = summarize_iva(statuses)
        else:
            m["IVA dedutível empresas?"] = compute_iva_status(
                m["Preço s/ IVA estimado (€)"], tipo, m["Autonomia elétrica (km)"], max_aut
            )

    all_models.sort(key=lambda x: (x["Tipo"], x["Preço desde (€ PVP)"]))

    output = {
        "updated":               datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total":                 len(all_models),
        "ev_count":              sum(1 for m in all_models if m["Tipo"] == "EV"),
        "phev_count":            sum(1 for m in all_models if m["Tipo"] == "PHEV"),
        "iva_limit_ev":          LIMITE_IVA_EV,
        "iva_limit_phev":        LIMITE_IVA_PHEV,
        "max_aut_ev_elegivel":   max_ev_aut,
        "max_aut_phev_elegivel": max_phev_aut,
        "models":                all_models,
    }

    out_path = root / "data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    com_variantes = sum(1 for m in all_models if m.get("Variantes"))
    print(f"\n✓ data.json escrito: {len(all_models)} modelos ({com_variantes} com variantes)")
    print(f"  EV: {output['ev_count']} | PHEV: {output['phev_count']}")
    print(f"  IVA Sim: {sum(1 for m in all_models if m['IVA dedutível empresas?']=='Sim')}")
    print(f"  IVA Parcial: {sum(1 for m in all_models if m['IVA dedutível empresas?']=='Parcial')}")
    print(f"  IVA Autonomia: {sum(1 for m in all_models if m['IVA dedutível empresas?']=='Autonomia')}")
    print(f"  IVA Não: {sum(1 for m in all_models if m['IVA dedutível empresas?']=='Não')}")
    print(f"  Ficheiro: {out_path}")


if __name__ == "__main__":
    main()
