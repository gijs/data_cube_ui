# Copyright 2016 United States Government as represented by the Administrator
# of the National Aeronautics and Space Administration. All Rights Reserved.
#
# Portion of this code is Copyright Geoscience Australia, Licensed under the
# Apache License, Version 2.0 (the "License"); you may not use this file
# except in compliance with the License. You may obtain a copy of the License
# at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# The CEOS 2 platform is licensed under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# Django specific
from celery.decorators import task
from celery.signals import worker_process_init, worker_process_shutdown
from .models import Query, Result, Metadata

import numpy as np
import math
import xarray as xr
import collections
import gdal
import shutil
import sys
import osr
import os
import datetime
from collections import OrderedDict
from dateutil.tz import tzutc

from utils.data_access_api import DataAccessApi
from utils.dc_mosaic import create_mosaic
from utils.dc_utilities import get_spatial_ref, save_to_geotiff, create_rgb_png_from_tiff, create_cfmask_clean_mask, split_task
from utils.dc_baseline import generate_baseline
from utils.dc_demutils import create_slope_mask
from utils.dc_water_classifier import wofs_classify
from data_cube_ui.utils import update_model_bounds_with_dataset, map_ranges

"""
Class for handling loading celery workers to perform tasks asynchronously.
"""

# Author: AHDS
# Creation date: 2016-06-23
# Modified by:
# Last modified date:

# constants up top for easy access/modification
# hardcoded colors input path..
color_paths = ['~/Datacube/data_cube_ui/utils/color_scales/ndvi', '~/Datacube/data_cube_ui/utils/color_scales/ndvi', '~/Datacube/data_cube_ui/utils/color_scales/ndvi_difference', '~/Datacube/data_cube_ui/utils/color_scales/ndvi_percentage_change']

base_result_path = '/datacube/ui_results/ndvi_anomaly/'
base_temp_path = '/datacube/ui_results_temp/'

# Datacube instance to be initialized.
# A seperate DC instance is created for each worker.
dc = None

#default measurements. leaves out all qa bands.
measurements = ['red', 'nir', 'cf_mask']

"""
functions used to combine time sliced data after being combined geographically.
Fill nodata uses the first timeslice as a base, then uses subsequent slices to
fill in indices with nodata values.
this should be used for recent/leastrecent + anything that is done in a single time chunk (median pixel?)
things like max/min ndvi should be able to compound max/min ops between ddifferent timeslices so this will be
different for that.
"""
def fill_nodata(dataset, dataset_intermediate):
    if dataset_intermediate is None:
        return dataset.copy(deep=True)
    dataset_out = dataset_intermediate.copy(deep=True)
    for key in list(dataset_out.data_vars):
        # Get raw data for current variable and mask the data
        dataset_out[key].values[dataset_out[key].values==-9999] = dataset[key].values[dataset_out[key].values==-9999]
    return dataset_out

#holds the different compositing algorithms. median, mean, mosaic?
# all options are required. setting None to a option will have the algo/task splitting
# process disregard it.
#experimentally optimized geo/time/slices_per_iter
processing_algorithms = {
    'median': {
        'geo_chunk_size': 0.05,
        'time_chunks': None,
        'time_slices_per_iteration': None,
        'reverse_time': True,
        'chunk_combination_method': fill_nodata,
        'processing_method': 'median'
    },
}

@task(name="ndvi_anomaly_task")
def create_ndvi_anomaly(query_id, user_id, single=False):
    """
    Creates metadata and result objects from a query id. gets the query, computes metadata for the
    parameters and saves the model. Uses the metadata to query the datacube for relevant data and
    creates the result. Results computed in single time slices for memory efficiency, pushed into a
    single numpy array containing the total result. this is then used to create png/tifs to populate
    a result model. Result model is constantly updated with progress and checked for task
    cancellation.

    Args:
        query_id (int): The ID of the query that will be created.
        user_id (string): The ID of the user that requested the query be made.

    Returns:
        Returns image url, data url for use only in tasks that reference this task.
    """

    print("Starting for query:" + query_id)
    # its fair to assume that the query_id will exist at this point, as if it wasn't it wouldn't
    # start the task.
    query = Query.objects.get(query_id=query_id, user_id=user_id)
    # if there is a matching query other than the one we're using now then do nothing.
    # the ui section has already grabbed the result from the db.
    if Result.objects.filter(query_id=query.query_id).exists():
        print("Repeat query, client will receive cached result.")
        return

    print("Got the query, creating metadata.")

    # creates the empty result.
    result = query.generate_result()

    if query.platform == "LANDSAT_ALL":
        error_with_message(result, "Combined products are not supported for NDVI Anomaly calculations.")
        return

    product_details = dc.dc.list_products()[dc.dc.list_products().name == query.product]
    # wrapping this in a try/catch, as it will throw a few different errors
    # having to do with memory etc.
    try:
        #selected scenes are indices in acquisitions, baseline months are 1-12 jan-dec.
        selected_scenes = [int(val) for val in query.time_start.split(',')]
        baseline_months = [int(val) for val in query.baseline.split(',')]
        # lists all acquisition dates for use in single tmeslice queries.
        acquisitions = dc.list_acquisition_dates(query.platform, query.product)

        # we will later iterate through chosen scenes.
        #for scene in selected_scene:
        scene = selected_scenes[0]
        baseline_scenes_base = acquisitions[0:scene]
        #can I use pandas/dfs to do a 'groupby' on month?
        baseline_scenes = [baseline_scene for baseline_scene in baseline_scenes_base if baseline_scene.month in baseline_months]
        if len(baseline_scenes_base) < 1:
            error_with_message(result, "Insufficient scene count for baseline length.")
            return
        #scene of interest being appended before processing: this is the MOST RECENT scene, e.g. index 0 after processing.
        baseline_scenes.append(acquisitions[scene])

        processing_options = processing_algorithms['median']
        #if its a single scene, load it all at once to prevent errors.
        if single:
            processing_options['time_chunks'] = None
            processing_options['time_slices_per_iteration'] = None

        # Reversed time = True will make it so most recent = First, oldest = Last.
        #default is in order from oldest -> newwest.
        lat_ranges, lon_ranges, time_ranges = split_task(resolution=product_details.resolution.values[0][1], latitude=(query.latitude_min, query.latitude_max), longitude=(
            query.longitude_min, query.longitude_max), acquisitions=baseline_scenes, geo_chunk_size=processing_options['geo_chunk_size'], time_chunks=processing_options['time_chunks'], reverse_time=processing_options['reverse_time'])

        result.total_scenes = len(time_ranges) * len(lat_ranges)
        # Iterates through the acquisition dates with the step in acquisitions_per_iteration.
        # Uses a time range computed with the index and index+acquisitions_per_iteration.
        # ensures that the start and end are both valid.
        print("Getting data and creating product")
        # create a temp folder that isn't on the nfs server so we can quickly
        # access/delete.
        if not os.path.exists(base_temp_path + query.query_id):
            os.mkdir(base_temp_path + query.query_id)
            os.chmod(base_temp_path + query.query_id, 0o777)

        time_chunk_tasks = []
        # iterate over the time chunks.
        print("Time chunks: " + str(len(time_ranges)))
        print("Geo chunks: " + str(len(lat_ranges)))
        for time_range_index in range(len(time_ranges)):
            # iterate over the geographic chunks.
            geo_chunk_tasks = []
            for geographic_chunk_index in range(len(lat_ranges)):
                geo_chunk_tasks.append(generate_ndvi_anomaly_chunk.delay(time_range_index, geographic_chunk_index, processing_options=processing_options, query=query, acquisition_list=time_ranges[
                                       time_range_index], lat_range=lat_ranges[geographic_chunk_index], lon_range=lon_ranges[geographic_chunk_index], measurements=measurements))
            time_chunk_tasks.append(geo_chunk_tasks)

        dataset_out_mosaic = None
        dataset_out_ndvi = None
        acquisition_metadata = {}
        for geographic_group in time_chunk_tasks:
            full_dataset = None
            tiles = []
            for t in geographic_group:
                tile = t.get()
                if tile == "CANCEL":
                    print("Cancelled task.")
                    shutil.rmtree(base_temp_path + query.query_id)
                    query.delete()
                    result.delete()
                    return
                if tile[0] is not None:
                    tiles.append(tile)
                result.scenes_processed += 1
                result.save()
            print("Got results for a time slice, computing intermediate product..")
            xr_tiles_mosaic = []
            xr_tiles_ndvi = []
            for tile in tiles:
                tile_metadata = tile[2]
                for acquisition_date in tile_metadata:
                    if acquisition_date in acquisition_metadata:
                        acquisition_metadata[acquisition_date]['clean_pixels'] += tile_metadata[acquisition_date]['clean_pixels']
                    else:
                        acquisition_metadata[acquisition_date] = {'clean_pixels': tile_metadata[acquisition_date]['clean_pixels']}
                xr_tiles_mosaic.append(xr.open_dataset(tile[0]))
                xr_tiles_ndvi.append(xr.open_dataset(tile[1]))
            if len(xr_tiles_mosaic) < 1:
                error_with_message(result, "There is no overlap between the selected scene and your area.")
                return
            #create cf mosaic
            full_dataset_mosaic = xr.concat(reversed(xr_tiles_mosaic), dim='latitude')
            dataset_mosaic = full_dataset_mosaic.load()
            dataset_out_mosaic = processing_options['chunk_combination_method'](dataset_mosaic, dataset_out_mosaic)

            #now ndvi_anomaly.
            full_dataset_ndvi = xr.concat(reversed(xr_tiles_ndvi), dim='latitude')
            dataset_ndvi = full_dataset_ndvi.load()
            dataset_out_ndvi = processing_options['chunk_combination_method'](dataset_ndvi, dataset_out_ndvi)

        latitude = dataset_out_mosaic.latitude
        longitude = dataset_out_mosaic.longitude

        # grabs the resolution.
        geotransform = [longitude.values[0], product_details.resolution.values[0][1],
                        0.0, latitude.values[0], 0.0, product_details.resolution.values[0][0]]
        #hardcoded crs for now. This is not ideal. Should maybe store this in the db with product type?
        crs = str("EPSG:4326")

        # remove intermediates
        shutil.rmtree(base_temp_path + query.query_id)

        # populate metadata values.
        dates = list(acquisition_metadata.keys())
        dates.sort()

        meta = query.generate_metadata(
            scene_count=len(dates), pixel_count=len(latitude)*len(longitude))

        for date in reversed(dates):
            meta.acquisition_list += date.strftime("%m/%d/%Y") + ","
            meta.clean_pixels_per_acquisition += str(
                acquisition_metadata[date]['clean_pixels']) + ","
            meta.clean_pixel_percentages_per_acquisition += str(
                acquisition_metadata[date]['clean_pixels'] * 100 / meta.pixel_count) + ","

        # Count clean pixels and correct for the number of measurements.
        clean_pixels = np.sum(dataset_out_mosaic[measurements[0]].values != -9999)
        meta.clean_pixel_count = clean_pixels
        meta.percentage_clean_pixels = (meta.clean_pixel_count / meta.pixel_count) * 100
        meta.save()

        # generate all the results
        file_path = base_result_path + query_id
        tif_path = file_path + '.tif'
        netcdf_path = file_path + '.nc'
        mosaic_png_path = file_path + '_mosaic.png'
        result_paths = [file_path + '_ndvi.png', file_path + '_baseline_ndvi.png', file_path + "_ndvi_difference.png", file_path + "_ndvi_percentage_change.png"]

        print("Creating query results.")
        #Mosaic
        save_to_geotiff(tif_path, gdal.GDT_Int16, dataset_out_mosaic, geotransform, get_spatial_ref(crs),
                        x_pixels=dataset_out_mosaic.dims['longitude'], y_pixels=dataset_out_mosaic.dims['latitude'],
                        band_order=['red', 'green', 'blue'])
        # we've got the tif, now do the png. -> RGB
        create_rgb_png_from_tiff(tif_path, mosaic_png_path, png_filled_path=None, fill_color=None, bands=[1,2,3], scale=(0, 4096))

        #ndvi_anomaly
        dataset_out_ndvi.to_netcdf(netcdf_path)
        save_to_geotiff(tif_path, gdal.GDT_Float64, dataset_out_ndvi, geotransform, get_spatial_ref(crs),
                        x_pixels=dataset_out_mosaic.dims['longitude'], y_pixels=dataset_out_mosaic.dims['latitude'],
                        band_order=['scene_ndvi', 'baseline_ndvi', 'ndvi_difference', 'ndvi_percentage_change'])
        # we've got the tif, now do the png set..
        # uses gdal dem with custom color maps..
        for index in range(len(color_paths)):
            cmd = "gdaldem color-relief -of PNG -b " + \
                str(index + 1) + " " + tif_path + " " + \
                color_paths[index] + " " + result_paths[index]
            os.system(cmd)

        # update the results and finish up.
        update_model_bounds_with_dataset([result, meta, query], dataset_out_mosaic)
        result.result_mosaic_path = mosaic_png_path
        result.scene_ndvi_path = result_paths[0]
        result.baseline_ndvi_path = result_paths[1]
        result.result_path = result_paths[2]
        result.ndvi_percentage_change_path = result_paths[3]
        result.data_path = tif_path
        result.data_netcdf_path = netcdf_path
        result.status = "OK"
        result.total_scenes = len(acquisitions)
        result.save()
        print("Finished processing results")
        # all data has been processed, create results and finish up.
        query.complete = True
        query.query_end = datetime.datetime.now()
        query.save()
    except:
        error_with_message(
            result, "There was an exception when handling this query.")
        raise
    # end error wrapping.
    return

@task(name="generate_ndvi_anomaly_chunk")
def generate_ndvi_anomaly_chunk(time_num, chunk_num, processing_options=None, query=None, acquisition_list=None, lat_range=None, lon_range=None, measurements=None):
    """
    responsible for generating a piece of a ndvi_anomaly product. This grabs the x/y area specified in the lat/lon ranges, gets all data
    from acquisition_list, which is a list of acquisition dates, and creates the ndvi_anomaly using the function named in processing_options.
    saves the result to disk using time/chunk num, and returns the path and the acquisition date keyed metadata.
    """
    selected_scene = acquisition_list[0]
    baseline_scenes = acquisition_list[1:]
    time_index = 0
    iteration_data = None
    acquisition_metadata = {}

    print("Starting chunk: " + str(time_num) + " " + str(chunk_num))
    while time_index < len(baseline_scenes):
        # check if the task has been cancelled. if the result obj doesn't exist anymore then return.
        try:
            result = Result.objects.get(query_id=query.query_id)
        except:
            print("Cancelled task as result does not exist")
            return
        if result.status == "CANCEL":
            print("Cancelling...")
            return "CANCEL"
        baseline_data = None
        scene_data = None
        # if everything needs to be loaded at once...
        if processing_options['time_chunks'] is None and processing_options['time_slices_per_iteration'] == None:
            datasets_in = []
            for acquisition in baseline_scenes:
                dataset = dc.get_dataset_by_extent(query.product, product_type=None, platform=query.platform, time=(acquisition, acquisition + datetime.timedelta(seconds=1)), longitude=lon_range, latitude=lat_range, measurements=measurements)
                if 'time' in dataset:
                    datasets_in.append(dataset.copy(deep=True))
                dataset = None
            if len(datasets_in) > 0:
                baseline_data = xr.concat(datasets_in, 'time')
        else:
            # time ranges set based on if the baseline_scenes has been reversed or not. If it has, then the 'start' index is the later date, and must be handled appropriately.
            start = baseline_scenes[time_index] + datetime.timedelta(seconds=1) if processing_options['reverse_time'] else baseline_scenes[time_index]
            if processing_options['time_slices_per_iteration'] is not None and (time_index + processing_options['time_slices_per_iteration'] - 1) < len(baseline_scenes):
                end = baseline_scenes[time_index + processing_options['time_slices_per_iteration'] - 1]
            else:
                end = baseline_scenes[-1] if processing_options['reverse_time'] else baseline_scenes[-1] + datetime.timedelta(seconds=1)
            time_range = (end, start) if processing_options['reverse_time'] else (start, end)

            baseline_data = dc.get_dataset_by_extent(query.product, product_type=None, platform=query.platform, time=time_range, longitude=lon_range, latitude=lat_range, measurements=measurements)

        # if baseline or scene data isn't there, continue.
        if 'cf_mask' not in baseline_data:
            time_index = time_index + (processing_options['time_slices_per_iteration'] if processing_options['time_slices_per_iteration'] is not None else 10000)
            continue

        clear_mask = create_cfmask_clean_mask(baseline_data.cf_mask)

        # update metadata. # here the clear mask has all the clean
        # pixels for each acquisition.
        for timeslice in range(clear_mask.shape[0]):
            time = raw_data.time.values[timeslice] if type(raw_data.time.values[timeslice]) == datetime.datetime else datetime.datetime.utcfromtimestamp(raw_data.time.values[timeslice].astype(int) * 1e-9)
            clean_pixels = np.sum(
                clear_mask[timeslice, :, :] == True)
            if time not in acquisition_metadata:
                acquisition_metadata[time] = {}
                acquisition_metadata[time]['clean_pixels'] = 0
            acquisition_metadata[time][
                'clean_pixels'] += clean_pixels

        for key in list(baseline_data.data_vars):
            baseline_data[key].values[np.invert(clear_mask)] = -9999
        #cloud filter + nan out all nodata.
        baseline_data = baseline_data.where(baseline_data != -9999)

        ndvi_baseline = (baseline_data.nir - baseline_data.red) / (baseline_data.nir + baseline_data.red)
        iteration_data = ndvi_baseline.mean('time') if processing_options['processing_method'] == 'mean' else ndvi_baseline.median('time') if processing_options['processing_method'] == 'median' else create_mosaic(ndvi_baseline, clean_mask=clear_mask, reverse_time=True, intermediate_product=None)
        time_index = time_index + (processing_options['time_slices_per_iteration'] if processing_options['time_slices_per_iteration'] is not None else 10000)

    #load and clean up the selected scene. Just mosaicked as it does the cfmask corrections
    scene_data = dc.get_dataset_by_extent(query.product, product_type=None, platform=query.platform, time=(selected_scene - datetime.timedelta(hours=1), selected_scene + datetime.timedelta(hours=1)), longitude=lon_range, latitude=lat_range)
    if 'cf_mask' not in scene_data or iteration_data is None:
        return [None, None, None]
    scene_cleaned = create_mosaic(scene_data, reverse_time=True, intermediate_product=None)

    #masks out nodata and water.
    water_class = wofs_classify(scene_cleaned, mosaic=True).wofs
    scene_cleaned_nan = scene_cleaned.copy(deep=True).where((scene_cleaned.red != -9999) & (water_class == 0))

    scene_ndvi = (scene_cleaned_nan.nir - scene_cleaned_nan.red) / (scene_cleaned_nan.nir + scene_cleaned_nan.red)
    ndvi_difference = scene_ndvi - iteration_data
    ndvi_percentage_change = (scene_ndvi - iteration_data) / iteration_data

    #convert to conventional nodata vals.
    scene_ndvi.values[~np.isfinite(scene_ndvi.values)] = -9999
    ndvi_difference.values[~np.isfinite(ndvi_difference.values)] = -9999
    ndvi_percentage_change.values[~np.isfinite(ndvi_percentage_change.values)] = -9999

    scene_ndvi_dataset = xr.Dataset({'scene_ndvi': scene_ndvi,
                                     'baseline_ndvi': iteration_data,
                                     'ndvi_difference': ndvi_difference,
                                     'ndvi_percentage_change': ndvi_percentage_change},
                                     coords={'latitude': scene_data.latitude,
                                             'longitude': scene_data.longitude})

    # Save this geographic chunk to disk.
    geo_path = base_temp_path + query.query_id + "/geo_chunk_" + \
        str(time_num) + "_" + str(chunk_num) + ".nc"
    geo_path_ndvi = base_temp_path + query.query_id + "/geo_chunk_ndvi_" + \
        str(time_num) + "_" + str(chunk_num) + ".nc"

    scene_cleaned.to_netcdf(geo_path)
    scene_ndvi_dataset.to_netcdf(geo_path_ndvi)

    print("Done with chunk: " + str(time_num) + " " + str(chunk_num))
    return [geo_path, geo_path_ndvi, acquisition_metadata]

def error_with_message(result, message):
    """
    Errors out under specific circumstances, used to pass error msgs to user. Uses the result path as
    a message container: TODO? Change this.

    Args:
        result (Result): The current result of the query being ran.
        message (string): The message to be stored in the result object.

    Returns:
        Nothing is returned as the method is ran asynchronously.
    """
    if os.path.exists(base_temp_path + result.query_id):
        shutil.rmtree(base_temp_path + result.query_id)
    result.status = "ERROR"
    result.result_path = message
    result.save()
    print(message)
    return

# Init/shutdown functions for handling dc instances.
# this is done to prevent synchronization/conflicts between workers when
# accessing DC resources.
@worker_process_init.connect
def init_worker(**kwargs):
    """
    Creates an instance of the DataAccessApi worker.
    """

    print("Creating DC instance for worker.")
    global dc
    dc = DataAccessApi()


@worker_process_shutdown.connect
def shutdown_worker(**kwargs):
    """
    Deletes the instance of the DataAccessApi worker.
    """

    print('Closing DC instance for worker.')
    global dc
    dc = None

@task(name="get_acquisition_list")
def get_acquisition_list(area, satellite):
    # lists all acquisition dates for use in single tmeslice queries.
    acquisitions = dc.list_acquisition_dates(satellite.satellite_id, satellite.product_prefix + area.area_id)
    return acquisitions
