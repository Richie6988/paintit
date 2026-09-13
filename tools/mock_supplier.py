"""Serveur fournisseur factice pour tester l'integration.

Lancement :  python tools/mock_supplier.py 8090
Puis pointer Django dessus :  export SUPPLIER_API_URL=http://127.0.0.1:8090/orders
Chaque bon de commande recu est affiche et enregistre dans tools/received/.
Repond {"id": "MOCK-xxxx", "reference": <ref>} pour alimenter supplier_ref.
"""
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = os.path.join(os.path.dirname(__file__), "received")
os.makedirs(OUT, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {"_raw": raw.decode("utf-8", "replace")}
        ref = data.get("reference", "unknown")
        mock_id = "MOCK-" + uuid.uuid4().hex[:6].upper()
        with open(os.path.join(OUT, f"{ref}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n=== Bon de commande recu : {ref} -> {mock_id} ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        body = json.dumps({"id": mock_id, "reference": ref}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    print(f"Mock supplier sur http://127.0.0.1:{port}/orders  (Ctrl+C pour arreter)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
