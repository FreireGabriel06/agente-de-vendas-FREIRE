#!/usr/bin/env python3
"""
Conserta uma pasta plana.

Se você baixou os arquivos um a um, eles caíram todos soltos no mesmo lugar e
o Python não acha os módulos — o erro é `ModuleNotFoundError: No module named
'core'`.

Rode este arquivo dentro da pasta bagunçada:

    python organizar.py

Ele move cada arquivo pra subpasta certa, cria os __init__.py e confere se
ficou tudo no lugar. Rodar duas vezes não faz mal.
"""
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

DESTINOS = {
    "core":         ["estados.py", "aprovacao.py", "conformidade.py", "privacidade.py"],
    "conectores":   ["mercadolivre.py", "shopee.py", "amazon.py"],
    "inteligencia": ["precificacao.py", "tendencias.py"],
    "atendimento":  ["persona.py", "bot.py"],
    "painel":       ["app.py", "configurar.py", "painel.html"],
}

# Esses ficam na raiz do projeto.
RAIZ = {
    "executar.py", "worker.py", "cli.py", "db.py", "config.py", "demo.py",
    "organizar.py", "requirements.txt", "agente.spec", "README.md",
    "iniciar.bat", "iniciar.sh", ".env.example", ".gitignore",
}

VERDE, AMARELO, VERMELHO, FIM = "\033[0;32m", "\033[0;33m", "\033[0;31m", "\033[0m"
if sys.platform == "win32":
    import os
    os.system("")  # habilita cor no console do Windows


def main():
    print("\n  Organizando a pasta do agente de vendas")
    print("  " + "─" * 38 + "\n")

    movidos, ja_ok, faltando = 0, 0, []

    for pasta, arquivos in DESTINOS.items():
        destino = BASE / pasta
        destino.mkdir(exist_ok=True)

        # Todo pacote Python precisa de __init__.py, mesmo vazio.
        init = destino / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")

        for nome in arquivos:
            solto = BASE / nome
            no_lugar = destino / nome

            if no_lugar.exists():
                if solto.exists() and solto != no_lugar:
                    # Existe nos dois lugares: o da subpasta é o bom, apaga o solto.
                    solto.unlink()
                    print(f"  {AMARELO}~{FIM} {nome} estava duplicado — removi a cópia solta")
                ja_ok += 1
                continue

            if solto.exists():
                shutil.move(str(solto), str(no_lugar))
                print(f"  {VERDE}→{FIM} {nome}  movido para {pasta}/")
                movidos += 1
            else:
                faltando.append(f"{pasta}/{nome}")

    # Confere os arquivos de raiz.
    faltando_raiz = [n for n in ("executar.py", "worker.py", "db.py", "config.py")
                     if not (BASE / n).exists()]

    print()
    if movidos:
        print(f"  {VERDE}{movidos} arquivo(s) reposicionado(s).{FIM}")
    if ja_ok and not movidos:
        print(f"  {VERDE}Já estava tudo no lugar.{FIM}")

    if faltando or faltando_raiz:
        print(f"\n  {VERMELHO}Faltam arquivos:{FIM}")
        for f in faltando + faltando_raiz:
            print(f"    {f}")
        print("\n  Baixe o .zip completo em vez dos arquivos avulsos.")
        return 1

    # Teste real: tenta importar tudo.
    print("\n  Testando os imports...")
    sys.path.insert(0, str(BASE))
    try:
        import config, db                                    # noqa: F401
        from core import estados, aprovacao, conformidade, privacidade   # noqa: F401
        from conectores import mercadolivre, shopee, amazon  # noqa: F401
        from inteligencia import precificacao, tendencias    # noqa: F401
        from atendimento import persona, bot                 # noqa: F401
        print(f"  {VERDE}Tudo importa corretamente.{FIM}")
    except ImportError as e:
        falta = str(e).split("'")[1] if "'" in str(e) else str(e)
        if falta in ("requests", "fastapi", "uvicorn", "cryptography", "pandas", "pytrends"):
            print(f"  {AMARELO}Estrutura ok. Falta instalar dependências: {falta}{FIM}")
            print("  Rode o iniciar.bat — ele instala tudo.")
            return 0
        print(f"  {VERMELHO}Ainda falta algo: {e}{FIM}")
        return 1

    print(f"\n  Pronto. Agora rode o {VERDE}iniciar.bat{FIM} (ou ./iniciar.sh).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
