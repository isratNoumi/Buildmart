"""Parity agents: check that a candidate build matches the current one, page by page.

    planner.py     1. which pages and samples to check
    driver.py      2. open both builds side by side, watch each region settle
    comparator.py  3. deterministic diff by tolerance class (no AI)
    judge.py       4. classify surviving diffs: expected / cosmetic / regression
    reporter.py    5. report.html, report.json, Jira-shaped defect files
    models.py      data passed between the agents
    cli.py         python -m agents.parity run ... | python -m agents.parity label ...

Inputs: approved page contracts (agents.discovery.contracts) and a site profile (baseline/site_profiles/).
"""
