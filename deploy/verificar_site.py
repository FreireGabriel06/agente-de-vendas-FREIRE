"""
Checks, from any computer, that the public website and a client panel are
ready for the Amazon developer registration: HTTPS works, HTTP redirects to
HTTPS, the privacy policy and terms answer and have no unfilled field, and the
panel's /saude answers.

    python verificar_site.py example.com             # site + cliente1
    python verificar_site.py example.com cliente2    # site + cliente2

Exit code 0 when everything passes, 1 otherwise.
"""
import sys
import urllib.error
import urllib.request

PAGINAS = {"": "Home", "privacy.html": "Privacy Policy", "terms.html": "Terms of Service"}


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def buscar(url: str, seguir: bool = True) -> tuple[int, dict, str]:
    """(status, cabeçalhos, corpo). Erros HTTP viram status; falha de rede
    sobe como OSError."""
    abridor = urllib.request.build_opener() if seguir else urllib.request.build_opener(_SemRedirecionar)
    pedido = urllib.request.Request(url, headers={"User-Agent": "verificar-site/1"})
    try:
        with abridor.open(pedido, timeout=15) as r:
            return r.status, dict(r.headers), r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), ""


def verificar(dominio: str, cliente: str = "cliente1", buscar=buscar) -> list[tuple[bool, str]]:
    resultados = []

    def checar(ok: bool, texto: str):
        resultados.append((ok, texto))

    for caminho, titulo in PAGINAS.items():
        url = f"https://{dominio}/{caminho}"
        try:
            status, _, corpo = buscar(url)
        except OSError as e:
            checar(False, f"{url}: sem resposta ({type(e).__name__})")
            continue
        checar(status == 200, f"{url}: HTTP {status}")
        if status == 200:
            checar(titulo in corpo, f"{url}: contém '{titulo}'")
            checar("{{" not in corpo, f"{url}: sem campo por preencher")

    try:
        status, cabecalhos, _ = buscar(f"http://{dominio}/", seguir=False)
        destino = {k.lower(): v for k, v in cabecalhos.items()}.get("location", "")
        checar(status in (301, 302, 307, 308) and destino.startswith("https://"),
               f"http://{dominio}/ redireciona para HTTPS")
    except OSError as e:
        checar(False, f"http://{dominio}/: sem resposta ({type(e).__name__})")

    url = f"https://{cliente}.{dominio}/saude"
    try:
        status, _, _ = buscar(url)
        # 503 = o painel respondeu, mas a saúde pede atenção; ainda serve para o cadastro.
        checar(status in (200, 503), f"{url}: painel respondeu (HTTP {status})")
    except OSError as e:
        checar(False, f"{url}: sem resposta ({type(e).__name__})")
    return resultados


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 1
    dominio = argv[0].strip().removeprefix("https://").removeprefix("http://").strip("/")
    resultados = verificar(dominio, argv[1] if len(argv) > 1 else "cliente1")
    for ok, texto in resultados:
        print(f"  [{'ok' if ok else 'FALHA'}] {texto}")
    falhas = sum(not ok for ok, _ in resultados)
    print("Tudo pronto para o cadastro." if not falhas else f"{falhas} verificação(ões) falharam.")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
