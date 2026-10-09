"""Compatibility note for the retired external validator example.

The demo now validates in ``DemoCompanyAnalysisJob.validate_output``. Codex
Job Runner no longer executes validator scripts, passes output through stdin,
or exposes validator environment variables. Keep this file only so links to
the former example have an explicit migration destination.
"""
