#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_map_to_vectors_upgraded.py
------------------------------
Pipeline genérico para transformar uma planta em PDF (mapa) em uma camada vetorial (GeoJSON),
extraindo polígonos a partir de regiões (ex.: bairros) desenhadas com preenchimento colorido.

Upgrade principal:
- Segmentação por *clusters de cor* (k-means no espaço Lab a/b), que costuma separar melhor
  “manchas” coloridas adjacentes do que um simples limiar HSV.
- Auto-crop focado na área colorida (remove tabelas/legendas laterais em muitas plantas).
- Filtro de "rabiscos" (compactness) pra eliminar redes viárias/curvas de nível.

Requisitos:
  pip install pymupdf opencv-python shapely numpy

Uso recomendado (modo kmeans):
  python3 pdf_map_to_vectors_upgraded.py --pdf plana.pdf --out out.geojson --debug-dir debug

Se precisar, volte pro HSV:
  python3 pdf_map_to_vectors_upgraded.py --pdf plana.pdf --segment hsv --out out.geojson --debug-dir debug
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple, Union

import numpy as np
import cv2
import fitz  # PyMuPDF

from shapely.geometry import Polygon, MultiPolygon, GeometryCollection, mapping
from shapely.ops import unary_union


# -----------------------------
# Utilidades
# -----------------------------

def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)

def write_image(path: str, img_bgr: np.ndarray) -> None:
    cv2.imwrite(path, img_bgr)

def rgb_to_bgr(img_rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

def bgr_to_rgb(img_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)


# -----------------------------
# Renderização PDF -> imagem
# -----------------------------

def render_pdf_page_to_bgr(pdf_path: str, page_index: int = 0, dpi: int = 300) -> np.ndarray:
    doc = fitz.open(pdf_path)
    if page_index < 0 or page_index >= len(doc):
        raise ValueError(f"page_index inválido: {page_index} (pdf tem {len(doc)} páginas)")

    page = doc[page_index]
    scale = dpi / 72.0
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)

    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]

    img_rgb = img  # PyMuPDF entrega em RGB
    return rgb_to_bgr(img_rgb)


# -----------------------------
# Crops
# -----------------------------

def auto_crop_nonwhite(img_bgr: np.ndarray, white_thresh: int = 245, pad: int = 10) -> Tuple[np.ndarray, Tuple[int,int,int,int]]:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mask = (gray < white_thresh).astype(np.uint8) * 255

    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        h, w = gray.shape[:2]
        return img_bgr, (0, 0, int(w), int(h))

    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())

    h, w = gray.shape[:2]
    x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
    x1 = min(w - 1, x1 + pad); y1 = min(h - 1, y1 + pad)

    crop = img_bgr[y0:y1+1, x0:x1+1].copy()
    return crop, (x0, y0, x1, y1)

def auto_crop_colorful(img_bgr: np.ndarray, s_min: int = 18, v_min: int = 35, pad: int = 80) -> Tuple[np.ndarray, Tuple[int,int,int,int]]:
    """
    Auto-crop que tenta pegar *só* a área que tem preenchimentos coloridos.
    Isso costuma excluir tabelas/legendas e a borda da folha.
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    _h, s, v = cv2.split(hsv)

    # pixels "potencialmente coloridos" (exclui branco puro e preto/linha)
    mask = ((s >= s_min) & (v >= v_min) & (v <= 255)).astype(np.uint8) * 255

    # limpa ruído (pontinhos)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)

    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        # fallback
        return auto_crop_nonwhite(img_bgr, white_thresh=245, pad=10)

    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())

    h, w = img_bgr.shape[:2]
    x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
    x1 = min(w - 1, x1 + pad); y1 = min(h - 1, y1 + pad)

    crop = img_bgr[y0:y1+1, x0:x1+1].copy()
    return crop, (x0, y0, x1, y1)

def crop_from_arg(img_bgr: np.ndarray, crop_arg: str) -> Tuple[np.ndarray, Tuple[int,int,int,int]]:
    """
    crop_arg:
      - "auto" / "nonwhite"
      - "auto-color" / "color"
      - "x0,y0,x1,y1" (inclusive-exclusive: x1,y1 são limites finais exclusivos)
    Retorna (img_crop, (x0,y0,x1,y1)) onde x1,y1 são inclusivos (para debug coerente).
    """
    if not crop_arg:
        crop_arg = "auto-color"

    c = crop_arg.strip().lower()
    if c in ("auto", "nonwhite"):
        return auto_crop_nonwhite(img_bgr, white_thresh=245, pad=10)
    if c in ("auto-color", "color", "autocolor"):
        return auto_crop_colorful(img_bgr, s_min=18, v_min=35, pad=80)

    parts = [p.strip() for p in crop_arg.split(",")]
    if len(parts) != 4:
        raise ValueError("Crop inválido. Use auto, auto-color, ou 'x0,y0,x1,y1'")

    x0, y0, x1, y1 = (int(p) for p in parts)

    # clamp
    h, w = img_bgr.shape[:2]
    x0 = max(0, min(w - 1, x0))
    y0 = max(0, min(h - 1, y0))
    x1 = max(0, min(w, x1))
    y1 = max(0, min(h, y1))

    img_crop = img_bgr[y0:y1, x0:x1].copy()
    # converter pra "inclusive" pro debug e pro offset
    return img_crop, (x0, y0, x1 - 1, y1 - 1)


# -----------------------------
# Redimensionamento de trabalho
# -----------------------------

def maybe_downscale(img_bgr: np.ndarray, max_side: int) -> Tuple[np.ndarray, float]:
    """
    Se a imagem for muito grande, reduz para acelerar.
    Retorna (img_resized, scale_up_factor).
    scale_up_factor = fator para voltar coordenadas pro tamanho original (ex.: 4.0).
    """
    h, w = img_bgr.shape[:2]
    if max(h, w) <= max_side:
        return img_bgr, 1.0

    scale = max_side / float(max(h, w))
    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
    scale_up = 1.0 / scale
    return resized, float(scale_up)


# -----------------------------
# Segmentação e vetorização
# -----------------------------

@dataclass
class SegmentParams:
    # HSV (modo simples)
    s_min: int = 35
    v_min: int = 55
    v_max: int = 255

    # morfologia
    open_ksize: int = 3
    close_ksize: int = 5
    close_iter: int = 2

    # filtragem
    min_area_px: int = 1200
    simplify_tol: float = 1.5
    approx_eps: float = 2.0
    compactness_min: float = 0.02  # remove "rabiscos"

def fill_holes(mask01: np.ndarray) -> np.ndarray:
    """
    Preenche buracos internos em uma máscara 0/255.
    """
    mask = mask01.copy()
    h, w = mask.shape[:2]
    flood = mask.copy()
    # floodFill precisa de máscara com 2px de borda
    ffmask = np.zeros((h + 2, w + 2), dtype=np.uint8)
    cv2.floodFill(flood, ffmask, seedPoint=(0, 0), newVal=255)
    flood_inv = cv2.bitwise_not(flood)
    filled = mask | flood_inv
    return filled

def build_hsv_color_mask(img_bgr: np.ndarray, p: SegmentParams) -> np.ndarray:
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    _h, s, v = cv2.split(hsv)

    mask = ((s >= p.s_min) & (v >= p.v_min) & (v <= p.v_max)).astype(np.uint8) * 255

    if p.open_ksize > 1:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (p.open_ksize, p.open_ksize))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)

    if p.close_ksize > 1:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (p.close_ksize, p.close_ksize))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=p.close_iter)

    mask = fill_holes(mask)
    return mask

def polygon_compactness(poly: Polygon) -> float:
    a = float(poly.area)
    per = float(poly.length)
    if per <= 0.0:
        return 0.0
    return (4.0 * math.pi * a) / (per * per)

def components_to_polygons(mask: np.ndarray, p: SegmentParams, scale_up: float = 1.0) -> List[Union[Polygon, MultiPolygon]]:
    """
    Converte componentes conectados da máscara em polígonos shapely.
    """
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out: List[Union[Polygon, MultiPolygon]] = []

    for label_id in range(1, num_labels):
        area = int(stats[label_id, cv2.CC_STAT_AREA])
        if area < p.min_area_px:
            continue

        comp = (labels == label_id).astype(np.uint8) * 255
        contours, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue

        cnt = max(contours, key=cv2.contourArea)
        if cv2.contourArea(cnt) < p.min_area_px:
            continue

        approx = cv2.approxPolyDP(cnt, epsilon=p.approx_eps, closed=True)
        coords = [(float(pt[0][0]) * scale_up, float(pt[0][1]) * scale_up) for pt in approx]
        if len(coords) < 3:
            continue

        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)

        if poly.is_empty or poly.area <= 0:
            continue

        # Remove "linhas" e rabiscos
        if polygon_compactness(poly) < p.compactness_min:
            continue

        if p.simplify_tol and p.simplify_tol > 0:
            poly = poly.simplify(p.simplify_tol, preserve_topology=True)

        if poly.is_empty or poly.area <= 0:
            continue

        out.append(poly)

    return out

def kmeans_color_segmentation_masks(
    img_bgr: np.ndarray,
    k: int,
    sample: int,
    chroma_min: float,
    v_min: int = 35,
) -> Tuple[np.ndarray, List[int], np.ndarray]:
    """
    Retorna:
      labels (H,W) int16,
      selected_clusters (list[int]),
      preview_bgr (H,W,3) imagem de clusters selecionados (debug)
    """
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)

    # "chroma" ≈ distância ao cinza (A=128,B=128)
    a0 = (A.astype(np.int16) - 128).astype(np.int16)
    b0 = (B.astype(np.int16) - 128).astype(np.int16)
    chroma = np.sqrt((a0.astype(np.float32) ** 2) + (b0.astype(np.float32) ** 2))

    # máscara de pixels úteis (evita branco puro e linhas pretas)
    cand = (L >= v_min) & (L <= 250) & (chroma >= 2.0)

    ys, xs = np.where(cand)
    if len(xs) < 500:
        # quase nada: devolve labels vazios
        labels = np.full((img_bgr.shape[0], img_bgr.shape[1]), -1, dtype=np.int16)
        return labels, [], np.zeros_like(img_bgr)

    n = len(xs)
    take = min(sample, n)
    idx = np.random.choice(n, size=take, replace=False)

    # cluster em (a,b) apenas (melhor pra separar cores)
    data = np.stack([A[ys[idx], xs[idx]], B[ys[idx], xs[idx]]], axis=1).astype(np.float32)

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1.0)
    flags = cv2.KMEANS_PP_CENTERS
    _compactness, _labels_sample, centers = cv2.kmeans(data, k, None, criteria, 3, flags)

    centers = centers.astype(np.float32)  # (k,2) em [0..255]
    ca = centers[:, 0] - 128.0
    cb = centers[:, 1] - 128.0
    centers_chroma = np.sqrt(ca * ca + cb * cb)

    # seleciona clusters com chroma suficiente
    selected = [i for i in range(k) if float(centers_chroma[i]) >= float(chroma_min)]
    if not selected:
        selected = list(range(k))

    # atribuição de label pra imagem toda (só por a,b, sem custo absurdo)
    A_f = A.astype(np.float32)
    B_f = B.astype(np.float32)

    best = np.full(A_f.shape, np.inf, dtype=np.float32)
    labels = np.full(A_f.shape, -1, dtype=np.int16)

    for i in range(k):
        da = A_f - centers[i, 0]
        db = B_f - centers[i, 1]
        dist = (da * da) + (db * db)
        upd = dist < best
        labels[upd] = i
        best[upd] = dist[upd]

    # marca background como -1
    labels[~cand] = -1

    # preview: pinta clusters selecionados com cores dos próprios centros (aprox)
    preview = np.zeros_like(img_bgr)
    for i in selected:
        m = (labels == i)
        # cria cor em Lab com L fixo e A/B do centro (só pra debug visual)
        lab_color = np.zeros((1, 1, 3), dtype=np.uint8)
        lab_color[0, 0, 0] = 200  # L
        lab_color[0, 0, 1] = np.clip(int(centers[i, 0]), 0, 255)
        lab_color[0, 0, 2] = np.clip(int(centers[i, 1]), 0, 255)
        bgr_color = cv2.cvtColor(lab_color, cv2.COLOR_LAB2BGR)[0, 0].tolist()
        preview[m] = bgr_color

    return labels, selected, preview

def masks_to_polygons_from_kmeans(
    img_bgr_work: np.ndarray,
    p: SegmentParams,
    k: int,
    sample: int,
    chroma_min: float,
    scale_up: float,
    debug_dir: str = "",
) -> List[Union[Polygon, MultiPolygon]]:
    labels, selected, preview = kmeans_color_segmentation_masks(
        img_bgr_work, k=k, sample=sample, chroma_min=chroma_min, v_min=max(20, p.v_min)
    )

    if debug_dir:
        write_image(os.path.join(debug_dir, "kmeans_selected_preview.png"), preview)

    if len(selected) == 0:
        return []

    polys: List[Union[Polygon, MultiPolygon]] = []

    # processa cluster a cluster (separa cores adjacentes)
    for cid in selected:
        mask = (labels == cid).astype(np.uint8) * 255

        if p.open_ksize > 1:
            k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (p.open_ksize, p.open_ksize))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open, iterations=1)

        if p.close_ksize > 1:
            k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (p.close_ksize, p.close_ksize))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_close, iterations=p.close_iter)

        mask = fill_holes(mask)

        # se o cluster é minúsculo, pula
        if int(mask.sum() / 255) < p.min_area_px:
            continue

        cluster_polys = components_to_polygons(mask, p, scale_up=scale_up)
        polys.extend(cluster_polys)

    return polys


# -----------------------------
# Debug overlays
# -----------------------------

def _iter_polygons(g) -> Iterable[Polygon]:
    if g is None or getattr(g, "is_empty", False):
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    if isinstance(g, GeometryCollection):
        out = []
        for gg in g.geoms:
            if isinstance(gg, Polygon):
                out.append(gg)
            elif isinstance(gg, MultiPolygon):
                out.extend(list(gg.geoms))
        return out
    # fallback
    try:
        out = []
        for gg in g.geoms:
            out.extend(_iter_polygons(gg))
        return out
    except Exception:
        return []

def draw_polys_overlay(img_bgr: np.ndarray, geoms, color=(0, 0, 255), thickness=2) -> np.ndarray:
    out = img_bgr.copy()
    for g in geoms:
        for poly in _iter_polygons(g):
            pts = np.array([(int(x), int(y)) for x, y in poly.exterior.coords], dtype=np.int32)
            if len(pts) >= 3:
                cv2.polylines(out, [pts], isClosed=True, color=color, thickness=thickness)
    return out


# -----------------------------
# GCP / Affine
# -----------------------------

def read_gcps_csv(path: str) -> List[Tuple[float, float, float, float]]:
    gcps = []
    with open(path, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        required = {"pixel_x", "pixel_y", "x", "y"}
        if not required.issubset(set(rd.fieldnames or [])):
            raise ValueError(f"GCP CSV precisa ter colunas {sorted(required)}")
        for row in rd:
            gcps.append((float(row["pixel_x"]), float(row["pixel_y"]), float(row["x"]), float(row["y"])))
    if len(gcps) < 3:
        raise ValueError("Precisa de pelo menos 3 GCPs para uma transformação afim.")
    return gcps

def solve_affine_from_gcps(gcps: List[Tuple[float, float, float, float]]) -> np.ndarray:
    A = []
    bx = []
    by = []
    for px, py, X, Y in gcps:
        A.append([px, py, 1.0])
        bx.append(X)
        by.append(Y)
    A = np.array(A, dtype=np.float64)
    bx = np.array(bx, dtype=np.float64)
    by = np.array(by, dtype=np.float64)

    solx, *_ = np.linalg.lstsq(A, bx, rcond=None)
    soly, *_ = np.linalg.lstsq(A, by, rcond=None)
    return np.vstack([solx, soly])  # 2x3

def apply_affine_to_coords(coords: List[Tuple[float, float]], M: np.ndarray) -> List[Tuple[float, float]]:
    out = []
    for px, py in coords:
        X = M[0, 0] * px + M[0, 1] * py + M[0, 2]
        Y = M[1, 0] * px + M[1, 1] * py + M[1, 2]
        out.append((float(X), float(Y)))
    return out

def transform_polygon_affine(poly: Polygon, M: np.ndarray) -> Polygon:
    ext = list(poly.exterior.coords)
    ext2 = apply_affine_to_coords([(x, y) for x, y in ext], M)

    holes2 = []
    for ring in poly.interiors:
        coords = list(ring.coords)
        holes2.append(apply_affine_to_coords([(x, y) for x, y in coords], M))

    out = Polygon(ext2, holes2)
    if not out.is_valid:
        out = out.buffer(0)
    return out

def apply_offset_to_geom(g, dx: float, dy: float):
    if isinstance(g, Polygon):
        return Polygon([(x + dx, y + dy) for x, y in g.exterior.coords])
    if isinstance(g, MultiPolygon):
        return MultiPolygon([Polygon([(x + dx, y + dy) for x, y in p.exterior.coords]) for p in g.geoms])
    return g


# -----------------------------
# Export GeoJSON
# -----------------------------

def export_geojson(geoms: List[Union[Polygon, MultiPolygon]], out_path: str, epsg: Optional[int] = None) -> None:
    feats = []
    idx = 1
    for g in geoms:
        if g is None or g.is_empty:
            continue
        feats.append({"type": "Feature", "properties": {"id": idx}, "geometry": mapping(g)})
        idx += 1

    fc = {"type": "FeatureCollection", "features": feats}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)

    if epsg is not None:
        with open(out_path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump({"epsg": epsg}, f, ensure_ascii=False)


# -----------------------------
# Main
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()

    ap.add_argument("--pdf", required=True, help="PDF de entrada (planta/mapa)")
    ap.add_argument("--page", type=int, default=0, help="Índice da página (0-based)")
    ap.add_argument("--dpi", type=int, default=300, help="DPI de renderização")
    ap.add_argument("--crop", default="auto-color", help="auto | nonwhite | auto-color | 'x0,y0,x1,y1'")
    ap.add_argument("--out", default="out.geojson", help="GeoJSON de saída")
    ap.add_argument("--debug-dir", default="debug", help="Pasta para PNGs de debug (vazio desativa)")

    # segmentação
    ap.add_argument("--segment", choices=["kmeans", "hsv"], default="kmeans", help="Estratégia de segmentação")
    ap.add_argument("--max-side", type=int, default=2600, help="Tamanho máximo (px) do lado maior no modo de trabalho")
    ap.add_argument("--k", type=int, default=14, help="Número de clusters (kmeans)")
    ap.add_argument("--sample", type=int, default=90000, help="Amostra de pixels para treinar kmeans")
    ap.add_argument("--chroma-min", type=float, default=12.0, help="Chroma mínimo do cluster para ser considerado 'cor'")

    # knobs HSV (também usados como base pro kmeans)
    ap.add_argument("--s-min", type=int, default=25)
    ap.add_argument("--v-min", type=int, default=45)
    ap.add_argument("--v-max", type=int, default=255)

    # morfologia e filtros
    ap.add_argument("--open-ksize", type=int, default=3)
    ap.add_argument("--close-ksize", type=int, default=5)
    ap.add_argument("--close-iter", type=int, default=2)
    ap.add_argument("--min-area-px", type=int, default=1200)
    ap.add_argument("--compactness-min", type=float, default=0.02)
    ap.add_argument("--simplify-tol", type=float, default=1.5)
    ap.add_argument("--approx-eps", type=float, default=2.0)

    # georef
    ap.add_argument("--gcp", default="", help="CSV de GCPs (pixel_x,pixel_y,x,y) para georreferenciar")
    ap.add_argument("--epsg", type=int, default=0, help="EPSG do output quando georreferenciar (ex: 31983)")

    args = ap.parse_args()
    ensure_dir(args.debug_dir)

    # 1) Render PDF
    img = render_pdf_page_to_bgr(args.pdf, page_index=args.page, dpi=args.dpi)
    if args.debug_dir:
        write_image(os.path.join(args.debug_dir, "render.png"), img)

    # 2) Crop
    img_crop, (x0, y0, x1, y1) = crop_from_arg(img, args.crop)
    crop_info = (int(x0), int(y0), int(x1), int(y1))

    if args.debug_dir:
        write_image(os.path.join(args.debug_dir, "crop.png"), img_crop)
        with open(os.path.join(args.debug_dir, "crop_box.json"), "w", encoding="utf-8") as f:
            json.dump({"crop_box": crop_info}, f)

    # 3) Prepare work image (downscale)
    img_work, scale_up = maybe_downscale(img_crop, max_side=args.max_side)
    if args.debug_dir and scale_up != 1.0:
        write_image(os.path.join(args.debug_dir, "work_resized.png"), img_work)

    p = SegmentParams(
        s_min=args.s_min,
        v_min=args.v_min,
        v_max=args.v_max,
        open_ksize=args.open_ksize,
        close_ksize=args.close_ksize,
        close_iter=args.close_iter,
        min_area_px=args.min_area_px,
        simplify_tol=args.simplify_tol,
        approx_eps=args.approx_eps,
        compactness_min=args.compactness_min,
    )

    geoms: List[Union[Polygon, MultiPolygon]] = []

    # 4) Segmentação
    if args.segment == "hsv":
        mask = build_hsv_color_mask(img_work, p)
        if args.debug_dir:
            write_image(os.path.join(args.debug_dir, "mask.png"), cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR))
        geoms = components_to_polygons(mask, p, scale_up=scale_up)
    else:
        geoms = masks_to_polygons_from_kmeans(
            img_work,
            p=p,
            k=args.k,
            sample=args.sample,
            chroma_min=args.chroma_min,
            scale_up=scale_up,
            debug_dir=args.debug_dir,
        )
        if args.debug_dir:
            # máscara agregada só pra inspeção
            # (gera uma visão geral aproximada dos pixels "selecionados" pelo kmeans)
            lab = cv2.cvtColor(img_work, cv2.COLOR_BGR2LAB)
            L, A, B = cv2.split(lab)
            a0 = (A.astype(np.int16) - 128).astype(np.int16)
            b0 = (B.astype(np.int16) - 128).astype(np.int16)
            chroma = np.sqrt((a0.astype(np.float32) ** 2) + (b0.astype(np.float32) ** 2))
            overview = ((chroma >= 2.0) & (L >= max(20, p.v_min)) & (L <= 250)).astype(np.uint8) * 255
            write_image(os.path.join(args.debug_dir, "mask_overview.png"), cv2.cvtColor(overview, cv2.COLOR_GRAY2BGR))

    # 5) Overlay debug
    if args.debug_dir:
        overlay = draw_polys_overlay(img_crop, geoms, color=(0, 0, 255), thickness=2)
        write_image(os.path.join(args.debug_dir, "polys_overlay.png"), overlay)

    # 6) Georreferenciar (opcional)
    epsg = args.epsg if args.epsg > 0 else None
    if args.gcp:
        gcps = read_gcps_csv(args.gcp)
        M = solve_affine_from_gcps(gcps)

        out_geo: List[Union[Polygon, MultiPolygon]] = []
        for g in geoms:
            for poly in _iter_polygons(g):
                # offset do crop (para coordenadas no espaço do render original)
                shifted = Polygon([(x + crop_info[0], y + crop_info[1]) for x, y in poly.exterior.coords])
                if not shifted.is_valid:
                    shifted = shifted.buffer(0)

                gg = transform_polygon_affine(shifted, M)
                if not gg.is_empty and gg.area > 0:
                    out_geo.append(gg)

        geoms = out_geo

        if args.debug_dir:
            np.savetxt(os.path.join(args.debug_dir, "affine_matrix_2x3.txt"), M, fmt="%.8f")

    # 7) Export GeoJSON
    export_geojson(geoms, args.out, epsg=epsg)

    print(f"OK: exportado {len(geoms)} geometrias em {args.out}")
    if args.debug_dir:
        print(f"Debug em: {args.debug_dir}/ (render.png, crop.png, polys_overlay.png, kmeans_selected_preview.png, mask_overview.png)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())