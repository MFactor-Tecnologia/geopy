# geopy

Estudo de vetorização de mapas temáticos com visão computacional. Transforma uma planta em PDF (por exemplo, a divisão de bairros de um plano diretor) em uma camada vetorial GeoJSON, extraindo polígonos a partir das regiões coloridas.

O caso de estudo é a **Planta da Divisão de Bairros (Anexo IX) do Plano Diretor de Leme/SP**, de 2018, incluída em [`plana-bairros.pdf`](plana-bairros.pdf).

![Planta original](docs/planta_original.jpg)

## Pipeline

1. **Renderização** da página a 300 DPI com PyMuPDF.
2. **Recorte** automático da área de interesse.
3. **Segmentação por cor**, em uma de duas estratégias:
   - `hsv`: limiar de saturação e valor, que gera uma máscara única com todas as cores;
   - `kmeans` (padrão): k-means nos canais a\*/b\* do espaço CIELAB, isolando a crominância da luminância. Cada cluster é processado separadamente, o que evita fundir bairros vizinhos de cores diferentes.
4. **Limpeza**: abertura e fechamento morfológicos, preenchimento de buracos, área mínima e filtro de compacidade (4πA/P²), que descarta feições lineares como rodovias, drenagem e curvas de nível.
5. **Vetorização**: contornos com OpenCV, simplificação por Douglas-Peucker e correção de geometrias com Shapely.
6. **Georreferenciamento** (opcional) por pontos de controle (GCPs), com transformação afim ajustada por mínimos quadrados.
7. **Exportação** em GeoJSON.

## Scripts

| Script | Descrição |
|---|---|
| [`pdf_map_to_vectors.py`](pdf_map_to_vectors.py) | Primeira versão: segmentação por limiar HSV em resolução cheia. |
| [`pdf_map_to_vectors_upgrade.py`](pdf_map_to_vectors_upgrade.py) | Versão atual: k-means em Lab, recorte por cor, imagem de trabalho reduzida e filtro de compacidade. |

## Estrutura

```
geopy/
├── pdf_map_to_vectors.py           # v1: segmentação HSV
├── pdf_map_to_vectors_upgrade.py   # versão atual: k-means em Lab
├── plana-bairros.pdf               # planta de origem (caso de estudo)
├── bairros_pixel.geojson           # resultado de exemplo (48 polígonos)
├── docs/                           # imagens usadas neste README
└── requirements.txt
```

## Instalação

Requer Python 3.10 ou superior.

```bash
git clone git@github.com:alexandremartinx/geopy.git
cd geopy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso

```bash
# Coordenadas em pixels (sem georreferenciamento)
.venv/bin/python pdf_map_to_vectors_upgrade.py --pdf plana-bairros.pdf --out bairros_pixel.geojson --debug-dir debug

# Segmentação por HSV
.venv/bin/python pdf_map_to_vectors_upgrade.py --pdf plana-bairros.pdf --segment hsv --out out.geojson

# Com georreferenciamento: CSV com colunas pixel_x,pixel_y,x,y (3 ou mais pontos)
.venv/bin/python pdf_map_to_vectors_upgrade.py --pdf plana-bairros.pdf --gcp gcps.csv --epsg 31983 --out bairros.geojson
```

Os valores `pixel_x` e `pixel_y` dos GCPs devem ser marcados em `debug/render.png`, a página inteira. O script compensa sozinho o deslocamento do recorte.

Principais parâmetros: `--k` (número de clusters), `--chroma-min`, `--min-area-px`, `--close-ksize`, `--compactness-min` e `--max-side`. A lista completa sai com `--help`.

## Artefatos de debug

Com `--debug-dir`, cada etapa grava um artefato para inspeção:

| Arquivo | Conteúdo |
|---|---|
| `render.png` | Página renderizada |
| `crop.png` / `crop_box.json` | Área recortada e suas coordenadas no render |
| `work_resized.png` | Imagem reduzida em que a segmentação é executada |
| `mask_overview.png` | Pixels com crominância mínima (só para inspeção) |
| `kmeans_selected_preview.png` | Clusters selecionados, pintados com a cor do centro |
| `mask.png` | Máscara binária (somente no modo `hsv`) |
| `polys_overlay.png` | Contornos dos polígonos sobre o recorte |
| `affine_matrix_2x3.txt` | Matriz afim (somente com `--gcp`) |

![Clusters do k-means](docs/clusters_kmeans.jpg)

## Resultados

O arquivo [`bairros_pixel.geojson`](bairros_pixel.geojson) contém **48 polígonos** extraídos no modo `kmeans`, em coordenadas de pixel do recorte e com o eixo Y para baixo.

![Polígonos extraídos](docs/poligonos_extraidos.jpg)

O resultado é parcial:

- **Bairros desenhados como grade de quadras não foram capturados.** O arruamento em branco fragmenta a mancha de cor em partes menores que a área mínima.
- **Bairros adjacentes de mesma cor foram unidos** em um único polígono. Em um mapa temático, a cor identifica uma classe, não um objeto.
- **O recorte por cor não isolou o mapa**, porque rosa dos ventos, brasão e logotipos também são coloridos.
- **Os polígonos não têm nome** nem estão georreferenciados.
- **O k-means não usa semente fixa**, então o resultado pode variar entre execuções.

## Próximos passos

- Ajustar a morfologia para transpor o arruamento entre quadras.
- Associar os nomes dos bairros a partir do texto extraível do PDF (`page.get_text("words")`), via ponto-em-polígono.
- Georreferenciar usando a grade de coordenadas UTM impressa na prancha.
- Avaliar a leitura direta dos vetores do PDF, quando o arquivo os preserva.

## Stack

Python, PyMuPDF, OpenCV, NumPy e Shapely.

## Fonte dos dados

Prefeitura Municipal de Leme/SP, Plano Diretor do Município, Anexo IX – Planta da Divisão de Bairros (setembro de 2018).
