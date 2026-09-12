# Fuerza de sectores — parte 1

> **🇬🇧 In English** — Stock screener in Python built on **Stan Weinstein's stage
> analysis**. It downloads the 11 SPDR sector ETFs plus a benchmark, measures each
> sector's **Mansfield relative strength** against the index, classifies it into
> one of Weinstein's four stages, and ranks them 0–100. Output is a one-page PDF,
> emailed automatically every Sunday by a GitHub Actions workflow — 12 downloads a
> week, so it runs comfortably inside the free tier.
>
> The core rule it encodes: **never buy something weaker than the index.** A sector
> can be up 8% and still be the wrong place to be if the index is up 12%. Relative
> strength carries 40 of the 100 points in the score for exactly that reason.
>
> The report also flags when a selected sector is only there on ranking merit while
> still underperforming the index — the screener tells you when it disagrees with
> the method.
>
> Docs below are in Spanish. Code, config and comments are self-explanatory.
>
> **Educational material. Not investment advice.**

Mide qué sectores del mercado estadounidense **baten al índice** y cuáles no,
siguiendo el método de análisis de fases de Stan Weinstein. Corre solo cada
domingo y manda un PDF por correo.

> **Material educativo. No es asesoramiento financiero** ni una recomendación de
> compra o venta. Invertir conlleva riesgo de pérdida.

---

## La idea

> **La acción es lo último que eliges, no lo primero.**

Weinstein compra fortaleza y en este orden: primero el mercado, después el
sector, después la industria, y solo al final la empresa. Este repositorio hace
los dos primeros pasos.

### Fuerza relativa, no rentabilidad

Un sector puede subir un 8 % y ser un mal sitio donde estar, si el índice subió
un 12 %. Lo que importa es la diferencia:

```
RS         = precio del sector / precio del índice
Mansfield  = (RS / media 52 semanas de RS − 1) × 100
```

**Por encima de 0, el sector bate al mercado.** Por debajo, va peor. Weinstein
no compra nada que esté por debajo de 0.

### Las cuatro fases

| Fase | Qué es | Qué se hace |
|---|---|---|
| 1 · Base | Suelo lateral tras la caída. MM30 plana | Esperar |
| 2 · Avance | Precio sobre la MM30 y la MM30 subiendo | **Aquí se compra** |
| 3 · Techo | La media se aplana tras la subida | Recoger |
| 4 · Caída | Precio bajo la MM30 y la MM30 bajando | No tocar |

### La nota (0–100)

| Componente | Puntos |
|---|---|
| Fuerza relativa de Mansfield | **40** |
| Tendencia de esa fuerza | 15 |
| Rentabilidad a 3 meses | 15 |
| Rentabilidad a 6 meses | 10 |
| Pendiente de la media de 30 semanas | 10 |
| Fase (2 = 10 · 1 = 4 · 3 = 2 · 4 = 0) | 10 |

Que la fuerza relativa pese 40 de 100 no es casualidad: es la regla de Weinstein
traducida a código.

---

## Uso

```bash
pip install -r requirements.txt

python sectores.py            # ranking en pantalla + PDF y CSV en output/
python sectores.py --email    # además lo manda por correo
```

Son **12 descargas** (11 ETF sectoriales + el índice). Nada pesado.

## Automático cada domingo

El workflow `.github/workflows/sectores.yml` lo ejecuta los domingos a las 07:00
UTC, guarda el informe como artefacto, publica el CSV en el repo y manda el PDF
por correo.

Hacen falta tres **secretos** en `Settings → Secrets and variables → Actions`:

| Secreto | Qué es |
|---|---|
| `SMTP_USER` | Tu dirección de Gmail |
| `SMTP_PASS` | Una **contraseña de aplicación** de Google, no la del correo |
| `EMAIL_TO` | A dónde mandar el informe |

También se puede lanzar a mano desde la pestaña **Actions** → *Run workflow*.

### Por qué esta parte sí corre en la nube

Doce descargas semanales no las limita nadie. Los pasos siguientes (industrias,
empresas, fundamentales) necesitan miles de peticiones, y las fuentes que las
sirven bloquean las IP de centros de datos como las de GitHub Actions. Eso se
resuelve en la parte 2.

`curl_cffi` está en las dependencias a propósito: sin él, Yahoo detecta un
cliente que no parece un navegador y acaba cortando las peticiones.

---

## Configuración

Todo en `config.yaml`: el índice de referencia, los 11 ETF, los parámetros de
Weinstein y cuántos sectores seleccionar.

El benchmark por defecto es **SPY**. Si cambias el universo a mercado global,
cambia también el benchmark — medir sectores estadounidenses contra un índice
mundial mete diferencias de región y de divisa dentro de la señal.

## Salidas

| Fichero | Contenido |
|---|---|
| `output/sectores_AAAAMMDD.pdf` | El informe |
| `output/sectores_AAAAMMDD.csv` | Los datos, para el histórico |

## Aviso

Los datos vienen de Yahoo Finance y pueden contener errores. Los sectores del
informe son el resultado de **una fecha concreta**: la semana siguiente serán
otros. Esto es una herramienta de análisis, no una señal de compra.
