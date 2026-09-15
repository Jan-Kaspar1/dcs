"""Lenovo hardware/simulation QA lane.

The lane tests one exact main revision on the Lenovo host: it builds the
checked-in controller/plant image contract from that source, runs scripted
acceptance scenarios against the simulated rig, and emits a versioned JSON
report (see ``qa_lane.report``). Reports are the lane's only output —
finding publication and dashboard rendering are owned elsewhere.
"""
