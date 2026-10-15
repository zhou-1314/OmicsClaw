"""Lightweight kernel helpers for replace-me."""
from __future__ import annotations

__all__ = ["summarize_metrics"]


def summarize_metrics(df) -> object:
    """Summarize numeric columns of a DataFrame with mean and standard deviation."""
    import numpy as np, pandas as pd
    num = df.select_dtypes(include=[np.number])
    return pd.DataFrame({
        "metric": num.columns,
        "mean": num.mean().to_numpy(),
        "std": num.std(ddof=0).to_numpy(),
    })
