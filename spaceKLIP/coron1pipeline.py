from __future__ import division

import matplotlib
matplotlib.rcParams.update({'font.size': 14})


# =============================================================================
# IMPORTS
# =============================================================================

import os
import pdb
import sys

import astropy.io.fits as pyfits
import matplotlib.pyplot as plt
import numpy as np

from tqdm.auto import trange

from jwst.lib import reffile_utils
from jwst.datamodels import dqflags, RampModel, SaturationModel
from jwst.pipeline import Detector1Pipeline, Image2Pipeline, Coron3Pipeline

from webbpsf_ext import robust

import logging
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


# =============================================================================
# MAIN
# =============================================================================

class Coron1Pipeline_spaceKLIP(Detector1Pipeline):
    """
    The spaceKLIP JWST stage 1 pipeline class.
    
    Apply all calibration steps to raw JWST ramps to produce 
    a 2-D slope product. Custom sub-class of ``Detector1Pipeline`` 
    with modifications for coronagraphic data.
    
    Included steps are: group_scale, dq_init, saturation, ipc, 
    superbias, refpix, rscd, firstframe, lastframe, linearity, 
    dark_current, reset, persistence, jump detection, ramp_fit, 
    and gain_scale. 
    """
    
    class_alias = "calwebb_coron1"

    spec = """
        save_intermediates = boolean(default=False) # Save all intermediate step results
        rate_int_outliers  = boolean(default=True)  # Flag outlier pixels in rateints
        return_rateints    = boolean(default=False) # Return rateints or rate product?
        remove_ktc         = boolean(default=True) # Remove kTC noise from data
        remove_fnoise      = boolean(default=True) # Remove 1/f noise from data
    """
    
    def __init__(self,
                 **kwargs):
        """
        Initialize the spaceKLIP JWST stage 1 pipeline class.
        
        Parameters
        ----------
        **kwargs : keyword arguments
            Default JWST stage 1 detector pipeline keyword arguments.
        
        Returns
        -------
        None.
        
        """
        
        # Initialize Detector1Pipeline class.
        super(Coron1Pipeline_spaceKLIP, self).__init__(**kwargs)
        
        # Set additional parameters in saturation step
        self.saturation.grow_diagonal = False
        self.saturation.flag_rcsat = True

        # Initialize reference pixel correction parameters
        self.refpix.nlower = 4
        self.refpix.nupper = 4
        self.refpix.nrow_off = 0
        # NOTE: nleft, right, and ncol_off don't actually do anything.
        #   For 1/f noise correction, set the `remove_fnoise` attribute to True
        #   to model and subtract the 1/f noise.
        self.refpix.nleft = 0
        self.refpix.nright = 0
        self.refpix.ncol_off = 0

        # Ramp fit saving options
        # NOTE: `save_calibrated_ramp` is already a Detector1Pipeline property
        self.ramp_fit.save_calibrated_ramp = False
        
    
    def process(self,
                input):
        """
        Process an input JWST datamodel with the spaceKLIP JWST stage 1
        pipeline.
        
        Parameters
        ----------
        input : jwst.datamodel
            Input JWST datamodel to be processed.
        
        Returns
        -------
        output : jwst.datamodel
            Output JWST datamodel.
        
        """
        
        # Open input as ramp model.
        input = RampModel(input)
        
        # Process MIR & NIR exposures differently.
        instrument = input.meta.instrument.name
        if instrument == 'MIRI':
            input = self.run_step(self.group_scale, input)
            input = self.run_step(self.dq_init, input)
            input = self.run_step(self.saturation, input)
            input = self.run_step(self.ipc, input)
            input = self.run_step(self.firstframe, input)
            input = self.run_step(self.lastframe, input)
            input = self.run_step(self.reset, input)
            input = self.run_step(self.linearity, input)
            input = self.run_step(self.rscd, input)
            input = self.run_step(self.dark_current, input)
            input = self.do_refpix(input)
            input = self.run_step(self.charge_migration, input)
            input = self.run_step(self.jump, input)
        else:
            input = self.run_step(self.group_scale, input)
            input = self.run_step(self.dq_init, input)
            input = self.do_saturation(input)
            input = self.run_step(self.ipc, input)
            input = self.run_step(self.superbias, input)
            input = self.do_refpix(input)
            input = self.run_step(self.linearity, input)
            input = self.run_step(self.persistence, input)
            input = self.run_step(self.dark_current, input)
            input = self.run_step(self.charge_migration, input)
            input = self.run_step(self.jump, input)
            if self.remove_ktc or self.remove_fnoise:
                input = self.subtract_ktc(input)
            if self.remove_fnoise:
                input = self.subtract_fnoise(input, model_type='savgol')
        
        # save the corrected ramp data, if requested
        if self.ramp_fit.save_calibrated_ramp or self.save_calibrated_ramp or self.save_intermediates:
            self.save_model(input, suffix='ramp')
        
        # Run ramp fitting & gain scale correction.
        res = self.run_step(self.ramp_fit, input, save_results=False)
        rate, rateints = (res, None) if self.ramp_fit.skip else res
        if self.rate_int_outliers and rateints is not None:
            # Flag additional outliers by comparing rateints and refit ramp
            input = self.apply_rateint_outliers(rateints, input)
            if input is None:
                input, ints_model = (rate, rateints)
            else:
                res = self.run_step(self.ramp_fit, input, save_results=False)
                input, ints_model = (res, None) if self.ramp_fit.skip else res
        else:
            # input is the rate product, ints_model is the rateints product
            input, ints_model = (rate, rateints)

        if input is None:
            self.ramp_fit.log.info('NoneType returned from ramp fitting. Gain scale correction skipped')
        else:
            self.gain_scale.suffix = 'rate'
            input = self.run_step(self.gain_scale, input, save_results=False)
        
        # apply the gain scale step to the multi-integration product,
        # if it exists, and then save it
        if ints_model is not None:
            self.gain_scale.suffix = 'rateints'
            ints_model = self.run_step(self.gain_scale, ints_model, save_results=False)
            if self.save_results or self.save_intermediates:
                self.save_model(ints_model, suffix='rateints')

        # Setup output file.
        self.setup_output(input)

        if self.return_rateints:
            return ints_model
        
        return input

    def setup_output(self, input):
        """Determine output file name suffix"""
        if input is None:
            return None
        # Determine the proper file name suffix to use later
        if input.meta.cal_step.ramp_fit == 'COMPLETE':
            if self.return_rateints:
                self.suffix = 'rateints'
            else:
                self.suffix = 'rate'
        else:
            self.suffix = 'ramp'

    def run_step(self,
                 step_obj,
                 input,
                 save_results=None,
                 **kwargs):
        """
        Run a JWST pipeline step.
        
        Parameters
        ----------
        step_obj : jwst.step
            JWST pipeline step to be run.
        input : jwst.datamodel
            Input JWST datamodel to be processed.
        save_results : bool, optional
            Save the JWST pipeline step product? None will default to the JWST
            pipeline step default. The default is None.
        **kwargs : keyword arguments
            Default JWST pipeline step keyword arguments.
        
        Returns
        -------
        res : jwst.datamodel
            Output JWST datamodel.
        
        """
        
        # Determine if we're saving results for real
        if step_obj.skip:
            # Skip save if step is skipped
            really_save_results = False
        elif (save_results is not None):
            # Use keyword specifications
            really_save_results = save_results
        elif self.save_intermediates:
            # Use save_intermediates attribute
            really_save_results = True
        elif step_obj.save_results:
            # Use step attribute
            really_save_results = True
        else:
            # Saving is unspecified
            really_save_results = False
        
        # Run step. Don't save results yet.
        step_save_orig = step_obj.save_results
        step_obj.save_results = False
        res = step_obj(input)
        step_obj.save_results = step_save_orig
        
        # Check if group scale correction or gain scale correction were skipped.
        if step_obj is self.group_scale:
            if res.meta.cal_step.group_scale == 'SKIPPED':
                really_save_results = False
        elif step_obj is self.gain_scale:
            if res.meta.cal_step.gain_scale == 'SKIPPED':
                really_save_results = False
        
        # Save results.
        if really_save_results:
            step_obj.output_dir = self.output_dir
            if isinstance(res, (tuple)):
                self.save_model(res[0], suffix=step_obj.suffix+'0')
                self.save_model(res[1], suffix=step_obj.suffix+'1')
            else:
                self.save_model(res, suffix=step_obj.suffix)
        
        return res
    
    def do_saturation(self,
                      input,
                      **kwargs):
        """
        Do the default or a custom saturation correction.
        
        Parameters
        ----------
        input : jwst.datamodel
            Input JWST datamodel to be processed.
        **kwargs : keyword arguments
            Default JWST stage 1 saturation step keyword arguments.
        
        Returns
        -------
        res : jwst.datamodel
            Output JWST datamodel.
        
        """

        from .imagetools import expand_mask
        
        # Save original step parameter.
        npix_grow = self.saturation.n_pix_grow_sat
        
        # Flag RC pixels as saturated?
        flag_rcsat = self.saturation.flag_rcsat
        if flag_rcsat:
            mask_rc = (input.pixeldq & dqflags.pixel['RC']) > 0
            # Do a bitwise OR of RC mask with groupdq to flip saturation bits
            input.groupdq = input.groupdq | (mask_rc * dqflags.pixel['SATURATED'])

        # Run step with default settings.
        if self.saturation.grow_diagonal or npix_grow == 0:
            res = self.run_step(self.saturation, input, **kwargs)
        else: # No diagonal growth
            # Initial run with 0 pixel growth
            self.saturation.n_pix_grow_sat = 0
            res = self.run_step(self.saturation, input, **kwargs)
            # Reset to original value
            self.saturation.n_pix_grow_sat = npix_grow

            self.saturation.log.info(f'Growing saturation flags by {npix_grow} pixels. Ignoring diagonal growth.')
            # Update saturation dq flags to grow in vertical and horizontal directions
            # Get saturation mask
            mask_sat = (input.groupdq & dqflags.pixel['SATURATED']) > 0

            # Expand the mask by npix_grow pixels
            mask_sat = expand_mask(mask_sat, npix_grow, grow_diagonal=False)

            # Do a bitwise OR of new mask with groupdq to flip saturation bit
            res.groupdq = res.groupdq | (mask_sat * dqflags.pixel['SATURATED'])

            # Do the same for the zero frames
            zframes = res.zeroframe if res.meta.exposure.zero_frame else None
            if zframes is not None:
                # Saturated zero frames have already been set to 0
                mask_sat = (zframes==0) | mask_rc if flag_rcsat else (zframes==0)
                # Expand the mask by npix_grow pixels
                mask_sat = expand_mask(mask_sat, npix_grow, grow_diagonal=False)
                # Set saturated pixels to 0 in zero frames
                res.zeroframe[mask_sat] = 0

        return res
    
    def apply_rateint_outliers(self, 
                               rateints_model, 
                               ramp_model, 
                               **kwargs):
        """Get pixel outliers in rateint model and apply to ramp model DQ
        
        Parameters
        ----------
        rateints_model : `~jwst.datamodels.CubeModel`
            Rateints model to use for outlier detection
        ramp_model : `~jwst.datamodels.RampModel`
            Ramp model to update with outlier flags

        Keyword Args
        ------------
        sigma_cut : float
            Sigma cut for outlier detection.
            Default is 5.
        nint_min : int
            Minimum number of integrations required for outlier detection.
            Default is 5.
        """

        from .imagetools import cube_outlier_detection

        inst = rateints_model.meta.instrument.name
        data = rateints_model.data[1:] if 'MIRI' in inst else rateints_model.data

        indbad = cube_outlier_detection(data, **kwargs)

        # Reshape outlier mask to match ramp data
        nint, ng, ny, nx = ramp_model.data.shape
        bpmask = indbad.reshape([nint, 1, ny, nx])
        if ng>1:
            bpmask = np.repeat(bpmask, ng, axis=1)

        # Update DO_NOT_USE and JUMP_DET flags
        mask_dnu = (ramp_model.groupdq & dqflags.pixel['DO_NOT_USE']) > 0
        mask_jd  = (ramp_model.groupdq & dqflags.pixel['JUMP_DET']) > 0

        # Update DO_NOT_USE and JUMP_DET flags with outlier mask
        mask_dnu = mask_dnu | bpmask
        mask_jd  = mask_jd  | bpmask

        # Update ramp model groupdq
        ramp_model.groupdq = ramp_model.groupdq | (mask_dnu * dqflags.pixel['DO_NOT_USE'])
        ramp_model.groupdq = ramp_model.groupdq | (mask_jd  * dqflags.pixel['JUMP_DET'])

        return ramp_model

    def do_refpix(self,
                  input,
                  **kwargs):
        """ Do the default or a custom pseudo reference pixel correction.
        
        If full frame, perform RefPix as normal.
        If no ref rows or columns specified, then perform RefPix as normal.

        Otherwise, temporarily set reference rows and columns in the DQ flags
        and force the reference correction. Can set number of reference
        rows and column via the  `nlower`, `nupper`, `nleft`, `nright` 
        attributes.

        Parameters
        ----------
        input : jwst.datamodel
            Input JWST datamodel to be processed.
        **kwargs : keyword arguments
            Default JWST stage 1 refpix step keyword arguments.
        
        Returns
        -------
        res : jwst.datamodel
            Output JWST datamodel.
        
        """
        
        # Is this a full frame observation?
        is_full_frame = 'FULL' in input.meta.subarray.name.upper()

        # Get number of reference pixels explicitly specified
        nlower = self.refpix.nlower
        nupper = self.refpix.nupper
        # The pipeline does not currently support col refpix correction for subarrays
        # nleft  = self.refpix.nleft 
        # nright = self.refpix.nright
        nref_set = nlower + nupper #+ nleft + nright
        
        # Perform normal operations if full frame or no refpix pixels specified
        if is_full_frame or nref_set==0:
            return self.run_step(self.refpix, input, **kwargs)
        else:
            return self.do_pseudo_refpix(input, **kwargs)
    
    def do_pseudo_refpix(self,
                         input,
                         **kwargs):
        """ Do a pseudo reference pixel correction
        
        Flag the requested edge rows and columns as reference pixels, 
        run the JWST stage 1 refpix step, and then unflag those 
        "pseudo" reference pixels.
        
        Parameters
        ----------
        input : jwst.datamodel
            Input JWST datamodel to be processed.
        **kwargs : keyword arguments
            Default JWST stage 1 refpix step keyword arguments.
        
        Returns
        -------
        res : jwst.datamodel
            Output JWST datamodel.
        """
        
        # Get number of custom reference pixel rows & columns.
        nlower = self.refpix.nlower
        nupper = self.refpix.nupper
        nleft = self.refpix.nleft
        nright = self.refpix.nright
        nrow_off = self.refpix.nrow_off
        ncol_off = self.refpix.ncol_off

        # Flag custom reference pixel rows & columns.
        self.refpix.log.info(f'Flagging [{nlower}, {nupper}] references rows at [bottom, top] of array')
        self.refpix.log.info(f'Flagging [{nleft}, {nright}] references columns at [left, right] of array')

        # Update pixel DQ mask to manually set reference pixels
        log.info(f'Flagging [{nlower}, {nupper}] references rows at [bottom, top] of array')
        log.info(f'Flagging [{nleft}, {nright}] references rows at [left, right] of array')
        pixeldq_orig = input.pixeldq.copy()
        if nlower>0:
            ib1 = nrow_off
            ib2 = ib1 + nlower
            input.pixeldq[ib1:ib2,:] = input.pixeldq[ib1:ib2,:]  | dqflags.pixel['REFERENCE_PIXEL']
        if nupper>0:
            it1 = -1 * (nupper + nrow_off)
            it2 = None if nrow_off == 0 else -1 * nrow_off
            input.pixeldq[it1:it2,:] = input.pixeldq[it1:it2,:] | dqflags.pixel['REFERENCE_PIXEL']
        if nleft>0:
            il1 = ncol_off
            il2 = il1 + nleft
            input.pixeldq[:,il1:il2] = input.pixeldq[:,il1:il2] | dqflags.pixel['REFERENCE_PIXEL']
        if nright>0:
            ir1 = -1 * (nright + ncol_off)
            ir2 = None if ncol_off == 0 else -1 * ncol_off
            input.pixeldq[:,ir1:ir2] = input.pixeldq[:,ir1:ir2] | dqflags.pixel['REFERENCE_PIXEL']

        # Turn off side reference pixels?
        use_side_orig = self.refpix.use_side_ref_pixels 
        if nleft + nright == 0:
            self.refpix.use_side_ref_pixels = False
        
        # Run step with custom reference pixel rows & columns.
        res = self.run_step(self.refpix, input, **kwargs)
        
        # Apply original step parameter.
        self.refpix.use_side_ref_pixels = use_side_orig
        
        # Unflag custom reference pixel rows & columns.
        self.refpix.log.info('Removing custom reference pixel flags')
        if nlower>0:
            res.pixeldq[ib1:ib2,:] = pixeldq_orig[ib1:ib2,:]
        if nupper>0:
            res.pixeldq[it1:it2,:] = pixeldq_orig[it1:it2,:]
        if nleft>0:
            res.pixeldq[:,il1:il2] = pixeldq_orig[:,il1:il2]
        if nright>0:
            res.pixeldq[:,ir1:ir2] = pixeldq_orig[:,ir1:ir2]

        return res

    def _fit_slopes(self,
                    input,
                    sat_frac=0.5):
        """Fit slopes to each integration
        
        Uses custom `cube_fit` function to fit slopes to each integration.
        Returns aray of slopes and bias values for each integration.
        Bias and slope arrays have shape (nints, ny, nx).

        Parameters
        ----------
        input : jwst.datamodel
            Input JWST datamodel housing the data to be fit.
        sat_frac : float
            Saturation threshold for fitting. Values above
            this fraction of the saturation level are ignored.
            Default is 0.5 to ensure that the fit is within 
            the linear range.
        """

        from .imagetools import cube_fit

        # Get saturation reference file
        # Get the name of the saturation reference file
        sat_name = self.saturation.get_reference_file(input, 'saturation')

        # Open the reference file data model
        sat_model = SaturationModel(sat_name)

        # Extract subarray from saturation reference file, if necessary
        if reffile_utils.ref_matches_sci(input, sat_model):
            sat_thresh = sat_model.data.copy()
        else:
            ref_sub_model = reffile_utils.get_subarray_model(input, sat_model)
            sat_thresh = ref_sub_model.data.copy()
            ref_sub_model.close()

        # Close the reference file
        sat_model.close()

        # Perform ramp fit to data to get bias offset
        group_time = input.meta.exposure.group_time
        ngroups = input.meta.exposure.ngroups
        nints = input.meta.exposure.nints
        tarr = np.arange(1, ngroups+1) * group_time
        data = input.data

        bias_arr = []
        slope_arr = []
        for i in range(nints):
            # Get group-level bpmask for this integration
            groupdq = input.groupdq[i]
            # Make sure to accumulate the group-level dq mask
            bpmask_arr = np.cumsum(groupdq, axis=0) > 0
            cf = cube_fit(tarr, data[i], bpmask_arr=bpmask_arr,
                          sat_vals=sat_thresh, sat_frac=sat_frac)
            bias_arr.append(cf[0])
            slope_arr.append(cf[1])
        bias_arr = np.asarray(bias_arr)
        slope_arr = np.asarray(slope_arr)

        # bias and slope arrays have shape [nints, ny, nx]
        # bias values are in units of DN and slope in DN/sec
        return bias_arr, slope_arr

    def subtract_ktc(self,
                     input,
                     sat_frac=0.5):
        
        bias_arr, _ = self._fit_slopes(input, sat_frac=sat_frac)

        # Subtract bias from each integration
        nints = input.meta.exposure.nints
        for i in range(nints):
            input.data[i] -= bias_arr[i]

        return input
    
    def subtract_fnoise(self,
                        input,
                        sat_frac=0.5,
                        **kwargs):
        """Model and subtract 1/f noise from each integration
        
        TODO: Make this into a Step class.
        TODO: Automatic function to determine if correction is necessary.

        Parameters
        ----------
        input : jwst.datamodel
            Input JWST datamodel to be processed.

        Keyword Args
        ------------
        model_type : str
            Must be 'median', 'mean', or 'savgol'. For 'mean' case,
            it uses a robust mean that ignores outliers and NaNs.
            The 'median' case uses `np.nanmedian`. The 'savgol' case
            uses a Savitzky-Golay filter to model the 1/f noise, 
            iteratively rejecting outliers from the model fit relative
            to the median model. The default is 'savgol'.
        """
        
        from .fnoise_clean import CleanSubarray

        is_full_frame = 'FULL' in input.meta.subarray.name.upper()
        nints    = input.meta.exposure.nints
        ngroups  = input.meta.exposure.ngroups
        noutputs = input.meta.exposure.noutputs

        if is_full_frame:
            assert noutputs == 4, 'Full frame data must have 4 outputs'
        else:
            assert noutputs == 1, 'Subarray data must have 1 output'

        ny, nx = input.data.shape[-2:]
        chsize = ny // noutputs

        # Fit slopes to get signal mask
        # Grab slopes if they've already been computed
        _, slope_arr = self._fit_slopes(input, sat_frac=sat_frac)
        slope_mean = robust.mean(slope_arr, axis=0)

        # Generate a mean signal ramp to subtract from each group
        group_time = input.meta.exposure.group_time
        ngroups = input.meta.exposure.ngroups
        tarr = np.arange(1, ngroups+1) * group_time
        signal_mean_ramp = slope_mean * tarr.reshape([-1,1,1])

        # Subtract 1/f noise from each integration
        data = input.data
        for i in trange(nints):
            cube = data[i]
            groupdq = input.groupdq[i]
            # Cumulative sum of group DQ flags
            bpmask_arr = np.cumsum(groupdq, axis=0) > 0
            for j in range(ngroups):
                # Exclude bad pixels
                im_mask = ~bpmask_arr[j] #& mask
                for ch in range(noutputs):
                    # Get channel x-indices
                    x1 = int(ch*chsize)
                    x2 = int(x1 + chsize)

                    # Channel subarrays
                    imch = cube[j, :, x1:x2]
                    sigch = signal_mean_ramp[j, :, x1:x2]
                    good_mask = im_mask[:, x1:x2]

                    # Remove averaged signal goup
                    imch_diff = imch - sigch

                    # Subtract 1/f noise
                    nf_clean = CleanSubarray(imch_diff, good_mask)
                    nf_clean.fit(**kwargs)
                    # Subtract model from data
                    data[i,j,:,x1:x2] -= nf_clean.model

                    del nf_clean

        return input

def run_single_file(fitspath, output_dir, steps={}, verbose=False, **kwargs):
    """ Run the JWST stage 1 detector pipeline on a single file.
    
    WARNING: Will overwrite exiting files.

    This customized implementation can:
    - Do a custom saturation correction where only the bottom/top/left/right
      and not the diagonal pixels next to a saturated pixel are flagged.
    - Do a pseudo reference pixel correction. Therefore, flag the requested
      edge rows and columns as reference pixels, run the JWST stage 1 refpix
      step, and unflag the pseudo reference pixels again. Only applicable for
      subarray data.
    - Remove horizontal 1/f noise spatial striping in NIRCam data.
    
    Parameters
    ----------
    fitspath : str
        Path to the input FITS file (uncal.fits).
    output_dir : str
        Path to the output directory to save the resulting data products.
    steps : dict, optional
        See here for how to use the steps parameter:
        https://jwst-pipeline.readthedocs.io/en/latest/jwst/user_documentation/running_pipeline_python.html#configuring-a-pipeline-step-in-python
        Custom step parameters are:
        - saturation/grow_diagonal : bool, optional
            Flag also diagonal pixels (or only bottom/top/left/right)? 
            The default is True.
        - saturation/flag_rcsat : bool, optional
            Flag RC pixels as saturated? The default is True.
        - refpix/nlower : int, optional
            Number of rows at frame bottom that shall be used as additional
            reference pixels. The default is 0.
        - refpix/nupper : int, optional
            Number of rows at frame top that shall be used as additional
            reference pixels. The default is 0.
        - ramp_fit/save_calibrated_ramp : bool, optional
            Save the calibrated ramp? The default is False.

        Additional useful step parameters:
        - saturation/n_pix_grow_sat : int, optional
            Number of pixels to grow for saturation flagging. Default is 1.
        - ramp_fit/suppress_one_group : bool, optional
            If True, skips slope calc for pixels with only 1 available group. 
            Default: False.
        - ramp_fit/maximum_cores : str, optional
            max number of parallel processes to create during ramp fitting.
            'none', 'quarter', 'half', or 'all'. Default: 'quarter'.
        The default is {}. 
    subdir : str, optional
        Name of the directory where the data products shall be saved. The
        default is 'stage1'.
    
    Keyword Args
    ------------
    save_results : bool, optional
        Save the JWST pipeline step products? The default is True.
    save_calibrate_ramp : bool
        Save intermediate step that is the calibrated ramp? 
        Default is False.
    save_intermediates : bool, optional
        Save intermediate steps, such as dq_init, saturation, refpix,
        jump, linearity, ramp, etc. Default is False.
    return_rateints : bool, optional
        Return the rateints model instead of rate? Default is False.
    rate_int_outliers : bool, optional
        Flag outlier pixels in rateints? Default is True.
        Uses the `cube_outlier_detection` function and requires
        a minimum of 5 integrations.
    remove_ktc : bool, optional
        Remove kTC noise by fitting ramp data to get bias? 
        Default is True.
    remove_fnoise : bool, optional
        Remove 1/f noise from data at group level? 
        Default is True.
    skip_charge : bool, optional
        Skip charge migration flagging step? Default: False.
    skip_jump : bool, optional
        Skip jump detection step? Default: False.
    skip_dark : bool, optional
        Skip dark current subtraction step? Default is True for 
        subarrays and False for full frame data.
        Dark current cal files for subarrays are really low SNR.
    skip_ipc : bool, optional
        Skip IPC correction step? Default: True.
    skip_persistence : bool, optional
        Skip persistence correction step? Default: True.
        Doesn't currently do anything.

    Returns
    -------
    None.
    
    """

    from webbpsf_ext.analysis_tools import nrc_ref_info

    # Print all info message if verbose, otherwise only errors or critical.
    from .logging_tools import all_logging_disabled
    log_level = logging.INFO if verbose else logging.ERROR

    # Initialize Coron1Pipeline.
    with all_logging_disabled(log_level):
        pipeline = Coron1Pipeline_spaceKLIP(output_dir=output_dir)

    # Options for saving results
    pipeline.save_results         = kwargs.get('save_results', True)
    pipeline.save_calibrated_ramp = kwargs.get('save_calibrated_ramp', False)
    pipeline.save_intermediates   = kwargs.get('save_intermediates', False)
    pipeline.return_rateints      = kwargs.get('return_rateints', False)

    # Skip certain steps?
    pipeline.charge_migration.skip = kwargs.get('skip_charge', False)
    pipeline.jump.skip             = kwargs.get('skip_jump', False)
    pipeline.ipc.skip              = kwargs.get('skip_ipc', True)
    pipeline.persistence.skip      = kwargs.get('skip_persistence', True)
    # Skip dark current for subarray by default, but not full frame
    skip_dark     = kwargs.get('skip_dark', None)
    if skip_dark is None:
        hdr0 = pyfits.getheader(fitspath, ext=0)
        is_full_frame = 'FULL' in hdr0['SUBARRAY']
        skip_dark = not is_full_frame  # False if full frame, otherwise True
    pipeline.dark_current.skip = skip_dark

    # Determine reference pixel correction parameters based on
    # instrument aperture name for NIRCam
    hdr0 = pyfits.getheader(fitspath, 0)
    if hdr0['INSTRUME'] == 'NIRCAM':
        # Array of reference pixel borders [lower, upper, left, right]
        nb, nt, nl, nr = nrc_ref_info(hdr0['APERNAME'], orientation='sci')
    else:
        nb, nt, nl, nr = (0, 0, 0, 0)
    # If everything is 0, set to defult to 4 around the edges
    if nb + nt == 0:
        nb = nt = 4
    if nl + nr == 0:
        nl = nr = 4
    pipeline.refpix.nlower   = kwargs.get('nlower', nb)
    pipeline.refpix.nupper   = kwargs.get('nupper', nt)
    pipeline.refpix.nrow_off = kwargs.get('nrow_off', 0)

    # Set some Step parameters
    pipeline.jump.rejection_threshold              = kwargs.get('rejection_threshold', 4)
    pipeline.jump.three_group_rejection_threshold  = kwargs.get('three_group_rejection_threshold', 4)
    pipeline.jump.four_group_rejection_threshold   = kwargs.get('four_group_rejection_threshold', 4)
    pipeline.saturation.n_pix_grow_sat = kwargs.get('n_pix_grow_sat', 1)
    pipeline.saturation.grow_diagonal  = kwargs.get('grow_diagonal', False)
    pipeline.saturation.flag_rcsat     = kwargs.get('flag_rcsat', True)
    pipeline.rate_int_outliers         = kwargs.get('rate_int_outliers', True)

    # 1/f noise correction
    pipeline.remove_ktc    = kwargs.get('remove_ktc', True)
    pipeline.remove_fnoise = kwargs.get('remove_fnoise', True)

    # Skip pixels with only 1 group in ramp_fit?
    pipeline.ramp_fit.suppress_one_group = kwargs.get('suppress_one_group', False)
    # Number of processor cores to use during ramp fitting process
    # 'none', 'quarter', 'half', or 'all'
    pipeline.ramp_fit.maximum_cores      = kwargs.get('maximum_cores', 'quarter')

    # Set parameters from step dictionary
    for key1 in steps.keys():
        for key2 in steps[key1].keys():
            setattr(getattr(pipeline, key1), key2, steps[key1][key2])
    
    # Run Coron1Pipeline. Raise exception on error.
    # Ensure that pipeline is closed out.
    try:
        with all_logging_disabled(log_level):
            res = pipeline.run(fitspath)
    except Exception as e:
        raise RuntimeError(
            'Caught exception during pipeline processing.'
            '\nException: {}'.format(e)
        )
    finally:
        pipeline.closeout()

    if isinstance(res, list):
        res = res[0]

    return res

def run_obs(database,
            steps={},
            subdir='stage1',
            overwrite=False,
            quiet=False,
            verbose=False,
            **kwargs):
    """
    Run the JWST stage 1 detector pipeline on the input observations database.
    This customized implementation can:
    - Do a custom saturation correction where only the bottom/top/left/right
      and not the diagonal pixels next to a saturated pixel are flagged.
    - Do a pseudo reference pixel correction. Therefore, flag the requested
      edge rows and columns as reference pixels, run the JWST stage 1 refpix
      step, and unflag the pseudo reference pixels again. Only applicable for
      subarray data.
    - Remove horizontal 1/f noise spatial striping in NIRCam data.
    
    Parameters
    ----------
    database : spaceKLIP.Database
        SpaceKLIP database on which the JWST stage 1 pipeline shall be run.
    steps : dict, optional
        See here for how to use the steps parameter:
        https://jwst-pipeline.readthedocs.io/en/latest/jwst/user_documentation/running_pipeline_python.html#configuring-a-pipeline-step-in-python
        Custom step parameters are:
        - saturation/grow_diagonal : bool, optional
            Flag also diagonal pixels (or only bottom/top/left/right)? 
            The default is True.
        - saturation/flag_rcsat : bool, optional
            Flag RC pixels as saturated? The default is True.
        - refpix/nlower : int, optional
            Number of rows at frame bottom that shall be used as additional
            reference pixels. The default is 0.
        - refpix/nupper : int, optional
            Number of rows at frame top that shall be used as additional
            reference pixels. The default is 0.
        - ramp_fit/save_calibrated_ramp : bool, optional
            Save the calibrated ramp? The default is False.
        Additional useful step parameters:
        - saturation/n_pix_grow_sat : int, optional
            Number of pixels to grow for saturation flagging. Default is 1.
        - ramp_fit/suppress_one_group : bool, optional
            If True, skips slope calc for pixels with only 1 available group. 
            Default: False.
        - ramp_fit/maximum_cores : str, optional
            max number of parallel processes to create during ramp fitting.
            'none', 'quarter', 'half', or 'all'. Default: 'quarter'.
        The default is {}. 
        Each of these parameters can be passed directly through `kwargs`.
    subdir : str, optional
        Name of the directory where the data products shall be saved. The
        default is 'stage1'.
    overwrite : bool, optional
        Overwrite existing files? Default is False.
    quiet : bool, optional
        Use progress bar to track progress instead of messages. 
        Overrides verbose and sets it to False. Default is False.
    verbose : bool, optional
        Print all info messages? Default is False.
    
    Keyword Args
    ------------
    save_results : bool, optional
        Save the JWST pipeline step products? The default is True.
    save_calibrate_ramp : bool
        Save intermediate step that is the calibrated ramp? 
        Default is False.
    save_intermediates : bool, optional
        Save intermediate steps, such as dq_init, saturation, refpix,
        jump, linearity, ramp, etc. Default is False.
    return_rateints : bool, optional
        Return the rateints model instead of rate? Default is False.
    rate_int_outliers : bool, optional
        Flag outlier pixels in rateints? Default is True.
        Uses the `cube_outlier_detection` function and requires
        a minimum of 5 integrations.
    remove_ktc : bool, optional
        Remove kTC noise by fitting ramp data to get bias? 
        Default is True.
    remove_fnoise : bool, optional
        Remove 1/f noise from data at group level? 
        Default is True.
    skip_charge : bool, optional
        Skip charge migration flagging step? Default: False.
    skip_jump : bool, optional
        Skip jump detection step? Default: False.
    skip_dark : bool, optional
        Skip dark current subtraction step? Default is True for 
        subarrays and False for full frame data.
        Dark current cal files for subarrays are really low SNR.
    skip_ipc : bool, optional
        Skip IPC correction step? Default: True.
    skip_persistence : bool, optional
        Skip persistence correction step? Default: True.
        Doesn't currently do anything.

    Returns
    -------
    None.
    
    """

    # Set output directory.
    output_dir = os.path.join(database.output_dir, subdir)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # Get list of concatenation keys.
    keys = list(database.obs.keys())
    nkeys = len(keys)
    if quiet:
        verbose = False
        itervals = trange(nkeys, desc='Concatenations')
    else:
        itervals = range(nkeys)

    # Loop through concatenations.
    for i in itervals:
        key = keys[i]
        if not quiet: log.info('--> Concatenation ' + key)
        
        # Loop through FITS files.
        nfitsfiles = len(database.obs[key])
        jtervals = trange(nfitsfiles, desc='FITS files', leave=False) if quiet else range(nfitsfiles)
        for j in jtervals:

            # Skip non-stage 0 files.
            head, tail = os.path.split(database.obs[key]['FITSFILE'][j])
            fitspath = os.path.abspath(database.obs[key]['FITSFILE'][j])
            if database.obs[key]['DATAMODL'][j] != 'STAGE0':
                if not quiet: log.info('  --> Coron1Pipeline: skipping non-stage 0 file ' + tail)
                continue

            # Get expected output file name
            outfile_name = tail.replace('uncal.fits', 'rateints.fits')
            fitsout_path = os.path.join(output_dir, outfile_name)

            # Skip if file already exists and overwrite is False.
            if os.path.isfile(fitsout_path) and not overwrite:
                if not quiet: log.info('  --> Coron1Pipeline: skipping already processed file ' + tail)
            else:
                if not quiet: log.info('  --> Coron1Pipeline: processing ' + tail)
                res = run_single_file(fitspath, output_dir, steps=steps, 
                                      verbose=verbose, **kwargs)
            
            # Update spaceKLIP database.
            database.update_obs(key, j, fitsout_path)
