"""
Fills the website template (deploy/site) with your business details and
writes deploy/site-pronto/, which Caddy serves. site-pronto/ is ignored by
git: your details stay out of the public repository.

    python preencher_site.py            # asks for each value
    MARCA=... EMPRESA=... python preencher_site.py   # or from the environment
"""
import os
import shutil
from datetime import date
from pathlib import Path

AQUI = Path(__file__).resolve().parent
CAMPOS = {
    "MARCA": "Product name shown on the site",
    "EMPRESA": "Legal business name",
    "ENDERECO": "Business address (city, country at least)",
    "EMAIL": "Contact e-mail",
    "PAIS": "Country whose law governs the terms",
}


def valores() -> dict:
    lidos = {}
    for campo, pergunta in CAMPOS.items():
        valor = os.getenv(campo, "").strip()
        while not valor:
            valor = input(f"{pergunta}: ").strip()
        lidos[campo] = valor
    lidos["ANO"] = str(date.today().year)
    lidos["DATA"] = date.today().strftime("%B %d, %Y")
    return lidos


def preencher(dados: dict, origem: Path = AQUI / "site", destino: Path = AQUI / "site-pronto") -> Path:
    if destino.exists():
        shutil.rmtree(destino)
    shutil.copytree(origem, destino)
    for arquivo in destino.glob("*.html"):
        texto = arquivo.read_text(encoding="utf-8")
        for campo, valor in dados.items():
            texto = texto.replace("{{" + campo + "}}", valor)
        if "{{" in texto:
            raise SystemExit(f"Campo sem valor em {arquivo.name}")
        arquivo.write_text(texto, encoding="utf-8")
    return destino


if __name__ == "__main__":
    print(f"Site pronto em {preencher(valores())}")
