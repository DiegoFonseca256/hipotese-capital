import json
import os
import logging
import re
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

# Carrega o .env ANTES de importar o LLM, que lê GROQ_API_KEY no import
load_dotenv()

from LLM import analisar_lote
from database import get_conn, init_db, ler_tickers_do_txt

# Configuração básica do logging para exibir no terminal
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# --- DATABASE ---

def listar_tickers():
    """Recupera lista de tickers já cadastrados."""
    with get_conn() as conn:
        cursor = conn.execute("SELECT ticker FROM empresas")
        return [row[0] for row in cursor.fetchall()]

def salvar_empresas_no_db(lista_tickers):
    """Atualiza dados cadastrais fixos via yfinance."""
    with get_conn() as conn:
        for ticker in lista_tickers:
            try:
                info = yf.Ticker(ticker + ".SA").info
                conn.execute('''
                    INSERT INTO empresas (ticker, nome, setor, segAtuacao, descricao)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(ticker) DO UPDATE SET
                        nome=excluded.nome, setor=excluded.setor,
                        segAtuacao=excluded.segAtuacao, descricao=excluded.descricao
                ''', (ticker, info.get("longName"), info.get("sectorDisp"),
                      info.get("industryDisp"), info.get("longBusinessSummary")))
                logger.info(f"{ticker}: Dados cadastrais atualizados.")
            except Exception as e:
                logger.error(f"Erro ao atualizar cadastro de {ticker}: {e}")

def cria_df_dados_cadastro():
    """Retorna DataFrame com dados da tabela empresas."""
    try:
        with get_conn() as conn:
            return pd.read_sql("SELECT * FROM empresas", conn)
    except Exception as e:
        logger.error(f"Erro ao ler banco de dados: {e}")
        return pd.DataFrame()

COLUNAS_SNAPSHOT = [
    "preco_atual", "variacao_dia", "debtToEquity", "freeCashflow", "ebitdaMargins",
    "LiquiCorrente", "VolMedDiario", "MargemOperacional", "min_52", "max_52",
    "pl", "roe", "dy", "market_cap", "beta",
]

def salvar_snapshot_no_db(df):
    """Persiste os indicadores de mercado e as notícias coletadas.

    Retorna {ticker: id_do_snapshot} para que a análise da LLM seja
    gravada depois no mesmo registro.
    """
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ids = {}
    with get_conn() as conn:
        for _, row in df.iterrows():
            valores = [_num(row.get(c)) for c in COLUNAS_SNAPSHOT]
            cur = conn.execute(
                f"INSERT INTO snapshots (ticker, data_coleta, {', '.join(COLUNAS_SNAPSHOT)}) "
                f"VALUES (?, ?, {', '.join('?' * len(COLUNAS_SNAPSHOT))})",
                [row["ticker"], agora, *valores],
            )
            ids[row["ticker"]] = cur.lastrowid

            for a in row.get("noticias") or []:
                url = a.get("url")
                ja_existe = conn.execute(
                    "SELECT 1 FROM noticias_historico WHERE ticker = ? AND url = ?",
                    (row["ticker"], url),
                ).fetchone()
                if ja_existe:
                    continue
                conn.execute('''
                    INSERT INTO noticias_historico
                        (ticker, data_noticia, titulo, fonte, url, imagem, descricao)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (row["ticker"], (a.get("publishedAt") or "")[:10], a.get("title"),
                      (a.get("source") or {}).get("name"), url, a.get("urlToImage"),
                      a.get("description")))
    logger.info(f"Snapshot de {len(ids)} ativos salvo com sucesso.")
    return ids

def salvar_analises_no_db(df_relatorios, snapshot_ids):
    """Grava a análise da LLM no snapshot e o sentimento nas notícias."""
    with get_conn() as conn:
        for _, rel in df_relatorios.iterrows():
            sid = snapshot_ids.get(rel["ticker"])
            if sid is None or rel.get("erro"):
                continue
            conn.execute(
                "UPDATE snapshots SET resumo_llm = ?, analise_llm = ?, perguntas_json = ? WHERE id = ?",
                (rel["resumo_llm"], rel["analise_llm"], rel["perguntas_json"], sid),
            )
            for n in json.loads(rel.get("noticias_json") or "[]"):
                sentimento = _normaliza_sentimento(n.get("sentimento"))
                titulo = (n.get("titulo") or "").strip()
                if sentimento and titulo:
                    conn.execute(
                        "UPDATE noticias_historico SET sentimento = ? WHERE ticker = ? AND TRIM(titulo) = ?",
                        (sentimento, rel["ticker"], titulo),
                    )

def _normaliza_sentimento(s):
    s = (s or "").lower()
    for valor in ("irrelevante", "positiva", "negativa", "neutra"):
        if valor in s:
            return valor
    return None

def _num(val):
    """Converte para float nativo (sqlite não aceita numpy.int64) ou None."""
    try:
        v = float(val)
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return None

def _escala(val, fator):
    v = _num(val)
    return None if v is None else v * fator

# --- COLETA ---

def nome_para_busca(nome_completo):
    """Reduz a razão social a um nome usado em manchetes.

    "Petróleo Brasileiro S.A. - Petrobras" -> "Petrobras"
    "Engie Brasil Energia S.A."           -> "Engie Brasil"
    """
    if not nome_completo:
        return ""
    nome = nome_completo.split(" - ")[-1]
    nome = re.sub(r"\b(S\.?A\.?|S/A|Holding|Participações|Energia)(?=\s|$)", "", nome)
    return " ".join(nome.split())

def busca_noticias(ticker, nome_empresa=""):
    """Busca notícias via NewsAPI pelo ticker ou pelo nome da empresa no título."""
    if not NEWS_API_KEY:
        logger.warning(f"Chave de notícias ausente para {ticker}.")
        return []
    nome = nome_para_busca(nome_empresa)
    params = {
        "q": f'{ticker} OR "{nome}"' if nome else ticker,
        "searchIn": "title",
        "language": "pt",
        "sortBy": "publishedAt",
        "pageSize": 20,
        "apiKey": NEWS_API_KEY,
    }
    try:
        res = requests.get("https://newsapi.org/v2/everything", params=params, timeout=10).json()
    except Exception as e:
        logger.error(f"Erro na NewsAPI para {ticker}: {e}")
        return []
    if res.get("status") != "ok":
        logger.error(f"NewsAPI recusou a busca de {ticker}: {res.get('message')}")
        return []

    # A NewsAPI ignora maiúsculas: "Vale" também casa com "vale a pena".
    # Mantém só manchetes que citam o ticker ou o nome com a grafia exata,
    # fora de palavras compostas ("Vale-transporte", "Vale-alimentação").
    padrao = re.compile(rf"(?<![\w-])({re.escape(ticker)}{'|' + re.escape(nome) if nome else ''})(?![\w-])")
    artigos = [a for a in res.get("articles", []) if padrao.search(a.get("title") or "")]
    return artigos[:5]

def pega_dados_mercado(lista_tickers):
    """Coleta indicadores financeiros e preços atuais.

    Unidades gravadas: percentuais em % (ROE, DY, margens, variação),
    Dívida/Equity em x, valores monetários em R$.
    """
    novos_dados = []
    for ticker in lista_tickers:
        try:
            t = yf.Ticker(ticker + ".SA")
            info = t.info
            hist = t.history(period="1y")

            novos_dados.append({
                "ticker": ticker,
                "preco_atual": info.get("currentPrice"),
                "variacao_dia": info.get("regularMarketChangePercent"),   # já em %
                "pl": info.get("trailingPE"),
                "roe": _escala(info.get("returnOnEquity"), 100),          # fração -> %
                "dy": info.get("dividendYield"),                          # já em %
                "market_cap": info.get("marketCap"),
                "beta": info.get("beta"),
                "debtToEquity": _escala(info.get("debtToEquity"), 0.01),  # % -> x
                "freeCashflow": info.get("freeCashflow"),
                "ebitdaMargins": _escala(info.get("ebitdaMargins"), 100),
                "MargemOperacional": _escala(info.get("operatingMargins"), 100),
                "LiquiCorrente": info.get("currentRatio"),
                "VolMedDiario": info.get("averageDailyVolume10Day"),
                "min_52": hist['Low'].min() if not hist.empty else None,
                "max_52": hist['High'].max() if not hist.empty else None,
                "noticias": busca_noticias(ticker, info.get("longName"))
            })
            logger.info(f"{ticker}: Dados de mercado coletados.")
        except Exception as e:
            logger.error(f"Falha na coleta de {ticker}: {e}")
    return novos_dados

# --- PIPELINE ---

def tratamento_dados(df):
    """Normaliza colunas financeiras para numérico."""
    df = df.copy()
    for col in COLUNAS_SNAPSHOT:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def cria_df_final(lista_tickers):
    """Coordena o fluxo: Coleta -> Banco -> LLM -> Banco."""
    init_db()
    salvar_empresas_no_db(lista_tickers)

    df_mercado = pd.DataFrame(pega_dados_mercado(lista_tickers))
    if df_mercado.empty:
        logger.error("Nenhum dado de mercado coletado; abortando.")
        return pd.DataFrame()
    df_cadastro = cria_df_dados_cadastro()

    df_base = pd.merge(df_cadastro, df_mercado, on="ticker", how="inner")
    df_tratado = tratamento_dados(df_base)

    # A LLM lê o snapshot do banco, então ele precisa ser salvo antes
    snapshot_ids = salvar_snapshot_no_db(df_tratado)

    logger.info("Iniciando análise via LLM (Groq)...")
    df_relatorios = analisar_lote(list(snapshot_ids), pausa=1.0)
    salvar_analises_no_db(df_relatorios, snapshot_ids)

    df_final = pd.merge(df_tratado, df_relatorios, on="ticker", how="left")

    sucessos = ((df_final['analise_llm'].fillna("") != "") & (df_final['erro'].fillna("") == "")).sum()
    logger.info(f"Pipeline finalizado: {sucessos}/{len(lista_tickers)} análises geradas.")
    return df_final

if __name__ == "__main__":
    init_db()

    # Gerenciamento de fila
    t_base = listar_tickers()
    t_novos = ler_tickers_do_txt("pendentes.txt")
    LISTA_FINAL = list(set(t_base + t_novos))

    if LISTA_FINAL:
        logger.info(f"Iniciando processamento de {len(LISTA_FINAL)} ativos.")
        cria_df_final(LISTA_FINAL)

        if t_novos:
            open("pendentes.txt", "w").close()
            logger.info("Arquivo de pendentes limpo.")
    else:
        logger.warning("Nenhum ticker encontrado para processar.")
