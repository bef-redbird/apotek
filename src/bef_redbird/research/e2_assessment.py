"""Development-only pooled descriptive and association assessment."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .e2_data import ResearchData
from .e2_metrics import paired_interval


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): value for key, value in row.items()}
        for row in frame.astype(object).where(pd.notna(frame), None).to_dict("records")
    ]


def assess(data: ResearchData) -> dict[str, Any]:
    con = data.connection
    con.execute("""CREATE OR REPLACE TEMP TABLE development AS
        SELECT f.row_key, f.package_number, f.period_index, f.period_start,
        f.identity_episode_id, f.eligible, f.aip_ore, f.history_periods, f.lag1_delta_ore,
        f.pack_size_token, f.pack_unit, f.peer_feature_state, f.peer_count,
        f.peer_lag1_observed_count, f.peer_lag1_rise_share, f.peer_lag1_fall_share,
        f.own_minus_peer_min_aip_ore,
        CASE WHEN next_maturity_date<DATE '2022-01-01' THEN next_status ELSE
            'pending_assessment' END AS next_status,
        CASE WHEN next_maturity_date<DATE '2022-01-01' THEN next_direction END AS next_direction,
        CASE WHEN next_maturity_date<DATE '2022-01-01' THEN next_delta_ore END AS next_delta_ore,
        CASE WHEN reversal_maturity_date<DATE '2022-01-01' THEN reversal_status ELSE
            'pending_assessment' END AS reversal_status,
        CASE WHEN reversal_maturity_date<DATE '2022-01-01' THEN reversal_category END AS
            reversal_category
        FROM predictors f JOIN labels l USING(row_key)
        WHERE f.period_start < DATE '2022-01-01'""")
    per_period = con.execute("""SELECT cast(period_start AS VARCHAR) AS period_start,
        count(*) AS rows, count(*) FILTER(WHERE eligible) AS eligible,
        count(*) FILTER(WHERE eligible AND next_status='observed') AS observed,
        count(*) FILTER(WHERE eligible AND next_status<>'observed') AS unknown,
        avg((next_direction=1)::INT) FILTER(WHERE eligible AND next_status='observed') AS rise,
        avg((next_direction=0)::INT) FILTER(WHERE eligible AND next_status='observed') AS unchanged,
        avg((next_direction=-1)::INT) FILTER(WHERE eligible AND next_status='observed') AS fall,
        avg(abs(next_delta_ore)/100.0) FILTER(WHERE eligible AND next_status='observed') AS
            absolute_change_dkk,
        quantile_cont(next_delta_ore/100.0,[0.01,0.05,0.5,0.95,0.99]) FILTER(WHERE eligible AND
            next_status='observed') AS delta_quantiles,
        quantile_cont(next_delta_ore::DOUBLE/nullif(aip_ore,0),[0.01,0.05,0.5,0.95,0.99])
            FILTER(WHERE eligible AND next_status='observed') AS relative_quantiles
        FROM development GROUP BY period_start ORDER BY period_start""").fetchdf()
    # DuckDB arrays need conversion before strict JSON serialization.
    for name in ("delta_quantiles", "relative_quantiles"):
        per_period[name] = per_period[name].map(
            lambda v: v.tolist() if isinstance(v, np.ndarray) else None
        )
    attrition = con.execute("""SELECT CASE WHEN period_start<DATE '2017-01-01' THEN
        'older_2008_2016' ELSE 'recent_2017_2021' END AS regime,
        CASE WHEN history_periods<6 THEN 'new_under_6_periods' ELSE 'established' END AS history,
        CASE WHEN lag1_delta_ore>0 THEN 'after_rise' WHEN lag1_delta_ore<0 THEN 'after_fall'
            ELSE 'unchanged_or_new' END AS last_change,
        next_status, count(*) AS rows FROM development WHERE eligible GROUP BY ALL ORDER BY
            ALL""").fetchdf()
    result: dict[str, Any] = {
        "version": "e2-development-assessment-v1",
        "exposure": (
            "Development origins 2008-01-14 through 2021-12-27 only; no E2 "
            "validation/evaluation scores inspected. Existing E1 exposure "
            "remains."
        ),
        "per_period": records(per_period),
        "attrition": records(attrition),
        "quantiles": [0.01, 0.05, 0.5, 0.95, 0.99],
        "limitations": [
            (
                "Missing next observations are not automatically a true exit; "
                "recorded eligibility/identity/gap states remain separate."
            ),
            "Package counts are not sales-based market shares.",
            "No causal response by a supplier is identified.",
            (
                "Development hypothesis assessment is exploratory; three pooled "
                "questions, no thousands-of-pairs search or multiplicity-adjusted "
                "significance claim."
            ),
        ],
    }
    if data.group_features:
        result["group_coverage"] = records(
            con.execute("""SELECT cast(date_trunc('year',period_start) AS VARCHAR) AS year,
                peer_feature_state,
            count(*) AS rows, avg(peer_count) AS mean_comparable_peers,
            avg((next_status<>'observed')::INT) FILTER(WHERE eligible) AS outcome_unknown
            FROM development GROUP BY ALL ORDER BY ALL""").fetchdf()
        )
        result["membership"] = records(
            con.execute("""WITH membership AS (
            SELECT g.*, f.period_index, f.identity_episode_id,
            lag(group_code) OVER(PARTITION BY g.package_number ORDER BY g.period_start)
                AS previous_group,
            lag(f.period_index) OVER(PARTITION BY g.package_number ORDER BY g.period_start) AS
                previous_index,
            lag(f.identity_episode_id) OVER(PARTITION BY g.package_number ORDER BY g.period_start)
                AS previous_episode
            FROM groups g JOIN features f USING(row_key) WHERE g.period_start<DATE '2022-01-01')
            SELECT cast(date_trunc('year',period_start) AS VARCHAR) AS year, count(*) AS rows,
            count(*) FILTER(WHERE group_code IS NOT NULL) AS known_group,
            count(*) FILTER(WHERE previous_group=group_code AND
                previous_index+1=period_index AND previous_episode=identity_episode_id)
                AS consecutive_same_code,
            count(*) FILTER(WHERE previous_group<>group_code AND
                previous_index+1=period_index AND previous_episode=identity_episode_id)
                AS consecutive_changed_code
            FROM membership GROUP BY ALL ORDER BY ALL""").fetchdf()
        )
        result["pooled_associations"] = pooled_associations(data)
        result["leadership_transition"] = records(
            con.execute("""SELECT
            CASE WHEN d.own_minus_peer_min_aip_ore<=0 THEN 'at_or_below_peers'
                 ELSE 'above_cheapest_peer' END AS current_position,
            count(*) AS comparable_next_group_observations,
            avg((ng.own_minus_peer_min_aip_ore<=0)::INT) AS next_at_or_below_peers,
            avg((d.next_direction=-1)::INT) AS next_own_fall
            FROM development d JOIN groups cg USING(row_key)
            JOIN features nf ON nf.package_number=d.package_number
                AND nf.period_index=d.period_index+1
                AND nf.identity_episode_id=d.identity_episode_id
                AND nf.pack_size_token=d.pack_size_token AND nf.pack_unit=d.pack_unit
            JOIN groups ng ON ng.row_key=nf.row_key AND ng.group_code=cg.group_code
            WHERE d.eligible AND d.next_status='observed' AND d.peer_count>=1
                AND ng.peer_count>=1 AND d.history_periods>=6
            GROUP BY ALL ORDER BY ALL""").fetchdf()
        )
        result["position_and_reversal"] = records(
            con.execute("""SELECT
            CASE WHEN own_minus_peer_min_aip_ore<0 THEN 'below_peers'
                 WHEN own_minus_peer_min_aip_ore=0 THEN 'tied_cheapest'
                 ELSE 'above_cheapest_peer' END AS price_position,
            count(*) AS origins, count(*) FILTER(WHERE next_status='observed') AS observed_next,
            avg((next_direction=-1)::INT) FILTER(WHERE next_status='observed') AS next_fall,
            avg((next_direction=1)::INT) FILTER(WHERE next_status='observed') AS next_rise,
            count(*) FILTER(WHERE lag1_delta_ore>0) AS already_observed_rises,
            count(*) FILTER(WHERE reversal_status='observed') AS observed_reversal_horizon,
            avg((reversal_category<>'persistence')::INT) FILTER(WHERE
                reversal_status='observed') AS any_reversal
            FROM development WHERE eligible AND peer_count>=1 AND history_periods>=6
            GROUP BY ALL ORDER BY ALL""").fetchdf()
        )
    return result


def pooled_associations(data: ResearchData) -> dict[str, Any]:
    frame = data.connection.execute("""SELECT identity_episode_id,period_start,
        sign(lag1_delta_ore)::DOUBLE AS own_current_direction,
        peer_lag1_rise_share-peer_lag1_fall_share AS peer_current_direction,
        own_minus_peer_min_aip_ore::DOUBLE/aip_ore AS relative_gap,
        (next_direction=1)::DOUBLE AS next_rise,
        (next_direction=-1)::DOUBLE AS next_fall,
        (reversal_category<>'persistence')::DOUBLE AS later_reversal,
        reversal_status, peer_lag1_observed_count,history_periods
        FROM development WHERE eligible AND next_status='observed'
        AND period_start>=DATE '2017-01-01' AND history_periods>=6
        AND peer_lag1_observed_count>=2""").fetchdf()
    if frame.empty:
        return {"status": "insufficient_supported_groups"}
    columns = [
        "own_current_direction",
        "peer_current_direction",
        "relative_gap",
        "next_rise",
        "next_fall",
    ]
    frame = frame.dropna(subset=columns).reset_index(drop=True)
    # Fixed clipping protects an interpretable linear probability diagnostic.
    frame["relative_gap"] = frame.relative_gap.clip(-10, 10)
    values = frame[columns].to_numpy(dtype=float)
    raw_correlation = float(np.corrcoef(values[:, 0], values[:, 1])[0, 1])
    # Alternating demeaning removes persistent episode and common period means.
    residual = pd.DataFrame(values, columns=columns)
    for _ in range(20):
        residual -= residual.groupby(frame.identity_episode_id).transform("mean")
        residual -= residual.groupby(frame.period_start).transform("mean")
    arr = residual.to_numpy()
    x = arr[:, :3]
    estimates = {}
    for target, offset in (("next_rise", 3), ("next_fall", 4)):
        coefficients = np.linalg.lstsq(x, arr[:, offset], rcond=None)[0]
        # Per-period estimating contributions retain temporal dependence for CI.
        residual_y = arr[:, offset] - x @ coefficients
        bread = np.linalg.pinv(x.T @ x / len(x))
        influence = (x * residual_y[:, None]) @ bread
        grouped = pd.DataFrame(influence).groupby(frame.period_start).mean().to_numpy()
        estimates[target] = {
            "coefficients": dict(zip(columns[:3], coefficients.tolist(), strict=True)),
            "peer_effect_temporal_interval": paired_interval(
                coefficients[1] + grouped[:, 1], seed=20261026, block=4
            ),
        }
    return {
        "status": "exploratory",
        "start": "2017-01-01",
        "end_exclusive": "2022-01-01",
        "n": len(frame),
        "periods": int(frame.period_start.nunique()),
        "episodes": int(frame.identity_episode_id.nunique()),
        "minimum_prior_observations": 6,
        "minimum_comparable_peers_with_observed_previous_price": 2,
        "contemporaneous_direction_correlation": raw_correlation,
        "within_episode_and_period_contemporaneous_correlation": float(
            np.corrcoef(arr[:, 0], arr[:, 1])[0, 1]
        ),
        "predictive_linear_probability": estimates,
        "interpretation": (
            "Directions/peer directional shares, not raw price-level "
            "correlation. Peer current changes and relative gaps precede next-"
            "period labels. Within-episode and period demeaning controls stable"
            " product differences and common market movements; not a causal "
            "estimate. Intervals are descriptive influence-block "
            "approximations; primary model ablations provide the predictive "
            "test."
        ),
    }
