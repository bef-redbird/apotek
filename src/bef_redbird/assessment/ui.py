"""Danish saved-assessment workspace. Inference occurs only after explicit submission."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from bef_redbird.research.e4_common import checked, read

from . import service


def display(frame: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "Rang": pd.to_numeric(frame["rank"]),
            "Varenummer": frame.package_number,
            "AIP nu · kr.": pd.to_numeric(frame.aip_ore) / 100,
            "Forventet AIP · kr.": pd.to_numeric(frame.amount_3y_next_price_dkk),
            "Ændring · kr.": pd.to_numeric(frame.amount_3y_delta_dkk),
            "Stigning · %": pd.to_numeric(frame.p_rise) * 100,
            "Fald · %": pd.to_numeric(frame.p_fall) * 100,
            "Uændret · %": pd.to_numeric(frame.p_unchanged) * 100,
        }
    )
    if "actual_next_dkk" in frame:
        out["Faktisk AIP · kr."] = frame.actual_next_dkk
        out["Faktisk ændring · kr."] = frame.actual_delta_dkk
        out["Udfald"] = frame.outcome_status.map(
            lambda x: "Observeret" if x == "observed" else "Ukendt"
        )
    return out


def table(frame: pd.DataFrame) -> None:
    data = display(frame)
    st.dataframe(
        data,
        hide_index=True,
        width="stretch",
        column_config={
            name: st.column_config.NumberColumn(format="%.2f")
            for name in data.columns
            if "kr." in name or "%" in name
        },
    )


def candidates(
    saved: dict[str, Any], frame: pd.DataFrame, root: Path, *, view: str = "history"
) -> None:
    assessment_id = saved["assessment_id"]
    key = assessment_id + "-" + view
    counts = saved["counts"]
    a, b, c, d = st.columns(4)
    a.metric(
        "Vurderede pakninger",
        f"{counts['eligible']:,}",
        help=f"Af {counts['population']:,} pakninger. Resten har dokumenterede udelukkelser.",
    )
    b.metric("Kandidater efter risikofilter", counts["selected"])
    c.metric(
        "Pakninger med brugt Cito",
        counts["cito_used"],
        help="Kun brugbare, matchende koder bruger Cito. Resten beregnes uden Cito.",
    )
    d.metric(
        "Observerede blandt viste",
        f"{counts['observed']} / {counts['shown']}",
        help=f"{counts['unknown']} ukendte udfald; ingen erstattes.",
    )
    st.caption(
        f"Gemt vurdering · {saved['completed_utc']} · ID {key[:16]} · "
        f"{'Med Cito og tilbagefald uden Cito' if saved['mode'] == 'hybrid' else 'Uden Cito'}"
    )
    st.info(
        "Diagnostisk vurdering til faglig gennemgang. Prisstigning er ikke "
        "fortjeneste, leveringsgaranti eller en ordreanbefaling."
    )
    chosen = service.top50(frame)
    st.subheader(f"De første {len(chosen)} kandidater")
    st.caption(
        "Rangeret efter sandsynlighed for stigning. Mindst 35 % "
        "stigningssandsynlighed, højst 10 % faldsandsynlighed; højst 10 % af den "
        "vurderede population. Listen er fastlagt før udfald tilknyttes."
    )
    query = st.text_input("Find varenummer", key=f"find-{key}")
    shown = chosen.loc[chosen.package_number.str.contains(query, regex=False)] if query else chosen
    table(shown)
    if chosen.empty:
        st.warning("Ingen pakninger opfylder det fastholdte filter. Listen fyldes ikke op.")
        return
    if saved["mode"] == "hybrid" and st.checkbox("Sammenlign uden Cito", key=f"compare-{key}"):
        current = pd.read_parquet(checked(saved["files"]["current"]))
        comparison = chosen[["row_key", "package_number", "rank", "p_rise", "p_fall"]].merge(
            current[["row_key", "rank", "p_rise", "p_fall"]],
            on="row_key",
            suffixes=("_cito", "_uden"),
            validate="one_to_one",
        )
        comparison["Ændring i stigningssandsynlighed · procentpoint"] = (
            pd.to_numeric(comparison.p_rise_cito) - pd.to_numeric(comparison.p_rise_uden)
        ) * 100
        st.caption(
            "Samme periode, markedsdata og pakninger. Kun Cito-information og tilhørende "
            "kalibrering varierer."
        )
        st.dataframe(
            comparison.drop(columns="row_key").rename(
                columns={
                    "package_number": "Varenummer",
                    "rank_cito": "Rang med Cito",
                    "rank_uden": "Rang uden Cito",
                    "p_rise_cito": "Stigning med Cito",
                    "p_rise_uden": "Stigning uden Cito",
                    "p_fall_cito": "Fald med Cito",
                    "p_fall_uden": "Fald uden Cito",
                }
            ),
            hide_index=True,
        )
    observed = chosen.loc[chosen.outcome_status.eq("observed")]
    if not observed.empty:
        st.subheader("Forventet og faktisk prisændring")
        plot = observed[["package_number", "amount_3y_delta_dkk", "actual_delta_dkk"]].copy()
        plot.columns = ["Varenummer", "Forventet ændring", "Faktisk ændring"]
        figure = px.scatter(
            plot,
            x="Forventet ændring",
            y="Faktisk ændring",
            hover_name="Varenummer",
            labels={
                "Forventet ændring": "Forventet ændring · kr.",
                "Faktisk ændring": "Faktisk ændring · kr.",
            },
            color_discrete_sequence=["#087e8b"],
        )
        x_min = min(0.0, float(plot["Forventet ændring"].min()))
        y_min = min(0.0, float(plot["Faktisk ændring"].min()))
        x_range = [
            x_min - 20 if x_min < 0 else 0,
            max(0.0, float(plot["Forventet ændring"].max())) + 20,
        ]
        y_range = [
            y_min - 10,
            max(0.0, float(plot["Faktisk ændring"].max())) + 20,
        ]
        lo, hi = max(x_range[0], y_range[0]), min(x_range[1], y_range[1])
        figure.update_xaxes(range=x_range)
        figure.update_yaxes(range=y_range)
        figure.add_trace(
            go.Scatter(
                x=[lo, hi],
                y=[lo, hi],
                mode="lines",
                name="Perfekt overensstemmelse",
                line={"dash": "dot", "color": "#82909c"},
            )
        )
        st.plotly_chart(figure, width="stretch", key=f"outcomes-{key}")
        rises = int(observed.actual_delta_dkk.gt(0).sum())
        falls = int(observed.actual_delta_dkk.lt(0).sum())
        st.caption(
            f"Retning: {rises} steg, {falls} faldt, {len(observed) - rises - falls} uændrede "
            f"blandt {len(observed)} observerede. {counts['unknown']} ukendte. "
            "Beløbsfejl vurderes separat fra retning."
        )
    st.subheader("Se en pakning nærmere")
    packages = chosen.package_number.tolist()
    package_key = f"package-{key}"
    position = packages.index(st.session_state.get(package_key, packages[0]))

    def move_candidate(offset: int) -> None:
        current = packages.index(st.session_state.get(package_key, packages[0]))
        st.session_state[package_key] = packages[current + offset]

    previous_column, package_column, next_column = st.columns([1, 3, 1])
    previous_column.button(
        "Forrige kandidat",
        key=f"previous-{key}",
        disabled=position == 0,
        on_click=move_candidate,
        args=(-1,),
    )
    package = package_column.selectbox("Varenummer", packages, key=package_key)
    next_column.button(
        "Næste kandidat",
        key=f"next-{key}",
        disabled=position == len(packages) - 1,
        on_click=move_candidate,
        args=(1,),
    )
    st.caption(f"Kandidat {packages.index(package) + 1} af {len(packages)} i rangorden")
    row = chosen.loc[chosen.package_number.eq(package)].iloc[0]
    period = saved["binding"]["period"]
    peers = supported_peers(frame, row)
    if period.get("history"):
        history = pd.read_parquet(checked(period["history"]))
        calendar = sorted(pd.to_datetime(history.period_start).unique())

        def price_history(number: str) -> pd.DataFrame:
            values = history.loc[history.package_number.eq(number)].copy()
            values["period_start"] = pd.to_datetime(values.period_start)
            return (
                values.set_index("period_start")
                .reindex(calendar)
                .rename_axis("period_start")
                .reset_index()
            )

        own = price_history(package)
        peer_ids = peers.package_number.tolist()
        selected_peers = st.multiselect(
            "Substitutionskonkurrenter i grafen",
            peer_ids,
            default=peer_ids[:3],
            max_selections=5,
            key=f"peer-chart-{key}-{package}",
            help="Samme understøttede gruppe, pakningsstørrelse og enhed ved periodens start.",
        )
        chart = go.Figure()
        chart.add_trace(
            go.Scatter(
                x=own.period_start,
                y=pd.to_numeric(own.aip_ore) / 100,
                mode="lines+markers",
                name=f"Valgt {package} · historisk AIP",
                line={"color": "#111111", "width": 4},
                marker={"size": 7},
                legendrank=0,
                connectgaps=False,
            )
        )
        chart.add_trace(
            go.Scatter(
                x=[saved["origin"], saved["target"]],
                y=[float(row.aip_ore) / 100, float(row.amount_3y_next_price_dkk)],
                mode="lines+markers",
                name="Forventet næste AIP",
                line={"dash": "dot", "color": "#111111", "width": 3},
                legendrank=1,
            )
        )
        if row.outcome_status == "observed" and pd.notna(row.actual_next_dkk):
            chart.add_trace(
                go.Scatter(
                    x=[saved["target"]],
                    y=[float(row.actual_next_dkk)],
                    mode="markers",
                    name="Faktisk AIP",
                    legendrank=2,
                    marker={"symbol": "triangle-up", "size": 14, "color": "#c45e2a"},
                    hovertemplate="Faktisk AIP: %{y:.2f} kr.<extra></extra>",
                )
            )
        selected_trace_count = len(chart.data)
        colours = ["#4477aa", "#228833", "#aa3377", "#ccbb44", "#66ccee"]
        for index, number in enumerate(selected_peers):
            colour = colours[index]
            past = price_history(number)
            peer = peers.loc[peers.package_number.eq(number)].iloc[0]
            if peer.outcome_status == "observed" and pd.notna(peer.actual_next_dkk):
                past = pd.concat(
                    [
                        past,
                        pd.DataFrame(
                            {
                                "period_start": [pd.Timestamp(saved["target"])],
                                "aip_ore": [float(peer.actual_next_dkk) * 100],
                            }
                        ),
                    ],
                    ignore_index=True,
                )
            chart.add_trace(
                go.Scatter(
                    x=past.period_start,
                    y=pd.to_numeric(past.aip_ore) / 100,
                    mode="lines+markers",
                    connectgaps=False,
                    name=f"{number} · faktisk historik",
                    legendgroup=number,
                    legendrank=10 + index,
                    line={"color": colour, "width": 1.5},
                )
            )
        # Plotly paints later traces above earlier ones; retain selected-first legend order.
        chart.data = chart.data[selected_trace_count:] + chart.data[:selected_trace_count]
        chart.update_layout(
            yaxis_title="AIP · kr.",
            xaxis_title="Prisperiodens start",
            legend={"orientation": "h", "yanchor": "top", "y": -0.25, "x": 0},
            margin={"b": 120},
            height=520,
        )
        st.plotly_chart(chart, width="stretch", key=f"history-{key}")
        st.caption(
            "Konkurrenternes kurver fortsætter til sidste kendte faktiske AIP. "
            "Den orange trekant viser kun den valgte paknings faktiske næste AIP. "
            "Manglende priser forbindes ikke. "
            "Klik i forklaringen for at skjule eller vise en kurve."
        )
    st.write(
        f"Stigning {float(row.p_rise):.1%} · Fald {float(row.p_fall):.1%} · "
        f"Uændret {float(row.p_unchanged):.1%}"
    )
    st.write(
        f"Forventet næste AIP: {float(row.amount_3y_next_price_dkk):.2f} kr. "
        f"Forventet ændring: {float(row.amount_3y_delta_dkk):+.2f} kr. "
        "Beløbsvisningen bruger tre års træningshistorik."
    )
    st.caption(
        "Der findes ikke et valideret prisinterval for denne præcise modelvisning. "
        "Forskelle mellem modeller er ikke et konfidensinterval."
    )
    st.write(
        "Modellen har brugt pakningens tidligere prisbevægelser og understøttede "
        "substitutionskonkurrenter. "
        f"Antal sammenlignelige pakninger: {row.get('peer_count', 'ukendt')}. "
        f"Cito: {'brugt' if row.information_route == 'advance' else 'ikke brugt'}."
    )
    st.caption(
        "Dette er modeltilknyttet evidens, ikke dokumentation for årsagen til en prisændring."
    )
    if not peers.empty:
        peer_table = peers[["package_number", "aip_ore", "abc"]].copy()
        peer_table["AIP · kr."] = pd.to_numeric(peer_table.aip_ore) / 100
        st.dataframe(
            peer_table.drop(columns="aip_ore").rename(
                columns={"package_number": "Konkurrent · varenummer", "abc": "Prisklasse"}
            ),
            hide_index=True,
        )
    with st.expander("Følsomhed og kildekvalitet"):
        st.write(
            {
                "3 års historik · kr.": row.amount_3y_next_price_dkk,
                "6 års historik · kr.": row.amount_6y_next_price_dkk,
                "Hele historikken · kr.": row.amount_full_next_price_dkk,
            }
        )
        st.caption(
            "Tre år er valgt som visningsreference. Længere historik vinder ikke "
            "nødvendigvis for alle perioder eller pakninger."
        )
        st.write(
            f"Kildekvalitet: {row.get('input_quality', 'se operatørens kildekvittering')}. "
            f"Tilbagefald: {row.get('fallback_reason', 'ukendt')}."
        )
    note = service.notes(root, assessment_id, package)
    with st.form(f"notes-{key}-{package}"):
        statuses = ["Ikke gennemgået", "Til opfølgning", "Gennemgået"]
        status = st.selectbox("Gennemgang", statuses, index=statuses.index(note["status"]))
        text = st.text_area("Faglig note", value=note["note"], max_chars=4000)
        if st.form_submit_button("Gem note"):
            service.notes(root, assessment_id, package, {"status": status, "note": text})
            st.success("Noten er gemt til denne vurdering og pakning.")


def main(configuration: Path | None) -> None:
    st.set_page_config(page_title="Apotek · Prisoverblik", page_icon="✚", layout="wide")
    st.markdown(
        "<style>.block-container{max-width:1480px;padding-top:4.5rem}h1,h2,h3{color:#163747}div[data-testid=stMetric]{background:#edf5f5;padding:14px;border-radius:10px}</style>",
        unsafe_allow_html=True,
    )
    st.caption("APOTEK / FAGLIGT PRISOVERBLIK")
    st.title("Prisestimering – test og resultater")
    st.write(
        "Et samlet overblik over forventede prisbevægelser og de seneste perioders faktiske udfald."
    )
    if configuration is None:
        st.warning("Operatøren skal først konfigurere de forberedte markedsdata.")
        return
    try:
        cfg = service.config(configuration)
    except Exception:
        st.error("De forberedte markedsdata kan ikke verificeres. Kontakt operatøren.")
        return
    root = Path(cfg["root"])
    periods = sorted(cfg["periods"], key=lambda p: p["origin"])
    latest = periods[-1]
    next_tab, history_tab, method_tab = st.tabs(
        ["Næste periode", "De seneste to perioder", "Sådan beregnes vurderingen"]
    )
    with next_tab:
        selected_origin = st.selectbox(
            "Vurderingsperiode",
            [p["origin"] for p in reversed(periods)],
            format_func=lambda origin: (
                origin
                + " → "
                + next(p["target"] for p in periods if p["origin"] == origin)
                + (
                    " · historisk genberegning"
                    if origin != periods[-1]["origin"]
                    else " · næste periode"
                )
            ),
        )
        latest = next(p for p in periods if p["origin"] == selected_origin)
        st.header(f"{latest['origin']} → {latest['target']}")
        st.success("Markedsdata er forberedt · vurderingen kan beregnes lokalt")
        st.caption(
            f"Forberedte input: {latest.get('prepared_utc', 'se kildekvittering')} · "
            f"Kildemodtagelse: {latest.get('source_received_utc') or 'se kildekvittering'}"
        )
        st.write(
            "Upload Cito, hvis rapporten gælder denne overgang. Uden en gyldig rapport "
            "kan du stadig analysere markedsdata."
        )
        with st.form("analyse"):
            uploaded = st.file_uploader("Cito-rapport", type=["xls", "xlsx"])
            c1, c2 = st.columns(2)
            origin = c1.text_input("Cito: nuværende periodes start", value=latest["origin"])
            target = c2.text_input("Cito: næste periodes start", value=latest["target"])
            receipt = st.text_input(
                "Faktisk modtaget · dato, tid og tidszone", placeholder="2026-10-28T11:00:00+01:00"
            )
            receipt_precision = st.selectbox(
                "Modtagelsestidens præcision", ["Præcist", "Omtrentligt", "Ukendt"]
            )
            evidence = st.text_input(
                "Grundlag for rapportperioden", placeholder="Fx bekræftet af rapportens afsender"
            )
            confirmed = st.checkbox("Jeg har kontrolleret Cito-perioden og modtagelsen")
            submitted = st.form_submit_button("Analysér", type="primary")
        if submitted:
            try:
                if uploaded and not confirmed:
                    raise ValueError("Bekræft Cito-perioden og modtagelsen før analyse.")
                context = (
                    {
                        "current_period": origin,
                        "target_period": target,
                        "next_period_start": target,
                        "reported_receipt": receipt,
                        "receipt_precision": {
                            "Præcist": "instant",
                            "Omtrentligt": "approximate",
                            "Ukendt": "unknown",
                        }[receipt_precision],
                        "period_evidence": evidence,
                    }
                    if uploaded
                    else None
                )
                job = service.request(
                    configuration,
                    latest["origin"],
                    uploaded.getvalue() if uploaded else None,
                    context,
                )
                service.launch(job)
                st.session_state["active_job"] = str(job)
            except ValueError as exc:
                st.error(str(exc))
            except Exception:
                st.error("Analysen kunne ikke startes. Seneste gemte vurdering er bevaret.")

        pending = [
            path
            for path in (root / "jobs").glob("*/launch.lock")
            if not (path.parent / "result.json").exists()
        ]

        @st.fragment(run_every="2s" if pending else None)
        def job_status() -> None:
            jobs = sorted(
                (root / "jobs").glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            selected_job = (
                Path(st.session_state["active_job"])
                if "active_job" in st.session_state
                else jobs[0].parent
                if jobs
                else None
            )
            if selected_job:
                try:
                    value = service.state(selected_job)
                except Exception:
                    st.error("Jobbets gemte filer kan ikke verificeres. Kontakt operatøren.")
                    return
                st.write(f"Job: {value['stage']}")
                if value["state"] == "failed" and st.button("Prøv jobbet igen"):
                    service.retry(selected_job)
                if value["state"] == "completed" and st.button("Åbn gemt vurdering"):
                    st.rerun(scope="app")

        job_status()
        available = saved_assessments(root)
        upcoming = [x for x in available if x[0]["origin"] == latest["origin"]]
        if upcoming:
            saved, frame = preferred(upcoming)
            candidates(saved, frame, root, view="analysis")
        else:
            st.info(
                "Ingen gemt vurdering endnu. Klik Analysér for at beregne uden Cito, eller "
                "upload en periodekorrekt rapport."
            )
    with history_tab:
        available = saved_assessments(root)
        historical = [p for p in periods[:-1]][-2:]
        if historical:
            selected_history = st.radio(
                "Vælg historisk prisovergang",
                [p["origin"] for p in historical],
                format_func=lambda origin: (
                    origin + " → " + next(p["target"] for p in historical if p["origin"] == origin)
                ),
                horizontal=True,
            )
            period = next(p for p in historical if p["origin"] == selected_history)
            st.header(f"{period['origin']} → {period['target']}")
            matches = [x for x in available if x[0]["origin"] == period["origin"]]
            if matches:
                candidates(*preferred(matches), root)
            else:
                st.info("Den historiske vurdering er endnu ikke forberedt af operatøren.")
    with method_tab:
        st.subheader("Sådan er prisestimaterne udviklet og testet")
        st.graphviz_chart(
            'digraph { rankdir=LR; node [shape=box,style=rounded]; "Historiske priser" -> '
            '"Understøttede substitutionskonkurrenter" -> "Cito, hvis gyldig" -> '
            '"Sandsynligheder og prisestimat"; }'
        )
        st.write(
            "Vi beregner sandsynligheden for stigning, "
            "fald og uændret pris separat fra prisændringens størrelse. Rangeringen "
            "bruger sandsynlighed og et fast risikofilter."
        )
        st.write(
            "Modellerne er tidligere trænede CatBoost-modeller. Den brede "
            "sandsynlighedsreference og beløbsmodellerne har træningsstop 1. juli 2026. "
            "Kalibreringen bruger udfald fra før 1. september. Ældre modeller bruges, når "
            "konkurrentgrundlaget kræver det. Der trænes ikke ved upload."
        )
        st.write(
            "Historikken rekonstrueres med kausale input og mærkes diagnostisk. Ukendte "
            "udfald bliver på listen. Leveringssvigt er kildeevidens; der er endnu ikke "
            "dokumenteret et understøttet signal i denne prognose."
        )
    st.divider()
    st.subheader("Tag overblikket med")
    if st.button("Opret PDF-rapport"):
        from .report import export_pdf

        available = saved_assessments(root)
        selected = [
            preferred([x for x in available if x[0]["origin"] == p["origin"]])
            for p in periods
            if any(x[0]["origin"] == p["origin"] for x in available)
        ]
        if selected:
            from bef_redbird.research.e4_common import identity
            from bef_redbird.research.group_features import pin

            from . import report as report_module

            destination = (
                root
                / "exports"
                / (
                    identity(
                        {
                            "assessments": [x[0]["assessment_id"] for x in selected],
                            "producer": pin(Path(str(report_module.__file__)))["sha256"],
                        }
                    )
                    + ".pdf"
                )
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            from bef_redbird.research.group_features import pin
            from bef_redbird.research.prospective import write_once

            export_receipt = destination.with_suffix(".json")
            if not destination.exists():
                export_pdf(selected, destination)
                write_once(
                    export_receipt,
                    {
                        "pdf": pin(destination),
                        "assessments": [x[0]["assessment_id"] for x in selected],
                    },
                )
            else:
                checked(read(export_receipt)["pdf"])
            st.session_state["export"] = str(destination)
    if st.session_state.get("export"):
        path = Path(st.session_state["export"])
        checked(read(path.with_suffix(".json"))["pdf"])
        st.download_button(
            "Hent PDF til manuel deling",
            path.read_bytes(),
            file_name="Apotek-prisoverblik.pdf",
            mime="application/pdf",
        )


def saved_assessments(root: Path) -> list[tuple[dict[str, Any], pd.DataFrame]]:
    result = []
    for path in (root / "jobs").glob("*/result.json"):
        try:
            result.append(service.load(checked(read(path)["assessment"])))
        except Exception:
            st.warning("En gemt vurdering kunne ikke verificeres og vises ikke.")
    return sorted(result, key=lambda x: x[0]["completed_utc"])


def preferred(
    items: list[tuple[dict[str, Any], pd.DataFrame]],
) -> tuple[dict[str, Any], pd.DataFrame]:
    latest = max(items, key=lambda x: x[0]["completed_utc"])
    snapshot = latest[0]["binding"]["period"]["features"]["sha256"]
    compatible = [x for x in items if x[0]["binding"]["period"]["features"]["sha256"] == snapshot]
    return max(compatible, key=lambda x: (x[0]["mode"] == "hybrid", x[0]["completed_utc"]))


def supported_peers(frame: pd.DataFrame, row: pd.Series) -> pd.DataFrame:
    if row.get("peer_feature_state") != "complete_equal_size_peers":
        return frame.iloc[:0].copy()
    required = ["origin_group", "pack_size_token", "pack_unit"]
    if any(pd.isna(row.get(name)) or not str(row.get(name)) for name in required):
        return frame.iloc[:0].copy()
    mask = frame.eligible & frame.package_number.ne(row.package_number)
    for name in required:
        mask &= frame[name].eq(row[name])
    return frame.loc[mask].sort_values(["aip_ore", "package_number"]).copy()
