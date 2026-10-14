"""Read-only usage projection over the same durable turns used by chat replay."""

import json
from datetime import UTC, datetime, timedelta, timezone

from .turn_submission import DesktopIngressError


def usage_statistics(database, *, days=30, timezone_offset_minutes=0, now=None):
    if (
        type(days) is not int
        or not 1 <= days <= 365
        or type(timezone_offset_minutes) is not int
        or not -840 <= timezone_offset_minutes <= 840
    ):
        raise DesktopIngressError("invalid_usage_window")
    current = (now or datetime.now(UTC)).astimezone(
        timezone(timedelta(minutes=timezone_offset_minutes))
    )
    end = current.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days - 1)
    expressions = []
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        field = f"$.usage.{name}"
        expressions.append(
            f"CASE WHEN json_type(u.payload_json,'{field}') IN ('integer','real') THEN MAX(0,json_extract(u.payload_json,'{field}')) ELSE 0 END AS {name}"
        )
    cte = f"""WITH counts AS (
      SELECT e.session_id, date(e.created_at,'unixepoch',?) AS date,
        COALESCE(json_extract(r.payload_json,'$.values.__desktop_model_snapshot__.model'),'unknown') AS model,
        {','.join(expressions)}
      FROM turn_events e JOIN turn_events u ON u.stream_id=e.stream_id AND u.type='turn_end'
      LEFT JOIN turn_ingress r ON r.session_id=e.session_id AND r.turn_id=e.turn_id
      WHERE e.type='exchange_end' AND json_extract(e.payload_json,'$.terminal')='converged'
        AND e.created_at>=? AND e.created_at<?
        AND NOT EXISTS (SELECT 1 FROM turn_events missing WHERE missing.stream_id=e.stream_id AND missing.type='turn_end'
          AND (json_type(missing.payload_json,'$.usage_reported') IS NOT 'true' OR json_type(missing.payload_json,'$.usage') IS NOT 'object'))
    ) """
    parameters = (
        f"{timezone_offset_minutes:+d} minutes",
        start.timestamp(),
        (end + timedelta(days=1)).timestamp(),
    )

    def project(connection):
        summary = dict(
            connection.execute(
                cte
                + """SELECT COALESCE(SUM(input_tokens),0) AS total_input_tokens,
          COALESCE(SUM(output_tokens),0) AS total_output_tokens,0 AS total_cost,
          COUNT(DISTINCT CASE WHEN input_tokens+output_tokens>0 THEN session_id END) AS total_sessions,
          COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,COALESCE(SUM(cache_write_tokens),0) AS cache_creation_tokens FROM counts""",
                parameters,
            ).fetchone()
        )
        daily = []
        size = 0
        for row in connection.execute(
            cte
            + "SELECT date,model,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,0 AS cost FROM counts GROUP BY date,model ORDER BY date,model",
            parameters,
        ):
            item = dict(row)
            size += len(json.dumps(item).encode())
            if size > 500000:
                raise DesktopIngressError(
                    "usage_statistics_requires_data_channel", status_code=409
                )
            daily.append(item)
        return {
            "summary": summary,
            "daily": daily,
            "date_range": {
                "start": start.date().isoformat(),
                "end": end.date().isoformat(),
            },
        }

    return database.run(project)
