import os as _mhr_os, json as _mhr_json
if _mhr_os.getenv("MHR_FREEZEC_DRYRUN_ON_STARTUP") == "1":
    from mhr_freezec_network_dryrun import run_network_dryrun as _mhr_run_network_dryrun
    print("MHR_FREEZEC_NETWORK_DRYRUN_RESULT=" + _mhr_json.dumps(_mhr_run_network_dryrun(), sort_keys=True), flush=True)

