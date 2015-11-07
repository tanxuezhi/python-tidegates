import os
import sys
import glob
import datetime
from textwrap import dedent

import arcpy
import numpy

import tidegates
from tidegates import utils


# ALL ELEVATIONS IN FEET
SEALEVELRISE = numpy.arange(7)
SURGES = {
    'MHHW':   4.0,
    '10yr':   8.0,
    '50yr':   9.6,
    '100yr': 10.5,
}


class BaseFlooder_Mixin(object):
    def __init__(self):
        #std attributes
        self.canRunInBackground = True

        # lazy properties
        self._workspace = None
        self._dem = None
        self._polygons = None
        self._ID_column = None
        self._flood_output = None
        self._building_output = None
        self._wetland_output = None
        self._wetlands = None
        self._buildings = None

    def isLicensed(self):
        """Set whether tool is licensed to execute."""
        return True

    def updateMessages(self, parameters): # pragma: no cover
        """Modify the messages created by internal validation for each
        parameter of the tool.  This method is called after internal
        validation."""
        return

    def updateParameters(self, parameters): # pragma: no cover
        """ Automatically called when any parameter is updated in the
        GUI.

        Flow is like this:
            1. User interacts with GUI, filling out some input element
            2. self.getParameterInfo is called
            3. Parameteter are fed to this method as a list

        """
        return

    def execute(self, parameters, messages):
        """ PART OF THE ESRI BLACK BOX

        This method is called when the tool is actually executed. It
        gets passed magics lists of parameters and messages that no one
        can actually see.

        Due to this mysterious nature, I do the following:
            1) turn all of the elements of the list into a dictionary
               so that we can access them in a meaningful way. This
               means, instead of doing something like
                    > dem = parameters[0].valueAsText
                    > zones = parameters[1].valueAsText
                    > ...
                for EVERY. SINGLE. PARAMETER, we can instead do
                something like:
                    > params = self._get_parameter_values(parameters, multivals=['elevation'])
                    > dem = params['dem']
                    > zones = params['zones'].
               This is much cleaner, in my opinion, and we don't have to
               magically know where in the list of parameters e.g., the
               DEM is found. Take note, ESRI.
            2) generate a list of scenarios usings `self._make_scenarios`.
            3) loop through those scenarios.
            4) call `self._analyze` on each scenario.
            5) call `self._finish_results` on all of the layers
               generated by the loop.

        """

        params = self._get_parameter_values(parameters, multivals=['elevation'])

        all_floods = []
        all_wetlands = []
        all_buildings = []
        scenario_list = self._make_scenarios(**params)
        with utils.WorkSpace(params['workspace']), utils.OverwriteState(True):
            for scenario in scenario_list:
                fldlyr, wtlndlyr, blgdlyr = self._analyze(
                    elev=scenario['elev'],
                    surge=scenario['surge_name'],
                    slr=scenario['slr'],
                    **params
                )
                all_floods.append(fldlyr.dataSource)
                all_wetlands.append(wtlndlyr.dataSource)
                all_buildings.append(blgdlyr.dataSource)

            self._finish_results(
                all_floods,
                all_wetlands,
                all_buildings,
                msg="\nMerging output layers and cleaning up",
                verbose=True,
                asMessage=True,
                **params
            )

        return None

    @staticmethod
    def _set_parameter_dependency(downstream, *upstream):
        """ Set the dependecy of a arcpy.Parameter

        Parameters
        ----------
        downstream : arcpy.Parameter
            The Parameter that is reliant on an upstream parameter.
        upstream : acrpy.Parameters
            An arbitraty number of "upstream" parameters on which the
            "downstream" parameter depends.

        Returns
        -------
        None

        See Also
        --------
        http://goo.gl/HcR6WJ

        """

        downstream.parameterDependencies = [u.name for u in upstream]

    @staticmethod
    def _show_header(title, verbose=True):
        underline = ''.join(['-'] * len(title))
        header = '\n{}\n{}'.format(title, underline)
        utils._status(header, verbose=verbose, asMessage=True, addTab=False)
        return header

    @staticmethod
    def _add_results_to_map(mapname, filename):
        ezmd = utils.EasyMapDoc(mapname)
        if ezmd.mapdoc is not None:
            ezmd.add_layer(filename)

        return ezmd

    @staticmethod
    def _add_scenario_columns(layer, elev=None, surge=None, slr=None):
        if elev is not None:
            utils.add_field_with_value(
                table=layer,
                field_name="flood_elev",
                field_value=float(elev),
                msg="Adding 'flood_elev' field to ouput",
                verbose=True,
                asMessage=True
            )

        if surge is not None:
            utils.add_field_with_value(
                table=layer,
                field_name="surge",
                field_value=str(surge),
                field_length=10,
                msg="Adding storm surge field to ouput",
                verbose=True,
                asMessage=True
            )

        if slr is not None:
            utils.add_field_with_value(
                table=layer,
                field_name="slr",
                field_value=int(slr),
                msg="Adding sea level rise field to ouput",
                verbose=True,
                asMessage=True
            )

    @property
    def workspace(self):
        if self._workspace is None:
            self._workspace = arcpy.Parameter(
                displayName="Analysis WorkSpace",
                name='workspace',
                datatype="DEWorkspace",
                parameterType="Required",
                direction="Input",
                multiValue=False
            )
        return self._workspace

    @staticmethod
    def _get_parameter_values(parameters, multivals=None):
        if multivals is None:
            multivals = []
        elif numpy.isscalar(multivals):
            multivals = [multivals]

        params = {}
        for p in parameters:
            value = p.valueAsText
            if p.name in multivals:
                value = value.split(';')
            params[p.name] = value

        return params

    @staticmethod
    def _prep_flooder_input(elev=None, surge=None, slr=None, flood_output=None):
        """ Prepares the basic inputs to the `_do_flood` method.

        Parameters
        ----------
        elev, slr : float, optional
            Final elevation and sea level rise associated with the
            scenario.
        surge : str, optional
            The name of the storm surge associated with the scenario
            (e.g., MHHW, 100yr).
        flood_output : str
            Path/filename to where the final flooded areas will be
            saved.

        Returns
        -------
        elevation : float
            Flood elevation for this scenario.
        title : str
            The basis of the header to be displayed as an arcpy.Message.
        temp_fname : str
            Path/name of the temporary file where the intermediate
            output will be saved.

        """
        if elev is None:
            elevation = float(slr + SURGES[surge])
            title = "Analyzing flood elevation: {} ft ({}, {})".format(elevation, surge, slr)
        else:
            elevation = float(elev)
            title = "Analyzing flood elevation: {} ft".format(elevation)

        if flood_output is None:
            raise ValueError('must provide a `flood_output`')

        basename, ext = os.path.splitext(flood_output)
        temp_fname = basename + str(elevation).replace('.', '_') + ext

        return elevation, title, temp_fname

    @property
    def dem(self):
        if self._dem is None:
            self._dem = arcpy.Parameter(
                displayName="Digital Elevation Model",
                name="dem",
                datatype="DERasterDataset",
                parameterType="Required",
                direction="Input",
                multiValue=False
            )
            self._set_parameter_dependency(self._dem, self.workspace)
        return self._dem

    @property
    def polygons(self):
        if self._polygons is None:
            self._polygons = arcpy.Parameter(
                displayName="Tidegate Zones of Influence",
                name="polygons",
                datatype="DEFeatureClass",
                parameterType="Required",
                direction="Input",
                multiValue=False
            )
            self._set_parameter_dependency(self._polygons, self.workspace)
        return self._polygons

    @property
    def ID_column(self):
        if self._ID_column is None:
            self._ID_column = arcpy.Parameter(
                displayName="Column with Tidegate IDs",
                name="ID_column",
                datatype="Field",
                parameterType="Required",
                direction="Input",
                multiValue=False
            )
            self._set_parameter_dependency(self._ID_column, self.polygons)
        return self._ID_column

    @property
    def flood_output(self):
        """ Where the flooded areas for each scenario will be saved.
        """
        if self._flood_output is None:
            self._flood_output = arcpy.Parameter(
                displayName="Output floods layer/filename",
                name="flood_output",
                datatype="GPString",
                parameterType="Required",
                direction="Input"
            )
        return self._flood_output

    @property
    def building_output(self):
        """ Where the flooded buildings for each scenario will be saved.
        """
        if self._building_output is None:
            self._building_output = arcpy.Parameter(
                displayName="Output layer/filename of impacted buildings",
                name="building_output",
                datatype="GPString",
                parameterType="Optional",
                direction="Input"
            )
        return self._building_output

    @property
    def wetland_output(self):
        """ Where the flooded wetlands for each scenario will be saved.
        """
        if self._wetland_output is None:
            self._wetland_output = arcpy.Parameter(
                displayName="Output layer/filename of impacted wetlands",
                name="wetland_output",
                datatype="GPString",
                parameterType="Optional",
                direction="Input"
            )
        return self._wetland_output

    @property
    def wetlands(self):
        if self._wetlands is None:
            self._wetlands = arcpy.Parameter(
                displayName="Wetlands",
                name="wetlands",
                datatype="DEFeatureClass",
                parameterType="Optional",
                direction="Input",
                multiValue=False
            )
            self._set_parameter_dependency(self._wetlands, self.workspace)
        return self._wetlands

    @property
    def buildings(self):
        if self._buildings is None:
            self._buildings = arcpy.Parameter(
                displayName="Buildings footprints",
                name="buildings",
                datatype="DEFeatureClass",
                parameterType="Optional",
                direction="Input",
                multiValue=False
            )
            self._set_parameter_dependency(self._buildings, self.workspace)
        return self._buildings

    def _make_scenarios(self, **params):
        """ Makes a list of dictionaries of all scenario parameters that
        will be analyzed by the toolbox.

        Parameters
        ----------
        **params : keyword arguments
            Keyword arguments of analysis parameters generated by
            `self._get_parameter_values`

        Returns
        -------
        scenarios : list of dictionaries
            A list of dictionaries describing each scenario to be
            analyzed. Keys of the dictionaries will be:
              - elev -- the custom elevation
              - surge_name - the name of a storm surge event
              - surge_elev - the elevation associated with "surge_name"
              - slr - the amount of sea level rise to be considered.

            When analyzing custom elevations, all other entries are set
            to None. Likewise, when evaluating standard scenarios,
            "elev" is None.

        """
        scenario_list = []

        # if elevation is in the parameters, then we *know* this is
        # a custom flood elevation. Otherwise, we're evaluating the
        # standard scenarios.
        elevations = params.get('elevation', None)

        # standard scenarioes
        if elevations is None:
            for surge_name, surge_elev in SURGES.items():
                for slr in SEALEVELRISE:
                    scenario = {
                        'elev': None,
                        'surge_name': surge_name,
                        'surge_elev': surge_elev,
                        'slr': slr
                    }
                    scenario_list.append(scenario)
        # custom floods
        else:
            for elev in elevations:
                scenario = {
                    'elev': float(elev),
                    'surge_name': None,
                    'surge_elev': None,
                    'slr': None
                }
                scenario_list.append(scenario)

        return scenario_list

    def _do_flood(self, dem, poly, idcol, elev, flood_output, surge=None, slr=None):
        """ Determines the extent of flooded for a single sceario.

        Parameters
        ----------
        dem, poly : str
            Path/filenames to the DEM and polygon (zones of influent)
            layers to be analyzed.
        idcol : str
            Name of the field in ``poly`` that uniquely identifies each
            zone of influence.
        elev : float
            The total flood elevation for the scenario
        flood_output : str
            Path/filename to where the areas of inundation will be
            saved.
        surge : str, optional
            The name of the storm surge scenario.
        slr : float, optional
            The amount of sea level rise being considered.

        Returns
        -------
        flooded_polygons : arcpy.mapping.Layer
            GIS layer of the polygons showing the extent flooded behind
            each tidegate.


        """
        flooded_polygons = tidegates.flood_area(
            dem=dem,
            polygons=poly,
            ID_column=idcol,
            elevation_feet=elev,
            filename=flood_output,
            verbose=True,
            asMessage=True
        )
        self._add_scenario_columns(flooded_polygons, elev=elev, surge=surge, slr=slr)

        return flooded_polygons

    def _do_assessment(self, floods_path, idcol, wetlands=None, buildings=None):
        """ Assesses the extent of impacts to wetlands and buildings due
        to a single flooding scenario.

        Parameters
        ----------
        floods_path : str
            Path/filename to the output of `self._do_flood`.
        idcol : str
            Name of the field in ``floods_path`` that uniquely
            identifies each zone of influence.
        wetlands, buildings : str, optional
            Paths/filenames to layers of the extent of wetlands and
            buildings in the area to be analyzed.

        Returns
        -------
        floods, flooded_wetlands, flooded_buildings : arcpy.mapping.Layers
            Layers (or None) of the floods and flood-impacted wetlands
            and buildings, respectively.

        """

        wl_name = utils.create_temp_filename(floods_path, prefix="_wetlands_")
        bldg_name = utils.create_temp_filename(floods_path, prefix="_buildinds_")

        floods, flooded_wetlands, flooded_buildings = tidegates.assess_impact(
            floods_path=floods_path,
            ID_column=idcol,
            wetlands_path=wetlands,
            wetlandsoutput=wl_name,
            buildings_path=buildings,
            buildingsoutput=bldg_name,
            cleanup=True,
            verbose=True,
            asMessage=True,
        )

        return floods, flooded_wetlands, flooded_buildings

    def _analyze(self, elev=None, surge=None, slr=None, **params):
        """ Helper function to call `_do_flood` and `_do_assessment`.

        Parameters
        ----------
        elev : float, optional
            Custom elevation to be analyzed
        slr : float, optional
            Sea level rise associated with the standard scenario.
        surge : str, optional
            The name of the storm surge associated with the scenario
            (e.g., MHHW, 100yr).
        **params : keyword arguments
            Keyword arguments of analysis parameters generated by
            `self._get_parameter_values`

        Returns
        -------
        floods, flooded_wetlands, flooded_buildings : arcpy.mapping.Layers
            Layers (or None) of the floods and flood-impacted wetlands
            and buildings, respectively.

        """
        elev, title, fname = self._prep_flooder_input(
            elev=elev,
            surge=surge,
            slr=slr,
            flood_output=params['flood_output']
        )
        self._show_header(title)

        fldlyr = self._do_flood(
            dem=params['dem'],
            poly=params['polygons'],
            idcol=params['ID_column'],
            elev=elev,
            flood_output=fname,
            surge=surge,
            slr=slr
        )

        fldlyr, wtlndlyr, blgdlyr = self._do_assessment(
            fname,
            params['ID_column'],
            wetlands=params['wetlands'],
            buildings=params['buildings']
        )

        return fldlyr, wtlndlyr, blgdlyr

    @utils.update_status()
    def _finish_results(self, all_floods, all_wetlands, all_buildings, **params):
        """ Merges and clean up compiled output from `_analyze`.

        Parameters
        ----------
        all_floods, all_wetlands, all_buildings : lists of str
            Lists of all of the floods, flooded wetlands, and flooded
            buildings, respectively, that will be merged and deleted.
        **params : keyword arguments
            Keyword arguments of analysis parameters generated by
            `self._get_parameter_values`

        Returns
        -------
        None

        """

        utils.concat_results(params['flood_output'], *all_floods)

        if params['wetland_output'] is not None:
            utils.concat_results("_tmp_wetlnds", *all_wetlands)
            base_wetlands = utils.load_data(params['wetlands'], 'layer')
            flooded_wetlands = utils.load_data("_tmp_wetlnds", "layer")
            utils.join_results_to_baseline(params['wetland_output'], flooded_wetlands, base_wetlands)

        if params['building_output'] is not None:
            utils.concat_results("_tmp_bldgs", *all_buildings)
            base_buildings = utils.load_data(params['buildings'], 'layer')
            flooded_buildings = utils.load_data("_tmp_bldgs", "layer")
            utils.join_results_to_baseline(params['building_output'], flooded_buildings, base_buildings)


        # clean everything no matter what
        tidegates.utils.cleanup_temp_results(*all_floods)
        tidegates.utils.cleanup_temp_results(*all_wetlands)
        tidegates.utils.cleanup_temp_results(*all_buildings)


class Flooder(BaseFlooder_Mixin):
    def __init__(self):
        # std attributes
        super(Flooder, self).__init__()
        self.label = "1 - Create Flood Scenarios"
        self.description = dedent("""
        Allows the user to create a custom flooding scenario given the
        following:
            - A DEM of the coastal area
            - A polygon layer describing the zones of influence of each
              tidegate
        """)

        # lazy properties
        self._elevation = None

    @staticmethod
    def _prep_elevation_and_filename(elev_string, filename):
        basename, ext = os.path.splitext(filename)
        fname = basename + elev_string.replace('.', '_') + ext
        elevation = float(elev_string)
        title = "Analyzing flood elevation: {} ft".format(elevation)

        return elevation, title, fname

    @property
    def elevation(self):
        if self._elevation is None:
            self._elevation = arcpy.Parameter(
                displayName="Water Surface Elevation",
                name="elevation",
                datatype="GPDouble",
                parameterType="Required",
                direction="Input",
                multiValue=True
            )
        return self._elevation

        params = [
            self.workspace,
            self.dem,
            self.polygons,
            self.ID_column,
            self.elevation,
            self.flood_output,
            self.wetlands,
            self.wetland_output,
            self.buildings,
            self.building_output,
        ]
        return params

    @property
    def elevation(self):
        """ The flood elevation for a custom scenario.
        """

        return results


class StandardScenarios(BaseFlooder_Mixin):
    def __init__(self):
        """Define the tool (tool name is the name of the class)."""
        # std attributes
        super(StandardScenarios, self).__init__()
        self.label = "2 - Evaluate all standard scenarios"

        self.description = dedent("""
        Allows the user to recreate the standard scenarios with their
        own input.

        The standard scenarios are each combination of storm surges
        (MHHW, 10-yr, 50-yr, 100-yr) and sea level rise up to 6 feet in
        1-ft increments.
        """)

    @staticmethod
    def _prep_elevation_and_filename(surge, slr, filename):
        basename, ext = os.path.splitext(filename)
        elevation = float(slr + SURGES[surge])
        fname = basename + str(elevation).replace('.', '_') + ext
        title = "Analyzing flood elevation: {} ft ({}, {})".format(elevation, surge, slr)

        return elevation, title, fname

        params = [
            self.workspace,
            self.dem,
            self.polygons,
            self.ID_column,
            self.flood_output,
            self.wetlands,
            self.wetland_output,
            self.buildings,
            self.building_output,
        ]
        return params
