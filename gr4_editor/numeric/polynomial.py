"""Stable polynomial initialization and matrix-only optimization."""
import cv2
import numpy as np


def terms(rgb):
    # Total-degree-three RGB polynomial; 20 coefficients per output channel.
    x = rgb.reshape(-1, 3)
    r, g, b = x.T
    return np.stack([
        np.ones_like(r), r, g, b,
        r*r, r*g, r*b, g*g, g*b, b*b,
        r*r*r, r*r*g, r*r*b, r*g*g, r*g*b, r*b*b,
        g*g*g, g*g*b, g*b*b, b*b*b,
    ], axis=1)


def fit(x, y, features=20):
    design = terms(x)[:, :features].astype(np.float64)
    response = y.astype(np.float64)
    # Small ridge term stabilizes poorly occupied RGB regions.
    normal = design.T @ design + np.eye(design.shape[1]) * 0.1
    normal[0, 0] -= 0.1
    return np.linalg.solve(normal, design.T @ response)


def predict(x, coefficients):
    return np.clip(terms(x)[:, :coefficients.shape[0]].astype(np.float64) @ coefficients, 0, 1).astype(np.float32)


def score(prediction, target):
    a = prediction.reshape(-1, 3)
    b = target.reshape(-1, 3)
    mae = float(np.abs(a-b).mean() * 255)
    lab_a = cv2.cvtColor(a.reshape(1, -1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3)
    lab_b = cv2.cvtColor(b.reshape(1, -1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3)
    delta = np.linalg.norm(lab_a-lab_b, axis=1)
    return {"rgb_mae_8bit": round(mae, 2), "delta_e76_median": round(float(np.median(delta)), 2),
            "delta_e76_p90": round(float(np.percentile(delta, 90)), 2)}


def log_domain(linear_rgb):
    # Compress the RAW-derived linear range before a small cubic RGB model.
    return np.log1p(6 * np.clip(linear_rgb, 0, 1)) / np.log(7)


def fit_matrix(rgb, target, curves):
    """Six-parameter matrix with unit row sums, minimize output RGB MSE."""
    axis = np.linspace(0, 1, 256)
    matrix = np.eye(3)
    iterations = []
    for c in range(3):
        others = [j for j in range(3) if j != c]
        basis = rgb[:, others] - rgb[:, c, None]
        parameters = np.zeros(2)
        row = curves[:, c].astype(float)

        def residual(p):
            z = np.clip(rgb[:, c] + basis @ p, 0, 1)
            return np.interp(z, axis, row) - target[:, c]

        for iteration in range(80):
            z = rgb[:, c] + basis @ parameters
            indices = np.minimum((np.clip(z, 0, 1) * 255).astype(int), 254)
            derivative = np.diff(row)[indices] * 255
            derivative[(z < 0) | (z > 1)] = 0
            jacobian = basis * derivative[:, None]
            step = np.linalg.lstsq(jacobian, -residual(parameters), rcond=None)[0]
            step *= min(1, .5 / max(np.linalg.norm(step), 1e-12))
            proposals = [np.clip(parameters + step * .5**q, -1.4, 1.4)
                         for q in range(9)]
            costs = [np.mean(residual(p)**2) for p in proposals]
            best = int(np.argmin(costs))
            if costs[best] >= np.mean(residual(parameters)**2) - 1e-12:
                break
            parameters = proposals[best]
        matrix[c, others] = parameters
        matrix[c, c] = 1 - parameters.sum()
        iterations.append(iteration + 1)
    return matrix, iterations
