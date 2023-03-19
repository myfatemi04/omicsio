from dataclasses import dataclass
from functools import cached_property
from typing import Optional

import matplotlib
import numpy as np
import os
import PIL.Image
import torch
import torchvision.transforms.functional as TF


@dataclass
class SpotLocations:
    image_x: torch.Tensor
    """
    x pixel locations of capture spots.
    """

    image_y: torch.Tensor
    """
    y pixel locations of capture spots.
    """

    row: torch.Tensor
    """
    row of capture spots in hexagonal array.
    """
    
    col: torch.Tensor
    """
    column of capture spots in hexagonal array.
    """

    dia: torch.Tensor
    """
    spot diameters in pixels.
    """

    def __mul__(self, scale_factor: float):
        if self.dia is None:
            dia = None
        else:
            dia = self.dia * scale_factor
        return SpotLocations(
            self.image_x * scale_factor,
            self.image_y * scale_factor,
            self.row,
            self.col,
            dia,
        )
    
    def __div__(self, dividend: float):
        if self.dia is None:
            dia = None
        else:
            dia = self.dia / dividend
        return SpotLocations(
            self.image_x / dividend,
            self.image_y / dividend,
            self.row,
            self.col,
            dia,
        )
    
    def select_subset(self, mask):
        return SpotLocations(
            self.image_x[mask],
            self.image_y[mask],
            self.row[mask],
            self.col[mask],
            self.dia[mask] if self.dia is not None else None,
        )

    def __len__(self):
        return self.image_x.shape[0]

# Used to prevent using too much memory
_image_cache = {}

class Slide:
    def __init__(self, image_path: str, spot_locations: SpotLocations, spot_counts: torch.Tensor, genes: list):
        self.image_path = image_path
        self.spot_locations = spot_locations
        self.spot_counts = spot_counts
        self.genes = genes
    
    def get_quadrant_masks(self):
        spot_locations = self.spot_locations
        row = spot_locations.row
        col = spot_locations.col
        max_row = row.max()
        max_col = col.max()
        min_row = row.min()
        min_col = col.min()
        mid_row = torch.div(max_row + min_row, 2, rounding_mode="floor")
        mid_col = torch.div(max_col + min_col, 2, rounding_mode="floor")

        top_left = (row <= mid_row) & (col <= mid_col)
        top_right = (row <= mid_row) & (col > mid_col)
        bottom_left = (row > mid_row) & (col <= mid_col)
        bottom_right = (row > mid_row) & (col > mid_col)

        return top_left, top_right, bottom_left, bottom_right

    # split into quadrants (and their complements)
    def create_quadrants(self):
        top_left, top_right, bottom_left, bottom_right = self.get_quadrant_masks()

        select_subset = lambda mask: Slide(
            image_path=self.image_path,
            spot_locations=self.spot_locations.select_subset(mask),
            spot_counts=self.spot_counts[mask],
            genes=self.genes
        )

        return (
            (select_subset(top_left), select_subset(~top_left)),
            (select_subset(top_right), select_subset(~top_right)),
            (select_subset(bottom_left), select_subset(~bottom_left)),
            (select_subset(bottom_right), select_subset(~bottom_right)),
        )

    @cached_property
    def image(self):
        import PIL.Image
        import torchvision.transforms.functional as TF

        # Resolve possibly relative path with os
        CACHE_KEY = os.path.abspath(self.image_path)

        if CACHE_KEY in _image_cache:
            return _image_cache[CACHE_KEY]

        if self.image_path.endswith(".svs"):
            import openslide
            
            image = openslide.OpenSlide(self.image_path)
        else:
            pil_image = PIL.Image.open(self.image_path)
            image = TF.to_tensor(pil_image)

        _image_cache[CACHE_KEY] = image

        return image
    
    def image_region(self, x, y, w, h):
        """
        Loads only one section of the image; useful for large files (like .svs)
        """

        if isinstance(self.image, torch.Tensor):
            return self.image[y:y + h, x:x + h]
        else:
            import openslide
            
            assert isinstance(self.image, openslide.OpenSlide), "Image must be either a Torch tensor or an openslide.OpenSlide object."

            pil_image_region = self.image.read_region((x, y), 0, (w, h))
            image_region = TF.to_tensor(pil_image_region)
            return image_region

    @staticmethod
    def load(path: str, custom_image_path: Optional[str]) -> 'Slide':
        import pickle

        with open(path, "rb") as f:
            result = pickle.load(f)

        if custom_image_path is not None:
            result.image_path = custom_image_path

    def save(self, path: str):
        import pickle

        with open(path, "wb") as f:
            pickle.dump(self, f)

    def log1p(self):
        return Slide(
            self.image_path,
            self.spot_locations,
            self.spot_counts.log1p(),
            self.genes,
        )
    
    def binary(self):
        return Slide(
            self.image_path,
            self.spot_locations,
            (self.spot_counts > self.spot_counts.median(dim=0)[0]).float(),
            self.genes,
        )

    def select_genes(self, genes, suppress_errors=False):
        indexes = []
        valid_genes = []
        for gene in genes:
            if gene not in self.genes:
                if suppress_errors:
                    print(f"WARNING: Gene {gene} not found in slide {self.image_path=}.")
                    continue
                
                raise ValueError(f"Gene {gene} not found in slide {self.image_path=}.")
            else:
                indexes.append(self.genes.index(gene))
                valid_genes.append(gene)
        
        return Slide(
            self.image_path,
            self.spot_locations,
            self.spot_counts[:, indexes],
            valid_genes,
        )
    
    def render(self, downsample, spot_counts, spot_size, cmap=matplotlib.colormaps['inferno']) -> PIL.Image.Image:
        """
        image: (3, height, width)
        spot_locations: SpotLocations
        spot_intensities: (num_spots,) -- intensities for *one* gene
        spot_size: int -- size of square to draw around each spot
        cmap: Maps intensity to RGB
        """
        spot_size_adjusted = torch.div(spot_size, downsample, rounding_mode="floor")
        new_image = self.image[:, ::downsample, ::downsample].clone()
        for spot in range(self.spot_locations.image_x.shape[0]):
            image_x = int((self.spot_locations.image_x[spot] - spot_size / 2) / downsample)
            image_y = int((self.spot_locations.image_y[spot] - spot_size / 2) / downsample)
            intensity = spot_counts[spot].item()
            r, g, b, a = cmap(intensity)

            gray = new_image[:, image_y:image_y + spot_size_adjusted, image_x:image_x + spot_size_adjusted].mean(dim=0)

            # (3, spot_size, spot_size)
            new_image[:, image_y:image_y + spot_size_adjusted, image_x:image_x + spot_size_adjusted] = \
                torch.stack([gray * (1 - a) + r * a, gray * (1 - a) + g * a, gray * (1 - a) + b * a])

        numpy_array = np.array(new_image.cpu().permute(1, 2, 0) * 255, dtype=np.uint8)
        pil_image = PIL.Image.fromarray(numpy_array)
        return pil_image

    # Utility methods that might add too many dependencies
    # @overload
    # def spatialde(self) -> pd.DataFrame: ...
    # @overload
    # def spatialde(self, spot_counts: torch.Tensor) -> pd.DataFrame: ...
    # def spatialde(self, spot_counts=None):
    #     import spatialde

    #     if spot_counts is None:
    #         spot_counts = self.spot_counts

    #     result = spatialde.run_spatialde(
    #         spot_counts.cpu().numpy(),
    #         self.genes,
    #         self.spot_locations.image_x.numpy(),
    #         self.spot_locations.image_y.numpy(),
    #     )

    #     return result

class PatchDataset(torch.utils.data.Dataset):
    def __init__(self, slide: Slide, patch_size: int, magnify: int, patch_transform, device):
        self.slide = slide
        self.patch_size = patch_size
        self.patch_transform = patch_transform
        self.device = device
        self.magnify = magnify

    @property
    def hp(self):
        return (self.slide.hp, self.patch_size, self.magnify)

    def __getitem__(self, index: int):
        image_x, image_y = self.slide.spot_locations.image_x[index], self.slide.spot_locations.image_y[index]
        image_x = int(image_x) - self.patch_size // 2
        image_y = int(image_y) - self.patch_size // 2
        patch = self.slide.image[
            :,
            image_y:image_y + self.patch_size,
            image_x:image_x + self.patch_size,
        ]
        spot_count = self.slide.spot_counts[index]

        if self.patch_transform is not None:
            patch = self.patch_transform(patch)

        if self.magnify != 1:
            patch = TF.resize(patch, (int(self.patch_size * self.magnify), int(self.patch_size * self.magnify)))

        return patch.to(self.device), spot_count.to(self.device)

    def __len__(self):
        return self.slide.spot_locations.image_x.shape[0]

class PatchDataset2Level(torch.utils.data.Dataset):
    def __init__(self, patchdataset1: PatchDataset, patchdataset2: PatchDataset):
        super().__init__()

        self.patchdataset1 = patchdataset1
        self.patchdataset2 = patchdataset2

        assert len(patchdataset1) == len(patchdataset2), f"Found two PatchDatasets of differing lengths: {len(patchdataset1)} and {len(patchdataset2)}"

    def __len__(self):
        return len(self.patchdataset1)

    def __getitem__(self, index: int):
        patch1, label1 = self.patchdataset1[index]
        patch2, label2 = self.patchdataset2[index]

        assert (label1 == label2).all(), f"Found two PatchDatasets with differing labels at index {index}: {label1} and {label2}"

        return (patch1, patch2), label1

    @staticmethod
    def collate_fn(datapoints):
        patch1 = []
        patch2 = []
        label = []

        for ((patch1_, patch2_), label_) in datapoints:
            patch1.append(patch1_)
            patch2.append(patch2_)
            label.append(label_)

        return (torch.stack(patch1), torch.stack(patch2)), torch.stack(label)

def load_spot_locations_json(src: str):
    import json

    with open(src) as f:
        locations = json.load(f)

    sample_spots = locations['oligo']
    sample_spots = [spot for spot in sample_spots if 'tissue' in spot and spot['tissue']]
    image_x = [spot['x'] for spot in sample_spots]
    image_y = [spot['y'] for spot in sample_spots]
    row = [spot['row'] for spot in sample_spots]
    col = [spot['col'] for spot in sample_spots]
    dia = [spot['dia'] for spot in sample_spots]

    return SpotLocations(
        image_x=torch.tensor(image_x),
        image_y=torch.tensor(image_y),
        row=torch.tensor(row),
        col=torch.tensor(col),
        dia=torch.tensor(dia),
    )

def load_spot_locations_csv(src: str):
    import pandas as pd

    locations = pd.read_csv(src, index_col='barcode')
    locations = locations.loc[locations['in_tissue'] == 1]
    barcode_order = list(locations.index)
    
    return SpotLocations(
        image_x=torch.tensor(locations['pxl_col_in_fullres'].to_numpy()),
        image_y=torch.tensor(locations['pxl_row_in_fullres'].to_numpy()),
        row=torch.tensor(locations['array_row'].to_numpy()),
        col=torch.tensor(locations['array_col'].to_numpy()),
        dia=None,
    ), barcode_order

def load_compressed_tsv(path):
    import csv
    import gzip

    return [*csv.reader(gzip.open(path, mode="rt"), delimiter="\t")]

def load_counts(matrix_path):
    import numpy as np
    import pandas as pd
    import scipy.io

    mat_filtered = scipy.io.mmread(matrix_path)

    matrix = pd.DataFrame.sparse.from_spmatrix(mat_filtered)
    dense = np.array(matrix.sparse.to_dense())

    return torch.tensor(dense).T

def load_slide_from_folder(main_folder: str, image_path: str, spot_image_scaling: float = 1.0) -> Slide:
    matrix_dir = main_folder + '/outs/filtered_feature_bc_matrix'

    assert os.path.exists(main_folder + '/outs'), f"Folder '{main_folder}' may be invalid. It does not contain a subdirectory called 'outs'."

    spot_locations, barcode_order = load_spot_locations_csv(main_folder + '/outs/spatial/tissue_positions.csv')

    spot_counts = load_counts(matrix_dir + '/matrix.mtx.gz')
    barcodes = [barcode for barcode, in load_compressed_tsv(matrix_dir + '/barcodes.tsv.gz')]
    spot_counts_by_barcode = {barcode: counts for barcode, counts in zip(barcodes, spot_counts)}
    spot_counts = torch.stack([spot_counts_by_barcode[barcode] for barcode in barcode_order], dim=0)

    genes = [gene for feature_id, gene, feature_type in load_compressed_tsv(matrix_dir + '/features.tsv.gz')]

    slide = Slide(
        image_path=image_path,
        spot_locations=spot_locations * spot_image_scaling,
        spot_counts=spot_counts,
        genes=genes,
    )

    return slide

def load_old_visium_from_folder(main_folder: str, image_path: str, spot_image_scaling: float = 1.0) -> Slide:
    import pandas as pd

    matrix_dir = f'{main_folder}/outs/filtered_feature_bc_matrix'
    barcodes = [barcode for barcode, in load_compressed_tsv(matrix_dir + '/barcodes.tsv.gz')]
    spot_counts = load_counts(matrix_dir + '/matrix.mtx.gz')
    spot_counts_by_barcode = {barcode: counts for barcode, counts in zip(barcodes, spot_counts)}

    tissue_positions = pd.read_csv(
        f'{main_folder}/outs/spatial/tissue_positions_list.csv',
        names=['barcode', 'in_tissue', 'row', 'col', 'image_y', 'image_x'],
        header=None,
    )
    tissue_positions = tissue_positions[tissue_positions['in_tissue'] == 1]
    # Ensure that they are parallel
    barcode_order = list(tissue_positions['barcode'])
    tissue_positions_obj = SpotLocations(
        image_x=torch.tensor(tissue_positions['image_x'].to_numpy()),
        image_y=torch.tensor(tissue_positions['image_y'].to_numpy()),
        row=torch.tensor(tissue_positions['row'].to_numpy()),
        col=torch.tensor(tissue_positions['col'].to_numpy()),
        dia=None,
    )
    spot_counts = torch.stack([spot_counts_by_barcode[barcode] for barcode in barcode_order], dim=0)

    genes = [gene for feature_id, gene, feature_type in load_compressed_tsv(matrix_dir + '/features.tsv.gz')]

    slide = Slide(
        image_path=image_path,
        spot_locations=tissue_positions_obj * spot_image_scaling,
        spot_counts=spot_counts,
        genes=genes,
    )
    
    return slide
