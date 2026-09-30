#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_map_to_vectors.py
---------------------
Pipeline genérico para transformar uma planta em PDF (mapa) em uma camada vetorial (GeoJSON),
extraindo polígonos a partir de regiões coloridas.

Principais features (pra portfólio):
- Renderização de PDF -> imagem (PyMuPDF/fitz)
- Pré-processamento de imagem (OpenCV)
- Segmentação de regiões (HSV + morfologia)
- Vetorização: contornos -> polígonos (Shapely)
- (Opcional) Georreferenciamento via GCP (pixel -> coordenadas projetadas) com transformação afim
- Export GeoJSON + debug PNGs

Requisitos:
  pip install pymupdf opencv-python shapely numpy

Opcional (apenas se quiser exportar GPKG):
  pip install geopandas pyproj

Uso rápido (sem georef, sai em "pixel space"):
  python3 pdf_map_to_vectors.py --pdf plana.pdf --out out.geojson --debug-dir debug

Com georreferenciamento por GCP:
  # 1) gere template de GCP
  python3 pdf_map_to_vectors.py --make-gcp-template gcps.csv

  # 2) preencha gcps.csv com 3+ pontos (pixel_x, pixel_y, x, y)
  # 3) rode com --gcp e --epsg
  python3 pdf_map_to_vectors.py --pdf plana.pdf --out out.geojson \
    --gcp gcps.csv --epsg 31983 --debug-dir debug

Dica prática para pegar pixel_x/pixel_y:
- Abra debug/render.png em um viewer que mostre coordenadas do cursor (GIMP/Inkscape),
  ou use o modo "--show-click-helper" (matplotlib) se tiver ambiente gráfico.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

# Dependências principais
import cv2
import fitz  # PyMuPDF
from shapely.geometry import Polygon, MultiPolygon, mapping
from shapely.ops import unary_union


# -----------------------------
# Utilidades
# -----------------------------

def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def write_image(path: str, img_bgr: np.ndarray) -> None:
    cv2.imwrite(path, img_bgr)


def bgr_to_rgb(img_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)


def rgb_to_bgr(img_rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)


# -----------------------------
# Renderização PDF -> imagem
# -----------------------------

def render_pdf_page_to_bgr(pdf_path: str, page_index: int = 0, dpi: int = 300) -> np.ndarray:
    doc = fitz.open(pdf_path)
    if page_index < 0 or page_index >= len(doc):
        raise ValueError(f"page_index inválido: {page_index} (pdf tem {len(doc)} páginas)")

    page = doc[page_index]

    # 72 dpi é a base do PDF. Escala = dpi/72.
    scale = dpi / 72.0
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)

    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]

    img_rgb = img  # PyMuPDF entrega em RGB
    img_bgr = rgb_to_bgr(img_rgb)
    return img_bgr


# -----------------------------
# Auto-crop (opcional)
# -----------------------------

def auto_crop_nonwhite(img_bgr: np.ndarray, white_thresh: int = 245, pad: int = 10) -> Tuple[np.ndarray, Tuple[int,int,int,int]]:
    """
    Corta a imagem para a bounding box do que NÃO é quase-branco.
    Retorna (crop_img, (x0,y0,x1,y1)).
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # máscara do que é "conteúdo" (não branco)
    mask = (gray < white_thresh).astype(np.uint8) * 255

    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        # nada detectado; retorna original
        h, w = gray.shape[:2]
        return img_bgr, (0, 0, w, h)

    x0, x1 = xs.min(), xs.max()
    y0, y1 = ys.min(), ys.max()

    h, w = gray.shape[:2]
    x0 = max(0, x0 - pad)
    y0 = max(0, y0 - pad)
    x1 = min(w - 1, x1 + pad)
    y1 = min(h - 1, y1 + pad)

    crop = img_bgr[y0:y1+1, x0:x1+1].copy()
    return crop, (x0, y0, x1, y1)


def parse_crop_arg(crop: str) -> Optional[Tuple[int,int,int,int]]:
    """
    crop = "x0,y0,x1,y1" ou "auto" ou "".
    """
    if not crop or crop.lower() == "auto":
        return None
    parts = [p.strip() for p in crop.split(",")]
    if len(parts) != 4:
        raise ValueError("Crop inválido. Use 'auto' ou 'x0,y0,x1,y1'")
    return tuple(int(p) for p in parts)  # type: ignore


# -----------------------------
# Segmentação e vetorização
# -----------------------------

@dataclass
class SegmentParams:
    s_min: int = 35      # saturação mínima (remove cinza)
    v_min: int = 55      # valor mínimo (remove preto)
    v_max: int = 245     # remove branco estourado
    open_ksize: int = 3  # morfologia para remover ruído
    close_ksize: int = 7 # morfologia para fechar buracos
    min_area_px: int = 1200  # descarta componentes pequenos
    simplify_tol: float = 1.5 # simplificação em pixels
    approx_eps: float = 2.0   # aproximação do contorno


def build_color_region_mask(img_bgr: np.ndarray, p: SegmentParams) -> np.ndarray:
    """
    Máscara binária para regiões "coloridas" (boa para plantas com polígonos coloridos).
    Exclui tons cinza (baixa saturação), linhas pretas (baixo V) e branco (alto V).
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)

    mask = (
        (s >= p.s_min) &
        (v >= p.v_min) &
        (v <= p.v_max)
    ).astype(np.uint8) * 255

    # Morfologia
    if p.open_ksize > 1:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (p.open_ksize, p.open_ksize))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)

    if p.close_ksize > 1:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (p.close_ksize, p.close_ksize))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)

    return mask


def components_to_polygons(mask: np.ndarray, p: SegmentParams) -> List[Polygon]:
    """
    Converte componentes conectados da máscara em polígonos shapely.
    """
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    polys: List[Polygon] = []

    for label_id in range(1, num_labels):
        area = int(stats[label_id, cv2.CC_STAT_AREA])
        if area < p.min_area_px:
            continue

        comp = (labels == label_id).astype(np.uint8) * 255

        contours, _hier = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue

        # Pega o maior contorno do componente
        cnt = max(contours, key=cv2.contourArea)
        if cv2.contourArea(cnt) < p.min_area_px:
            continue

        # Aproxima contorno
        eps = p.approx_eps
        approx = cv2.approxPolyDP(cnt, epsilon=eps, closed=True)

        coords = [(float(pt[0][0]), float(pt[0][1])) for pt in approx]
        if len(coords) < 3:
            continue

        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)

        if poly.is_empty or (poly.area <= 0):
            continue

        # Simplifica (em pixels)
        if p.simplify_tol and p.simplify_tol > 0:
            poly = poly.simplify(p.simplify_tol, preserve_topology=True)

        if poly.is_empty or (poly.area <= 0):
            continue

        polys.append(poly)

    return polys


# -----------------------------
# Georreferenciamento por GCP (transformação afim)
# -----------------------------

def read_gcps_csv(path: str) -> List[Tuple[float,float,float,float]]:
    """
    CSV com colunas: pixel_x, pixel_y, x, y
    """
    gcps = []
    with open(path, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        required = {"pixel_x", "pixel_y", "x", "y"}
        if not required.issubset(set(rd.fieldnames or [])):
            raise ValueError(f"GCP CSV precisa ter colunas {sorted(required)}")
        for row in rd:
            gcps.append((
                float(row["pixel_x"]),
                float(row["pixel_y"]),
                float(row["x"]),
                float(row["y"]),
            ))
    if len(gcps) < 3:
        raise ValueError("Precisa de pelo menos 3 GCPs para uma transformação afim.")
    return gcps


def solve_affine_from_gcps(gcps: List[Tuple[float,float,float,float]]) -> np.ndarray:
    """
    Resolve:
      [X]   [a b c] [px]
      [Y] = [d e f] [py]
                    [1 ]
    via mínimos quadrados (3+ pontos).
    Retorna matriz 2x3.
    """
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

    # least squares
    solx, *_ = np.linalg.lstsq(A, bx, rcond=None)
    soly, *_ = np.linalg.lstsq(A, by, rcond=None)

    M = np.vstack([solx, soly])  # 2x3
    return M


def apply_affine_to_coords(coords: List[Tuple[float,float]], M: np.ndarray) -> List[Tuple[float,float]]:
    out = []
    for px, py in coords:
        X = M[0,0]*px + M[0,1]*py + M[0,2]
        Y = M[1,0]*px + M[1,1]*py + M[1,2]
        out.append((float(X), float(Y)))
    return out


def transform_polygon_affine(poly: Polygon, M: np.ndarray) -> Polygon:
    ext = list(poly.exterior.coords)
    ext2 = apply_affine_to_coords([(x,y) for x,y in ext], M)

    holes2 = []
    for ring in poly.interiors:
        coords = list(ring.coords)
        holes2.append(apply_affine_to_coords([(x,y) for x,y in coords], M))

    out = Polygon(ext2, holes2)
    if not out.is_valid:
        out = out.buffer(0)
    return out


# -----------------------------
# Export GeoJSON
# -----------------------------

def export_geojson(polys: List[Polygon], out_path: str, epsg: Optional[int] = None) -> None:
    features = []
    for i, poly in enumerate(polys, start=1):
        if poly.is_empty:
            continue
        geom = mapping(poly)
        props = {"id": i}
        features.append({"type": "Feature", "properties": props, "geometry": geom})

    fc = {"type": "FeatureCollection", "features": features}

    # GeoJSON "crs" é deprecated na spec, mas muita gente gosta de ter um sidecar.
    # Então: gravamos um .meta.json com EPSG quando informado.
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)

    if epsg is not None:
        meta_path = out_path + ".meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"epsg": epsg}, f, ensure_ascii=False)


# -----------------------------
# Helper: template de GCP
# -----------------------------

def make_gcp_template(path: str) -> None:
    ensure_dir(os.path.dirname(path) or "")
    with open(path, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["pixel_x", "pixel_y", "x", "y"])
        # linhas exemplo (substitua):
        wr.writerow([1000, 1200, 250000.0, 7520000.0])
        wr.writerow([2000, 1300, 251000.0, 7520100.0])
        wr.writerow([1500, 2200, 250500.0, 7519000.0])


# -----------------------------
# Debug overlays
# -----------------------------

def draw_polys_overlay(img_bgr: np.ndarray, polys, color=(0, 0, 255), thickness=2) -> np.ndarray:
    out = img_bgr.copy()

    def draw_one(poly: Polygon):
        if poly.is_empty:
            return
        pts = np.array([(int(x), int(y)) for x, y in poly.exterior.coords], dtype=np.int32)
        if len(pts) >= 3:
            cv2.polylines(out, [pts], isClosed=True, color=color, thickness=thickness)

        # desenha buracos (opcional, ajuda debug)
        for ring in poly.interiors:
            pts_hole = np.array([(int(x), int(y)) for x, y in ring.coords], dtype=np.int32)
            if len(pts_hole) >= 3:
                cv2.polylines(out, [pts_hole], isClosed=True, color=(255, 0, 0), thickness=1)

    for g in polys:
        if g is None or g.is_empty:
            continue
        if isinstance(g, Polygon):
            draw_one(g)
        elif isinstance(g, MultiPolygon):
            for part in g.geoms:
                draw_one(part)
        else:
            # caso apareça GeometryCollection etc.
            try:
                for part in g.geoms:
                    if isinstance(part, Polygon):
                        draw_one(part)
            except Exception:
                pass

    return out


# -----------------------------
# Main
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="PDF de entrada (planta/mapa)")
    ap.add_argument("--page", type=int, default=0, help="Índice da página (0-based)")
    ap.add_argument("--dpi", type=int, default=300, help="DPI de renderização")
    ap.add_argument("--crop", default="auto", help="auto ou 'x0,y0,x1,y1' em pixels")
    ap.add_argument("--out", default="out.geojson", help="GeoJSON de saída")

    ap.add_argument("--debug-dir", default="debug", help="Pasta para PNGs de debug (vazio desativa)")
    ap.add_argument("--gcp", default="", help="CSV de GCPs (pixel_x,pixel_y,x,y) para georreferenciar")
    ap.add_argument("--epsg", type=int, default=0, help="EPSG do output quando georreferenciar (ex: 31983)")

    # knobs de segmentação
    ap.add_argument("--s-min", type=int, default=35)
    ap.add_argument("--v-min", type=int, default=55)
    ap.add_argument("--v-max", type=int, default=245)
    ap.add_argument("--open-ksize", type=int, default=3)
    ap.add_argument("--close-ksize", type=int, default=7)
    ap.add_argument("--min-area-px", type=int, default=1200)
    ap.add_argument("--simplify-tol", type=float, default=1.5)
    ap.add_argument("--approx-eps", type=float, default=2.0)

    ap.add_argument("--make-gcp-template", default="", help="Gera um CSV template de GCP e sai")

    args = ap.parse_args()

    if args.make_gcp_template:
        make_gcp_template(args.make_gcp_template)
        print(f"Template de GCP criado em: {args.make_gcp_template}")
        return 0

    if not args.pdf:
        ap.error("--pdf é obrigatório (ou use --make-gcp-template)")

    ensure_dir(args.debug_dir)

    # 1) Render PDF
    img = render_pdf_page_to_bgr(args.pdf, page_index=args.page, dpi=args.dpi)
    if args.debug_dir:
        write_image(os.path.join(args.debug_dir, "render.png"), img)

    # 2) Crop
    crop_tuple = parse_crop_arg(args.crop)
    if crop_tuple is None:
        img_crop, (x0, y0, x1, y1) = auto_crop_nonwhite(img, white_thresh=245, pad=10)
        crop_info = tuple(int(v) for v in (x0, y0, x1, y1))
    else:
        x0, y0, x1, y1 = crop_tuple
        img_crop = img[y0:y1, x0:x1].copy()
        crop_info = (x0, y0, x1, y1)

    if args.debug_dir:
        write_image(os.path.join(args.debug_dir, "crop.png"), img_crop)
        with open(os.path.join(args.debug_dir, "crop_box.json"), "w", encoding="utf-8") as f:
            json.dump({"crop_box": crop_info}, f)

    # 3) Segmentação (máscara de regiões coloridas)
    p = SegmentParams(
        s_min=args.s_min,
        v_min=args.v_min,
        v_max=args.v_max,
        open_ksize=args.open_ksize,
        close_ksize=args.close_ksize,
        min_area_px=args.min_area_px,
        simplify_tol=args.simplify_tol,
        approx_eps=args.approx_eps,
    )
    mask = build_color_region_mask(img_crop, p)
    if args.debug_dir:
        write_image(os.path.join(args.debug_dir, "mask.png"), cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR))

    # 4) Vetorização
    polys = components_to_polygons(mask, p)

    if args.debug_dir:
        overlay = draw_polys_overlay(img_crop, polys, color=(0, 0, 255), thickness=2)
        write_image(os.path.join(args.debug_dir, "polys_overlay.png"), overlay)

    # 5) (Opcional) Georreferenciar por GCP
    epsg = args.epsg if args.epsg > 0 else None
    if args.gcp:
        gcps = read_gcps_csv(args.gcp)
        M = solve_affine_from_gcps(gcps)

        # Como recortamos a imagem, precisamos compensar o offset do crop:
        # pixels no GeoJSON devem considerar o pixel original.
        # Aqui, aplicamos o offset (x0, y0) antes do affine.
        x0, y0, _, _ = crop_info

        polys_geo: List[Polygon] = []
        for poly in polys:
            # aplica offset do crop
            shifted = Polygon([(x + x0, y + y0) for x, y in poly.exterior.coords])
            if not shifted.is_valid:
                shifted = shifted.buffer(0)

            # aplica affine
            g = transform_polygon_affine(shifted, M)
            if not g.is_empty and g.area > 0:
                polys_geo.append(g)

        polys = polys_geo

        if args.debug_dir:
            # salva matriz
            np.savetxt(os.path.join(args.debug_dir, "affine_matrix_2x3.txt"), M, fmt="%.8f")

    # 6) Export GeoJSON
    export_geojson(polys, args.out, epsg=epsg)

    print(f"OK: exportado {len(polys)} polígonos em {args.out}")
    if args.debug_dir:
        print(f"Debug em: {args.debug_dir}/ (render.png, crop.png, mask.png, polys_overlay.png)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
