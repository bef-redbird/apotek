"""Danish, applied-result documentation for the review workspace."""

from decimal import Decimal, localcontext

import streamlit as st

from bef_redbird.domain import AnalysisResult


def _percent(value: str) -> str:
    # Settings permit long exact decimals; documentation must not round a boundary.
    with localcontext() as context:
        context.prec = 160
        result = format(Decimal(value) * 100, "f")
    return (result.rstrip("0").rstrip(".") if "." in result else result).replace(".", ",")


def render_method_guide(analysis: AnalysisResult) -> None:
    """Explain the saved analysis, independent of pending controls or queue size."""
    settings = analysis.settings
    st.divider()
    st.subheader("Sådan læses analysen", anchor="metode")
    st.caption(
        "Metode, sammenhænge og anvendte indstillinger. Prioriteringen udpeger produkter "
        "til menneskelig gennemgang; den dokumenterer ikke en købsgevinst."
    )

    with st.expander("Fra Cito-kandidat til din vurdering"):
        st.markdown(
            "1. **Cito udpeger kandidaten:** varenummer, opbevaringskode, aktuel kostpris, "
            "nuværende og kommende kode samt gruppe danner grundlaget. A/B → C beskriver "
            "en ændret relativ prisposition. Kommende C kan skyldes egen prisstigning, "
            "billigere konkurrenter eller begge dele; næste AIP er ikke dermed kendt.\n"
            "2. **Varenummer forbinder til egen LMS-historik:** kun AIP-rækker fra "
            "LMS-dataarket bruges. Historiske mål gælder kandidatens egne priser. "
            "Cito-kostpris og AIP holdes adskilt og afstemmes i den bekræftede periode.\n"
            "3. **Gruppen forbinder til fundne alternativer i rapporten:** sammenligningen "
            "bruger samme gruppe-ID i den aktuelle, gyldige Cito-rapport før kandidatfiltrene. "
            "Også H/K, andre kodeskift og dyrere produkter kan derfor indgå. Gruppens "
            "fuldstændighed er ikke verificeret; den dokumenterer ikke historisk medlemskab. "
            "AIP pr. pakning dokumenterer ikke sammenlignelig enhedspris eller ekspeditionspris.\n"
            "4. **Du træffer vurderingen:** kontrollér egen historik, alternativer og "
            "dataproblemer og gem status og note. Historisk lav pris kan skyldes varigt "
            "prisfald. Afstand til median er hverken forventet afkast eller besparelse. "
            "Overvej afsætning og lagerbinding før indkøb. Mængder, efterspørgsel, "
            "returret og priskompensation indgår ikke i beregningerne."
        )

    with st.expander("Anvendte indstillinger og tidsgrænse"):
        periods = [p for p in analysis.history.periods if p <= analysis.context.current_period][
            -settings.lookback_periods :
        ]
        # Plain text prevents source-controlled metadata from becoming Markdown or HTML.
        st.text(
            f"Bekræftet aktuel periode: {analysis.context.current_period:%d-%m-%Y}\n"
            f"Historikvindue: {settings.lookback_periods} perioder; "
            f"{len(periods)} tilgængelige frem til den aktuelle periode\n"
            f"Mindst {settings.minimum_valid_prices} gyldige priser\n"
            f"Væsentlig stigning: strengt over {_percent(settings.rise_threshold)} %\n"
            f"Lav prisposition: percentil højst {_percent(settings.low_price_percentile)} %\n"
            f"Gentagne stigninger: mindst {settings.repeated_rise_minimum}\n"
            f"Procentvis tilbagefaldsvisning: mindst "
            f"{settings.minimum_events_for_rate_display} observerbare efterperioder"
        )
        if periods:
            st.caption(f"Anvendt vindue: {periods[0]:%d-%m-%Y} – {periods[-1]:%d-%m-%Y}.")
        st.markdown(
            "Dette er **den anvendte analyses** indstillinger, ikke eventuelle ændringer, "
            "der endnu ikke er anvendt. Screeningstærsklerne er ikke dokumenteret som "
            "kommercielt optimale.\n\n"
            "Kun prisperioder til og med den bekræftede aktuelle periode bruges i "
            "beregninger og prioritering. En aktuel AIP kræver en observation netop dér; "
            "sidst kendte pris udfylder den ikke. Retrospektiv diagramvisning kan vise "
            "senere observationer, men ændrer ikke analysen. Importtid, modtagelsestid "
            "og prisperiodens start er forskellige oplysninger. Dateret historik beviser "
            "ikke, at samme kildeversion var tilgængelig på en tidligere beslutningsdato."
        )
        st.text(
            "Metodeversioner: "
            + "; ".join(f"{key}: {value}" for key, value in sorted(analysis.versions.items()))
        )

    with st.expander("Udvælgelse og krav til prioritering"):
        st.markdown(
            "**Grundfilter i rækkefølge:** kontrollér de seks krævede felter og "
            "modstridende rækker → sammenlæg identiske seksfeltsrækker → fravælg "
            "opbevaringskode H og K → behold nuværende A eller B og kommende C → behold "
            "aktuel Cito-kostpris **under 1.000 kr.** Præcis 1.000 kr. er udenfor.\n\n"
            "En kandidat kan kun prioriteres, når alle disse kontroller er opfyldt:\n\n"
            "- Den bekræftede periode findes i LMS-priskalenderen.\n"
            "- Ingen modstridende AIP-observationer for varen ligger i analysevinduet.\n"
            "- AIP i den aktuelle periode findes og er positiv.\n"
            "- Aktuel Cito-kostpris og aktuel AIP er præcis ens i hele øre.\n"
            "- Hele det ønskede antal historikperioder er tilgængeligt.\n"
            "- Antallet af gyldige priser opfylder det anvendte minimum.\n\n"
            "Kandidater, der fejler én eller flere kontroller, bevares i **Kan ikke "
            "prioriteres** med årsager. Køen fyldes ikke op med dem. Manglende gruppe "
            "begrænser sammenligningen, men er ikke i sig selv et prioriteringsstop."
        )

    with st.expander("Beregninger og præcise grænser"):
        st.markdown(
            "**Vindue og gyldighed.** W er de seneste indlæste LMS-perioder til og med "
            "den bekræftede periode, begrænset af historikvinduet. S er de positive, "
            "gyldige AIP-priser i W; manglende og nulpriser tæller ikke. "
            "Dækning = antal priser i S / antal perioder i W. Et kort vindue vises som "
            "kort; der indsættes ikke priser. Uden perioder er dækningen ubestemt.\n\n"
            "**Median og prisposition.** Medianen er den midterste gyldige pris, eller "
            "gennemsnittet af de to midterste ved et lige antal. Afledte halve øre "
            "bevares i beregningerne; priser vises med to decimaler i appen og Excel. "
            "Afstand til egen historiske median = (median i øre − aktuel AIP "
            "i øre) / 100, målt i kroner. Prispercentil = (antal priser under aktuel AIP "
            "+ 0,5 × antal priser lig aktuel AIP) / antal gyldige priser. Lige priser "
            "får dermed midterplacering; en konstant serie giver 50 %.\n\n"
            "**Stigninger.** En gyldig naboperiode kræver to positive priser præcis "
            "14 dage fra hinanden i W. Ændring = ny pris / forrige pris − 1. En væsentlig "
            "stigning skal være **strengt over** den anvendte tærskel; ved en tærskel "
            "på 5 % tæller præcis 5 % ikke. Stigningsfrekvens = antal væsentlige "
            "stigninger / antal gyldige nabopar. Ingen nabopar giver en ubestemt frekvens. "
            "Stigningstærskel og percentilgrænse afgøres med eksakte heltalsforhold, "
            "før forholdstal afrundes til visning.\n\n"
            "**Tilbagefald.** Kald prisen før stigningen A, toppen B og næste pris C. "
            "Alle tre priser skal være gyldige, inde i W og have 14 dage mellem hvert "
            "nabopar. Ethvert tilbagefald kræver C < B; fuldt tilbagefald kræver C ≤ A. "
            "Nævneren er kun stigninger med en observeret, gyldig efterperiode, "
            "ikke alle stigninger. Uden sådanne hændelser er raten ubestemt, ikke 0 %. "
            "Under det anvendte minimum vises antal og begrænset grundlag frem for "
            "en selvstændig procent.\n\n"
            "**Størrelse af tilbagefald.** Næste ændring = C / B − 1; tilbagegivet andel "
            "af stigningen = (B − C) / (B − A). Andelen kan være negativ eller over 1; "
            "over 1 betyder, at prisen faldt under udgangspunktet. Den afkortes ikke."
        )

    with st.expander("Prioritetsrækkefølge, visning og gemte vurderinger"):
        st.markdown(
            "Den deterministiske rækkefølge bruger tre trin:\n\n"
            "1. **Trin 0:** percentil højst den anvendte lavprisgrænse, positiv afstand "
            "til egen median og mindst det anvendte antal gentagne stigninger.\n"
            "2. **Trin 1:** samme lave prisposition og positive medianafstand, men "
            "uden tilstrækkeligt mange stigninger til trin 0.\n"
            "3. **Trin 2:** øvrige kandidater, der kan prioriteres.\n\n"
            "Inden for hvert trin sorteres først efter **medianafstand faldende**, "
            "så **prispercentil stigende**, så **varenummer stigende**. Der bruges "
            "ingen vægtet score. Tilbagefald er historisk kontekst og ændrer ikke denne "
            "rækkefølge. Flere kandidater fra samme gruppe kan indgå.\n\n"
            "**Visningsvalg:** Antal kandidater vælger de første N i den fulde "
            "prioritering; Alle prioriterbare viser hele prioriteringen. Søgning og "
            "filtre afgrænser den valgte liste uden at hente lavere placeringer ind i "
            "den prioriterede liste. Tabelsortering, produktvalg og diagramudsnit ændrer hverken "
            "beregninger, oprindelige prioriteter eller analyseidentitet. "
            "Alle kandidater omfatter også dem, der ikke kan prioriteres.\n\n"
            "**Vurderinger og eksport:** status og noter tilhører varenummeret i den "
            "konkrete analyse. Kladder bevares under produktskift i sessionen; brug Gem "
            "vurdering eller Gem og næste kandidat for at få dem med i Excel og sessionskopien. "
            "Ændret N sletter ikke gemte vurderinger. Nye anvendte kilder, periode eller "
            "metodeindstillinger giver en ny analyse. Excel følger det valgte "
            "eksportomfang: den aktuelle kanoniske kø, særskilt valgte produkter eller "
            "alle kandidater. Søgning og tabelsortering ændrer ikke eksportomfanget. "
            "Eksporten indeholder anvendte indstillinger, kildeidentitet og de "
            "eksporterede varenumre. JSON kan kun gendannes til en matchende analyse; "
            "gendannelse erstatter kladder. Sessionen er midlertidig: hent en kopi "
            "for at bevare gemte vurderinger efter genstart."
        )
