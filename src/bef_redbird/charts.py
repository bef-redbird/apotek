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
    colors = ("#1D4F45", "#0072B2", "#B45309", "#7B4EA3", "#B23A48", "#64731C")
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
        company = companies.get(item_id, "")
        # Substitutes often share a name; keep ID and company as the visible identity.
        short_company = company if len(company) <= 12 else company[:9] + "…"
        legend_label = f"{item_id} · {short_company}" if company else item_id
        hover_company = company if len(company) <= 24 else company[:21] + "…"
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
                marker={"size": 7, "symbol": symbols[index], "maxdisplayed": 24},
                hovertemplate=(
                    f"<b>{item_id}</b>"
                    + (f" · {escape(hover_company)}" if hover_company else "")
                    + "<br>AIP %{y:.2f} kr.<extra></extra>"
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
        # Reserve legend space so adding curves never resizes the plot area.
        margin={"l": 74, "r": 16, "t": 76, "b": 40, "autoexpand": False},
        xaxis_tickformat="%d-%m-%Y",
        separators=",.",
        yaxis_title="AIP pr. pakning (DKK)",
        yaxis_tickformat=".2f",
        showlegend=True,
        legend={
            "orientation": "h",
            "y": 1.02,
            "yanchor": "bottom",
            "x": 0,
            "font": {"size": 13},
            "maxheight": 0.25,
        },
        font={"size": 14, "color": "#20372F"},
        hovermode="x unified",
        hoverlabel={
            "bgcolor": "#FFFFFF",
            "bordercolor": "#A9BFB4",
            "font": {"size": 13, "color": "#20372F"},
        },
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
    )
    figure.update_xaxes(
        showgrid=True,
        gridcolor="#E5ECE8",
        showline=True,
        linecolor="#A9BFB4",
        tickfont={"size": 12, "color": "#42594F"},
        unifiedhovertitle={"text": "Periode %{x|%d-%m-%Y} · LMS"},
    )
    figure.update_yaxes(
        showgrid=True,
        gridcolor="#E5ECE8",
        showline=True,
        linecolor="#A9BFB4",
        tickfont={"size": 12, "color": "#42594F"},
        title={"font": {"size": 13, "color": "#20372F"}},
    )
    return figure
