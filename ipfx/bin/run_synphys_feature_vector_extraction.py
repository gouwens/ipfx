import numpy as np
from ipfx.ephys_data_set import Sweep, SweepSet
import ipfx.feature_vectors as fv

from neuroanalysis.miesnwb import MiesNwb
import argschema as ags
import os
import json
import logging
import traceback
from collections import defaultdict
from multiprocessing import Pool


class SynPhysFeatureVectorSchema(ags.ArgSchema):
    nwb_file = ags.fields.InputFile(default="/allen/programs/celltypes/workgroups/ivscc/nathang/synphys_ephys/data/2018_11_06_140408-compressed.nwb")
    output_dir = ags.fields.OutputDir(default="/allen/programs/celltypes/workgroups/ivscc/nathang/fv_output/")
    project = ags.fields.String(default="SynPhys")


class MPSweep(Sweep):
    """Adapter for neuroanalysis.Recording => ipfx.Sweep
    """
    def __init__(self, rec):
        pri = rec['primary']
        cmd = rec['command']
        t = pri.time_values
        v = pri.data * 1e3  # convert to mV
        holding = rec.stimulus.items[0].amplitude  # todo: select holding item explicitly; don't assume it is [0]
        i = (cmd.data - holding) * 1e12   # convert to pA with holding current removed
        srate = pri.sample_rate
        sweep_num = rec.parent.key
        clamp_mode = rec.clamp_mode  # this will be 'ic' or 'vc'; not sure if that's right

        Sweep.__init__(self, t, v, i,
                       clamp_mode=clamp_mode,
                       sampling_rate=srate,
                       sweep_number=sweep_num,
                       epochs=None)
        self.select_epoch('stim')
        self.duration = self.t[-1] - self.t[0]
        self.select_epoch('sweep')


stim_list = [
    'TargetV_DA_0',
    'If_Curve_DA_0',
    # 'Chirp_DA_0',
    # 'TargetV_DA_0'
]

def sweeps_dict_from_cell(cell):
    recordings = cell.electrode.recordings
    sweeps_dict = {stim:list() for stim in stim_list}
    for recording in recordings:
        for name in stim_list:
            if recording.patch_clamp_recording.stim_name == name:
                sweeps_dict[name].append(recording.sync_rec.ext_id)
    return sweeps_dict

def min_duration_of_sweeplist(sweep_list):
    if len(sweep_list)==0:
        return 0
    else:
        return min(mpsweep.duration for mpsweep in sweep_list)

def mp_cell_id(cell):
    """Get an id for an MP database cell object (combined timestamp and cell id).
    """
    cell_id = "{ts}_{ext_id}".format(ts=cell.experiment.acq_timestamp, ext_id=cell.ext_id)
    return cell_id

def cell_from_mpid(mpid):
    """Get an MP database cell object by its id (combined timestamp and cell id).
    """
    import multipatch_analysis.database as db
    timestamp, ext_id = mpid.split('_')
    timestamp = float(timestamp)
    ext_id = int(ext_id)
    experiment = db.experiment_from_timestamp(timestamp)
    cell = experiment.cells[ext_id]
    return cell

def mpsweep_from_recording(recording):
    """Get an MPSweep object containing sweep data from a MP database recording object.
    """
    electrode = recording.electrode
    miesnwb = electrode.experiment.data
    sweep_id = recording.sync_rec.ext_id
    sweep = miesnwb.contents[sweep_id][electrode.device_id]
    return MPSweep(sweep)

def run_mpa_cell(specimen_id):
    try:
        # specimen_id = mp_cell_id(cell)
        cell = cell_from_mpid(specimen_id)
        nwb = cell.experiment.data
        channel = cell.electrode.device_id
        sweeps_dict = sweeps_dict_from_cell(cell)
        supra_sweep_ids = sweeps_dict['If_Curve_DA_0']
        sub_sweep_ids = sweeps_dict['TargetV_DA_0']
        lsq_supra_sweep_list = [MPSweep(nwb.contents[i][channel]) for i in supra_sweep_ids]
        lsq_sub_sweep_list = [MPSweep(nwb.contents[i][channel]) for i in sub_sweep_ids]

        lsq_supra_sweeps = SweepSet(lsq_supra_sweep_list)
        lsq_sub_sweeps = SweepSet(lsq_sub_sweep_list)
        all_sweeps = [lsq_supra_sweeps, lsq_sub_sweeps]
        for sweepset in all_sweeps:
            sweepset.align_to_start_of_epoch('stim')
        
        lsq_supra_dur = min_duration_of_sweeplist(lsq_supra_sweep_list)
        lsq_sub_dur = min_duration_of_sweeplist(lsq_sub_sweep_list)

    except Exception as detail:
        logging.warn("Exception when processing specimen {}".format(specimen_id))
        logging.warn(detail)
        return {"error": {"type": "dataset", "details": traceback.format_exc(limit=1)}}

    try:
        all_features = fv.extract_multipatch_feature_vectors(lsq_supra_sweeps, 0., lsq_supra_dur,
                                                         lsq_sub_sweeps, 0., lsq_sub_dur)
    except Exception as detail:
        logging.warn("Exception when processing specimen {}".format(specimen_id))
        logging.warn(detail)
        return {"error": {"type": "processing", "details": traceback.format_exc(limit=1)}}
    return all_features

def run_cells(specimen_ids, output_dir, project='mp_test', run_parallel=True):
    # specimen_ids = [mp_cell_id(cell) for cell in cells_list]
    if run_parallel:
        pool = Pool()
        results = pool.map(run_mpa_cell, specimen_ids)
    else:
        results = map(run_mpa_cell, specimen_ids)

    filtered_set = [(i, r) for i, r in zip(specimen_ids, results) if not "error" in r.keys()]
    error_set = [{"id": i, "error": d} for i, d in zip(specimen_ids, results) if "error" in d.keys()]
    if len(filtered_set) == 0:
        logging.info("No specimens had results")
        return

    with open(os.path.join(output_dir, "fv_errors_{:s}.json".format(project)), "w") as f:
        json.dump(error_set, f, indent=4)

    used_ids, results = zip(*filtered_set)
    logging.info("Finished with {:d} processed specimens".format(len(used_ids)))

    k_sizes = {}
    for k in results[0].keys():
        if k not in k_sizes and results[0][k] is not None:
            k_sizes[k] = len(results[0][k])
        data = np.array([r[k] if k in r else np.nan * np.zeros(k_sizes[k])
                        for r in results])
        if len(data.shape) == 1: # it'll be 1D if there's just one specimen
            data = np.reshape(data, (1, -1))
        if data.shape[0] < len(used_ids):
            logging.warn("Missing data!")
            missing = np.array([k not in r for r in results])
            print k, np.array(used_ids)[missing]
        np.save(os.path.join(output_dir, "fv_{:s}_{:s}.npy".format(k, project)), data)

    np.save(os.path.join(output_dir, "fv_ids_{:s}.npy".format(project)), used_ids)

def main(nwb_file, output_dir, project, **kwargs):
    nwb = MiesNwb(nwb_file)


    # SPECIFICS FOR EXAMPLE NWB =========

    # Only analyze one channel at a time
    channel = 0

    # We can work out code to automatically extract these based on stimulus names later.
    if_sweep_inds = [39, 45]
    targetv_sweep_inds = [15, 21]

    # END SPECIFICS =====================


    # Assemble all Recordings and convert to Sweeps
    supra_sweep_ids = list(range(*if_sweep_inds))
    sub_sweep_ids = list(range(*targetv_sweep_inds))

    supra_recs = [nwb.contents[i][channel] for i in supra_sweep_ids]
    sub_recs = [nwb.contents[i][channel] for i in sub_sweep_ids]

    lsq_supra_sweep_list, lsq_supra_dur = recs_to_sweeps(supra_recs)
    lsq_sub_sweep_list, lsq_sub_dur = recs_to_sweeps(sub_recs)
    lsq_supra_sweeps = SweepSet(lsq_supra_sweep_list)
    lsq_sub_sweeps = SweepSet(lsq_sub_sweep_list)

    all_features = fv.extract_multipatch_feature_vectors(lsq_supra_sweeps, 0., lsq_supra_dur,
                                                         lsq_sub_sweeps, 0., lsq_sub_dur)

    specimen_ids = [0]
    results = [all_features]

    filtered_set = [(i, r) for i, r in zip(specimen_ids, results) if not "error" in r.keys()]
    error_set = [{"id": i, "error": d} for i, d in zip(specimen_ids, results) if "error" in d.keys()]
    if len(filtered_set) == 0:
        logging.info("No specimens had results")
        return

    with open(os.path.join(output_dir, "fv_errors_{:s}.json".format(project)), "w") as f:
        json.dump(error_set, f, indent=4)

    used_ids, results = zip(*filtered_set)
    logging.info("Finished with {:d} processed specimens".format(len(used_ids)))

    k_sizes = {}
    for k in results[0].keys():
        if k not in k_sizes and results[0][k] is not None:
            k_sizes[k] = len(results[0][k])
        data = np.array([r[k] if k in r else np.nan * np.zeros(k_sizes[k])
                        for r in results])
        if len(data.shape) == 1: # it'll be 1D if there's just one specimen
            data = np.reshape(data, (1, -1))
        if data.shape[0] < len(used_ids):
            logging.warn("Missing data!")
            missing = np.array([k not in r for r in results])
            print k, np.array(used_ids)[missing]
        np.save(os.path.join(output_dir, "fv_{:s}_{:s}.npy".format(k, project)), data)

    np.save(os.path.join(output_dir, "fv_ids_{:s}.npy".format(project)), used_ids)


if __name__ == "__main__":
    module = ags.ArgSchemaParser(schema_type=SynPhysFeatureVectorSchema)
    main(**module.args)