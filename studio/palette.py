"""Nuancier fournisseur : lecture du CSV (reference ; couleur hexadecimale)."""
import csv
import io
import re

HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_csv(data):
    """-> (couleurs [{"code", "hex"}], rapport {"lines", "kept", "duplicates", "same_code", "invalid"}).
    1re colonne = reference fournisseur, 2e = hexadecimal. Separateur ; ou , ; en-tete et colonnes vides ignores."""
    if isinstance(data, bytes):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                data = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    sample = data[:2000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    out, seen, codes = [], set(), {}
    rep = {"lines": 0, "kept": 0, "duplicates": [], "same_code": [], "invalid": []}
    for row in csv.reader(io.StringIO(data), delimiter=delim):
        cells = [c.strip() for c in row if c.strip()]
        if len(cells) < 2:
            continue
        rep["lines"] += 1
        code, m = cells[0], HEX.match(cells[1])
        if not m:
            if rep["lines"] > 1:          # la 1re ligne non valide = en-tete
                rep["invalid"].append(code)
            continue
        hx = "#" + m.group(1).upper()
        if (code, hx) in seen:
            rep["duplicates"].append(code)
            continue
        if code in codes and codes[code] != hx:
            rep["same_code"].append(code)   # meme reference, deux teintes : on garde les deux
        seen.add((code, hx))
        codes.setdefault(code, hx)
        out.append({"code": code, "hex": hx})
    rep["kept"] = len(out)
    return out, rep
