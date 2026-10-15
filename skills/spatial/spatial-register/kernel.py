"""Lightweight kernel helpers for spatial-register (weighted Procrustes alignment & RMSD)."""
from __future__ import annotations
__all__ = ['procrustes_register', 'registration_error']

def procrustes_register(coords_ref: np.ndarray, coords_mov: np.ndarray, pi: np.ndarray | None=None) -> tuple[np.ndarray, np.ndarray]:
    """Align moving 2D spatial coordinates onto reference coordinates via transport-weighted SVD."""
    import numpy as np
    X = np.asarray(coords_ref, dtype=float)
    Y = np.asarray(coords_mov, dtype=float)
    if pi is None:
        n = min(len(X), len(Y))
        pi = np.eye(n) / float(n)
        X, Y = (X[:n], Y[:n])
    w_x = pi.sum(axis=1, keepdims=True)
    w_y = pi.sum(axis=0, keepdims=True).T
    mu_x = (X * w_x).sum(axis=0) / max(float(w_x.sum()), 1e-12)
    mu_y = (Y * w_y).sum(axis=0) / max(float(w_y.sum()), 1e-12)
    Xc = X - mu_x
    Yc = Y - mu_y
    H = Yc.T @ pi.T @ Xc
    U, _, Vt = np.linalg.svd(H)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        Vt[-1, :] *= -1
        R = U @ Vt
    Y_aligned = (np.asarray(coords_mov, dtype=float) - mu_y) @ R + mu_x
    return (np.asarray(coords_ref, dtype=float), Y_aligned)

def registration_error(coords_ref: np.ndarray, coords_mov: np.ndarray, pi: np.ndarray | None=None) -> dict:
    """Compute centroid offset and transport-weighted root-mean-square distance (RMSD)."""
    import numpy as np
    X = np.asarray(coords_ref, dtype=float)
    Y = np.asarray(coords_mov, dtype=float)
    centroid_shift = float(np.linalg.norm(X.mean(axis=0) - Y.mean(axis=0)))
    if pi is not None:
        d2 = np.sum((X[:, None, :] - Y[None, :, :]) ** 2, axis=-1)
        rmsd = float(np.sqrt(np.sum(pi * d2) / max(float(pi.sum()), 1e-12)))
    else:
        n = min(len(X), len(Y))
        rmsd = float(np.sqrt(np.mean(np.sum((X[:n] - Y[:n]) ** 2, axis=1))))
    return {'centroid_shift': round(centroid_shift, 4), 'rmsd': round(rmsd, 4)}
