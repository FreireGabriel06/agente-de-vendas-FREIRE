# -*- mode: python ; coding: utf-8 -*-
# Gera o binário: pyinstaller agente.spec
# O painel.html vai embutido; o .env fica FORA, lido da pasta de execução.

a = Analysis(
    ['executar.py'],
    pathex=[],
    binaries=[],
    datas=[('painel/painel.html', 'painel')],
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
        'conectores.mercadolivre', 'conectores.amazon', 'conectores.shopee',
        'core.estados', 'core.aprovacao', 'core.conformidade', 'core.privacidade',
        'inteligencia.precificacao', 'inteligencia.tendencias',
        'atendimento.persona', 'atendimento.bot', 'worker',
    ],
    hookspath=[], runtime_hooks=[], excludes=['matplotlib', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
    name='agente-comercial',
    console=True, upx=True, strip=False,
    disable_windowed_traceback=False, argv_emulation=False,
)
