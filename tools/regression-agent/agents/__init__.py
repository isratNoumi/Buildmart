"""The agents. Each runs on its own from the command line; the backend starts them as jobs.
All of them write to output/ (see paths.py).

    discovery/   explore the site, write the application model and page contracts  python -m agents.discovery
    planner/     journeys from discovery; pick pages/journeys from changes  python -m agents.planner
    parity/      compare the current and candidate builds               python -m agents.parity
    e2e/         journey spec tools and the Playwright journey runner   python -m agents.e2e.run_journeys
"""
