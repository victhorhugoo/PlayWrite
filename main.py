import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path
 
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
 
from list_queries import QUERIES
 
BASE = Path(__file__).parent
LINK = "https://dbc-f0bcd00e-80e9.cloud.databricks.com/"
 
NAVEGADOR = "chromium"                     # "msedge" ou "chromium"
PERFIL = BASE / f"perfil_{NAVEGADOR}"      # perfil do navegador (guarda a sessão)
PASTA_SAIDA = BASE / "resultados"
PASTA_LOGS = BASE / "logs"
PASTA_DOWNLOADS = BASE / "downloads_tmp"   # downloads ficam no disco (recuperáveis)
 
MAX_TENTATIVAS = 3   # 1 = sem repetição (bom para diagnosticar); depois, 2

ULTIMO_CSV = {"corpo": None}
 
for pasta in (PASTA_SAIDA, PASTA_LOGS, PASTA_DOWNLOADS):
    pasta.mkdir(exist_ok=True)
PERFIL.mkdir(parents=True, exist_ok=True)
 
logging.basicConfig(
    filename=PASTA_LOGS / "log.txt",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)
 
 
class SessaoExpirada(Exception):
    """Login expirado: seguir com as outras queries não adianta."""
 
 
def nome_arquivo(texto):
    """Troca caracteres problemáticos por '_' para usar no nome do arquivo."""
    return re.sub(r"[^\w\-]+", "_", texto).strip("_")
 
 
def tirar_print(page, nome):
    """Salva um print da tela, sem quebrar se o navegador já fechou."""
    try:
        page.screenshot(path=PASTA_LOGS / f"{nome}.png")
    except Exception:
        logging.warning("Print não tirado: navegador já fechado")
 
 
def registrar_eventos(contexto, page):
    """Diagnóstico: grava no log o que acontece com o navegador."""
 
    def ao_abrir_aba(nova):
        logging.info("EVENTO: aba aberta")
        nova.on("close", lambda *_: logging.warning("EVENTO: aba extra fechou"))
 
    contexto.on("page", ao_abrir_aba)
    contexto.on("close", lambda *_: logging.warning("EVENTO: contexto fechou"))
    page.on("close", lambda *_: logging.warning("EVENTO: página principal fechou"))
    page.on("crash", lambda *_: logging.error("EVENTO: página travou (crash)"))
    page.on(
        "download",
        lambda d: logging.info(f"EVENTO: download iniciado: {d.suggested_filename}"),
    )

def registrar_rede(page):
    """Guarda o corpo da resposta que parece ser o CSV."""
 
    def ao_responder(r):
        tipo = r.headers.get("content-type", "")
        if "workspace-files" in r.url and "octet" in tipo:
            try:
                ULTIMO_CSV["corpo"] = r.body()
                logging.info(f"REDE: corpo capturado ({len(ULTIMO_CSV['corpo'])} bytes)")
            except Exception as e:
                logging.warning(f"REDE: não consegui ler o corpo: {type(e).__name__}")
 
    page.on("response", ao_responder)
 
def limpar_downloads_antigos(dias=1):
    """Apaga arquivos antigos de downloads_tmp para a pasta não crescer."""
    limite = time.time() - dias * 86400
    for f in PASTA_DOWNLOADS.iterdir():
        if f.is_file() and f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)
 
 
def csv_completo(caminho):
    """Confere se todas as linhas têm o mesmo número de colunas do cabeçalho."""
    with open(caminho, newline="", encoding="utf-8-sig") as f:
        linhas = list(csv.reader(f))
    return len(linhas) > 1 and all(len(l) == len(linhas[0]) for l in linhas)
 
 
def recuperar_download(destino, desde):
    """Procura em downloads_tmp o arquivo baixado após 'desde' e o copia."""
    candidatos = [
        f for f in PASTA_DOWNLOADS.iterdir()
        if f.is_file() and f.stat().st_mtime >= desde - 1
    ]
    if not candidatos:
        raise RuntimeError("Nenhum arquivo baixado encontrado em downloads_tmp")
 
    arquivo = max(candidatos, key=lambda f: f.stat().st_mtime)
    logging.info(f"Arquivo encontrado: {arquivo.name} ({arquivo.stat().st_size} bytes)")
 
    if not csv_completo(arquivo):
        raise RuntimeError("Arquivo baixado parece incompleto")
 
    destino.write_bytes(arquivo.read_bytes())
    arquivo.unlink(missing_ok=True)
    logging.warning(f"Arquivo recuperado de downloads_tmp para {destino}")
 
 
def link_queries(page):
    """Link 'Queries' do menu lateral (evita bater em links de abas abertas)."""
    return page.get_by_test_id("UnifiedSideNav").get_by_role("link", name="Queries")
 
 
def verificar_login(page):
    """Confirma que a sessão está ativa; senão, falha com mensagem clara."""
    try:
        link_queries(page).wait_for(timeout=15000)
    except Exception:
        tirar_print(page, "sessao_expirada")
        raise SessaoExpirada(
            "Sessão expirada: refaça o login com o codegen usando o mesmo perfil"
        )
 
 
def baixar_resultado(page, nome_query, destino):
    """Abre a query salva pelo nome, executa e salva o CSV em 'destino'."""
    link_queries(page).click()
    page.get_by_role(
        "link", name=re.compile(rf"^{re.escape(nome_query)}")
    ).click()
 
    # Executa a query
    botao_run = page.get_by_test_id("notebook-query-run-button-idle")
    botao_run.click()
 
    # Se o warehouse estiver parado, confirma para ligar (senão, segue)
    try:
        page.get_by_role("button", name="Start, attach and run").click(timeout=10000)
    except PlaywrightTimeoutError:
        pass
 
    # Espera a execução terminar (o botão volta ao estado "idle")
    page.wait_for_timeout(2000)
    botao_run.wait_for(state="visible", timeout=180000)
 
    # Setinha da aba Table -> Download CSV -> All rows
    page.get_by_test_id(re.compile(r"^MoreViz")).click(timeout=60000)
    page.get_by_test_id(re.compile(r"^CommandResultTabDownloadCSV")).click()
 
    inicio_download = time.time()
    with page.expect_download(timeout=120000) as download_info:
        page.get_by_test_id(
            re.compile(r"^CommandResultTabDownloadPreviewCSV")
        ).click()
 
    # Salva na hora; se o navegador caiu, tenta recuperar o arquivo do disco
    try:
        download_info.value.save_as(destino)
    except Exception:
        logging.warning("save_as falhou; tentando o corpo capturado")
        if ULTIMO_CSV["corpo"]:
            destino.write_bytes(ULTIMO_CSV["corpo"])
            logging.warning(f"Arquivo gravado a partir da resposta de rede: {destino}")
        else:
            recuperar_download(destino, inicio_download)
 
 
def abrir_sessao(p):
    """Abre o navegador, vai ao workspace e confere o login."""
    opcoes = {"channel": "msedge"} if NAVEGADOR == "msedge" else {}
    contexto = p.chromium.launch_persistent_context(
        user_data_dir=PERFIL,
        headless=False,
        accept_downloads=True,
        downloads_path=PASTA_DOWNLOADS,
        no_viewport=True,
        args=["--start-maximized"],
        **opcoes,
    )
    contexto.set_default_timeout(60000)
    contexto.set_default_navigation_timeout(60000)
 
    page = contexto.new_page()
    registrar_eventos(contexto, page)
    registrar_rede(page)
    page.goto(LINK)
    verificar_login(page)
    return contexto, page
 
 
def fechar_sessao(contexto):
    """Fecha o navegador, sem quebrar se ele já tiver fechado."""
    if contexto is None:
        return
    logging.info("NOsso codigo: Fechando navegador agora")
    try:
        contexto.close()
    except Exception:
        pass
    time.sleep(3)   # dá tempo de o perfil ser liberado
 
 
def main():
    logging.info(f"Início da execução (navegador: {NAVEGADOR})")
    limpar_downloads_antigos()
 
    data = datetime.now().strftime("%Y-%m-%d_%H-%M")
    concluidas = []
    falhas = []
 
    with sync_playwright() as p:
        contexto = None
        page = None
        try:
            for nome in QUERIES:
                destino = PASTA_SAIDA / f"{nome_arquivo(nome)}_{data}.csv"
 
                for tentativa in range(1, MAX_TENTATIVAS + 1):
                    msg = f"Query '{nome}' (tentativa {tentativa}/{MAX_TENTATIVAS})"
                    logging.info(msg)
                    print(msg)
 
                    try:
                        if page is None or page.is_closed():
                            # primeira query, ou o navegador caiu: abre de novo
                            fechar_sessao(contexto)
                            contexto, page = abrir_sessao(p)
                        else:
                            page.goto(LINK)   # volta ao ponto de partida
 
                        baixar_resultado(page, nome, destino)
 
                        concluidas.append(nome)
                        logging.info(f"Arquivo salvo em {destino}")
                        print(f"Arquivo salvo em {destino}")
                        try: 
                            page.wait_for_timeout(2000)  # espera um pouco antes de ir para a próxima query
                        except Exception:
                            pass
                        break
 
                    except SessaoExpirada:
                        logging.exception("Sessão expirada: execução interrompida")
                        raise
 
                    except Exception as e:
                        logging.exception(f"Falha na query: {nome}")
                        if page is not None:
                            tirar_print(page, f"erro_{nome_arquivo(nome)}")
                        primeira_linha = (str(e).splitlines() or [""])[0]
                        print(f"FALHOU: {nome} -> {type(e).__name__}: {primeira_linha}")
                else:
                    # só executa se NENHUMA tentativa deu certo (sem 'break')
                    falhas.append(nome)
                    logging.error(
                        f"Desistindo de '{nome}' após {MAX_TENTATIVAS} tentativa(s)"
                    )
        finally:
            fechar_sessao(contexto)   # só aqui, quando acabou a lista
 
    logging.info(f"Concluídas: {concluidas} | Falhas: {falhas}")
 
    if falhas:
        raise RuntimeError(f"Falharam {len(falhas)} query(s): {falhas}")
 
    logging.info("Execução concluída")
 
 
if __name__ == "__main__":
    main()

'''
import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from list_queries import QUERIES

for query_name in QUERIES:
    print(query_name)

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
LINK = "https://dbc-f0bcd00e-80e9.cloud.databricks.com/"
PERFIL = BASE / "perfil_navegador"        # perfil do navegador (guarda a sessão)
PASTA_SAIDA = BASE / "resultados"
PASTA_LOGS = BASE / "logs"
PASTA_DOWNLOADS = BASE / "downloads_tmp"  # downloads ficam no disco (recuperáveis)

MAX_TENTATIVAS = 1   # 1 = sem repetição (bom para diagnosticar); depois, 2

for pasta in (PASTA_SAIDA, PASTA_LOGS, PASTA_DOWNLOADS):
    pasta.mkdir(exist_ok=True)
PERFIL.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=PASTA_LOGS / "log.txt",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)


class SessaoExpirada(Exception):
    """Login expirado: seguir com as outras queries não adianta."""


def nome_arquivo(texto):
    """Troca caracteres problemáticos por '_' para usar no nome do arquivo."""
    return re.sub(r"[^\w\-]+", "_", texto).strip("_")


def tirar_print(page, nome):
    """Salva um print da tela, sem quebrar se o navegador já fechou."""
    try:
        page.screenshot(path=PASTA_LOGS / f"{nome}.png")
    except Exception:
        logging.warning("Print não tirado: navegador já fechado")


def registrar_eventos(contexto, page):
    """Diagnóstico: grava no log o que acontece com o navegador."""

    def ao_abrir_aba(nova):
        logging.info("EVENTO: aba aberta")
        nova.on("close", lambda *_: logging.warning("EVENTO: aba extra fechou"))

    contexto.on("page", ao_abrir_aba)
    contexto.on("close", lambda *_: logging.warning("EVENTO: contexto fechou"))
    page.on("close", lambda *_: logging.warning("EVENTO: página principal fechou"))
    page.on("crash", lambda *_: logging.error("EVENTO: página travou (crash)"))
    page.on(
        "download",
        lambda d: logging.info(f"EVENTO: download iniciado: {d.suggested_filename}"),
    )


def limpar_downloads_antigos(dias=1):
    """Apaga arquivos antigos de downloads_tmp para a pasta não crescer."""
    limite = time.time() - dias * 86400
    for f in PASTA_DOWNLOADS.iterdir():
        if f.is_file() and f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)


def csv_completo(caminho):
    """Confere se todas as linhas têm o mesmo número de colunas do cabeçalho."""
    with open(caminho, newline="", encoding="utf-8-sig") as f:
        linhas = list(csv.reader(f))
    return len(linhas) > 1 and all(len(l) == len(linhas[0]) for l in linhas)


def recuperar_download(destino, desde):
    """Procura em downloads_tmp o arquivo baixado após 'desde' e o copia."""
    candidatos = [
        f for f in PASTA_DOWNLOADS.iterdir()
        if f.is_file() and f.stat().st_mtime >= desde - 1
    ]
    if not candidatos:
        raise RuntimeError("Nenhum arquivo baixado encontrado em downloads_tmp")

    arquivo = max(candidatos, key=lambda f: f.stat().st_mtime)
    logging.info(f"Arquivo encontrado: {arquivo.name} ({arquivo.stat().st_size} bytes)")

    if not csv_completo(arquivo):
        raise RuntimeError("Arquivo baixado parece incompleto")

    destino.write_bytes(arquivo.read_bytes())
    arquivo.unlink(missing_ok=True)
    logging.warning(f"Arquivo recuperado de downloads_tmp para {destino}")


def link_queries(page):
    """Link 'Queries' do menu lateral (evita bater em links de abas abertas)."""
    return page.get_by_test_id("UnifiedSideNav").get_by_role("link", name="Queries")


def verificar_login(page):
    """Confirma que a sessão está ativa; senão, falha com mensagem clara."""
    try:
        link_queries(page).wait_for(timeout=15000)
    except Exception:
        tirar_print(page, "sessao_expirada")
        raise SessaoExpirada(
            "Sessão expirada: refaça o login com o codegen usando o mesmo perfil"
        )


def baixar_resultado(page, nome_query, destino):
    """Abre a query salva pelo nome, executa e salva o CSV em 'destino'."""
    link_queries(page).click()
    page.get_by_role(
        "link", name=re.compile(rf"^{re.escape(nome_query)}")
    ).click()

    # Executa a query
    botao_run = page.get_by_test_id("notebook-query-run-button-idle")
    botao_run.click()

    # Se o warehouse estiver parado, confirma para ligar (senão, segue)
    try:
        page.get_by_role("button", name="Start, attach and run").click(timeout=10000)
    except PlaywrightTimeoutError:
        pass

    # Espera a execução terminar (o botão volta ao estado "idle")
    page.wait_for_timeout(2000)
    botao_run.wait_for(state="visible", timeout=180000)

    # Setinha da aba Table -> Download CSV -> All rows
    page.get_by_test_id(re.compile(r"^MoreViz")).click(timeout=60000)
    page.get_by_test_id(re.compile(r"^CommandResultTabDownloadCSV")).click()

    inicio_download = time.time()
    with page.expect_download(timeout=120000) as download_info:
        page.get_by_test_id(
            re.compile(r"^CommandResultTabDownloadPreviewCSV")
        ).click()

    # Salva na hora; se o navegador caiu, tenta recuperar o arquivo do disco
    try:
        download_info.value.save_as(destino)
    except Exception:
        logging.warning("save_as falhou; tentando recuperar o arquivo do disco")
        recuperar_download(destino, inicio_download)


def abrir_sessao(p):
    """Abre o navegador, vai ao workspace e confere o login."""
    contexto = p.chromium.launch_persistent_context(
        user_data_dir=PERFIL,
        channel="msedge",
        headless=False,
        accept_downloads=True,
        downloads_path=PASTA_DOWNLOADS,
        no_viewport=True,
        args=["--start-maximized"],
    )
    contexto.set_default_timeout(60000)
    contexto.set_default_navigation_timeout(60000)
 
    page = contexto.new_page()
    registrar_eventos(contexto, page)
    page.goto(LINK)
    verificar_login(page)
    return contexto, page
 
 
def fechar_sessao(contexto):
    """Fecha o navegador, sem quebrar se ele já tiver fechado."""
    if contexto is None:
        return
    try:
        contexto.close()
    except Exception:
        pass
    time.sleep(3)   # dá tempo de o perfil ser liberado
 
 
def main():
    logging.info("Início da execução")
    limpar_downloads_antigos()
 
    data = datetime.now().strftime("%Y-%m-%d_%H-%M")
    concluidas = []
    falhas = []
 
    with sync_playwright() as p:
        contexto = None
        page = None
        try:
            for nome in QUERIES:
                destino = PASTA_SAIDA / f"{nome_arquivo(nome)}_{data}.csv"
 
                for tentativa in range(1, MAX_TENTATIVAS + 1):
                    msg = f"Query '{nome}' (tentativa {tentativa}/{MAX_TENTATIVAS})"
                    logging.info(msg)
                    print(msg)
 
                    try:
                        if page is None or page.is_closed():
                            # primeira query, ou o navegador caiu: abre de novo
                            fechar_sessao(contexto)
                            contexto, page = abrir_sessao(p)
                        else:
                            page.goto(LINK)   # volta ao ponto de partida
 
                        baixar_resultado(page, nome, destino)
 
                        concluidas.append(nome)
                        logging.info(f"Arquivo salvo em {destino}")
                        print(f"Arquivo salvo em {destino}")
                        break
 
                    except SessaoExpirada:
                        logging.exception("Sessão expirada: execução interrompida")
                        raise
 
                    except Exception as e:
                        logging.exception(f"Falha na query: {nome}")
                        if page is not None:
                            tirar_print(page, f"erro_{nome_arquivo(nome)}")
                        primeira_linha = (str(e).splitlines() or [""])[0]
                        print(f"FALHOU: {nome} -> {type(e).__name__}: {primeira_linha}")
                else:
                    falhas.append(nome)
                    logging.error(f"Desistindo de '{nome}' após {MAX_TENTATIVAS} tentativa(s)")
        finally:
            fechar_sessao(contexto)   # só aqui, quando acabou a lista
 
    logging.info(f"Concluídas: {concluidas} | Falhas: {falhas}")
 
    if falhas:
        raise RuntimeError(f"Falharam {len(falhas)} query(s): {falhas}")
 
    logging.info("Execução concluída") 

if __name__ == "__main__":
    main()
    '''