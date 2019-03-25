# Licensed under a 3-clause BSD style license - see LICENSE.rst
import logging
import numpy as np
from astropy.nddata.utils import NoOverlapError, PartialOverlapError
from astropy.coordinates import Angle
from astropy.utils import lazyproperty
from ..maps import Map, WcsGeom
from .counts import fill_map_counts
from .exposure import make_map_exposure_true_energy, _map_spectrum_weight
from .background import make_map_background_irf


__all__ = ["MapMaker", "MapMakerObs", "MapMakerRing"]

log = logging.getLogger(__name__)


class MapMaker:
    """Make maps from IACT observations.

    Parameters
    ----------
    geom : `~gammapy.maps.WcsGeom`
        Reference image geometry in reco energy
    offset_max : `~astropy.coordinates.Angle`
        Maximum offset angle
    geom_true : `~gammapy.maps.WcsGeom`
        Reference image geometry in true energy, used for exposure maps and PSF.
        If none, the same as geom is assumed
    exclusion_mask : `~gammapy.maps.Map`
        Exclusion mask
    """

    def __init__(self, geom, offset_max, geom_true=None, exclusion_mask=None):
        if not isinstance(geom, WcsGeom):
            raise ValueError("MapMaker only works with WcsGeom")

        if geom.is_image:
            raise ValueError("MapMaker only works with geom with an energy axis")

        self.geom = geom
        self.geom_true = geom_true if geom_true else geom
        self.offset_max = Angle(offset_max)
        self.exclusion_mask = exclusion_mask

    def _get_empty_maps(self, selection):
        # Initialise zero-filled maps
        maps = {}
        for name in selection:
            if name == "exposure":
                maps[name] = Map.from_geom(self.geom_true, unit="m2 s")
            else:
                maps[name] = Map.from_geom(self.geom, unit="")
        return maps

    def run(self, observations, selection=None):
        """
        Run MapMaker for a list of observations to create
        stacked counts, exposure and background maps

        Parameters
        --------------
        observations : `~gammapy.data.Observations`
            Observations to process
        selection : list
            List of str, selecting which maps to make.
            Available: 'counts', 'exposure', 'background'
            By default, all maps are made.

        Returns
        -----------
        maps: dict of stacked counts, background and exposure maps.
        """
        selection = _check_selection(selection)
        maps = self._get_empty_maps(selection)

        for obs in observations:
            log.info("Processing observation: OBS_ID = {}".format(obs.obs_id))

            try:
                obs_maker = self._get_obs_maker(obs)
            except NoOverlapError:
                log.info(
                    "Skipping observation {}, no overlap with map.".format(obs.obs_id)
                )
                continue

            maps_obs = obs_maker.run(selection)

            for name in selection:
                data = maps_obs[name].quantity.to_value(maps[name].unit)
                if name == "exposure":
                    maps[name].fill_by_coord(obs_maker.coords_etrue, data)
                else:
                    maps[name].fill_by_coord(obs_maker.coords, data)
        self._maps = maps
        return maps

    def _get_obs_maker(self, obs, mode="trim"):
        # Compute cutout geometry and slices to stack results back later
        cutout_kwargs = {
            "position": obs.pointing_radec,
            "width": 2 * self.offset_max,
            "mode": mode,
        }

        cutout_geom = self.geom.cutout(**cutout_kwargs)
        cutout_geom_etrue = self.geom_true.cutout(**cutout_kwargs)

        if self.exclusion_mask is not None:
            cutout_exclusion = self.exclusion_mask.cutout(**cutout_kwargs)
        else:
            cutout_exclusion = None

        # Make maps for this observation
        return MapMakerObs(
            observation=obs,
            geom=cutout_geom,
            geom_true=cutout_geom_etrue,
            offset_max=self.offset_max,
            exclusion_mask=cutout_exclusion,
        )

    @staticmethod
    def _maps_sum_over_axes(maps, spectrum, keepdims):
        """Compute weighted sum over map axes.

        Parameters
        ----------
        spectrum : `~gammapy.spectrum.models.SpectralModel`
            Spectral model to compute the weights.
            Default is power-law with spectral index of 2.

        keepdims : bool, optional
            If this is set to True, the energy axes is kept with a single bin.
            If False, the energy axes is removed

        """
        images = {}
        for name, map in maps.items():
            if name == "exposure":
                map = _map_spectrum_weight(map, spectrum)
            if name == "exclusion":
                map = map.slice_by_idx({"energy":0})
            images[name] = map.sum_over_axes(keepdims=keepdims)
        return images

    def run_images(self, observations=None, spectrum=None, keepdims=False):
        """Create images by summing over the energy axis.

        Exposure is weighted with an assumed spectrum,
        resulting in a weighted mean exposure image.

        Parameters
        ----------
        observations: `~gammapy.data.Observations`
            Observations to process

        Either MapMaker.run() has to be called before calling this function,
        or observations need to be passed.

        If  MapMaker.run() has been called before, then those maps will be
        summed over. Else, new maps will be computed and then summed.

        spectrum : `~gammapy.spectrum.models.SpectralModel`
            Spectral model to compute the weights.
            Default is power-law with spectral index of 2.

        keepdims : bool, optional
            If this is set to True, the energy axes is kept with a single bin.
            If False, the energy axes is removed

        Returns
        -------
        images : dict of `~gammapy.maps.Map`
        """
        if not hasattr(self, "_maps"):
            if observations is None:
                raise ValueError("Requires observations...")
            self.run(observations)

        images = self._maps_sum_over_axes(self._maps, spectrum, keepdims)
        return images


class MapMakerObs:
    """Make maps for a single IACT observation.

    Parameters
    ----------
    observation : `~gammapy.data.DataStoreObservation`
        Observation
    geom : `~gammapy.maps.WcsGeom`
        Reference image geometry
    geom_true : `~gammapy.maps.WcsGeom`
        Reference image geometry in true energy, used for exposure maps and PSF.
        If none, the same as geom is assumed
    fov_mask : `~numpy.ndarray`
        Mask to select pixels in field of view
    exclusion_mask : `~gammapy.maps.Map`
        Exclusion mask (used by some background estimators)
    """

    def __init__(
        self, observation, geom, offset_max, geom_true=None, exclusion_mask=None
    ):
        self.observation = observation
        self.geom = geom
        self.geom_true = geom_true if geom_true else geom
        self.offset_max = offset_max
        self.exclusion_mask = exclusion_mask
        self.maps = {}

    def _fov_mask(self, coords):
        pointing = self.observation.pointing_radec
        offset = coords.skycoord.separation(pointing)
        fov_mask = offset >= self.offset_max
        return fov_mask

    @lazyproperty
    def fov_mask_etrue(self):
        return self._fov_mask(self.coords_etrue)

    @lazyproperty
    def fov_mask(self):
        return self._fov_mask(self.coords)

    @lazyproperty
    def coords(self):
        coords = self.geom.get_coord()
        return coords

    @lazyproperty
    def coords_etrue(self):
        # Compute field of view mask on the cutout in true energy
        coords_etrue = self.geom_true.get_coord()
        return coords_etrue

    def run(self, selection=None):
        """Make maps.

        Returns dict with keys "counts", "exposure" and "background".

        Parameters
        ----------
        selection : list
            List of str, selecting which maps to make.
            Available: 'counts', 'exposure', 'background'
            By default, all maps are made.
        """
        selection = _check_selection(selection)

        for name in selection:
            getattr(self, "_make_" + name)()

        return self.maps

    def _make_counts(self):
        counts = Map.from_geom(self.geom)
        fill_map_counts(counts, self.observation.events)
        if self.fov_mask is not None:
            counts.data[..., self.fov_mask] = 0
        self.maps["counts"] = counts

    def _make_exposure(self):
        exposure = make_map_exposure_true_energy(
            pointing=self.observation.pointing_radec,
            livetime=self.observation.observation_live_time_duration,
            aeff=self.observation.aeff,
            geom=self.geom_true,
        )
        if self.fov_mask_etrue is not None:
            exposure.data[..., self.fov_mask_etrue] = 0
        self.maps["exposure"] = exposure

    def _make_background(self):
        background = make_map_background_irf(
            pointing=self.observation.fixed_pointing_info,
            ontime=self.observation.observation_time_duration,
            bkg=self.observation.bkg,
            geom=self.geom,
        )
        if self.fov_mask is not None:
            background.data[..., self.fov_mask] = 0

        # TODO: decide what background modeling options to support
        # Extra things like FOV norm scale or ring would go here.

        self.maps["background"] = background


def _check_selection(selection):
    """Handle default and validation of selection"""
    available = ["counts", "exposure", "background"]
    if selection is None:
        selection = available

    if not isinstance(selection, list):
        raise TypeError("Selection must be a list of str")

    for name in selection:
        if name not in available:
            raise ValueError("Selection not available: {!r}".format(name))

    return selection


class MapMakerRing(MapMaker):
    """Make maps from IACT observations.

    The main motivation for this class in addition to the `MapMaker`
    is to have the common image background estimation methods,
    like `~gammapy.background.RingBackgroundEstimator`,
    that work using on and off maps.

    To ensure adequate statistics, only observations that are fully
    contained within the reference geometry will be analysed

    Parameters
    ----------
    geom : `~gammapy.maps.WcsGeom`
        Reference image geometry
    offset_max : `~astropy.coordinates.Angle`
        Maximum offset angle
    exclusion_mask : `~gammapy.maps.Map`
        Exclusion mask
    background_estimator : `~gammapy.background.RingBackgroundEstimator`
        or `~gammapy.background.AdaptiveRingBackgroundEstimator`
        Ring background estimator or something with an equivalent API.

    Example
    ---------
    ::
        import numpy as np
        from astropy import units as u
        from astropy.coordinates import SkyCoord
        from gammapy.maps import Map, WcsGeom, MapAxis
        from gammapy.cube import MapMakerRing
        from gammapy.data import DataStore
        from gammapy.background import RingBackgroundEstimator

        # Create observation list
        data_store = DataStore.from_file("$GAMMAPY_DATA/hess-dl3-dr1/hess-dl3-dr3-with-background.fits.gz")
        data_sel = data_store.obs_table["TARGET_NAME"] == "MSH 15-52"
        obs_table = data_store.obs_table[data_sel]
        observations = data_store.get_observations(obs_table["OBS_ID"])

        #Define the geom
        energy_axis = MapAxis.from_edges(np.logspace(0, 5.0, 5), unit="TeV", name="energy")
        geom = WcsGeom.create(
             skydir=pos_msh1552,
             binsz=0.02,
             width=(5, 5),
             axes=[energy_axis])

        energy_axis = MapAxis.from_edges(np.logspace(-1., 1., 4), unit='TeV', name='energy')

        #Make a region mask
        regions = CircleSkyRegion(center=pos_msh1552, radius=0.3 * u.deg)
        mask = Map.from_geom(geom)
        mask.data = mask.geom.region_mask([regions], inside=False)

        #Instantiate ring background estimator
        ring_bkg = RingBackgroundEstimator(r_in="0.5 deg", width="0.3 deg")

        #Execute this class
        im = MapMakerRing(geom=geom, offset_max=2.0 * u.deg, exclusion_mask=mask, background_estimator=ring_bkg)
        images = im.run_images(observations)
    
    """

    def __init__(
        self, geom, offset_max, exclusion_mask=None, background_estimator=None
    ):
        super().__init__(
            geom=geom,
            offset_max=offset_max,
            exclusion_mask=exclusion_mask,
            geom_true=None,
        )
        self.background_estimator = background_estimator

    def _run(self, observations, sum_over_axis=False, spectrum=None, keepdims=False):
        """
        Parameters
        --------------
        observations : `~gammapy.data.Observations`
            Observations to process

        Returns
        -----------
        maps: dict of stacked maps.
            on: The counts map
            exposure_on: The on exposure map, which is just the
                 template background map from the IRF
            exposure_off: The off exposure map convolved with the ring
            off: The off map
        """
        selection = ["on", "exposure_on", "off", "exposure_off"]
        maps = self._get_empty_maps(selection)
        if sum_over_axis:
            maps = self._maps_sum_over_axes(maps, spectrum, keepdims)

        for obs in observations:
            try:
                obs_maker = self._get_obs_maker(obs, mode="strict")
            except NoOverlapError:
                log.info(
                    "Skipping observation {}, no overlap with map.".format(obs.obs_id)
                )
                continue
            except PartialOverlapError:
                log.info(
                    "Skipping observation {}, partial overlap with map.".format(
                        obs.obs_id
                    )
                )
                continue

            maps_obs = obs_maker.run()
            maps_obs["exclusion"] = obs_maker.exclusion_mask

            if sum_over_axis:
                maps_obs = self._maps_sum_over_axes(maps_obs, spectrum, keepdims)
                maps_obs["exclusion"] = obs_maker.exclusion_mask.get_image_by_idx([0])

            maps_obs_bkg = self.background_estimator.run(maps_obs)
            maps_obs.update(maps_obs_bkg)
            maps_obs["exposure_on"] = maps_obs.pop("background")
            maps_obs["on"] = maps_obs.pop("counts")

            # Now paste the returned maps on the ref geom
            for name in selection:
                data = maps_obs[name].quantity.to_value(maps[name].unit)
                maps[name].fill_by_coord(maps_obs[name].geom.get_coord(), data)

        self._maps = maps
        return maps

    def run_images(self, observations, spectrum=None, keepdims=False):
        """Returns dictionaries of stacked2D maps.
        The maps are summed over on the energy axis for a classical image analysis

        Parameters
        ---------

        observations: `~gammapy.data.Observations`
            Observations to process
        spectrum : `~gammapy.spectrum.models.SpectralModel`, optional
            Spectral model to compute the weights.
            Default is power-law with spectral index of 2.
        keepdims : bool, optional
            If this is set to True, the energy axes is kept with a single bin.
            If False, the energy axes is removed

        Returns
        -----------
        maps: dict of stacked maps.
            on: The counts map
            exposure_on: The on exposure map, which is just the
                 template background map from the IRF
            exposure_off: The off exposure map convolved with the ring
            off: The off map

        """

        return self._run(
            observations, sum_over_axis=True, spectrum=spectrum, keepdims=keepdims
        )

    def run(self, observations):
        """Returns a list of dictionaries of 3D maps
        Significance and excess can be computed for each slice

        Returns
        -----------
        maps: dict of stacked maps.
            on: The counts map
            exposure_on: The on exposure map, which is just the
                 template background map from the IRF
            exposure_off: The off exposure map convolved with the ring
            off: The off map
        """
        return self._run(observations, sum_over_axis=False)
