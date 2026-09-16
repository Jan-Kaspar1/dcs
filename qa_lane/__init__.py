"""Lenovo hardware/simulation QA lane.

The lane tests one exact main revision on the Lenovo host: it builds the
checked-in controller/plant image contract from that source, runs scripted
acceptance scenarios against the simulated rig, and emits a versioned JSON
report (see ``qa_lane.report``). Three run kinds share that skeleton:
``qa-*`` assessment runs drive the deterministic scenario set, ``qav-*``
verification runs replay one finding's original case on a fix-containing
revision, and ``qax-*`` exploration runs hand the running rig to a
time-bounded Devin session that picks its own charter (see
``qa_lane.explorer``). Reports are the lane's only output — finding
publication and dashboard rendering are owned elsewhere.
"""
