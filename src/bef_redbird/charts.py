"""Small, decision-time bounded Plotly views over the saved analysis."""

from datetime import timedelta
from html import escape

import plotly.graph_objects as go

from bef_redbird.domain import AnalysisResult


def history_chart(
    analysis: AnalysisResult,
    product_id: str,
    overlays: tuple[str, ...] = (),
    *,
    retrospective: bool = False,
    view_periods: int | None = None,
) -> go.Figure:
    """Keep explicit missing slots; never invent a future or carried-forward price."""
    periods = analysis.history.periods
    cutoff = analysis.context.current_period
    observed = [d for d in periods if retrospective or d <= cutoff]
    calendar = set(observed)
    if observed:
        cursor = min(periods)
        while cursor <= max(observed):
            calendar.add(cursor)
            cursor += timedelta(days=14)
    dates = sorted(calendar)
    if view_periods:
        dates = dates[-view_periods:]
    indices = {period: i for i, period in enumerate(periods)}
    figure = go.Figure()
    colors = ("#365D52", "#7C6594", "#B27D3C", "#477D95", "#995D69", "#7A7C49")
    ids = (product_id, *tuple(dict.fromkeys(overlays))[:5])
    dashes = ("solid", "dash", "dot", "dashdot", "longdash", "longdashdot")
    symbols = ("circle", "square", "diamond", "triangle-up", "cross", "x")
    companies = getattr(analysis.history, "companies", {})
    for index, item_id in enumerate(dict.fromkeys(ids)):
        prices = analysis.history.prices.get(item_id, ())
        values = []
        for period in dates:
            value = (
                prices[indices[period]]
                if (period in indices and indices[period] < len(prices))
                else None
            )
            values.append(value / 100 if value is not None else None)
        label = analysis.history.labels.get(item_id, "Navn ikke oplyst")
        company = companies.get(item_id, "")
        # Substitutes often share a name; keep ID and company as the visible identity.
        short_company = company if len(company) <= 12 else company[:9] + "…"
        legend_label = f"{item_id} · {short_company}" if company else item_id
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=values,
                name=escape(legend_label),
                meta=item_id,
                mode="lines+markers",
                connectgaps=False,
                line={
                    "shape": "hv",
                    "color": colors[index],
                    "width": 3.5 if index == 0 else 2.5,
                    "dash": dashes[index],
                },
                marker={"size": 6, "symbol": symbols[index]},
                hovertemplate=(
                    f"{escape(label)}<br>Varenr. {item_id}<br>Periode %{{x|%d-%m-%Y}}<br>"
                    "AIP %{y:.2f} DKK<br>LMS · observeret pris<extra></extra>"
                ),
            )
        )
    if retrospective and dates and max(dates) > cutoff:
        figure.add_vrect(
            x0=cutoff.isoformat(),
            x1=max(dates).isoformat(),
            fillcolor="#D6D9D4",
            opacity=0.4,
            line_width=0,
            annotation_text="Efter beslutningsperioden",
            annotation_position="top left",
        )
    figure.update_layout(
        height=260,
        margin={"l": 12, "r": 20, "t": 8, "b": 4},
        xaxis_tickformat="%d-%m-%Y",
        separators=",.",
        yaxis_title="AIP pr. pakning (DKK)",
        yaxis_tickformat=".2f",
        showlegend=bool(overlays),
        legend={
            "orientation": "h",
            "y": 1,
            "yanchor": "bottom",
            "x": 0,
            "font": {"size": 13},
            "maxheight": 0.2,
        },
        font={"size": 14, "color": "#202621"},
        hovermode="closest",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
    )
    figure.update_xaxes(showgrid=True, gridcolor="#E4E7E3", showline=True, linecolor="#9AA69F")
    figure.update_yaxes(showgrid=True, gridcolor="#E4E7E3", showline=True, linecolor="#9AA69F")
    return figure
