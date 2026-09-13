@echo off
REM Instala na primeira vez, so executa nas seguintes.
REM Basta dar duplo clique neste arquivo.

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   Agente Comercial
echo   ----------------
echo.

REM ---------- Python ----------
where py >nul 2>&1
if %errorlevel%==0 (set PY=py) else (
  where python >nul 2>&1
  if !errorlevel!==0 (set PY=python) else (
    echo   [X] Python nao encontrado.
    echo.
    echo   Baixe em https://python.org/downloads
    echo   IMPORTANTE: marque "Add Python to PATH" durante a instalacao.
    echo.
    pause
    exit /b 1
  )
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo   [X] Python antigo demais. O projeto precisa da versao 3.10 ou maior.
  pause
  exit /b 1
)
echo   [ok] Python encontrado

REM ---------- ambiente virtual ----------
if not exist ".venv" (
  echo   Criando ambiente virtual ^(so nesta primeira vez^)...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo   [X] Falha ao criar o ambiente virtual.
    pause
    exit /b 1
  )
  echo   [ok] Ambiente criado
)
call .venv\Scripts\activate.bat

REM ---------- dependencias ----------
if not exist ".venv\instalado.txt" (
  echo   Instalando dependencias ^(leva de 1 a 3 minutos^)...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo   [X] Falha ao instalar dependencias.
    pause
    exit /b 1
  )
  echo instalado > .venv\instalado.txt
  echo   [ok] Dependencias instaladas
) else (
  echo   [ok] Dependencias ja instaladas
)

REM ---------- configuracao ----------
if not exist ".env" (
  copy .env.example .env >nul
  echo   [!] Criado .env a partir do modelo, ainda SEM credenciais.
  echo       O painel sobe e funciona, mas nao busca pedido nenhum
  echo       ate voce preencher as chaves do marketplace no arquivo .env
  echo.
)

REM ---------- estrutura ----------
if not exist "core\privacidade.py" (
  echo   [!] Os arquivos estao fora de lugar ^(pasta plana^).
  echo       Organizando...
  if exist "organizar.py" (
    python organizar.py
  ) else (
    call :organizar
  )
  if not exist "core\privacidade.py" (
    echo.
    echo   [X] Nao consegui organizar: faltam arquivos.
    echo       Baixe o agente-de-vendas.zip, extraia, e rode este
    echo       arquivo de dentro da pasta extraida.
    pause
    exit /b 1
  )
  echo   [ok] Arquivos reposicionados
  echo.
)

REM ---------- banco ----------
if not exist "agente.db" (
  python -c "import sys; sys.path.insert(0,'.'); from db import inicializar; from core.privacidade import inicializar_lgpd; inicializar(); inicializar_lgpd()"
  if errorlevel 1 (
    echo   [X] Falha ao criar o banco. Veja a mensagem acima.
    pause
    exit /b 1
  )
  echo   [ok] Banco criado e chave LGPD gerada
  echo   [!] Faca backup do arquivo .chave_lgpd
  echo       Sem ele os dados de comprador ficam ilegiveis.
  echo.
)

REM ---------- dados de exemplo (so na primeira vez) ----------
if not exist ".primeira_execucao" (
  echo feito > .primeira_execucao
  set /p RESP="  Carregar dados de exemplo pra ver o painel funcionando? [S/n] "
  if /i not "!RESP!"=="n" (
    python demo.py >nul 2>&1
    echo   [ok] Dados de exemplo carregados
  )
  echo.
)

echo   Abrindo o painel em http://127.0.0.1:8777
echo   Feche esta janela ou tecle Ctrl+C para encerrar.
echo.
python executar.py
pause

REM ============================================================
REM  Reorganiza uma pasta plana sem depender do organizar.py.
REM  So move arquivos com nome conhecido — nada mais e tocado.
REM ============================================================
:organizar
for %%D in (core conectores inteligencia atendimento painel) do (
  if not exist "%%D" mkdir "%%D"
  if not exist "%%D\__init__.py" type nul > "%%D\__init__.py"
)
for %%F in (estados.py aprovacao.py conformidade.py privacidade.py) do (
  if exist "%%F" move /y "%%F" "core\" >nul
)
for %%F in (mercadolivre.py shopee.py amazon.py) do (
  if exist "%%F" move /y "%%F" "conectores\" >nul
)
for %%F in (precificacao.py tendencias.py) do (
  if exist "%%F" move /y "%%F" "inteligencia\" >nul
)
for %%F in (persona.py bot.py) do (
  if exist "%%F" move /y "%%F" "atendimento\" >nul
)
for %%F in (app.py configurar.py painel.html) do (
  if exist "%%F" move /y "%%F" "painel\" >nul
)
goto :eof
