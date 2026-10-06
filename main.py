import logging
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
LINK = "https://dbc-f0bcd00e-80e9.cloud.databricks.com/"
PERFIL = BASE / "perfil_navegador"   # perfil do navegador (guarda a sessão)
PASTA_SAIDA = BASE / "resultados"
PASTA_LOGS = BASE / "logs"

MAX_TENTATIVAS = 3   # quantas vezes tentar cada query antes de desistir

# Queries a baixar: o INÍCIO do nome salvo no Databricks
QUERIES = [
    "teste_record_automacao",
    "teste_queries_3",
]

PASTA_SAIDA.mkdir(exist_ok=True)
PASTA_LOGS.mkdir(exist_ok=True)
PERFIL.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=PASTA_LOGS / "log.txt",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)


class SessaoExpirada(Exception):
    """Login expirado: repetir a tentativa não adianta."""


def nome_arquivo(texto):
    """Troca caracteres problemáticos por '_' para usar no nome do arquivo."""
    return re.sub(r"[^\w\-]+", "_", texto).strip("_")


def tirar_print(page, nome):
    """Salva um print da tela, sem quebrar se o navegador já fechou."""
    try:
        page.screenshot(path=PASTA_LOGS / f"{nome}.png")
    except Exception:
        logging.warning("Print não tirado: navegador já fechado")


def verificar_login(page):
    """Confirma que a sessão está ativa; senão, falha com mensagem clara."""
    try:
        page.get_by_role("link", name="Queries").wait_for(timeout=15000)
    except Exception:
        tirar_print(page, "sessao_expirada")
        raise SessaoExpirada(
            "Sessão expirada: refaça o login com o codegen usando o mesmo perfil"
        )


def baixar_resultado(page, nome_query, destino):
    """Abre a query salva pelo nome, executa e salva o CSV em 'destino'."""
    page.get_by_role("link", name="Queries").click()
    page.get_by_role(
        "link", name=re.compile(re.escape(nome_query))
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

    with page.expect_download(timeout=120000) as download_info:
        page.get_by_test_id(
            re.compile(r"^CommandResultTabDownloadPreviewCSV")
        ).click()

    # Salva na hora, com o navegador ainda aberto
    download_info.value.save_as(destino)


def baixar_uma_query(p, nome, destino):
    """Abre um navegador novo, baixa UMA query e fecha o navegador."""
    contexto = p.chromium.launch_persistent_context(
        user_data_dir=PERFIL,
        channel="msedge",
        headless=False,
        accept_downloads=True,
        no_viewport=True,
        args=["--start-maximized"],
    )
    page = None
    try:
        page = contexto.new_page()
        page.goto(LINK)
        verificar_login(page)
        baixar_resultado(page, nome, destino)
    except Exception:
        if page:
            tirar_print(page, f"erro_{nome_arquivo(nome)}")
        raise
    finally:
        try:
            contexto.close()
        except Exception:
            pass


def main():
    logging.info("Início da execução")

    data = datetime.now().strftime("%Y-%m-%d_%H-%M")
    pendentes = list(QUERIES)
    tentativas = {q: 0 for q in QUERIES}
    concluidas = []
    falhas = []

    with sync_playwright() as p:
        while pendentes:
            nome = pendentes.pop(0)
            tentativas[nome] += 1
            destino = PASTA_SAIDA / f"{nome_arquivo(nome)}_{data}.csv"

            logging.info(
                f"Query '{nome}' (tentativa {tentativas[nome]}/{MAX_TENTATIVAS})"
            )
            try:
                baixar_uma_query(p, nome, destino)
                concluidas.append(nome)
                logging.info(f"Arquivo salvo em {destino}")
                print(f"Arquivo salvo em {destino}")

            except SessaoExpirada:
                logging.exception("Sessão expirada: execução interrompida")
                raise

            except Exception:
                logging.exception(f"Falha na query: {nome}")
                if tentativas[nome] < MAX_TENTATIVAS:
                    pendentes.append(nome)   # volta para o fim da fila
                else:
                    falhas.append(nome)
                    logging.error(f"Desistindo de '{nome}' após {MAX_TENTATIVAS} tentativas")

            time.sleep(3)   # dá tempo de o perfil ser liberado antes da próxima

    logging.info(f"Concluídas: {concluidas} | Falhas: {falhas}")

    if falhas:
        raise RuntimeError(f"Falharam {len(falhas)} query(s): {falhas}")

    logging.info("Execução concluída")


if __name__ == "__main__":
    main()