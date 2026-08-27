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

The deterministic renderer follows the structure in `assets/final-report-template.md`. It renders the Report Agent's synthesis, not a concatenation of all earlier stage reports. The internal `role_payload` remains structured and unchanged, but the report contains ordinary Chinese prose rather than JSON, raw SQL, internal field labels, code blocks, or inline-code formatting. Keep the business-meaning and confirmation sections. Place identifiers, evidence locations, and hashes in the final plain-text technical appendix. Do not infer missing results or causes while formatting.

After the user approves the Report artifact, rebuild lineage, publish the immutable final report, and call `runctl.py finalize`; finalization generates `final/run-summary.json` separately. Do not rewrite an already approved historical report to apply a new layout; create a separate preview or a new approved revision.

Never hide Review caveats, invent results, claim an unexecuted query ran, include credentials, or include unnecessary user-level rows.
