"""
dashboard.py
Interface Streamlit — Hipótese Capital (versão completa)

Execução:
    1. python main.py
    2. streamlit run dashboard.py

Requisitos:
    pip install streamlit plotly pandas yfinance python-dotenv
"""
import html as html_lib
import json
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
from dotenv import load_dotenv

from LLM import _fmt, _fmt_grande, _variacao_fmt
from database import get_conn, init_db, adicionar_ticker_ao_txt

load_dotenv()

st.set_page_config(page_title="Hipótese Capital", page_icon="📊", layout="wide")

# ---------------------------------------------------------------------------
# CARREGAMENTO DE DADOS VIA SQLITE
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="Buscando dados no banco...")
def carregar_dados():

    query = """
    SELECT e.nome, e.setor, e.segAtuacao, e.descricao, s.*
    FROM empresas e
    JOIN snapshots s ON e.ticker = s.ticker
    WHERE s.id IN (SELECT MAX(id) FROM snapshots GROUP BY ticker)
    """
    try:
        with get_conn() as conn:
            df_db = pd.read_sql(query, conn)
        return df_db
    except Exception as e:
        st.error(f"Erro ao acessar o banco de dados: {e}")
        return pd.DataFrame()

# Inicializa o DB caso não exista e carrega
init_db()
df = carregar_dados()

if df.empty:
    st.warning("⚠️ Banco de dados vazio ou não encontrado. Execute `python main.py` primeiro.")
    st.stop()


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def coletar_historico(ticker: str, periodo: str = "6mo") -> pd.DataFrame:
    """Histórico de preços para o gráfico — único dado buscado ao vivo."""
    try:
        hist = yf.Ticker(ticker + ".SA").history(period=periodo)
        return hist[["Close", "Volume"]].reset_index()
    except Exception:
        return pd.DataFrame()


CORES_SENTIMENTO  = {"positiva": "#22c55e", "negativa": "#ef4444", "neutra": "#94a3b8"}
LABELS_SENTIMENTO = {"positiva": "Positiva", "negativa": "Negativa", "neutra": "Neutra"}


def carregar_noticias(ticker: str, limite: int = 12) -> list:
    with get_conn() as conn:
        query = "SELECT * FROM noticias_historico WHERE ticker = ? ORDER BY id DESC LIMIT ?"
        return conn.execute(query, (ticker, limite)).fetchall()


def renderizar_cards_noticias(noticias: list):

    if not noticias:
        st.caption("Nenhuma notícia nesta categoria.")
        return

    cores = CORES_SENTIMENTO
    labels = LABELS_SENTIMENTO
    placeholder = "https://placehold.co/400x200/1e293b/94a3b8?text=Sem+imagem"

    cards_html = ""
    def _safe(texto):
        return html_lib.escape(texto or "", quote=True)

    for n in noticias:

        cor = cores.get(n['sentimento'], "#94a3b8")
        label  = labels.get(n['sentimento'], "Não classificada")
        titulo_safe = _safe(n['titulo'])
        descr_safe  = _safe(n['descricao'])

        cards_html += f"""
        <a href="{_safe(n["url"])}" target="_blank" style="text-decoration:none;">
          <div class="card">
            <div class="card-img-wrap">
              <img src="{_safe(n["imagem"]) or placeholder}" onerror="this.src='{placeholder}'" alt="{titulo_safe}"/>
              <span class="badge" style="background:{cor}">{label}</span>
            </div>
            <div class="card-body">
              <p class="card-title">{titulo_safe}</p>
              <p class="card-descr">{descr_safe}</p>
              <p class="card-meta">{_safe(n['fonte'])} · {_safe(n['data_noticia'])}</p>
            </div>
          </div>
        </a>"""

    # Estilos CSS simplificados para o modo DB
    html = f"""
    <style>
      .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }}
      .card {{ background: #1e293b; border-radius: 8px; overflow: hidden; font-family: sans-serif; height: 100%; }}
      .card-img-wrap {{ position: relative; }}
      .card-img-wrap img {{ width: 100%; height: 130px; object-fit: cover; display: block; }}
      .badge {{ position: absolute; top: 8px; left: 8px; color: #fff; font-size: 11px; font-weight: 600;
                padding: 2px 8px; border-radius: 999px; }}
      .card-body {{ padding: 10px 14px 14px; }}
      .card-title {{ font-size: 14px; color: #f1f5f9; font-weight: 600; margin: 0 0 6px; }}
      .card-descr {{ font-size: 12px; color: #cbd5e1; margin: 0 0 8px;
                     display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
      .card-meta {{ font-size: 10px; font-weight: bold; color: #94a3b8; margin: 0; }}
    </style>
    <div class="grid">{cards_html}</div>"""

    linhas = (len(noticias) + 2) // 3
    components.html(html, height=min(linhas, 2) * 330, scrolling=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("📊 Hipótese Capital")
    st.caption("Painel de Análise Fundamentalista")
    st.divider()

    ticker = st.selectbox("Ticker", df["ticker"].unique())
    st.divider()

    st.sidebar.subheader("📌 Agendar Nova Análise")
    novo_ticker_pendente = st.sidebar.text_input("Ticker para a fila (ex: VALE3):").upper().strip()

    if st.sidebar.button("Adicionar à Fila (.txt)"):
        if novo_ticker_pendente:
            from database import adicionar_ticker_ao_txt
            adicionar_ticker_ao_txt(novo_ticker_pendente)
            st.sidebar.success(f"{novo_ticker_pendente} adicionado ao pendentes.txt")
        else:
            st.sidebar.error("Digite um ticker válido.")

    st.divider()
    if st.button("🔄 Recarregar dados", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    groq_ok = bool(os.getenv("GROQ_API_KEY"))
    news_ok = bool(os.getenv("NEWS_API_KEY"))
    st.caption(f"LLM : {'✅ Groq ok' if groq_ok else '❌ GROQ_API_KEY ausente'}")
    st.caption(f"News: {'✅ NewsAPI ok' if news_ok else '⚠️  Sem NewsAPI'}")



# ---------------------------------------------------------------------------
# Filtra linha do ticker selecionado
# ---------------------------------------------------------------------------
linha = df[df["ticker"] == ticker].iloc[0]


# ---------------------------------------------------------------------------
# Cabeçalho
# ---------------------------------------------------------------------------
col_titulo, col_preco = st.columns([3, 1])

with col_titulo:
    st.title(f"{linha.get('nome', ticker)}")
    st.caption(f"**{ticker}** · {linha.get('setor', '')} · {linha.get('segAtuacao', '')}")

with col_preco:
    st.metric(
        label="Preço atual",
        value=_fmt(linha.get("preco_atual"), prefixo="R$ "),
        delta=_variacao_fmt(linha.get("variacao_dia")),
    )

st.divider()

# ---------------------------------------------------------------------------
# Indicadores principais
# ---------------------------------------------------------------------------
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Market Cap",    _fmt_grande(linha.get("market_cap")))
c2.metric("P/L",           _fmt(linha.get("pl"), sufixo="x"))
c3.metric("ROE",           _fmt(linha.get("roe"), sufixo="%"))
c4.metric("Div. Yield",    _fmt(linha.get("dy"), sufixo="%"))
c5.metric("Dívida/Equity", _fmt(linha.get("debtToEquity"), sufixo="x"))
c6.metric("Beta",          _fmt(linha.get("beta")))

with st.expander("📐 Todos os indicadores"):
    ca, cb = st.columns(2)
    with ca:
        st.metric("Margem EBITDA",      _fmt(linha.get("ebitdaMargins"), sufixo="%"))
        st.metric("Margem Operacional", _fmt(linha.get("MargemOperacional"), sufixo="%"))
        st.metric("Liquidez Corrente",  _fmt(linha.get("LiquiCorrente"), sufixo="x"))
    with cb:
        st.metric("Free Cash Flow",     _fmt_grande(linha.get("freeCashflow")))
        st.metric("Vol. Médio Diário",  _fmt(linha.get("VolMedDiario"), dec=0))
        st.metric("Máx. 52 semanas",    _fmt(linha.get("max_52"), prefixo="R$ "))
        st.metric("Mín. 52 semanas",    _fmt(linha.get("min_52"), prefixo="R$ "))

# Barra de progresso do range de 52 semanas
try:
    preco  = float(linha.get("preco_atual"))
    min_52 = float(linha.get("min_52"))
    max_52 = float(linha.get("max_52"))
    rng = max_52 - min_52
    if rng > 0:
        pct = (preco - min_52) / rng
        st.markdown(f"**Range 52 semanas** — preço atual em **{pct*100:.0f}%** do range (mín → máx)")
        st.progress(min(max(pct, 0.0), 1.0))
except (TypeError, ValueError):
    pass

st.divider()

# ---------------------------------------------------------------------------
# Gráfico histórico de preços
# ---------------------------------------------------------------------------
with st.expander("📈 Histórico de preços", expanded=True):
    periodo = st.radio(
        "Período", ["1mo", "3mo", "6mo", "1y", "2y"],
        index=2, horizontal=True, label_visibility="collapsed",
    )
    with st.spinner("Carregando histórico..."):
        hist = coletar_historico(ticker, periodo)

    if not hist.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=hist["Date"], y=hist["Close"],
            mode="lines", name="Preço",
            line=dict(color="#1f77b4", width=2),
            fill="tozeroy", fillcolor="rgba(31,119,180,0.08)",
        ))
        fig.update_layout(
            margin=dict(l=0, r=0, t=10, b=0), height=260,
            xaxis=dict(showgrid=False),
            yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.caption("Histórico não disponível.")

st.divider()

# ---------------------------------------------------------------------------
# Análise LLM
# ---------------------------------------------------------------------------
st.markdown("### 🤖 Análise da IA")

resumo  = linha.get("resumo_llm", "")
analise = linha.get("analise_llm", "")

if not resumo or pd.isna(resumo):
    st.warning("Análise não disponível. Execute `python main.py` com `GROQ_API_KEY` configurado.")
else:
    col_esq, col_dir = st.columns([3, 2])

    with col_esq:
        st.subheader("Resumo do negócio")
        st.info(resumo)
        st.subheader("Análise dos indicadores")
        st.write(analise)

    with col_dir:
        st.subheader("❓ Perguntas para investigar")
        try:
            for i, q in enumerate(json.loads(linha.get("perguntas_json") or "[]"), 1):
                st.warning(f"**{i}.** {q}")
        except (json.JSONDecodeError, TypeError):
            st.caption("Perguntas não disponíveis.")

st.divider()

# ---------------------------------------------------------------------------
# Notícias: cards com foto, separados em abas pelo sentimento dado pela IA
# ---------------------------------------------------------------------------
st.subheader("📰 Notícias classificadas pela IA")

noticias = carregar_noticias(ticker)
descartadas = sum(1 for n in noticias if n["sentimento"] == "irrelevante")
noticias = [n for n in noticias if n["sentimento"] != "irrelevante"]
if descartadas:
    st.caption(f"{descartadas} notícia(s) ocultada(s): a IA indicou que não tratam desta empresa.")
if not noticias:
    st.caption("Sem notícias registradas para este ticker.")
else:
    grupos = {"Todas": noticias}
    for chave, label in LABELS_SENTIMENTO.items():
        grupos[label] = [n for n in noticias if n["sentimento"] == chave]
    nao_classificadas = [n for n in noticias if n["sentimento"] not in LABELS_SENTIMENTO]
    if nao_classificadas:
        grupos["Não classificadas"] = nao_classificadas

    abas = st.tabs([f"{nome} ({len(itens)})" for nome, itens in grupos.items()])
    for aba, itens in zip(abas, grupos.values()):
        with aba:
            renderizar_cards_noticias(itens)