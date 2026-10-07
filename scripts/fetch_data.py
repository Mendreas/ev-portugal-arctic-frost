"""
fetch_data.py — corre no GitHub Actions todas as semanas.

Fontes:
  - API do Guia do Automóvel (apinode.netcar.pt): preço e fotos de carros NOVOS em stock
  - manual_models.json: modelos à venda em PT sem stock no Guia
  - range_overrides.json: autonomias, variantes, links, observações
  - specs.json: bateria, carga DC, autonomia real, ano (valores indicativos)

Regras:
  - Só carros novos (condition != "used"); os usados são ignorados.
  - Marcas/modelos são normalizados (sem maiúsculas, hífenes ou espaços) para evitar duplicados.
  - extra_models.json: modelos sem preço novo disponível (preço de referência estimado, marcado)
  - Campos sem dados ficam vazios (a app mostra "—"); nada é inventado.
"""

import json
import re
import time
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://apinode.netcar.pt/v1/classifieds/search"
HEADERS = {
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

# Tipologia simplificada para os filtros da app
BODY_MAP = {
    "SUV": "SUV", "Crossover": "SUV", "Monovolume": "SUV",
    "Carrinha": "Carrinha", "Station Wagon": "Carrinha",
    "Hatchback": "Citadino", "Micro Carro": "Citadino", "Citadino": "Citadino",
    "Sedan": "Sedan", "Coupé": "Sedan", "Cabrio": "Sedan",
    "Comercial": "Comercial",
}

# Tipologia dos modelos sem carroçaria no Guia (chave normalizada do modelo)
BODY_BY_MODEL = {
    "5etechelectric": "Citadino", "inster": "Citadino", "kia ev2": "Citadino", "ev2": "Citadino",
    "4etechelectric": "SUV", "e208": "Citadino", "ds3etense": "SUV", "mg4electric": "Citadino",
    "ev3": "SUV", "ev4": "Sedan", "ev5": "SUV", "ev6": "SUV", "ev9": "SUV",
    "b10": "SUV", "c10": "SUV", "mokkaelectric": "SUV", "e2008": "SUV",
    "fronteraelectric": "SUV", "kauaielectric": "SUV", "edoblo": "Comercial",
    "model3": "Sedan", "modely": "SUV", "models": "Sedan", "atto3": "SUV", "sealu": "SUV",
    "scenicetechelectric": "SUV", "3": "SUV", "e408": "SUV", "ec4": "SUV",
    "ec4x": "SUV", "e3008": "SUV", "ioniq5": "SUV", "ioniq6": "Sedan", "ioniq9": "SUV",
    "ix1": "SUV", "grandlandelectric": "SUV", "espacetourer": "Comercial",
    "ec40": "SUV", "ex60": "SUV", "ex90": "SUV", "es90": "Sedan", "eqb": "SUV",
    "claeq": "Sedan", "id7": "Sedan", "no8": "SUV", "idbuzz": "Comercial",
    "q6etron": "SUV", "a6etron": "Sedan", "tang": "SUV", "polestar3": "SUV",
    "macanelectric": "SUV", "cyberster": "Sedan",
}

# Tipologia por palavras-chave nos PHEV (modelos manuais)
PHEV_SUV_WORDS = ["aircross", "x1", "sportage", "q3", "tiguan", "kodiaq", "c-hr", "chr", "tucson",
                  "cx-60", "cx60", "cx-80", "cx80", "xc60", "xc90", "ds 7", "ds7", "sorento",
                  "hs", "s9", "jaecoo", "omoda", "seal u", "grandland", "3008", "5008", "nº4", "no4"]
PHEV_WAGON_WORDS = ["variant", "v60"]
PHEV_HATCH_WORDS = ["astra", "308", "prius", "a3", "golf"]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def api_search(fuel: str, max_results: int = 500) -> list:
    payload = json.dumps({"search": {"fuel": fuel}, "index": 0, "total": max_results}).encode()
    req = urllib.request.Request(API_URL, data=payload, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read()).get("available_cars", [])
    except Exception as e:
        print(f"  ⚠ Erro a obter fuel={fuel!r}: {e}")
        return []


def photos_of(car: dict) -> list:
    """Devolve até 8 URLs de fotos (tamanho large/medium/original)."""
    out = []
    for img in (car.get("car_images") or []):
        for size in ("large", "medium", "original"):
            if img.get(size):
                out.append(img[size])
                break
    main = car.get("main_image") or {}
    for size in ("large", "medium", "original"):
        if main.get(size):
            out.insert(0, main[size])
            break
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:8]


def build_api_catalogue(listings: list, tipo: str) -> dict:
    """Só carros novos. Chave = norm(marca)+norm(modelo)."""
    models = {}
    for c in listings:
        if (c.get("condition") or "").lower() == "used":
            continue
        maker = (c.get("maker_name") or "").strip()
        model = (c.get("car_model") or "").strip()
        if not maker or not model:
            continue
        try:
            pvp = float(c.get("pvp") or c.get("promo_price") or 0)
            hp = int(float(c.get("engine_hp") or 0))
            year = int(float(c.get("year") or 0))
        except ValueError:
            continue
        if pvp < 5_000:
            continue
        key = norm(maker) + "|" + norm(model)
        ph = photos_of(c)
        cur = models.get(key)
        if not cur or pvp < cur["pvp"]:
            models[key] = {"Marca": maker, "Modelo": model, "Tipo": tipo, "pvp": pvp, "hp": hp,
                           "carrocaria": c.get("body_type", ""), "fotos": ph, "ano": year}
        else:
            for u in ph:
                if u not in cur["fotos"] and len(cur["fotos"]) < 8:
                    cur["fotos"].append(u)
    return models


def limite_iva(tipo: str) -> float:
    return LIMITE_IVA_EV if tipo == "EV" else LIMITE_IVA_PHEV


def compute_iva_status(pvp_sem_iva, tipo, autonomia, max_aut_elegivel) -> str:
    if pvp_sem_iva <= limite_iva(tipo):
        return "Sim"
    if autonomia and max_aut_elegivel and autonomia > max_aut_elegivel:
        return "Autonomia"
    return "Não"


def summarize_iva(statuses: list) -> str:
    s = set(statuses)
    if s == {"Sim"}:
        return "Sim"
    if "Sim" in s:
        return "Parcial"
    if "Autonomia" in s:
        return "Autonomia"
    return "Não"


def tipologia(m: dict) -> str:
    b = BODY_MAP.get(m.get("Carroçaria", ""), "")
    if b:
        return b
    mk = norm(m["Modelo"])
    if mk in BODY_BY_MODEL:
        return BODY_BY_MODEL[mk]
    name = m["Modelo"].lower()
    if m["Tipo"] == "PHEV":
        if any(w in name for w in PHEV_WAGON_WORDS):
            return "Carrinha"
        if any(w in name for w in PHEV_SUV_WORDS):
            return "SUV"
        if any(w in name for w in PHEV_HATCH_WORDS):
            return "Citadino"
        return "Sedan"
    return "Outro"


def build_entry(marca, modelo, tipo, pvp, hp, carrocaria, fotos, ano, ov, spec) -> dict:
    variantes_ov = ov.get("variantes")
    if variantes_ov:
        variantes = []
        for v in variantes_ov:
            v_pvp = v["pvp"]
            variantes.append({
                "nome": v["nome"], "pvp": v_pvp,
                "pvp_sem_iva": round(v_pvp / (1 + IVA_RATE), 2),
                "autonomia_km": v.get("autonomia_km", 0) or 0,
                "hp": v.get("hp", 0) or 0,
                "estimado": bool(v.get("estimado", False)),
                "iva_dedutivel": "",
            })
        mv = min(variantes, key=lambda v: v["pvp"])
        max_aut = max(v["autonomia_km"] for v in variantes)
        pvp_agr, sem_agr, hp_agr = mv["pvp"], mv["pvp_sem_iva"], mv["hp"]
    else:
        variantes = None
        pvp_agr, sem_agr = pvp, round(pvp / (1 + IVA_RATE), 2)
        max_aut, hp_agr = ov.get("autonomia_km", 0) or 0, hp

    e = {
        "Marca": marca, "Modelo": modelo, "Tipo": tipo,
        "Preço desde (€ PVP)": pvp_agr,
        "Preço s/ IVA estimado (€)": sem_agr,
        "Autonomia elétrica (km)": max_aut,
        "Potência (cv)": hp_agr,
        "Carroçaria": carrocaria,
        "Foto": fotos[0] if fotos else "",
        "Fotos": fotos,
        "Ano": spec.get("ano") or ano or 0,
        "Bateria útil (kWh)": spec.get("bateria_kwh", 0),
        "Carga DC máx. (kW)": spec.get("dc_kw", 0),
        "Tempo 10-80% DC (min)": spec.get("tempo_10_80_min", 0),
        "Autonomia real (km)": spec.get("autonomia_real_km", 0),
        "Dados técnicos": spec.get("fonte", "indicativo") if spec else "",
        "IVA dedutível empresas?": "",
        "Representante PT": ov.get("representante_pt", ""),
        "Fonte Guia": ov.get("fonte_guia", ""),
        "Observações": ov.get("observacoes", ""),
    }
    if variantes:
        e["Variantes"] = variantes
    return e


def load_json(path: Path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def main():
    root = Path(__file__).parent.parent
    overrides_raw = load_json(root / "range_overrides.json", {})
    specs_raw = load_json(root / "specs.json", {})
    manual_list = load_json(root / "manual_models.json", [])
    extra_list = load_json(root / "extra_models.json", [])

    def index_by_key(d):
        out = {}
        for k, v in d.items():
            marca, _, modelo = k.partition("||")
            out[norm(marca) + "|" + norm(modelo)] = v
        return out

    overrides, specs = index_by_key(overrides_raw), index_by_key(specs_raw)
    print(f"Overrides: {len(overrides)} | Specs: {len(specs)} | Manuais: {len(manual_list)}")

    print("\nA obter EVs (só novos)...")
    api = build_api_catalogue(api_search("Elétrico"), "EV")
    print("A obter PHEVs (só novos)...")
    for fuel in PHEV_FUELS:
        time.sleep(0.4)
        api.update(build_api_catalogue(api_search(fuel), "PHEV"))
    print(f"Modelos novos com stock no Guia: {len(api)}")

    # 1) Base = modelos manuais (nomes e dados cuidados)
    base = {}
    for m in manual_list:
        key = norm(m["Marca"]) + "|" + norm(m["Modelo"])
        base[key] = {
            "Marca": m["Marca"], "Modelo": m["Modelo"], "Tipo": m["Tipo"],
            "pvp": m.get("Preço desde (€ PVP)", 0), "hp": m.get("Potência (cv)", 0),
            "carrocaria": m.get("Carroçaria", ""),
            "fotos": [m["Foto"]] if m.get("Foto") else [], "ano": 0,
            "man": m,
        }

    # 1b) Modelos sem preço novo disponível: preço de referência ESTIMADO (marcado na app)
    for m in extra_list:
        key = norm(m["Marca"]) + "|" + norm(m["Modelo"])
        if key not in base:
            base[key] = {"Marca": m["Marca"], "Modelo": m["Modelo"], "Tipo": m["Tipo"],
                         "pvp": m["pvp_estimado"], "hp": 0, "carrocaria": "",
                         "fotos": [m["Foto"]] if m.get("Foto") else [], "ano": 0,
                         "preco_estimado": True, "tipologia": m.get("Tipologia", "")}

    # 2) API enriquece (preço real, fotos reais) ou acrescenta modelos novos
    for key, a in api.items():
        if key in base:
            b = base[key]
            b["pvp"] = a["pvp"] or b["pvp"]
            b["hp"] = a["hp"] or b["hp"]
            b["carrocaria"] = a["carrocaria"] or b["carrocaria"]
            b["fotos"] = a["fotos"] + [u for u in b["fotos"] if u not in a["fotos"]]
            b["ano"] = a["ano"]
        else:
            base[key] = a

    all_models = []
    for key, b in base.items():
        ov = overrides.get(key, {})
        spec = specs.get(key, {})
        entry = build_entry(b["Marca"], b["Modelo"], b["Tipo"], b["pvp"], b["hp"],
                            b["carrocaria"], b["fotos"][:8], b["ano"], ov, spec)
        man = b.get("man")
        if man and not ov.get("variantes"):
            if not entry["Autonomia elétrica (km)"]:
                entry["Autonomia elétrica (km)"] = man.get("Autonomia elétrica (km)", 0)
            for f in ("Representante PT", "Fonte Guia", "Observações"):
                if not entry[f]:
                    entry[f] = man.get(f, "")
        entry["Preço estimado"] = bool(b.get("preco_estimado")) and not ov.get("variantes")
        entry["Tipologia"] = b.get("tipologia") or tipologia(entry)
        all_models.append(entry)

    def max_elegivel(tipo):
        c = []
        for m in all_models:
            if m["Tipo"] != tipo:
                continue
            rows = m.get("Variantes") or [{"pvp_sem_iva": m["Preço s/ IVA estimado (€)"],
                                           "autonomia_km": m["Autonomia elétrica (km)"]}]
            c += [v["autonomia_km"] for v in rows
                  if v["pvp_sem_iva"] <= limite_iva(tipo) and v["autonomia_km"]]
        return max(c) if c else 0

    max_ev, max_phev = max_elegivel("EV"), max_elegivel("PHEV")
    print(f"Máx. autonomia elegível EV: {max_ev} km | PHEV: {max_phev} km")

    for m in all_models:
        tipo = m["Tipo"]
        mx = max_ev if tipo == "EV" else max_phev
        if m.get("Variantes"):
            sts = []
            for v in m["Variantes"]:
                v["iva_dedutivel"] = compute_iva_status(v["pvp_sem_iva"], tipo, v["autonomia_km"], mx)
                sts.append(v["iva_dedutivel"])
            m["IVA dedutível empresas?"] = summarize_iva(sts)
        else:
            m["IVA dedutível empresas?"] = compute_iva_status(
                m["Preço s/ IVA estimado (€)"], tipo, m["Autonomia elétrica (km)"], mx)

    all_models.sort(key=lambda x: (x["Tipo"], x["Preço desde (€ PVP)"]))

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(all_models),
        "ev_count": sum(1 for m in all_models if m["Tipo"] == "EV"),
        "phev_count": sum(1 for m in all_models if m["Tipo"] == "PHEV"),
        "iva_limit_ev": LIMITE_IVA_EV, "iva_limit_phev": LIMITE_IVA_PHEV,
        "max_aut_ev_elegivel": max_ev, "max_aut_phev_elegivel": max_phev,
        "models": all_models,
    }
    with open(root / "data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n✓ data.json: {len(all_models)} modelos "
          f"(EV {out['ev_count']}, PHEV {out['phev_count']}); "
          f"{sum(1 for m in all_models if m.get('Variantes'))} com variantes; "
          f"{sum(1 for m in all_models if not m['Autonomia elétrica (km)'])} sem autonomia")


if __name__ == "__main__":
    main()
