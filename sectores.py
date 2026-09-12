#!/usr/bin/env python3
"""Parte 1 del screener — SECTORES FUERTES Y DÉBILES (método Weinstein).

Descarga 11 ETF sectoriales y el índice de referencia, mide la fuerza relativa
de cada sector frente al mercado y publica un ranking en PDF.

Son 12 descargas semanales: es la única parte del sistema lo bastante ligera
como para correr en GitHub Actions sin que nadie la bloquee.

Uso:
    python sectores.py                 # ranking + PDF en output/
    python sectores.py --email         # además lo manda por correo
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
import yfinance as yf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

log = logging.getLogger("sectores")
RAIZ = Path(__file__).resolve().parent
SALIDA = RAIZ / "output"

FASES = {1: "1 · BASE", 2: "2 · AVANCE", 3: "3 · TECHO", 4: "4 · CAÍDA"}


# ---------------------------------------------------------------- indicadores
def sma(serie: pd.Series, ventana: int) -> pd.Series:
    return serie.rolling(ventana, min_periods=ventana).mean()


def pendiente_pct(serie: pd.Series, lookback: int) -> pd.Series:
    return (serie / serie.shift(lookback) - 1.0) * 100.0


def mansfield(close: pd.Series, indice: pd.Series, ma: int = 52) -> pd.Series:
    """Fuerza Relativa de Mansfield.

    RS = precio del sector / precio del índice. El indicador es cuánto se
    desvía ese ratio de su propia media de 52 semanas.

        > 0  el sector BATE al mercado   (requisito innegociable de Weinstein)
        < 0  el sector va PEOR que el mercado
    """
    ratio = close / indice.reindex(close.index).ffill()
    return (ratio / sma(ratio, ma) - 1.0) * 100.0


def clasificar_fase(feat: pd.DataFrame, umbral: float) -> pd.Series:
    """Las 4 fases de Weinstein, a partir del precio y de la MM30."""
    encima = feat["encima_ma"].fillna(False).to_numpy(dtype=bool)
    pend = feat["ma30_pend"].to_numpy(dtype=float)
    rs = feat["rs"].fillna(0.0).to_numpy(dtype=float)

    subiendo = pend > umbral
    bajando = pend < -umbral

    fase = np.select(
        [
            encima & subiendo,               # tendencia alcista confirmada
            ~encima & subiendo & (rs > 0),    # recorte dentro de la Fase 2
            encima & ~subiendo,               # sube pero la media se agota → techo
            ~encima & bajando,                # tendencia bajista
        ],
        [2, 2, 3, 4],
        default=1,                            # bajo la media y media plana → base
    )
    fase = np.where(np.isnan(pend) | pd.isna(feat["ma30"]).to_numpy(), np.nan, fase)
    return pd.Series(fase, index=feat.index, name="fase")


# ------------------------------------------------------------------- descarga
def descargar(tickers: list[str], cfg: dict) -> dict[str, pd.DataFrame]:
    """Velas semanales de Yahoo. Una sola petición para los 12 tickers."""
    log.info("Descargando %d tickers (%s, %s)…", len(tickers),
             cfg["data"]["period"], cfg["data"]["interval"])
    crudo = yf.download(
        tickers,
        period=cfg["data"]["period"],
        interval=cfg["data"]["interval"],
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=False,
    )
    if crudo is None or crudo.empty:
        raise RuntimeError("Yahoo no devolvió datos. ¿Sin conexión o límite de peticiones?")

    salida: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = crudo[t] if isinstance(crudo.columns, pd.MultiIndex) else crudo
        except KeyError:
            log.warning("Sin datos para %s.", t)
            continue
        df = df.rename(columns=str.lower).dropna(how="all")
        if {"close", "high", "low"} <= set(df.columns) and len(df) >= 60:
            salida[t] = df
        else:
            log.warning("Histórico insuficiente para %s (%d velas).", t, len(df))
    return salida


def calcular(df: pd.DataFrame, indice: pd.Series, cfg: dict) -> pd.DataFrame:
    w = cfg["weinstein"]
    out = df.copy()
    out["ma30"] = sma(out["close"], w["ma_period"])
    out["ma30_pend"] = pendiente_pct(out["ma30"], w["slope_lookback"])
    out["rs"] = mansfield(out["close"], indice, w["rs_ma_period"])
    out["rs_pend"] = out["rs"] - out["rs"].shift(4)
    out["encima_ma"] = out["close"] > out["ma30"]
    out["max_52s"] = out["high"].rolling(52, min_periods=17).max()
    out["dist_max_52s"] = (out["close"] / out["max_52s"] - 1.0) * 100.0
    out["fase"] = clasificar_fase(out, w["slope_threshold"])
    return out


# -------------------------------------------------------------------- ranking
def _puntos(serie: pd.Series, maximo: float) -> pd.Series:
    return serie.rank(pct=True, na_option="bottom") * maximo


def ranking_sectores(cfg: dict) -> tuple[pd.DataFrame, dict]:
    etfs: dict[str, str] = cfg["sectors"]["etfs"]
    benchmark = cfg["benchmark"]
    precios = descargar(sorted(set(etfs.values()) | {benchmark}), cfg)

    if benchmark not in precios:
        raise RuntimeError(f"Sin datos del benchmark {benchmark}: no puedo medir fuerza relativa.")
    cierre_indice = precios[benchmark]["close"]

    filas = []
    for sector, etf in etfs.items():
        df = precios.get(etf)
        if df is None:
            continue
        f = calcular(df, cierre_indice, cfg).iloc[-1]
        filas.append({
            "sector": sector,
            "etf": etf,
            "fase_n": f["fase"],
            "fase": FASES.get(int(f["fase"]), "?") if pd.notna(f["fase"]) else "?",
            "mansfield": round(float(f["rs"]), 2) if pd.notna(f["rs"]) else np.nan,
            "rs_pend": round(float(f["rs_pend"]), 2) if pd.notna(f["rs_pend"]) else np.nan,
            "ma30_pend": round(float(f["ma30_pend"]), 2) if pd.notna(f["ma30_pend"]) else np.nan,
            "ret_3m": _var(df["close"], 13),
            "ret_6m": _var(df["close"], 26),
            "dist_max_52s": round(float(f["dist_max_52s"]), 2) if pd.notna(f["dist_max_52s"]) else np.nan,
        })

    s = pd.DataFrame(filas)
    if s.empty:
        raise RuntimeError("No se pudo calcular la fuerza de ningún sector.")

    # Nota 0-100. La fuerza relativa pesa 40: es la frase de Weinstein en código.
    nota = pd.Series(0.0, index=s.index)
    nota += _puntos(s["mansfield"], 40)
    nota += _puntos(s["rs_pend"], 15)
    nota += _puntos(s["ret_3m"], 15)
    nota += _puntos(s["ret_6m"], 10)
    nota += _puntos(s["ma30_pend"], 10)
    nota += s["fase_n"].map({2: 10.0, 1: 4.0, 3: 2.0, 4: 0.0}).fillna(0.0)
    s["nota"] = nota.round(1)

    s = s.sort_values("nota", ascending=False).reset_index(drop=True)
    s.insert(0, "puesto", range(1, len(s) + 1))

    return s, regimen_mercado(precios[benchmark], cierre_indice, cfg, benchmark)


def _var(close: pd.Series, semanas: int) -> float:
    if len(close) <= semanas:
        return np.nan
    return round(float((close.iloc[-1] / close.iloc[-1 - semanas] - 1.0) * 100.0), 2)


def regimen_mercado(df: pd.DataFrame, cierre: pd.Series, cfg: dict, ticker: str) -> dict:
    """La fase del PROPIO mercado. Si está en Fase 4, no hay sector que salve nada."""
    f = calcular(df, cierre, cfg).iloc[-1]   # RS del índice contra sí mismo = 0
    fase = float(f["fase"]) if pd.notna(f["fase"]) else np.nan
    return {
        "ticker": ticker,
        "fase_n": fase,
        "fase": FASES.get(int(fase), "?") if pd.notna(fase) else "?",
        "cierre": round(float(f["close"]), 2),
        "ma30": round(float(f["ma30"]), 2) if pd.notna(f["ma30"]) else np.nan,
        "ma30_pend": round(float(f["ma30_pend"]), 2) if pd.notna(f["ma30_pend"]) else np.nan,
        "comprable": bool(pd.notna(fase) and fase in (1, 2, 3) and (f["encima_ma"] or fase == 2)),
    }


def sectores_fuertes(s: pd.DataFrame, cfg: dict) -> list[str]:
    """Los N mejores que además NO estén en Fase 4."""
    top_n = int(cfg["sectors"]["top_n"])
    return s[s["fase_n"] != 4].head(top_n)["sector"].tolist()


# ------------------------------------------------------------------ informe
AZUL = colors.HexColor("#1B2A4A")
VERDE = colors.HexColor("#1E7A46")
ROJO = colors.HexColor("#A62B2B")
GRIS = colors.HexColor("#6B7280")


def construir_pdf(s: pd.DataFrame, mercado: dict, fuertes: list[str], ruta: Path) -> Path:
    estilos = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=estilos["Title"], fontSize=19, textColor=AZUL, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=estilos["Normal"], fontSize=9.5, textColor=GRIS, spaceAfter=12)
    h2 = ParagraphStyle("h2", parent=estilos["Heading2"], fontSize=12.5, textColor=AZUL,
                        spaceBefore=14, spaceAfter=6)
    normal = ParagraphStyle("n", parent=estilos["Normal"], fontSize=9.5, leading=13.5)
    pie = ParagraphStyle("pie", parent=estilos["Normal"], fontSize=7.5, textColor=GRIS, leading=10)

    doc = SimpleDocTemplate(str(ruta), pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=15 * mm, bottomMargin=14 * mm,
                            title="Fuerza de sectores")
    hoy = dt.date.today()
    el = [Paragraph("Fuerza relativa de los sectores", h1),
          Paragraph(f"Semana del {hoy:%d/%m/%Y} · método Weinstein · "
                    f"referencia: {mercado['ticker']}", sub)]

    # --- el mercado primero ---
    el.append(Paragraph("1 · ¿Está el mercado comprable?", h2))
    veredicto = "SÍ" if mercado["comprable"] else "NO"
    color_v = VERDE if mercado["comprable"] else ROJO
    el.append(Table(
        [[f"{mercado['ticker']} en {mercado['fase']}",
          f"cierre {mercado['cierre']}",
          f"MM30 {mercado['ma30']} ({mercado['ma30_pend']:+.2f}%)",
          veredicto]],
        colWidths=[52 * mm, 34 * mm, 56 * mm, 26 * mm],
        style=TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("TEXTCOLOR", (3, 0), (3, 0), color_v),
            ("ALIGN", (3, 0), (3, 0), "CENTER"),
            ("BOX", (0, 0), (-1, -1), 0.8, AZUL),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ])))
    if not mercado["comprable"]:
        el.append(Spacer(1, 5))
        el.append(Paragraph(
            "<b>El mercado no acompaña.</b> Weinstein no compra cuando el índice está "
            "por debajo de su media de 30 semanas y girando a la baja: en Fase 4 casi "
            "todo cae, incluso lo que parece fuerte.", normal))

    # --- ranking ---
    el.append(Paragraph("2 · Ranking de sectores", h2))
    cab = ["#", "Sector", "ETF", "Nota", "Mansfield", "Fase", "3M %", "6M %", "vs máx 52s"]
    filas = [cab]
    for _, r in s.iterrows():
        filas.append([
            str(r["puesto"]), r["sector"], r["etf"], f"{r['nota']:.1f}",
            f"{r['mansfield']:+.2f}", r["fase"],
            f"{r['ret_3m']:+.1f}" if pd.notna(r["ret_3m"]) else "—",
            f"{r['ret_6m']:+.1f}" if pd.notna(r["ret_6m"]) else "—",
            f"{r['dist_max_52s']:+.1f}" if pd.notna(r["dist_max_52s"]) else "—",
        ])
    tabla = Table(filas, colWidths=[9 * mm, 41 * mm, 13 * mm, 15 * mm, 22 * mm,
                                    24 * mm, 15 * mm, 15 * mm, 20 * mm], repeatRows=1)
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.2),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("BOX", (0, 0), (-1, -1), 0.6, AZUL),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
    ]
    for i, (_, r) in enumerate(s.iterrows(), start=1):
        if r["sector"] in fuertes:
            estilo.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#E8F5EE")))
        if pd.notna(r["mansfield"]):
            estilo.append(("TEXTCOLOR", (4, i), (4, i), VERDE if r["mansfield"] > 0 else ROJO))
        if r["fase_n"] == 4:
            estilo.append(("TEXTCOLOR", (5, i), (5, i), ROJO))
    tabla.setStyle(TableStyle(estilo))
    el.append(tabla)

    # --- selección ---
    el.append(Paragraph("3 · Sectores seleccionados", h2))
    if fuertes:
        baten = s[(s["sector"].isin(fuertes)) & (s["mansfield"] > 0)]["sector"].tolist()
        flojos = [x for x in fuertes if x not in baten]
        texto = ("Los <b>" + str(len(fuertes)) + "</b> mejores que no están en Fase 4: <b>"
                 + " · ".join(fuertes) + "</b>.<br/>")
        if flojos:
            texto += (
                "<br/><b>Aviso:</b> de esos, solo <b>" + (" · ".join(baten) if baten else "ninguno")
                + "</b> bate realmente al índice (Mansfield &gt; 0). "
                + (" · ".join(flojos)) + " entra por puntuación relativa, pero va peor que el "
                "mercado. La regla de Weinstein es no comprar nada por debajo de 0.<br/>")
        texto += ("<br/>En la parte 2 el análisis baja a las industrias que hay dentro de "
                  "estos sectores.")
        el.append(Paragraph(texto, normal))
    else:
        el.append(Paragraph(
            "<b>Ningún sector seleccionable:</b> todos en Fase 4. "
            "Con el mercado así, el método dice que no se compra.", normal))

    el.append(Paragraph("Cómo se lee", h2))
    el.append(Paragraph(
        "<b>Mansfield</b> es la fuerza relativa frente al índice. Por encima de 0 el sector "
        "bate al mercado; por debajo, va peor. Un sector puede subir un 8 % y seguir siendo "
        "mal sitio si el índice subió un 12 %.<br/><br/>"
        "<b>Las fases</b> son de Stan Weinstein: 1 base, 2 avance (donde se compra), "
        "3 techo, 4 caída.<br/><br/>"
        "<b>La nota</b> pondera fuerza relativa (40), su tendencia (15), momento a 3 y 6 meses "
        "(25), pendiente de la media de 30 semanas (10) y la fase (10).", normal))

    el.append(Spacer(1, 10))
    el.append(Paragraph(
        "Este informe es material educativo y NO es asesoramiento financiero, ni una "
        "recomendación de compra o de venta. Los datos proceden de Yahoo Finance y pueden "
        "contener errores. Los sectores que aparecen aquí son el resultado de una fecha "
        "concreta; la semana que viene serán otros. Invertir conlleva riesgo de pérdida.", pie))

    doc.build(el)
    return ruta


def enviar_email(pdf: Path, s: pd.DataFrame, fuertes: list[str], mercado: dict) -> None:
    user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    destino = os.environ.get("EMAIL_TO", user)
    if not (user and pwd and destino):
        log.warning("Faltan SMTP_USER / SMTP_PASS / EMAIL_TO. No envío el correo.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"Sectores · semana del {dt.date.today():%d/%m/%Y}"
    msg["From"], msg["To"] = user, destino
    estado = "comprable" if mercado["comprable"] else "NO comprable"
    top = "\n".join(f"  {r['puesto']}. {r['sector']:<24} nota {r['nota']:>5.1f}   "
                    f"Mansfield {r['mansfield']:+.2f}   {r['fase']}"
                    for _, r in s.head(5).iterrows())
    msg.set_content(
        f"Mercado ({mercado['ticker']}): {mercado['fase']} — {estado}\n\n"
        f"Sectores seleccionados: {' · '.join(fuertes) if fuertes else 'ninguno'}\n\n"
        f"Top 5:\n{top}\n\n"
        f"El informe completo va adjunto.\n\n"
        f"---\nMaterial educativo. No es asesoramiento financiero."
    )
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)

    servidor = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    puerto = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(servidor, puerto, context=ssl.create_default_context()) as smtp:
        smtp.login(user, pwd)
        smtp.send_message(msg)
    log.info("Correo enviado a %s.", destino)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ranking de fuerza de los sectores (Weinstein).")
    ap.add_argument("--email", action="store_true", help="enviar el informe por correo")
    ap.add_argument("--config", default=str(RAIZ / "config.yaml"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    s, mercado = ranking_sectores(cfg)
    fuertes = sectores_fuertes(s, cfg)

    print(f"\nMercado ({mercado['ticker']}): {mercado['fase']} — "
          f"{'comprable' if mercado['comprable'] else 'NO comprable'}\n")
    print(s[["puesto", "sector", "etf", "nota", "mansfield", "fase", "ret_3m"]]
          .to_string(index=False))
    print(f"\nSeleccionados: {' · '.join(fuertes) if fuertes else 'ninguno'}\n")

    SALIDA.mkdir(exist_ok=True)
    sello = dt.date.today().strftime("%Y%m%d")
    s.to_csv(SALIDA / f"sectores_{sello}.csv", index=False)
    pdf = construir_pdf(s, mercado, fuertes, SALIDA / f"sectores_{sello}.pdf")
    print(f"PDF: {pdf}")

    if args.email:
        enviar_email(pdf, s, fuertes, mercado)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
