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

from django.db import models
from data_cube_ui.models import Area, Compositor
from data_cube_ui.models import Query as BaseQuery, Metadata as BaseMetadata, Result as BaseResult, ResultType as BaseResultType

"""
Models file that holds all the classes representative of the database tabeles.  Allows for queries
to be created for basic CRUD operations.
"""

# Author: AHDS
# Creation date: 2016-06-23
# Modified by: MAP
# Last modified date:

class Query(BaseQuery):
    """
    Extends base query, adds app specific elements.
    """
    query_type = models.CharField(max_length=25, default="")
    animated_product = models.CharField(max_length=25, default="None")
    compositor = models.CharField(max_length=25, default="most_recent")

    #functs.
    def get_type_name(self):
        """
        Gets the ResultType.result_type attribute associated with the given Query object.

        Returns:
            result_type (string): The result type of the query.
        """
        return ResultType.objects.filter(result_id=self.query_type, satellite_id=self.platform)[0].result_type;

    def get_compositor_name(self):
        """
        Gets the ResultType.result_type attribute associated with the given Query object.

        Returns:
            result_type (string): The result type of the query.
        """
        return Compositor.objects.get(compositor_id=self.compositor).compositor_name;

    def generate_query_id(self):
        """
        Creates a Query ID based on a number of different attributes including start_time, end_time
        latitude_min and max, longitude_min and max, measurements, platform, product, and query_type

        Returns:
            query_id (string): The ID of the query built up by object attributes.
        """
        query_id = self.time_start.strftime("%Y-%m-%d")+'-'+self.time_end.strftime("%Y-%m-%d")+'-'+str(self.latitude_max)+'-'+str(self.latitude_min)+'-'+str(self.longitude_max)+'-'+str(self.longitude_min)+'-'+self.compositor+'-'+self.platform+'-'+self.product+'-'+self.query_type + '-' + self.animated_product
        return query_id

    def generate_metadata(self, scene_count=0, pixel_count=0):
        meta = Metadata(query_id=self.query_id, scene_count=scene_count, pixel_count=pixel_count,
                        latitude_min=self.latitude_min, latitude_max=self.latitude_max, longitude_min=self.longitude_min, longitude_max=self.longitude_max)
        meta.save()
        return meta

    def generate_result(self):
        result = Result(query_id=self.query_id, latitude_min=self.latitude_min,
                        latitude_max=self.latitude_max, longitude_min=self.longitude_min, longitude_max=self.longitude_max, total_scenes=0, scenes_processed=0, status="WAIT")
        result.save()
        return result

class Metadata(BaseMetadata):
    """
    Extends base metadata.
    """
    pass

class Result(BaseResult):
    """
    Extends base result, only adds app specific fields.
    """

    #result path + other data. More to come.
    result_filled_path = models.CharField(max_length=250, default="")
    animation_path = models.CharField(max_length=250, default="None")
    data_path = models.CharField(max_length=250, default="")
    data_netcdf_path = models.CharField(max_length=250, default="")

class ResultType(BaseResultType):
    """
    extends base result type, adding the app specific fields.
    """

    red = models.CharField(max_length=25)
    green = models.CharField(max_length=25)
    blue = models.CharField(max_length=25)
    fill = models.CharField(max_length=25, default="red")
