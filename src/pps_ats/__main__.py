from pps_ats.cli import main

# Guard required: batch worker processes (spawned on Windows) re-import this module.
if __name__ == "__main__":
    raise SystemExit(main())
