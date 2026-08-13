# Final Report Guidance

Report runs only after Review `PASS` or `PASS_WITH_RISKS`. The Report Agent returns validated JSON; `scripts/render_stage_report.py` creates `final/final-report.md`.

The final report is answer-first and includes:

- business context and decision question;
- approved metrics and definitions;
- data sources and executed-query evidence;
- validated findings separated from hypotheses;
- evidence-linked charts;
- Review decision, required caveats, and lineage gaps;
- prioritized recommendations and next steps.

The deterministic renderer uses the compact structure in `assets/final-report-template.md`. After the report is complete, rebuild lineage and call `runctl.py finalize`; finalization generates `final/run-summary.json` separately so operational metadata does not clutter the business narrative.

Never hide Review caveats, invent results, claim an unexecuted query ran, include credentials, or include unnecessary user-level rows.
