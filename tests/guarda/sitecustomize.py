"""
Carregado sozinho pelo Python em todo processo filho que um teste abre: o
conftest põe esta pasta no PYTHONPATH. Instala a guarda de rede e recusa
rodar se AGENTE_DADOS faltar ou apontar para dentro do repositório, que pode
guardar os arquivos reais do dono (incidente de 30/09/2026; achado S-82).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guarda_rede  # noqa: E402

guarda_rede.instalar()

_raiz = Path(__file__).resolve().parent.parent.parent
_dados = os.environ.get("AGENTE_DADOS", "").strip()
if not _dados:
    sys.exit("guarda dos testes: subprocesso sem AGENTE_DADOS; a pasta de dados seria a do repositório")
_pasta = Path(_dados).resolve()
if _pasta == _raiz or _pasta.is_relative_to(_raiz):
    sys.exit(f"guarda dos testes: AGENTE_DADOS aponta para dentro do repositório ({_pasta})")
