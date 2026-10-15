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
        # IDs stay available as trace metadata; human labels are escaped for Plotly HTML.
        short_label = label if len(label) <= 55 else label[:52] + "…"
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=values,
                name=escape(f"{item_id} · {short_label}"),
                meta=item_id,
                mode="lines+markers",
                connectgaps=False,
                line={
                    "shape": "hv",
                    "color": colors[index],
                    "width": 3 if index == 0 else 2,
                    "dash": dashes[index],
                },
                marker={"size": 5, "symbol": symbols[index]},
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
        height=360,
        margin={"l": 8, "r": 8, "t": 45, "b": 8},
        xaxis_title="Prisperiodens start",
        xaxis_tickformat="%d-%m-%Y",
        separators=",.",
        yaxis_title="AIP pr. pakning (DKK)",
        showlegend=True,
        legend={"orientation": "h", "y": -0.25, "yanchor": "top", "x": 0, "font": {"size": 11}},
        hovermode="closest",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return figure
