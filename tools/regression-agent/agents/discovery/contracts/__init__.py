"""Page contracts: per-page-type checklists used to judge parity between two builds.

    models.py    - what a contract looks like (Region, PageContract)
    skeleton.py  - build draft contracts from discovery results (no AI)
    store.py     - read/write/approve baseline/discovery/contracts/<page_id>.json
    profile.py   - site profile: samples and rules for one website
    cli.py       - python -m agents.discovery.contracts ...

The AI part is discovery's contracts mode: python -m agents.discovery --mode contracts --site-profile ...
"""

from .models import ContractStatus, PageContract, Region, Tolerance

__all__ = ["ContractStatus", "PageContract", "Region", "Tolerance"]
