"""
fetch_data.py — corre no GitHub Actions todas as semanas.
Vai buscar dados à API do Guia do Automóvel (apinode.netcar.pt),
faz merge com range_overrides.json (autonomias + links manuais),
e gera data.json para a app HTML consumir.
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

# Limite de IVA dedutível (preço s/ IVA)
LIMITE_IVA_EV   = 62_500   # art.º 21.º n.º 2 al. f) CIVA
LIMITE_IVA_PHEV = 50_000   # art.º 21.º n.º 2 al. g) CIVA
IVA_RATE        = 0.23

# Combustíveis PHEV reconhecidos pelo Guia
PHEV_FUELS = [
    "Híbrido (Plug-In)",
    "Gasolina / Híbrido Plug-in",
    "Híbrido Plug-In Gasóleo",
]

# ── Helpers ────────────────────────────────────────────────────────────────────
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
    """
    Agrupa listings por (Marca, Modelo).
    Guarda o preço mais baixo encontrado e a melhor foto.
    """
    models: dict = {}
    for c in listings:
        maker = (c.get("maker_name") or "").strip()
        model = (c.get("car_model") or "").strip().lstrip()
        if not maker or not model:
            continue

        pvp = c.get("pvp") or c.get("promo_price") or 0
        if not pvp or pvp < 5_000:
            continue   # dados inválidos

        key = f"{maker}||{model}"
        photo = best_photo(c)
        hp    = c.get("engine_hp") or 0
        body  = c.get("body_type", "")

        existing = models.get(key)
        if not existing or pvp < existing["pvp"]:
            models[key] = {
                "Marca":     maker,
                "Modelo":    model,
                "Tipo":      tipo,
                "pvp":       pvp,
                "hp":        hp,
                "carrocaria": body,
                "foto":      photo,
                "fuel_raw":  c.get("fuel", ""),
            }
        else:
            # Actualiza foto se ainda não temos uma
            if photo and not existing["foto"]:
                existing["foto"] = photo
            # Actualiza HP se ficou 0
            if hp and not existing["hp"]:
                existing["hp"] = hp

    return models


def compute_iva(pvp: float, pvp_sem_iva: float, tipo: str, autonomia: int, max_aut_elegivel: int) -> str:
    limite = LIMITE_IVA_EV if tipo == "EV" else LIMITE_IVA_PHEV
    if pvp_sem_iva <= limite:
        return "Sim"
    # Critério de autonomia: acima do limite mas com autonomia superior ao máximo elegível
    if autonomia and max_aut_elegivel and autonomia > max_aut_elegivel:
        return "Autonomia"   # incluído por autonomia superior
    return "Não"


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    root = Path(__file__).parent.parent  # raiz do repositório

    # Carrega overrides manuais (autonomia, links, observações)
    overrides_path = root / "range_overrides.json"
    overrides = {}
    if overrides_path.exists():
        with open(overrides_path, encoding="utf-8") as f:
            overrides = json.load(f)
    print(f"Overrides carregados: {len(overrides)} entradas")

    # ── Fetch EV ──
    print("\nA obter EVs...")
    ev_listings = api_search("Elétrico")
    print(f"  EV listings: {len(ev_listings)}")
    ev_models = build_catalogue(ev_listings, "EV")

    # ── Fetch PHEV ──
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

    # Carrega modelos manuais (modelos sem stock no Guia mas que existem no mercado PT)
    manual_path = root / "manual_models.json"
    manual_models_list = []
    if manual_path.exists():
        with open(manual_path, encoding="utf-8") as f:
            manual_models_list = json.load(f)
    print(f"Modelos manuais carregados: {len(manual_models_list)}")

    # ── Merge com overrides ──
    all_models = []

    # Chaves já adicionadas pela API (para evitar duplicados com manual)
    api_keys_added: set = set()

    def merge(models_dict: dict):
        for key, m in models_dict.items():
            ov = overrides.get(key, {})
            pvp         = m["pvp"]
            pvp_sem_iva = round(pvp / (1 + IVA_RATE), 2)
            autonomia   = ov.get("autonomia_km", 0) or 0

            entry = {
                "Marca":                     m["Marca"],
                "Modelo":                    m["Modelo"],
                "Tipo":                      m["Tipo"],
                "Preço desde (€ PVP)":       pvp,
                "Preço s/ IVA estimado (€)": pvp_sem_iva,
                "Autonomia elétrica (km)":   autonomia,
                "Potência (cv)":             m["hp"],
                "Carroçaria":                m["carrocaria"],
                "Foto":                      m["foto"],
                "IVA dedutível empresas?":   "",   # preenchido abaixo
                "Representante PT":          ov.get("representante_pt", ""),
                "Fonte Guia":                ov.get("fonte_guia", ""),
                "Observações":               ov.get("observacoes", ""),
            }
            all_models.append(entry)
            api_keys_added.add(key)

    merge(ev_models)
    merge(phev_models)

    # Adiciona modelos manuais que não foram encontrados pela API
    manual_added = 0
    for m in manual_models_list:
        key = f"{m['Marca']}||{m['Modelo']}"
        if key not in api_keys_added:
            # Tenta enriquecer com overrides se existir
            ov = overrides.get(key, {})
            pvp = m.get("Preço desde (€ PVP)", 0)
            entry = {
                "Marca":                     m["Marca"],
                "Modelo":                    m["Modelo"],
                "Tipo":                      m["Tipo"],
                "Preço desde (€ PVP)":       pvp,
                "Preço s/ IVA estimado (€)": round(pvp / (1 + IVA_RATE), 2) if pvp else 0,
                "Autonomia elétrica (km)":   ov.get("autonomia_km") or m.get("Autonomia elétrica (km)", 0),
                "Potência (cv)":             m.get("Potência (cv)", 0),
                "Carroçaria":                m.get("Carroçaria", ""),
                "Foto":                      m.get("Foto", ""),
                "IVA dedutível empresas?":   "",   # calculado abaixo
                "Representante PT":          ov.get("representante_pt") or m.get("Representante PT", ""),
                "Fonte Guia":                ov.get("fonte_guia") or m.get("Fonte Guia", ""),
                "Observações":               ov.get("observacoes") or m.get("Observações", ""),
            }
            all_models.append(entry)
            manual_added += 1

    print(f"Modelos manuais adicionados: {manual_added}")

    # Calcular máxima autonomia elegível por tipo (para critério de autonomia superior)
    def max_aut_elegivel(tipo: str, limite: float) -> int:
        eligible = [
            m["Autonomia elétrica (km)"]
            for m in all_models
            if m["Tipo"] == tipo
            and m["Preço s/ IVA estimado (€)"] <= limite
            and m["Autonomia elétrica (km)"]
        ]
        return max(eligible) if eligible else 0

    max_ev_aut   = max_aut_elegivel("EV",   LIMITE_IVA_EV)
    max_phev_aut = max_aut_elegivel("PHEV", LIMITE_IVA_PHEV)
    print(f"\nMáx. autonomia elegível EV: {max_ev_aut} km")
    print(f"Máx. autonomia elegível PHEV: {max_phev_aut} km")

    # Preencher IVA
    for m in all_models:
        limite = LIMITE_IVA_EV if m["Tipo"] == "EV" else LIMITE_IVA_PHEV
        max_aut = max_ev_aut if m["Tipo"] == "EV" else max_phev_aut
        m["IVA dedutível empresas?"] = compute_iva(
            m["Preço desde (€ PVP)"],
            m["Preço s/ IVA estimado (€)"],
            m["Tipo"],
            m["Autonomia elétrica (km)"],
            max_aut,
        )

    # Ordenar por tipo + preço
    all_models.sort(key=lambda x: (x["Tipo"], x["Preço desde (€ PVP)"]))

    # ── Escrever data.json ──
    output = {
        "updated":       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total":         len(all_models),
        "ev_count":      sum(1 for m in all_models if m["Tipo"] == "EV"),
        "phev_count":    sum(1 for m in all_models if m["Tipo"] == "PHEV"),
        "iva_limit_ev":  LIMITE_IVA_EV,
        "iva_limit_phev": LIMITE_IVA_PHEV,
        "max_aut_ev_elegivel":   max_ev_aut,
        "max_aut_phev_elegivel": max_phev_aut,
        "models":        all_models,
    }

    out_path = root / "data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✓ data.json escrito: {len(all_models)} modelos")
    print(f"  EV: {output['ev_count']} | PHEV: {output['phev_count']}")
    print(f"  IVA Sim: {sum(1 for m in all_models if m['IVA dedutível empresas?']=='Sim')}")
    print(f"  IVA Autonomia: {sum(1 for m in all_models if m['IVA dedutível empresas?']=='Autonomia')}")
    print(f"  IVA Não: {sum(1 for m in all_models if m['IVA dedutível empresas?']=='Não')}")
    print(f"  Ficheiro: {out_path}")


if __name__ == "__main__":
    main()
