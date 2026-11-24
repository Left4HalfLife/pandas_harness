import base64
import io
import json
import math
from pathlib import Path

import pandas as pd
from PIL import Image


def encode_result(result, directory):
    if not isinstance(result, dict) or "type" not in result or "value" not in result:
        raise ValueError("Analysis must return a typed result")
    kind, value = result["type"], result["value"]
    if kind == "dataframe" and isinstance(value, (pd.DataFrame, pd.Series)):
        if isinstance(value, pd.Series):
            value = value.to_frame()
        if len(value.columns) > 100:
            raise ValueError("Result has too many columns")
        return {
            "type": "table",
            "value": json.loads(value.head(100).to_json(orient="split", date_format="iso")),
            "rows": len(value),
        }
    if kind == "plot":
        path = Path(value).resolve()
        if not path.is_relative_to(Path(directory).resolve()):
            raise ValueError("Chart must be inside the analysis directory")
        data = path.read_bytes()
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Chart exceeds size limit")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("Only PNG charts are supported")
            image.verify()
        return {"type": "image", "value": base64.b64encode(data).decode("ascii")}
    if kind in {"string", "number"}:
        return {"type": "text", "value": str(value)[:20000]}
    raise ValueError("Unsupported analysis result")


def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("Invalid analysis response")
    kind, value = result.get("type"), result.get("value")
    if kind == "text" and isinstance(value, str) and len(value) <= 20000:
        return {"type": kind, "value": value}
    if kind == "image" and isinstance(value, str):
        data = base64.b64decode(value, validate=True)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Image too large")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("Invalid image")
            image.verify()
        return {"type": kind, "value": value}
    if kind == "table" and isinstance(value, dict):
        columns, rows = value.get("columns"), value.get("data")
        if (
            isinstance(columns, list) and len(columns) <= 100
            and all(isinstance(col, (str, int, float)) for col in columns)
            and isinstance(rows, list) and len(rows) <= 100
            and all(isinstance(row, list) and len(row) == len(columns) for row in rows)
            and all(
                cell is None or isinstance(cell, (str, bool, int))
                or isinstance(cell, float) and math.isfinite(cell)
                for row in rows for cell in row
            )
        ):
            total = result.get("rows", len(rows))
            if not isinstance(total, int) or total < len(rows):
                raise ValueError("Invalid row count")
            return {"type": kind, "value": {"columns": columns, "data": rows}, "rows": total}
    raise ValueError("Invalid analysis response")
