'`matflow_damask.main.py`'

import json
from textwrap import dedent
from pathlib import Path

import numpy as np
import pkg_resources
from damask_parse import (
    read_geom,
    read_table,
    write_load_case,
    write_geom,
    write_material_config,
)
from damask import DADF5
from damask_parse.utils import (
    get_header,
    parse_damask_spectral_version_info,
    volume_element_from_2D_microstructure,
)
from damask_parse import __version__ as damask_parse_version

from matflow_damask import (
    input_mapper,
    output_mapper,
    cli_format_mapper,
    func_mapper,
    register_output_file,
    software_versions,
)
from matflow_damask.utils import get_HDF5_incremental_quantity


@input_mapper(
    input_file='orientation_coordinate_system.json',
    task='generate_volume_element',
    method='random_voronoi'
)
def write_orientation_coordinate_system_from_seeds(path, microstructure_seeds):
    with Path(path).open('w') as handle:
        json.dump(microstructure_seeds['orientation_coordinate_system'], handle)


@input_mapper(
    input_file='orientation_coordinate_system.json',
    task='generate_volume_element',
    method='random_voronoi_from_orientations'
)
def write_orientation_coordinate_system_from_orientations(path, orientations):
    with Path(path).open('w') as handle:
        json.dump(orientations['orientation_coordinate_system'], handle)


def read_orientation_coordinate_system(path):
    with Path(path).open('r') as handle:
        return json.load(handle)


@output_mapper('microstructure_seeds', 'generate_microstructure_seeds', 'random')
def read_seeds_from_random(seeds_path, orientation_coordinate_system):
    'Parse the file from the `seeds_fromRandom` DAMASK command.'

    header_lns = get_header(seeds_path)
    num_header_lns = len(header_lns)

    grid_size = None
    random_seed = None
    for ln in header_lns:
        if ln.startswith('grid'):
            grid_size = [int(j) for j in [i for i in ln.split()][1:][1::2]]
        if ln.startswith('randomSeed'):
            random_seed = int(ln.split()[1])

    data = np.loadtxt(seeds_path, skiprows=(num_header_lns + 1), ndmin=2)
    position = data[:, 0:3]
    eulers = data[:, 3:6]

    out = {
        'position': position,
        'eulers': eulers,
        'grid_size': grid_size,
        'random_seed': random_seed,
        'orientation_coordinate_system': orientation_coordinate_system,
    }

    return out


@output_mapper('volume_element', 'generate_volume_element', 'random_voronoi')
@output_mapper('volume_element', 'generate_volume_element', 'random_voronoi_from_orientations')
def read_damask_geom(geom_path, ori_coord_system_path, model_coordinate_system):

    volume_element = read_geom(geom_path)
    volume_element['model_coordinate_system'] = model_coordinate_system

    ori_coord_sys = read_orientation_coordinate_system(ori_coord_system_path)
    volume_element['orientation_coordinate_system'] = ori_coord_sys

    return volume_element


@input_mapper('load.load', 'simulate_volume_element_loading', 'CP_FFT')
def write_damask_load_case(path, load_case):
    write_load_case(path, load_case)


@input_mapper('geom.geom', 'simulate_volume_element_loading', 'CP_FFT')
@input_mapper('geom.geom', 'visualise_volume_element', 'VTK')
def write_damask_geom(path, volume_element):
    write_geom(volume_element, path)


@input_mapper('material.config', 'simulate_volume_element_loading', 'CP_FFT')
def write_damask_material(path, material_properties, volume_element):
    write_material_config(material_properties, Path(path).parent, volume_element)


@output_mapper('volume_element_response', 'simulate_volume_element_loading', 'CP_FFT')
def read_damask_hdf5_file(hdf5_path, incremental_data, operations=None):
    """Operate on and extract data from an HDF5 file generated by a DAMASK run.

    Parameters
    ----------
    hdf5_path : Path or str
        Path to the HDF5 file.
    incremental_data : list of dict
        List of incremental data to extract from the HDF5 file. This is a list of dicts
        with the following keys:
            name: str
                The name by which the quantity will be stored in the output dict.
            path: str
                The HDF5 "path" to the dataset.
            transform: list of dict, optional
                If specified this is a list of dicts, each with the following keys:
                    sum_along_axes : int, optional
                        If specified, take the sum the array along this axis.
                    mean_along_axes: int, optional
                        If specified, take the mean average of the array along this axis.
    operations : list of dict, optional
        List of methods to invoke on the DADF5 object. This is a list of dicts with the
        following keys:
            name : str
                The name of the DADF5 method.
            args : dict
                Parameter names and their values to pass to the DADF5 method. This
                assumes all DADF5 method parameters are of positional-or-keyword type.
            opts : dict
                Additional options.

    Returns
    -------
    volume_element_response : dict
        Dict with keys determined by the `incremental_data` list.

    """

    sim_data = DADF5(hdf5_path)

    for op in operations or []:
        func = getattr(sim_data, op['name'], None)
        if not func:
            raise AttributeError(f'The DADF5 object has no attribute: {op["name"]}.')
        else:
            func(**op['args'])

        # Deal with specific options:
        if op['opts'].get('add_Mises', {}):

            if op["name"] == 'add_Cauchy':
                label = f'sigma'

            elif op["name"] == 'add_strain_tensor':
                # Include defaults from `DADF5.add_strain_tensor`:
                t = op['args'].get('t', 'U')
                m = op['args'].get('m', 0)
                F = op['args'].get('F', 'F')
                label = f'epsilon_{t}^{m}({F})'

            else:
                msg = (f'Operation "{op["name"]}" is not compatible with option '
                       f'"add_Mises".')
                raise ValueError(msg)

            sim_data.add_Mises(label)

    volume_element_response = {}
    for inc_dat_spec in incremental_data:
        inc_dat = get_HDF5_incremental_quantity(
            hdf5_path=hdf5_path,
            dat_path=inc_dat_spec['path'],
            transforms=inc_dat_spec.get('transforms'),
        )
        volume_element_response.update({inc_dat_spec['name']: inc_dat})

    return volume_element_response


@input_mapper('orientation.seeds', 'generate_volume_element', 'random_voronoi')
def write_microstructure_seeds(path, microstructure_seeds):

    grid_size = microstructure_seeds['grid_size']
    position = microstructure_seeds['position']
    eulers = microstructure_seeds['eulers']

    data = np.hstack([position, eulers, np.arange(1, len(position) + 1)[:, None]])

    header = f"""
        3 header
        grid a {grid_size[0]} b {grid_size[1]} c {grid_size[2]}
        microstructures {len(data)}
        1_pos 2_pos 3_pos 1_euler 2_euler 3_euler microstructure
    """

    fmt = ['%20.16f'] * 6 + ['%10g']
    header = dedent(header).strip()
    np.savetxt(path, data, header=header, comments='', fmt=fmt)


@input_mapper(
    'orientation.seeds',
    'generate_volume_element',
    'random_voronoi_from_orientations'
)
def write_microstructure_new_orientations(path, microstructure_seeds, orientations):

    grid_size = microstructure_seeds['grid_size']
    position = microstructure_seeds['position']
    eulers = orientations['euler_angles']

    data = np.hstack([position, eulers, np.arange(1, len(position) + 1)[:, None]])

    header = f"""
        3 header
        grid a {grid_size[0]} b {grid_size[1]} c {grid_size[2]}
        microstructures {len(data)}
        1_pos 2_pos 3_pos 1_euler 2_euler 3_euler microstructure
    """

    fmt = ['%20.16f'] * 6 + ['%10g']
    header = dedent(header).strip()
    np.savetxt(path, data, header=header, comments='', fmt=fmt)


@cli_format_mapper('size', 'generate_volume_element', 'random_voronoi_from_orientations')
def format_rve_size(size):
    return ' '.join(['{}'.format(i) for i in size])


@func_mapper(task='generate_volume_element', method='extrusion')
def volume_element_from_microstructure_image(microstructure_image, depth, image_axes):
    out = {
        'volume_element': volume_element_from_2D_microstructure(
            microstructure_image,
            depth,
            image_axes
        )
    }
    return out


@software_versions()
def get_versions(executable='DAMASK_spectral'):
    'Get versions of pertinent software associated with this extension.'

    out = {
        'DAMASK_spectral': parse_damask_spectral_version_info(executable=executable),
        'damask (Python)': {'version': pkg_resources.get_distribution('damask').version},
        'damask-parse (Python)': {'version': damask_parse_version},
    }
    return out


register_output_file('VTR_file', 'geom.vtr', 'visualise_volume_element', 'VTK')
